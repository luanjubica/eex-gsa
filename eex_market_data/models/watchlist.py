from datetime import datetime

import pytz

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, ValidationError

METRICS = [('last', 'Last'), ('bid', 'Bid'), ('ask', 'Ask'), ('settlement', 'Settlement'),
           ('volume', 'Volume'), ('open', 'Open'), ('high', 'High'), ('low', 'Low')]
HISTORY_METRICS = [('last', 'Close / last'), ('settlement', 'Settlement'), ('open', 'Open'),
                   ('high', 'High'), ('low', 'Low'), ('volume', 'Volume')]


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
    refresh_interval = fields.Integer(default=300, string='Collection interval (seconds; 0 = manual)')
    refresh_requested = fields.Boolean(readonly=True, default=True, copy=False)
    last_scheduled = fields.Datetime(readonly=True, copy=False)
    show_last = fields.Boolean(default=True)
    show_bid = fields.Boolean(default=True)
    show_ask = fields.Boolean(default=True)
    show_settlement = fields.Boolean(default=True)
    show_volume = fields.Boolean(default=True)
    show_open = fields.Boolean(default=False)
    show_high = fields.Boolean(default=False)
    show_low = fields.Boolean(default=False)

    @api.constrains('refresh_interval', 'instrument_ids', 'owner_id', 'company_id')
    def _validate_watchlist(self):
        for record in self:
            if record.refresh_interval != 0 and record.refresh_interval < 60:
                raise ValidationError(_('Use 0 for manual collection, or at least 60 seconds.'))
            if len(record.instrument_ids) > 500:
                raise ValidationError(_('A watchlist can contain at most 500 instruments.'))
            if record.company_id not in record.owner_id.company_ids:
                raise ValidationError(_('The owner must belong to the watchlist company.'))

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
        if 'instrument_ids' in vals:
            vals = dict(vals, refresh_requested=True)
        return super().write(vals)

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

    def action_history(self):
        self._check_read()
        return {'type': 'ir.actions.client', 'tag': 'eex_market_data.history_dashboard',
                'params': {'watchlist_id': self.id}}

    def action_history_details(self):
        self._check_read()
        return {'type': 'ir.actions.act_window', 'name': _('EEX daily history'), 'res_model': 'eex.historical',
                'view_mode': 'graph,tree,pivot',
                'views': [(False, 'graph'), (False, 'tree'), (False, 'pivot')],
                'domain': [('instrument_id', 'in', self.instrument_ids.ids)],
                'context': {'search_default_last_price': 1, 'fill_temporal': False}}

    def action_history_refresh(self):
        self._check_read()
        markets = self.instrument_ids.mapped('market_id').filtered('enabled')
        markets.sudo().write({'history_requested': True})
        for market in markets:
            self.env['eex.job'].sudo()._enqueue(market.connection_id, 'dates', market)
        connection = self.env['eex.connection'].sudo().search([('company_id', '=', self.company_id.id)], limit=1)
        days = connection.history_backfill_days if connection else 10
        return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': {
            'title': _('History queued'),
            'message': _('The last %s available trading days will load in the background.') % days,
            'type': 'success', 'sticky': False}}

    def action_history_export(self):
        self._check_read()
        return {'type': 'ir.actions.act_url', 'url': '/eex/watchlist/%s/history/export' % self.id, 'target': 'self'}

    @api.model
    def history_dashboard_data(self, watchlist_id, metric='last'):
        self.check_access_rights('read')
        watchlist = self.browse(int(watchlist_id)).exists()
        if not watchlist:
            return {'name': '', 'metric': metric, 'metrics': [], 'tables': [], 'days': 10, 'updated_at': False}
        watchlist._check_read()
        labels = dict(HISTORY_METRICS)
        if metric not in labels:
            metric = 'last'
        connection = self.env['eex.connection'].sudo().search([
            ('company_id', '=', watchlist.company_id.id)], limit=1)
        days = connection.history_backfill_days if connection else 10
        instruments = watchlist.instrument_ids.sorted(
            lambda instrument: (instrument.market_id.name, instrument.short_code,
                                instrument.delivery_start or fields.Date.today(), instrument.maturity))
        history = self.env['eex.historical'].search([
            ('instrument_id', 'in', instruments.ids)], order='trade_date, instrument_id, metric')
        tables = []
        latest_fetch = max(history.mapped('fetched_at'), default=False)
        for market in instruments.mapped('market_id').sorted('name'):
            market_instruments = instruments.filtered(lambda instrument: instrument.market_id == market)
            market_history = history.filtered(lambda row: row.market_id == market)
            dates = sorted(set(market_history.mapped('trade_date')))[-days:]
            values = {(row.trade_date, row.instrument_id.id): row.value for row in market_history
                      if row.metric == metric}
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
        columns = [{'key': key, 'label': label} for key, label in METRICS if watchlist['show_' + key]]
        result.update(selected=watchlist.id, name=watchlist.name, columns=columns,
                      refresh_interval=watchlist.refresh_interval,
                      minimum_refresh=connection.minimum_refresh if connection else 60,
                      timing=connection.data_timing if connection else 'unknown',
                      enabled=bool(connection and connection.enabled),
                      pending=watchlist.refresh_requested,
                      can_edit=watchlist.owner_id == self.env.user or self.env.user.has_group('eex_market_data.group_eex_manager'))
        now = fields.Datetime.now()
        today = datetime.now(pytz.timezone('Europe/Berlin')).date()
        jobs = self.env['eex.job'].sudo().search([('market_id', 'in', watchlist.instrument_ids.market_id.ids)])
        jobs_by_key = {}
        for job in jobs:
            key = (job.market_id.id, job.kind)
            current = jobs_by_key.get(key)
            if not current or (job.last_attempt or datetime.min) > (current.last_attempt or datetime.min):
                jobs_by_key[key] = job
        enabled_markets = watchlist.instrument_ids.mapped('market_id').filtered('enabled')
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
        for instrument in watchlist.instrument_ids.sorted(lambda i: (i.short_code, i.delivery_start or today, i.maturity)):
            feeds = {}
            for kind in ('stat', 'tob', 'spr'):
                quote = instrument.quote_ids.filtered(lambda q: q.kind == kind)[:1]
                job = jobs_by_key.get((instrument.market_id.id, kind))
                status = 'waiting'
                if not connection or not connection.enabled or not connection.active or not instrument.market_id.enabled:
                    status = 'paused'
                elif kind not in connection._feeds():
                    status = 'disabled'
                elif job and job.message:
                    status = 'error'
                elif quote:
                    status = 'empty' if quote.status == 'empty' else 'ok'
                    if (now - quote.fetched_at).total_seconds() > max(connection.stale_after, watchlist.refresh_interval * 2):
                        status = 'stale'
                    elif quote.trade_date != today and status == 'ok':
                        status = 'previous_day'
                feeds[kind] = {'status': status, 'values': quote.values_json or {} if quote else {},
                               'trade_date': str(quote.trade_date) if quote else None,
                               'source_at': fields.Datetime.to_string(quote.source_at) if quote and quote.source_at else None,
                               'fetched_at': fields.Datetime.to_string(quote.fetched_at) if quote else None,
                               'message': job.message if job else None}
            result['rows'].append({'id': instrument.id, 'name': instrument.name, 'long_name': instrument.long_name,
                'market': instrument.market_id.name, 'currency': instrument.currency, 'price_unit': instrument.price_unit,
                'volume_unit': instrument.volume_unit, 'available': instrument.available,
                'delivery_start': str(instrument.delivery_start) if instrument.delivery_start else '',
                'delivery_end': str(instrument.delivery_end) if instrument.delivery_end else '',
                'feeds': feeds})
        return result
