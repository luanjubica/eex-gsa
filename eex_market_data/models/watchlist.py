import json
import re
from datetime import datetime

import pytz

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, ValidationError

from ..services.client import INVALID_TOKEN_MESSAGE, MISSING_TOKEN_MESSAGE

INSTRUMENT_COLUMNS = [
    ('name', 'Instrument', 'instrument', 'name', 'text'),
    ('long_name', 'Description', 'instrument', 'long_name', 'text'),
    ('short_code', 'Short code', 'instrument', 'short_code', 'text'),
    ('isin', 'ISIN', 'instrument', 'isin', 'text'),
    ('market', 'Market', 'instrument', 'market', 'text'),
    ('commodity', 'Commodity', 'instrument', 'commodity', 'text'),
    ('area', 'Area', 'instrument', 'area', 'text'),
    ('maturity', 'Maturity', 'instrument', 'maturity', 'text'),
    ('maturity_type', 'Maturity type', 'instrument', 'maturity_type', 'text'),
    ('delivery_start', 'Delivery start', 'instrument', 'delivery_start', 'date'),
    ('delivery_end', 'Delivery end', 'instrument', 'delivery_end', 'date'),
    ('expiry_date', 'Expiry date', 'instrument', 'expiry_date', 'date'),
    ('currency', 'Currency', 'instrument', 'currency', 'text'),
    ('price_unit', 'Price unit', 'instrument', 'price_unit', 'text'),
    ('volume_unit', 'Volume unit', 'instrument', 'volume_unit', 'text'),
    ('available', 'Catalogue status', 'instrument', 'available', 'availability'),
    ('reference_date', 'Catalogue date', 'instrument', 'reference_date', 'date'),
]
METRIC_COLUMNS = [
    ('last', 'Last', 'stat', 'last', 'number'),
    ('bid', 'Bid', 'tob', 'bid', 'number'),
    ('ask', 'Ask', 'tob', 'ask', 'number'),
    ('settlement', 'Settlement', 'spr', 'settlement', 'number'),
    ('volume', 'Volume', 'stat', 'volume', 'number'),
    ('open', 'Open', 'stat', 'open', 'number'),
    ('high', 'High', 'stat', 'high', 'number'),
    ('low', 'Low', 'stat', 'low', 'number'),
]
FEED_COLUMNS = [
    ('stat_status', 'Statistics status', 'stat', 'status', 'status'),
    ('stat_trade_date', 'Statistics trading date', 'stat', 'trade_date', 'date'),
    ('stat_source_at', 'Statistics source (UTC)', 'stat', 'source_at', 'datetime'),
    ('stat_fetched_at', 'Statistics fetched (UTC)', 'stat', 'fetched_at', 'datetime'),
    ('tob_status', 'Bid / ask status', 'tob', 'status', 'status'),
    ('tob_trade_date', 'Bid / ask trading date', 'tob', 'trade_date', 'date'),
    ('tob_source_at', 'Bid / ask source (UTC)', 'tob', 'source_at', 'datetime'),
    ('tob_fetched_at', 'Bid / ask fetched (UTC)', 'tob', 'fetched_at', 'datetime'),
    ('spr_status', 'Settlement status', 'spr', 'status', 'status'),
    ('spr_trade_date', 'Settlement trading date', 'spr', 'trade_date', 'date'),
    ('spr_source_at', 'Settlement source (UTC)', 'spr', 'source_at', 'datetime'),
    ('spr_fetched_at', 'Settlement fetched (UTC)', 'spr', 'fetched_at', 'datetime'),
]
BOARD_COLUMNS = INSTRUMENT_COLUMNS + METRIC_COLUMNS + FEED_COLUMNS
BOARD_COLUMN_FIELDS = ['show_' + column[0] for column in BOARD_COLUMNS]
HISTORY_METRICS = [('settlement', 'Settlement close'), ('last', 'Last trade'), ('open', 'Open'),
                   ('high', 'High'), ('low', 'Low'), ('volume', 'Volume')]
BOOK_MONTH_CODES = {1: 'F', 2: 'G', 3: 'H', 4: 'J', 5: 'K', 6: 'M',
                    7: 'N', 8: 'Q', 9: 'U', 10: 'V', 11: 'X', 12: 'Z'}


