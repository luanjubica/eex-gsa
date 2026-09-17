import logging
import time
from datetime import datetime, timedelta

import pytz

from odoo import api, fields, models, _
from odoo.exceptions import AccessError

from ..services.client import EexClient, EexError
from ..services.normalization import reference_values, quote_values, utc_timestamp

_logger = logging.getLogger(__name__)
WORKER_LOCK = 1162170674
KINDS = [('commodities', 'Discover commodities'), ('areas', 'Discover areas'),
         ('dates', 'Discover trade dates'), ('catalogue', 'Synchronize instruments'),
         ('stat', 'Statistics'), ('tob', 'Bid / ask'), ('spr', 'Settlement')]


class EexJob(models.Model):
    _name = 'eex.job'
    _description = 'EEX Collection Job'
    _order = 'next_run, id'

    name = fields.Char(required=True)
    connection_id = fields.Many2one('eex.connection', required=True, ondelete='cascade')
    company_id = fields.Many2one(related='connection_id.company_id', store=True, index=True)
    market_id = fields.Many2one('eex.market', ondelete='cascade')
    commodity = fields.Char()
    kind = fields.Selection(KINDS, required=True)
    state = fields.Selection([('pending', 'Queued'), ('done', 'Completed'), ('failed', 'Needs attention')], default='pending', required=True)
    next_run = fields.Datetime(default=fields.Datetime.now, required=True, index=True)
    last_attempt = fields.Datetime()
    last_success = fields.Datetime()
    attempts = fields.Integer(default=0)
    message = fields.Char(readonly=True)
    _sql_constraints = [('job_unique', 'unique(name)', 'Collection job already exists.')]

    @api.model
    def _enqueue(self, connection, kind, market=None, commodity=None):
        # Serializes enqueue with collection across Odoo workers. Private method:
        # only validated admin actions and the cron scheduler can reach it.
        self.env.cr.execute('SELECT pg_advisory_xact_lock(%s)', [WORKER_LOCK])
        name = '%s:%s:%s:%s' % (connection.id, kind, market.id if market else 0, commodity or '')
        job = self.search([('name', '=', name)], limit=1)
        if not job:
            return self.create({'name': name, 'connection_id': connection.id, 'kind': kind,
                                'market_id': market.id if market else False, 'commodity': commodity})
        if job.state == 'done':
            earliest = (job.last_attempt or fields.Datetime.now()) + timedelta(seconds=connection.minimum_refresh)
            job.write({'state': 'pending', 'attempts': 0, 'next_run': max(fields.Datetime.now(), earliest)})
        return job

    def action_retry(self):
        if not self.env.user.has_group('eex_market_data.group_eex_manager'):
            raise AccessError(_('Only EEX administrators can retry failed jobs.'))
        self.check_access_rights('read')
        self.check_access_rule('read')
        self.filtered(lambda j: j.state == 'failed').sudo().write({
            'state': 'pending', 'attempts': 0, 'next_run': fields.Datetime.now(), 'message': False})
        return True

    @api.model
    def _schedule(self):
        now = fields.Datetime.now()
        today = datetime.now(pytz.timezone('Europe/Berlin')).date()
        for connection in self.env['eex.connection'].search([('enabled', '=', True)]):
            for market in connection.market_ids.filtered('enabled'):
                checked = market.dates_checked_at
                checked_day = pytz.UTC.localize(checked).astimezone(pytz.timezone('Europe/Berlin')).date() if checked else None
                if not checked or checked_day != today or (now - checked).total_seconds() >= 21600:
                    self._enqueue(connection, 'dates', market)
            lists = self.env['eex.watchlist'].search([('company_id', '=', connection.company_id.id)])
            for watchlist in lists:
                interval = max(watchlist.refresh_interval, connection.minimum_refresh)
                due = not watchlist.last_scheduled or (now - watchlist.last_scheduled).total_seconds() >= interval
                if not due or not (watchlist.refresh_interval or watchlist.refresh_requested):
                    continue
                markets = watchlist.instrument_ids.mapped('market_id').filtered('enabled')
                for market in markets:
                    if not market.trade_date:
                        self._enqueue(connection, 'dates', market)
                    else:
                        for kind in connection._feeds():
                            self._enqueue(connection, kind, market)
                watchlist.write({'last_scheduled': now, 'refresh_requested': False})

    @api.model
    def _cron_collect(self):
        self = self.sudo()
        self.env.cr.execute('SELECT pg_try_advisory_xact_lock(%s)', [WORKER_LOCK])
        if not self.env.cr.fetchone()[0]:
            return
        self._schedule()
        deadline = time.monotonic() + 40
        processed = set()
        for _unused in range(20):
            if time.monotonic() >= deadline:
                break
            job = self.search([
                ('state', '=', 'pending'), ('next_run', '<=', fields.Datetime.now()),
                ('connection_id.enabled', '=', True), ('connection_id.active', '=', True),
                ('id', 'not in', list(processed)),
            ], limit=1)
            if not job:
                break
            processed.add(job.id)
            now = fields.Datetime.now()
            try:
                with self.env.cr.savepoint():
                    job._execute()
            except EexError as error:
                attempts = job.attempts + 1
                retry = error.retryable and attempts < 5
                job.write({'last_attempt': now, 'attempts': attempts,
                           'state': 'pending' if retry else 'failed', 'message': str(error),
                           'next_run': now + timedelta(seconds=max(error.retry_after, min(3600, 60 * 2 ** attempts)))})
                if error.retry_after:
                    # Applies EEX throttling to the entire queue, not just this job.
                    self.search([('state', '=', 'pending'), ('next_run', '<', job.next_run)]).write({'next_run': job.next_run})
                    break
            except Exception:
                # No remote payloads, headers or exception strings in logs.
                _logger.error('EEX job %s failed during processing; inspect schema/normalization.', job.id)
                job.write({'last_attempt': now, 'state': 'failed',
                           'message': 'Processing failed. Check the response schema and server installation.'})
            else:
                job.write({'state': 'done', 'last_attempt': now, 'last_success': fields.Datetime.now(),
                           'attempts': 0, 'message': False})

    def _execute(self):
        self.ensure_one()
        connection = self.connection_id
        client = EexClient(connection._token())
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
        elif self.kind == 'catalogue':
            if not market.trade_date:
                raise EexError('Discover trading dates first.')
            rows = client.get('rd', 'derivatives', market.commodity, market.area, market.trade_date)
            if not rows:
                raise EexError('Reference catalogue is empty; keeping the existing catalogue.', retryable=True)
            seen = []
            Instrument = self.env['eex.instrument']
            for row in rows:
                values = reference_values(row)
                if not values:
                    continue
                if row['Cmdty'] != market.commodity or row['Area'] != market.area:
                    continue
                instrument = Instrument.search([('market_id', '=', market.id), ('isin', '=', values['isin'])], limit=1)
                values.update(market_id=market.id, reference_date=market.trade_date, available=True)
                if instrument:
                    instrument.write(values)
                else:
                    instrument = Instrument.create(values)
                seen.append(instrument.id)
            Instrument.search([('market_id', '=', market.id), ('id', 'not in', seen)]).write({'available': False})
            market.catalogue_checked_at = fields.Datetime.now()
            for kind in connection._feeds():
                self._enqueue(connection, kind, market)
        else:
            if self.kind not in connection._feeds():
                return
            if not market.trade_date:
                raise EexError('Discover trading dates first.')
            rows = client.get(self.kind, 'derivatives', market.commodity, market.area, market.trade_date)
            self._store_quotes(rows)

    def _store_quotes(self, rows):
        now = fields.Datetime.now()
        market = self.market_id
        connection = self.connection_id
        # Cache only selected instruments. The upstream request is shared across
        # every user's watchlist for this market, irrespective of watchlist size.
        lists = self.env['eex.watchlist'].search([('company_id', '=', connection.company_id.id)])
        instruments = lists.mapped('instrument_ids').filtered(lambda i: i.market_id == market)
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
        for instrument in instruments:
            pair = by_isin.get(instrument.isin)
            payload, source_at = pair if pair else ({}, None)
            values = {'instrument_id': instrument.id, 'kind': self.kind, 'values_json': payload,
                      'trade_date': market.trade_date, 'source_at': source_at, 'fetched_at': now,
                      'status': 'ok' if any(v is not None for v in payload.values()) else 'empty'}
            quote = Quote.search([('instrument_id', '=', instrument.id), ('kind', '=', self.kind)], limit=1)
            if quote:
                quote.write(values)
            else:
                quote = Quote.create(values)
            if connection.retain_history and (not quote.last_sample_at or
                    (now - quote.last_sample_at).total_seconds() >= connection.history_interval):
                for metric, value in payload.items():
                    if value is not None:
                        self.env['eex.snapshot'].create({
                            'instrument_id': instrument.id, 'quote_id': quote.id, 'metric': metric,
                            'value': value, 'trade_date': market.trade_date, 'captured_at': now,
                            'source_at': source_at})
                quote.last_sample_at = now
