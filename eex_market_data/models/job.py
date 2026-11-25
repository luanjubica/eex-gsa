import logging
import time
from datetime import datetime, timedelta

import pytz

from odoo import api, fields, models, _
from odoo.exceptions import AccessError

from ..services.client import EexClient, EexError, EexTruncation, INVALID_TOKEN_MESSAGE, MISSING_TOKEN_MESSAGE
from ..services.normalization import reference_values, quote_values, utc_timestamp

_logger = logging.getLogger(__name__)
WORKER_LOCK = 1162170674
SERVICE_LOCK = 1162170675
RETRY_PARAM = 'eex_market_data.collector_retry_at'
KINDS = [('commodities', 'Discover commodities'), ('areas', 'Discover areas'),
         ('dates', 'Discover trade dates'), ('catalogue', 'Synchronize instruments'),
         ('stat', 'Statistics'), ('tob', 'Bid / ask'), ('spr', 'Settlement'),
         ('hist_stat', 'Historical statistics'), ('hist_spr', 'Historical settlement')]
HISTORY_ENDPOINTS = {'hist_stat': ('stats', 'stat'), 'hist_spr': ('sprs', 'spr')}


class EexJob(models.Model):
    _name = 'eex.job'
    _description = 'EEX Collection Job'
    _order = 'next_run, id'

    name = fields.Char(required=True)
    connection_id = fields.Many2one('eex.connection', required=True, ondelete='cascade')
    company_id = fields.Many2one(related='connection_id.company_id', store=True, index=True)
    market_id = fields.Many2one('eex.market', ondelete='cascade')
    instrument_id = fields.Many2one('eex.instrument', ondelete='cascade')
    commodity = fields.Char()
    history_from = fields.Date(readonly=True, index=True)
    history_to = fields.Date(readonly=True, index=True)
    history_scope = fields.Char(readonly=True)
    history_window_start = fields.Date(readonly=True)
    history_range_complete = fields.Boolean(readonly=True)
    kind = fields.Selection(KINDS, required=True)
    state = fields.Selection([('pending', 'Queued'), ('done', 'Completed'), ('failed', 'Needs attention')], default='pending', required=True)
    next_run = fields.Datetime(default=fields.Datetime.now, required=True, index=True)
    last_attempt = fields.Datetime()
    last_success = fields.Datetime()
    attempts = fields.Integer(default=0)
    message = fields.Char(readonly=True)
    _sql_constraints = [('job_unique', 'unique(name)', 'Collection job already exists.')]

    @api.model
    def _lock_queue(self):
        self.env.cr.execute('SELECT pg_advisory_xact_lock(%s)', [WORKER_LOCK])

    @api.model
    def _enqueue(self, connection, kind, market=None, commodity=None, instrument=None,
                 history_from=None, history_to=None, repeat=True, history_scope=None, partition=False,
                 history_window_start=None):
        self._lock_queue()
        name = self._job_name(connection, kind, market, commodity)
        if instrument:
            name += ':' + str(instrument.id)
        if partition:
            name += ':%s:%s' % (history_from, history_to)
        job = self.search([('name', '=', name)], limit=1)
        if not job:
            return self.create({'name': name, 'connection_id': connection.id, 'kind': kind,
                                'market_id': market.id if market else False, 'commodity': commodity,
                                'instrument_id': instrument.id if instrument else False,
                                'history_from': history_from, 'history_to': history_to,
                                'history_scope': history_scope, 'history_window_start': history_window_start})
        range_changed = bool(history_from and history_to and
                             (job.history_from != history_from or job.history_to != history_to))
        scope_changed = bool(history_scope and job.history_scope != history_scope)
        if range_changed or scope_changed or history_window_start and history_window_start != job.history_window_start:
            job.write({'history_from': history_from, 'history_to': history_to, 'history_scope': history_scope,
                       'history_window_start': history_window_start, 'history_range_complete': False})
        restored_token = (job.state == 'failed' and job.message == MISSING_TOKEN_MESSAGE
                          and bool(connection._token()))
        if job.state == 'done' and not repeat and not (range_changed or scope_changed):
            return job
        if job.state == 'done' or restored_token or range_changed or scope_changed:
            earliest = (job.last_attempt or fields.Datetime.now()) + timedelta(seconds=connection.minimum_refresh)
            job.write({'state': 'pending', 'attempts': 0, 'message': False,
                       'next_run': max(fields.Datetime.now(), earliest)})
        return job

    @staticmethod
    def _job_name(connection, kind, market=None, commodity=None):
        return '%s:%s:%s:%s' % (connection.id, kind, market.id if market else 0, commodity or '')

    def _enqueue_market_history(self, connection, market, kind, dates, scope, force):
        window_start = dates[0]
        history_from = window_start
        job = self.search([('name', '=', self._job_name(connection, kind, market))], limit=1)
        if job and not force and job.history_scope == scope and job.history_window_start:
            if job.history_window_start == window_start and job.history_to == dates[-1]:
                history_from = job.history_from
            elif (job.state == 'done' and job.history_range_complete
                  and job.history_window_start <= window_start and job.history_to < dates[-1]):
                first_new = next(index for index, day in enumerate(dates) if day > job.history_to)
                # Re-read the two sessions before the cursor for settlement corrections.
                history_from = dates[max(0, first_new - 2)]
        return self._enqueue(connection, kind, market, history_from=history_from, history_to=dates[-1],
                             repeat=force, history_scope=scope, history_window_start=window_start)

    def action_retry(self):
        if not self.env.user.has_group('eex_market_data.group_eex_manager'):
            raise AccessError(_('Only EEX administrators can retry failed jobs.'))
        self.check_access_rights('read')
        self.check_access_rule('read')
        self._lock_queue()
        self.filtered(lambda j: j.state == 'failed').sudo().write({
            'state': 'pending', 'attempts': 0, 'next_run': fields.Datetime.now(), 'message': False})
        return True

    @api.model
    def _schedule(self, service=False):
        now = fields.Datetime.now()
        today = datetime.now(pytz.timezone('Europe/Berlin')).date()
        for connection in self.env['eex.connection'].search([('enabled', '=', True)]):
            for market in connection.market_ids.filtered('enabled'):
                checked = market.dates_checked_at
                checked_day = pytz.UTC.localize(checked).astimezone(pytz.timezone('Europe/Berlin')).date() if checked else None
                recheck_open = (service and connection.fast_last and today.weekday() < 5
                                and market.trade_date != today)
                if not checked or checked_day != today or (now - checked).total_seconds() >= (300 if recheck_open else 21600):
                    self._enqueue(connection, 'dates', market)
            lists = self.env['eex.watchlist'].search([('company_id', '=', connection.company_id.id)])
            for watchlist in lists:
                interval = max(watchlist.refresh_interval, connection.minimum_refresh)
                due = not watchlist.last_scheduled or (now - watchlist.last_scheduled).total_seconds() >= interval
                if not due or not (watchlist.refresh_interval or watchlist.refresh_requested):
                    continue
                markets = watchlist._watched_markets().filtered('enabled')
                for market in markets:
                    if not market.trade_date:
                        self._enqueue(connection, 'dates', market)
                    else:
                        for kind in connection._feeds():
                            self._enqueue(connection, kind, market)
                watchlist.write({'last_scheduled': now, 'refresh_requested': False})

    @api.model
    def _schedule_fast_stats(self):
        now = fields.Datetime.now()
        today = datetime.now(pytz.timezone('Europe/Berlin')).date()
        for connection in self.env['eex.connection'].search([
                ('enabled', '=', True), ('fast_last', '=', True), ('collect_stat', '=', True)]):
            lists = self.env['eex.watchlist'].search([
                ('company_id', '=', connection.company_id.id), ('refresh_interval', '>', 0)])
            markets = lists._effective_instruments().market_id.filtered(
                lambda market: market.enabled and market.trade_date == today)
            if not markets:
                continue
            jobs_by_market = {job.market_id.id: job for job in self.search([
                ('connection_id', '=', connection.id), ('kind', '=', 'stat'), ('market_id', 'in', markets.ids)])}
            for market in markets:
                job = jobs_by_market.get(market.id)
                if not job:
                    self._enqueue(connection, 'stat', market)
                    continue
                # Jobs with a recorded failure keep their backoff or require an admin retry.
                if job.message or job.state == 'failed':
                    continue
                due = (job.last_attempt or now) + timedelta(seconds=connection.fast_last_interval)
                if due <= now and (job.state == 'done' or job.next_run > due):
                    job.write({'state': 'pending', 'next_run': due})

    @api.model
    def _collection_paused(self):
        retry_at = self.env['ir.config_parameter'].sudo().get_param(RETRY_PARAM)
        return bool(retry_at and fields.Datetime.to_datetime(retry_at) > fields.Datetime.now())

    @api.model
    def _due_jobs(self):
        return [
            ('state', '=', 'pending'), ('next_run', '<=', fields.Datetime.now()),
            ('connection_id.enabled', '=', True), ('connection_id.active', '=', True),
        ]

    @api.model
    def _cron_collect(self):
        self = self.sudo()
        if self.env['eex.connection']._uses_service_collector():
            return
        self.env.cr.execute('SELECT pg_try_advisory_xact_lock(%s)', [WORKER_LOCK])
        if not self.env.cr.fetchone()[0] or self._collection_paused():
            return
        self._schedule()
        deadline = time.monotonic() + 40
        processed = set()
        for _unused in range(20):
            if time.monotonic() >= deadline:
                break
            job = self.search(self._due_jobs() + [('id', 'not in', list(processed))], limit=1)
            if not job:
                break
            processed.add(job.id)
            if job._run():
                break

    def _run(self, client=None):
        """Run a single job. Return true when EEX asks the collector to back off."""
        self.ensure_one()
        now = fields.Datetime.now()
        try:
            with self.env.cr.savepoint():
                self._execute(client)
        except EexError as error:
            attempts = self.attempts + 1
            retry = error.retryable and attempts < 5
            next_run = now + timedelta(seconds=max(error.retry_after, min(3600, 60 * 2 ** attempts)))
            self.write({'last_attempt': now, 'attempts': attempts, 'state': 'pending' if retry else 'failed',
                        'message': str(error), 'next_run': next_run})
            if error.retry_after:
                self.env['ir.config_parameter'].sudo().set_param(RETRY_PARAM, fields.Datetime.to_string(next_run))
                return True
            if str(error) in (INVALID_TOKEN_MESSAGE, MISSING_TOKEN_MESSAGE):
                self.search([('connection_id', '=', self.connection_id.id), ('state', '=', 'pending')]).write({
                    'state': 'failed', 'message': str(error),
                })
        except Exception:
            # Avoid logging request headers or payloads.
            _logger.error('EEX job %s failed during processing; inspect schema/normalization.', self.id)
            self.write({'last_attempt': now, 'state': 'failed',
                        'message': 'Processing failed. Check the response schema and server installation.'})
        else:
            self.write({'state': 'done', 'last_attempt': now, 'last_success': fields.Datetime.now(),
                        'attempts': 0, 'message': False})
            if self.kind == 'stat' and self.env['eex.connection']._uses_service_collector():
                self._notify_latest()
        return False

    def _notify_latest(self):
        if self.market_id and self.market_id.enabled:
            self.env['bus.bus']._sendone(self.market_id._price_channel(), 'eex/latest', self.market_id.id)

    def _watched_instruments(self):
        lists = self.env['eex.watchlist'].search([('company_id', '=', self.company_id.id)])
        return lists._effective_instruments().filtered(lambda instrument: instrument.market_id == self.market_id)

    def _execute(self, client=None):
        self.ensure_one()
        connection = self.connection_id
        client = client or EexClient(connection._token())
        market = self.market_id
        if market and not market.enabled:
            return
        if self.kind == 'commodities':
            for commodity in client.get('rd', 'derivatives', listing=True):
                if not isinstance(commodity, str):
                    raise EexError('Unexpected commodity catalogue format.')
                self._enqueue(connection, 'areas', commodity=commodity)
        elif self.kind == 'areas':
            for area in client.get('rd', 'derivatives', self.commodity, listing=True):
                if not isinstance(area, str):
                    raise EexError('Unexpected area catalogue format.')
                values = {'connection_id': connection.id, 'commodity': self.commodity, 'area': area}
                if not self.env['eex.market'].search([('connection_id', '=', connection.id),
                        ('commodity', '=', self.commodity), ('area', '=', area)], limit=1):
                    self.env['eex.market'].create(values)
        elif self.kind == 'dates':
            rows = client.get('rd', 'derivatives', market.commodity, market.area, listing=True)
            today = datetime.now(pytz.timezone('Europe/Berlin')).date()
            dates = [fields.Date.to_date(value) for value in rows]
            eligible = [value for value in dates if value <= today]
            if not eligible:
                raise EexError('No available trading date for this market.', retryable=True)
            market.write({'trade_date': max(eligible), 'dates_checked_at': fields.Datetime.now()})
            self._enqueue(connection, 'catalogue', market)
            if connection.retain_history:
                history_dates = sorted(value for value in eligible if value < today)[-connection.history_backfill_days:]
                force = market.history_requested
                if history_dates:
                    instruments = self._watched_instruments()
                    if instruments:
                        scope = ','.join(str(instrument_id) for instrument_id in sorted(instruments.ids))
                        for kind in sorted(set(connection._feeds()) & {'stat', 'spr'}):
                            self._enqueue_market_history(connection, market, 'hist_' + kind,
                                                         history_dates, scope, force)
                market.history_requested = False
        elif self.kind == 'catalogue':
            if not market.trade_date:
                raise EexError('Discover trading dates first.')
            rows = client.get('rd', 'derivatives', market.commodity, market.area, market.trade_date)
            if not rows:
                raise EexError('Reference catalogue is empty; keeping the existing catalogue.', retryable=True)
            catalogue = {}
            for row in rows:
                values = reference_values(row)
                if values and row['Cmdty'] == market.commodity and row['Area'] == market.area:
                    catalogue[values['isin']] = values
            if not catalogue:
                raise EexError('Reference catalogue has no matching futures; keeping the existing catalogue.', retryable=True)
            Instrument = self.env['eex.instrument']
            existing = Instrument.search([('market_id', '=', market.id)])
            by_isin = {instrument.isin: instrument for instrument in existing}
            new_instruments = []
            unavailable = existing.filtered(lambda instrument: instrument.available and instrument.isin not in catalogue)
            unavailable.write({'available': False})
            for isin, values in catalogue.items():
                values.update(market_id=market.id, reference_date=market.trade_date, available=True)
                instrument = by_isin.get(isin)
                if instrument:
                    instrument.write(values)
                else:
                    new_instruments.append(values)
            Instrument.create(new_instruments)
            market.catalogue_checked_at = fields.Datetime.now()
            for kind in connection._feeds():
                self._enqueue(connection, kind, market)
        elif self.kind in HISTORY_ENDPOINTS:
            endpoint, base_kind = HISTORY_ENDPOINTS[self.kind]
            instrument = self.instrument_id
            if not connection.retain_history or base_kind not in connection._feeds() or not self.history_from or not self.history_to:
                return
            path = [endpoint, 'derivatives', market.commodity, market.area]
            if instrument:
                path += [instrument.short_code, instrument.maturity]
            try:
                rows = client.get(*path, params={
                    'from': str(self.history_from), 'to': str(self.history_to),
                    'instrumentType': 'Simple Instrument', 'productType': 'Future',
                })
            except EexTruncation:
                if instrument or self.history_from == self.history_to:
                    raise
                midpoint = self.history_from + (self.history_to - self.history_from) // 2
                for date_from, date_to in ((self.history_from, midpoint), (midpoint + timedelta(days=1), self.history_to)):
                    self._enqueue(connection, self.kind, market, history_from=date_from, history_to=date_to,
                                  history_scope=self.history_scope, partition=True)
                self.history_range_complete = False
                return
            self._store_history(rows, base_kind)
            self.history_range_complete = True
        else:
            if self.kind not in connection._feeds():
                return
            if not market.trade_date:
                raise EexError('Discover trading dates first.')
            rows = client.get(self.kind, 'derivatives', market.commodity, market.area, market.trade_date,
                              params={'instrumentType': 'Simple Instrument', 'productType': 'Future'})
            self._store_quotes(rows)


    def _store_history(self, rows, kind):
        now = fields.Datetime.now()
        market = self.market_id
        instruments = self.instrument_id or self._watched_instruments()
        instruments_by_isin = {instrument.isin: instrument for instrument in instruments}
        latest = {}
        for row in rows:
            values = quote_values(row, kind)
            if values is None or row.get('Cmdty') != market.commodity or row.get('Area') != market.area:
                continue
            trade_date = fields.Date.to_date(row.get('TrdDate'))
            if not trade_date or not self.history_from <= trade_date <= self.history_to:
                raise EexError('EEX returned a trading date outside the requested history range; stored history preserved.')
            isin = row.get('InstrumentISIN')
            if isin not in instruments_by_isin:
                continue
            timestamp = utc_timestamp(row.get('Tm'))
            key = (isin, trade_date)
            previous = latest.get(key)
            if previous and previous[1] and timestamp and timestamp < previous[1]:
                continue
            latest[key] = (values, timestamp)
        History = self.env['eex.historical']
        if not latest:
            return
        existing = History.search([
            ('instrument_id', 'in', instruments.ids), ('kind', '=', kind),
            ('trade_date', '>=', self.history_from), ('trade_date', '<=', self.history_to),
        ])
        by_key = {(row.instrument_id.id, row.trade_date, row.metric): row for row in existing}
        create_values = []
        for (isin, trade_date), (payload, source_at) in latest.items():
            instrument = instruments_by_isin[isin]
            for metric, value in payload.items():
                if value is None:
                    continue
                values = {'instrument_id': instrument.id, 'kind': kind, 'trade_date': trade_date,
                          'metric': metric, 'value': value, 'source_at': source_at, 'fetched_at': now}
                row = by_key.get((instrument.id, trade_date, metric))
                if not row:
                    create_values.append(values)
                elif (not row.source_at or source_at and source_at >= row.source_at):
                    if row.value != value or row.source_at != source_at:
                        row.write(values)
        History.create(create_values)

    def _store_quotes(self, rows):
        now = fields.Datetime.now()
        market = self.market_id
        connection = self.connection_id
        # Cache only selected instruments. The upstream request is shared across
        # every user's watchlist for this market, irrespective of watchlist size.
        instruments = self._watched_instruments()
        by_isin = {}
        for row in rows:
            values = quote_values(row, self.kind)
            if values is None or row.get('Cmdty') != market.commodity or row.get('Area') != market.area:
                continue
            if fields.Date.to_date(row.get('TrdDate')) != market.trade_date:
                raise EexError('EEX returned an unexpected trading date; previous cache preserved.')
            isin = row.get('InstrumentISIN')
            if not isin:
                continue
            timestamp = utc_timestamp(row.get('Tm'))
            previous = by_isin.get(isin)
            if previous:
                if not timestamp or not previous[1]:
                    raise EexError('Ambiguous duplicate quote rows; previous cache preserved.')
                if timestamp < previous[1]:
                    continue
            by_isin[isin] = (values, timestamp)
        Quote = self.env['eex.quote']
        known = {quote.instrument_id.id: quote for quote in Quote.search([
            ('instrument_id', 'in', instruments.ids), ('kind', '=', self.kind)])}
        snapshots = []
        new_quotes = []
        same_values = []
        for instrument in instruments:
            pair = by_isin.get(instrument.isin)
            payload, source_at = pair if pair else ({}, None)
            values = {'instrument_id': instrument.id, 'kind': self.kind, 'values_json': payload,
                      'trade_date': market.trade_date, 'source_at': source_at, 'fetched_at': now,
                      'status': 'ok' if any(v is not None for v in payload.values()) else 'empty'}
            quote = known.get(instrument.id)
            if not quote:
                new_quotes.append(values)
            elif (quote.trade_date == market.trade_date and quote.source_at == (source_at or False)
                  and quote.status == values['status'] and (quote.values_json or {}) == payload):
                same_values.append(quote.id)
            else:
                quote.write(values)

        Quote.browse(same_values).write({'fetched_at': now})
        known.update({quote.instrument_id.id: quote for quote in Quote.create(new_quotes)})
        if not connection.retain_history:
            return
        due = Quote.browse([quote.id for quote in known.values() if not quote.last_sample_at
                            or (now - quote.last_sample_at).total_seconds() >= connection.history_interval])
        for quote in due:
            for metric, value in (quote.values_json or {}).items():
                if value is not None:
                    snapshots.append({
                        'instrument_id': quote.instrument_id.id, 'quote_id': quote.id, 'metric': metric,
                        'value': value, 'trade_date': market.trade_date, 'captured_at': now,
                        'source_at': quote.source_at})
        self.env['eex.snapshot'].create(snapshots)
        due.write({'last_sample_at': now})