class EexWatchlist(models.Model):
    _name = 'eex.watchlist'
    _description = 'EEX Personal Watchlist'
    _order = 'name, id'
    _check_company_auto = True

    name = fields.Char(required=True, default='My futures')
    active = fields.Boolean(default=True)
    owner_id = fields.Many2one('res.users', required=True, default=lambda self: self.env.user, ondelete='cascade')
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    shared = fields.Boolean(string='Share with EEX users in this company', default=False)
    instrument_ids = fields.Many2many('eex.instrument', string='Instruments', check_company=True)
    series_ids = fields.One2many('eex.watchlist.series', 'watchlist_id', string='Rolling contracts',
                                 check_company=True, copy=True)
    refresh_interval = fields.Integer(default=300, string='Collection interval (seconds; 0 = manual)')
    refresh_requested = fields.Boolean(readonly=True, default=True, copy=False)
    last_scheduled = fields.Datetime(readonly=True, copy=False)
    show_name = fields.Boolean(string='Instrument', default=True)
    show_long_name = fields.Boolean(string='Description', default=True)
    show_short_code = fields.Boolean(string='Short code')
    show_isin = fields.Boolean(string='ISIN')
    show_market = fields.Boolean(string='Market', default=True)
    show_commodity = fields.Boolean(string='Commodity')
    show_area = fields.Boolean(string='Area')
    show_maturity = fields.Boolean(string='Maturity', default=True)
    show_maturity_type = fields.Boolean(string='Maturity type')
    show_delivery_start = fields.Boolean(string='Delivery start', default=True)
    show_delivery_end = fields.Boolean(string='Delivery end', default=True)
    show_expiry_date = fields.Boolean(string='Expiry date')
    show_currency = fields.Boolean(string='Currency', default=True)
    show_price_unit = fields.Boolean(string='Price unit', default=True)
    show_volume_unit = fields.Boolean(string='Volume unit', default=True)
    show_available = fields.Boolean(string='Catalogue status')
    show_reference_date = fields.Boolean(string='Catalogue date')
    show_last = fields.Boolean(string='Last', default=True)
    show_bid = fields.Boolean(string='Bid', default=True)
    show_ask = fields.Boolean(string='Ask', default=True)
    show_settlement = fields.Boolean(string='Settlement', default=True)
    show_volume = fields.Boolean(string='Volume', default=True)
    show_open = fields.Boolean(string='Open', default=False)
    show_high = fields.Boolean(string='High', default=False)
    show_low = fields.Boolean(string='Low', default=False)
    show_stat_status = fields.Boolean(string='Statistics status', default=True)
    show_stat_trade_date = fields.Boolean(string='Statistics trading date')
    show_stat_source_at = fields.Boolean(string='Statistics source timestamp')
    show_stat_fetched_at = fields.Boolean(string='Statistics fetched timestamp', default=True)
    show_tob_status = fields.Boolean(string='Bid / ask status', default=True)
    show_tob_trade_date = fields.Boolean(string='Bid / ask trading date')
    show_tob_source_at = fields.Boolean(string='Bid / ask source timestamp')
    show_tob_fetched_at = fields.Boolean(string='Bid / ask fetched timestamp', default=True)
    show_spr_status = fields.Boolean(string='Settlement status', default=True)
    show_spr_trade_date = fields.Boolean(string='Settlement trading date')
    show_spr_source_at = fields.Boolean(string='Settlement source timestamp')
    show_spr_fetched_at = fields.Boolean(string='Settlement fetched timestamp', default=True)

    @api.constrains('refresh_interval', 'instrument_ids', 'series_ids', 'owner_id', 'company_id')
    def _validate_watchlist(self):
        for record in self:
            if record.refresh_interval != 0 and record.refresh_interval < 60:
                raise ValidationError(_('Use 0 for manual collection, or at least 60 seconds.'))
            if len(record.instrument_ids) + len(record.series_ids) > 500:
                raise ValidationError(_('A watchlist can contain at most 500 instruments.'))
            if record.company_id not in record.owner_id.company_ids:
                raise ValidationError(_('The owner must belong to the watchlist company.'))

    @api.constrains(*BOARD_COLUMN_FIELDS)
    def _validate_board_columns(self):
        for record in self:
            if not any(record[field_name] for field_name in BOARD_COLUMN_FIELDS):
                raise ValidationError(_('Select at least one market board column.'))

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.su and not self.env.user.has_group('eex_market_data.group_eex_manager'):
            for vals in vals_list:
                if vals.get('owner_id', self.env.uid) != self.env.uid:
                    raise AccessError(_('You can only create your own watchlists.'))
                if vals.get('company_id', self.env.company.id) not in self.env.companies.ids:
                    raise AccessError(_('Select one of your allowed companies.'))
        return super().create(vals_list)

    def write(self, vals):
        if not self.env.su:
            if {'last_scheduled', 'refresh_requested'} & vals.keys():
                raise AccessError(_('Use the refresh action to request collection.'))
            if not self.env.user.has_group('eex_market_data.group_eex_manager'):
                if 'owner_id' in vals and vals['owner_id'] != self.env.uid:
                    raise AccessError(_('You cannot transfer ownership of a watchlist.'))
        if {'instrument_ids', 'series_ids'} & vals.keys():
            vals = dict(vals, refresh_requested=True)
        return super().write(vals)

    def _effective_instruments(self):
        return self.instrument_ids | self.series_ids.instrument_id

    def _watched_markets(self):
        return self.instrument_ids.market_id | self.series_ids.market_id

    def _check_read(self):
        self.ensure_one()
        self.check_access_rights('read')
        self.check_access_rule('read')

    def action_refresh(self):
        self._check_read()
        self.sudo().write({'refresh_requested': True})
        return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': {
            'title': _('Refresh queued'), 'message': _('Collection runs in the background, subject to the minimum interval.'),
            'type': 'success', 'sticky': False}}

    def action_dashboard(self):
        self._check_read()
        return {'type': 'ir.actions.client', 'tag': 'eex_market_data.dashboard', 'params': {'watchlist_id': self.id}}

    def action_export(self):
        self._check_read()
        return {'type': 'ir.actions.act_url', 'url': '/eex/watchlist/%s/export' % self.id, 'target': 'self'}

    def action_book_export(self):
        self._check_read()
        return {'type': 'ir.actions.act_url', 'url': '/eex/watchlist/%s/book/export' % self.id, 'target': 'self'}

    @staticmethod
    def _book_symbol(instrument):
        short_code = (instrument.short_code or '').strip().upper()
        if not re.fullmatch(r'[A-Z0-9_]+', short_code):
            raise ValidationError(_('Instrument %s has a short code that cannot be exported.') % instrument.name)
        maturity = (instrument.maturity or '').strip()
        match = re.fullmatch(r'(\d{4})(\d{2})', maturity)
        if not match or int(match.group(2)) not in BOOK_MONTH_CODES:
            raise ValidationError(_('Instrument %s has a maturity that cannot be exported.') % instrument.name)
        return '/E.%s%s%s' % (short_code, BOOK_MONTH_CODES[int(match.group(2))], match.group(1)[2:])

    def _book_yaml(self):
        self.ensure_one()
        connection = self.env['eex.connection'].sudo().search([('company_id', '=', self.company_id.id)], limit=1)
        instruments = self.instrument_ids.sorted(
            lambda instrument: (instrument.commodity, instrument.area, instrument.short_code,
                                instrument.delivery_start or fields.Date.today(), instrument.maturity))
        grouped = {}
        for instrument in instruments:
            grouped.setdefault(instrument.market_id, {}).setdefault(self._book_symbol(instrument), None)
        for series in self.series_ids:
            grouped.setdefault(series.market_id, {}).setdefault(series.symbol, None)
        markets = sorted(grouped, key=lambda market: (market.commodity, market.area))

        default_commodity = markets[0].commodity if markets else 'POWER'
        lines = [
            '# Generated from the Odoo EEX market board %s.' % json.dumps(self.name, ensure_ascii=False),
            '# Save this file as config/book.yaml for the eex-data tool.',
            'history_days: %s' % (connection.history_backfill_days if connection else 10),
            'history_keep_days: %s' % (connection.retention_days if connection else 90),
            'commodity: %s' % default_commodity,
            'market: derivatives',
            '',
        ]
        if not grouped:
            lines.append('sheets: []')
            return '\n'.join(lines) + '\n'

        lines.append('sheets:')
        for market in markets:
            lines.extend([
                '  - name: %s' % json.dumps(market.name, ensure_ascii=False),
                '    area: %s' % json.dumps(market.area, ensure_ascii=False),
            ])
            if market.commodity != default_commodity:
                lines.append('    commodity: %s' % json.dumps(market.commodity, ensure_ascii=False))
            lines.append('    instruments:')
            lines.extend('      - %s' % symbol for symbol in grouped[market])
            lines.append('')
        return '\n'.join(lines).rstrip() + '\n'

    def action_history(self):
        self._check_read()
        return {'type': 'ir.actions.client', 'tag': 'eex_market_data.history_dashboard',
                'params': {'watchlist_id': self.id}}

    def action_history_details(self, metric='settlement'):
        self._check_read()
        labels = dict(HISTORY_METRICS)
        metric = metric if metric in labels else 'settlement'
        return {'type': 'ir.actions.act_window', 'name': _('EEX daily history: %s') % labels[metric],
                'res_model': 'eex.historical', 'view_mode': 'graph,tree,pivot',
                'views': [(False, 'graph'), (False, 'tree'), (False, 'pivot')],
                'domain': [('instrument_id', 'in', self._effective_instruments().ids), ('metric', '=', metric)],
                'context': {'fill_temporal': False}}

    def action_history_refresh(self):
        self._check_read()
        markets = self._watched_markets().filtered('enabled')
        # Queue before touching market rows, like the collector, to preserve lock order.
        for market in markets:
            self.env['eex.job'].sudo()._enqueue(market.connection_id, 'dates', market)
        markets.sudo().write({'history_requested': True})
        connection = self.env['eex.connection'].sudo().search([('company_id', '=', self.company_id.id)], limit=1)
        days = connection.history_backfill_days if connection else 10
        return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': {
            'title': _('History queued'),
            'message': _('The last %s available trading days will load in the background.') % days,
            'type': 'success', 'sticky': False}}

    def action_history_export(self):
        self._check_read()
        return {'type': 'ir.actions.act_url', 'url': '/eex/watchlist/%s/history/export' % self.id, 'target': 'self'}

    def _history_rows(self, instruments, days, metric=None):
        history = self.env['eex.historical']
        domain = [('instrument_id', 'in', instruments.ids)]
        if metric:
            domain.append(('metric', '=', metric))
        groups = history.read_group(domain, ['trade_date'], ['trade_date:day'],
                                    limit=days, orderby='trade_date desc')
        dates = sorted(group['__range']['trade_date:day']['from'] for group in groups)
        rows = history.search(domain + [('trade_date', 'in', dates)],
                              order='trade_date, instrument_id, metric') if dates else history
        return dates, rows

    @api.model
    def history_dashboard_data(self, watchlist_id, metric='settlement'):
        self.check_access_rights('read')
        watchlist = self.browse(int(watchlist_id)).exists()
        if not watchlist:
            return {'name': '', 'metric': metric, 'metrics': [], 'tables': [], 'days': 10, 'updated_at': False}
        watchlist._check_read()
        labels = dict(HISTORY_METRICS)
        if metric not in labels:
            metric = 'settlement'
        connection = self.env['eex.connection'].sudo().search([
            ('company_id', '=', watchlist.company_id.id)], limit=1)
        days = connection.history_backfill_days if connection else 10
        instruments = watchlist._effective_instruments().sorted(
            lambda instrument: (instrument.market_id.name, instrument.short_code,
                                instrument.delivery_start or fields.Date.today(), instrument.maturity))
        tables = []
        latest_fetch = False
        for market in instruments.mapped('market_id').sorted('name'):
            market_instruments = instruments.filtered(lambda instrument: instrument.market_id == market)
            dates, market_history = watchlist._history_rows(market_instruments, days, metric)
            for row in market_history:
                if not latest_fetch or row.fetched_at > latest_fetch:
                    latest_fetch = row.fetched_at
            values = {(str(row.trade_date), row.instrument_id.id): row.value for row in market_history}
            tables.append({
                'id': market.id,
                'name': market.name,
                'instruments': [{'id': instrument.id, 'name': instrument.name,
                                 'short_code': instrument.short_code, 'maturity': instrument.maturity,
                                 'currency': instrument.currency or '',
                                 'unit': (instrument.volume_unit if metric == 'volume' else instrument.price_unit) or ''}
                                for instrument in market_instruments],
                'rows': [{'date': str(trade_date),
                          'values': [values.get((trade_date, instrument.id))
                                     for instrument in market_instruments]}
                         for trade_date in dates],
            })
        return {'name': watchlist.name, 'watchlist_id': watchlist.id, 'metric': metric,
                'metric_label': labels[metric], 'metrics': [{'key': key, 'label': label}
                                                            for key, label in HISTORY_METRICS],
                'tables': tables, 'days': days,
                'updated_at': fields.Datetime.to_string(latest_fetch) if latest_fetch else False}

    @staticmethod
    def _dashboard_feed(instrument, kind, connection, active_feeds, quote, job, now, today, stale_after):
        status = 'waiting'
        if not connection or not connection.enabled or not connection.active or not instrument.market_id.enabled:
            status = 'paused'
        elif kind not in active_feeds:
            status = 'disabled'
        elif job and job.message:
            status = 'authentication' if job.message in (MISSING_TOKEN_MESSAGE, INVALID_TOKEN_MESSAGE) else 'error'
        elif quote:
            status = 'empty' if quote.status == 'empty' else 'ok'
            if (now - quote.fetched_at).total_seconds() > stale_after:
                status = 'stale'
            elif quote.trade_date != today and status == 'ok':
                status = 'previous_day'
        return {
            'status': status,
            'values': (quote.values_json or {}) if quote else {},
            'trade_date': str(quote.trade_date) if quote else None,
            'source_at': fields.Datetime.to_string(quote.source_at) if quote and quote.source_at else None,
            'fetched_at': fields.Datetime.to_string(quote.fetched_at) if quote else None,
            'message': job.message if job else None,
        }

    def _stat_stale_after(self, connection, fast_last):
        return (max(30, 3 * connection.fast_last_interval) if fast_last
                else max(connection.stale_after, self.refresh_interval * 2) if connection else 60)

    @api.model
    def dashboard_latest(self, watchlist_id, market_ids):
        """Refresh only statistics for the requested markets on an accessible board."""
        watchlist = self.browse(int(watchlist_id)).exists()
        if not watchlist:
            return None
        watchlist._check_read()
        requested = set(int(market_id) for market_id in market_ids)
        instruments = watchlist._effective_instruments().filtered(
            lambda instrument: instrument.market_id.id in requested)
        markets = instruments.market_id
        connection = self.env['eex.connection'].sudo().search([('company_id', '=', watchlist.company_id.id)], limit=1)
        feeds = connection._feeds() if connection else []
        fast_last = connection and connection.fast_last and connection._uses_service_collector()
        fetched = self.env['eex.quote'].search([('instrument_id', 'in', instruments.ids), ('kind', '=', 'stat')])
        quotes = {quote.instrument_id.id: quote for quote in fetched}
        jobs = self.env['eex.job'].sudo().search([('market_id', 'in', markets.ids), ('kind', '=', 'stat')])
        by_market = {job.market_id.id: job for job in jobs}
        now = fields.Datetime.now()
        today = datetime.now(pytz.timezone('Europe/Berlin')).date()
        stale_after = watchlist._stat_stale_after(connection, fast_last)
        return {'rows': [
            {'id': instrument.id, 'market_id': instrument.market_id.id, 'feed': self._dashboard_feed(
                instrument, 'stat', connection, feeds, quotes.get(instrument.id),
                by_market.get(instrument.market_id.id), now, today, stale_after)}
            for instrument in instruments
        ]}

    @api.model
    def dashboard_data(self, watchlist_id=False):
        self.check_access_rights('read')
        lists = self.search([])
        watchlist = self.browse(int(watchlist_id)).exists() if watchlist_id else lists[:1]
        result = {'watchlists': [{'id': item.id, 'name': item.name} for item in lists],
                  'selected': False, 'rows': [], 'columns': []}
        if not watchlist:
            return result
        watchlist._check_read()
        connection = self.env['eex.connection'].sudo().search([('company_id', '=', watchlist.company_id.id)], limit=1)
        columns = [{'key': key, 'label': label, 'source': source, 'field': field, 'type': value_type}
                   for key, label, source, field, value_type in BOARD_COLUMNS
                   if watchlist['show_' + key]]
        instruments = watchlist._effective_instruments()
        markets = watchlist._watched_markets()
        service = connection and connection._uses_service_collector()
        fast_last = bool(service and connection.fast_last and connection.collect_stat)
        result.update(selected=watchlist.id, name=watchlist.name, columns=columns,
                      live_stats=fast_last,
                      fast_last_interval=connection.fast_last_interval if fast_last else False,
                      market_ids=markets.ids if fast_last else [],
                      unresolved_series=watchlist.series_ids.filtered(lambda line: not line.instrument_id).mapped('symbol'),
                      refresh_interval=watchlist.refresh_interval,
                      minimum_refresh=connection.minimum_refresh if connection else 60,
                      board_refresh_interval=connection.board_refresh_interval if connection else 60,
                      timing=connection.data_timing if connection else 'unknown',
                      enabled=bool(connection and connection.enabled),
                      pending=watchlist.refresh_requested,
                      can_edit=watchlist.owner_id == self.env.user or self.env.user.has_group('eex_market_data.group_eex_manager'))
        now = fields.Datetime.now()
        today = datetime.now(pytz.timezone('Europe/Berlin')).date()
        jobs = self.env['eex.job'].sudo().search([
            ('market_id', 'in', markets.ids), ('kind', 'in', ['stat', 'tob', 'spr'])])
        jobs_by_key = {}
        for job in jobs:
            key = (job.market_id.id, job.kind)
            current = jobs_by_key.get(key)
            if not current or (job.last_attempt or datetime.min) > (current.last_attempt or datetime.min):
                jobs_by_key[key] = job
        enabled_markets = markets.filtered('enabled')
        feed_kinds = connection._feeds() if connection and connection.enabled and connection.active else []
        expected = [(market.id, kind) for market in enabled_markets for kind in feed_kinds]
        completed = failed = 0
        for key in expected:
            job = jobs_by_key.get(key)
            attempted = bool(job and watchlist.last_scheduled and job.last_attempt and
                             job.last_attempt >= watchlist.last_scheduled)
            if job and (attempted or job.state == 'failed') and job.state in ('done', 'failed'):
                completed += 1
                failed += int(job.state == 'failed')
        total = len(expected)
        if not connection or not connection.enabled or not connection.active:
            progress_status, progress_label = 'paused', _('Collection paused')
        elif watchlist.refresh_requested:
            progress_status, progress_label = 'queued', _('Queued for collection')
            completed = 0
        elif total and completed < total:
            progress_status = 'collecting'
            progress_label = _('Collecting %s of %s feeds') % (completed, total)
        elif failed:
            progress_status = 'error'
            progress_label = _('Complete with %s errors') % failed
        else:
            progress_status, progress_label = 'complete', _('Collection complete')
        result['collection_progress'] = {
            'status': progress_status, 'label': progress_label, 'done': completed, 'total': total,
            'percent': round(100 * completed / total) if total else 0,
            'last_scheduled': fields.Datetime.to_string(watchlist.last_scheduled)
                              if watchlist.last_scheduled else False,
        }
        quotes = {(quote.instrument_id.id, quote.kind): quote for quote in instruments.quote_ids}
        normal_stale = max(connection.stale_after, watchlist.refresh_interval * 2) if connection else 60
        fast_stale = watchlist._stat_stale_after(connection, fast_last)
        for instrument in instruments.sorted(lambda i: (i.short_code, i.delivery_start or today, i.maturity)):
            feeds = {
                kind: self._dashboard_feed(instrument, kind, connection, feed_kinds,
                                           quotes.get((instrument.id, kind)),
                                           jobs_by_key.get((instrument.market_id.id, kind)), now, today,
                                           fast_stale if kind == 'stat' else normal_stale)
                for kind in ('stat', 'tob', 'spr')
            }
            result['rows'].append({'id': instrument.id, 'name': instrument.name, 'long_name': instrument.long_name,
                'short_code': instrument.short_code, 'isin': instrument.isin,
                'market': instrument.market_id.name, 'market_id': instrument.market_id.id, 'commodity': instrument.commodity, 'area': instrument.area,
                'maturity': instrument.maturity, 'maturity_type': instrument.maturity_type,
                'currency': instrument.currency, 'price_unit': instrument.price_unit,
                'volume_unit': instrument.volume_unit, 'available': instrument.available,
                'delivery_start': str(instrument.delivery_start) if instrument.delivery_start else '',
                'delivery_end': str(instrument.delivery_end) if instrument.delivery_end else '',
                'expiry_date': str(instrument.expiry_date) if instrument.expiry_date else '',
                'reference_date': str(instrument.reference_date) if instrument.reference_date else '',
                'feeds': feeds})
        return result
