from datetime import datetime

import pytz

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, ValidationError

METRICS = [('last', 'Last'), ('bid', 'Bid'), ('ask', 'Ask'), ('settlement', 'Settlement'),
           ('volume', 'Volume'), ('open', 'Open'), ('high', 'High'), ('low', 'Low')]


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
        jobs_by_key = {(job.market_id.id, job.kind): job for job in jobs}
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
