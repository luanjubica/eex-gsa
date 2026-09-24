from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import AccessError
from odoo.osv import expression

METRICS = [(x, x.title()) for x in ('last', 'open', 'high', 'low', 'volume', 'bid', 'ask', 'settlement')]
FEEDS = [('stat', 'Statistics'), ('tob', 'Bid / ask'), ('spr', 'Settlement')]


class EexMarket(models.Model):
    _name = 'eex.market'
    _description = 'EEX Market'
    _order = 'commodity, area'

    name = fields.Char(compute='_compute_name', store=True)
    connection_id = fields.Many2one('eex.connection', required=True, ondelete='cascade')
    company_id = fields.Many2one(related='connection_id.company_id', store=True, index=True)
    commodity = fields.Char(required=True)
    area = fields.Char(required=True)
    enabled = fields.Boolean(default=False)
    trade_date = fields.Date(readonly=True)
    dates_checked_at = fields.Datetime(readonly=True)
    catalogue_checked_at = fields.Datetime(readonly=True)
    history_requested = fields.Boolean(default=True, readonly=True)
    _sql_constraints = [('market_unique', 'unique(connection_id, commodity, area)', 'Market already exists.')]

    @api.depends('commodity', 'area')
    def _compute_name(self):
        for record in self:
            record.name = '%s / %s' % (record.commodity, record.area)

    def action_sync(self):
        if not self.env.user.has_group('eex_market_data.group_eex_manager'):
            raise AccessError(_('Only EEX administrators can synchronize the catalogue.'))
        self.check_access_rights('write')
        self.check_access_rule('write')
        for record in self.filtered('enabled'):
            self.env['eex.job'].sudo()._enqueue(record.connection_id, 'dates', record)
        return True


class EexInstrument(models.Model):
    _name = 'eex.instrument'
    _description = 'EEX Futures Instrument'
    _order = 'short_code, delivery_start, maturity, id'

    name = fields.Char(required=True)
    market_id = fields.Many2one('eex.market', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='market_id.company_id', store=True, index=True)
    commodity = fields.Char(related='market_id.commodity', store=True)
    area = fields.Char(related='market_id.area', store=True)
    isin = fields.Char(required=True, index=True)
    short_code = fields.Char(required=True, index=True)
    maturity = fields.Char(required=True)
    long_name = fields.Char()
    currency = fields.Char()
    price_unit = fields.Char()
    volume_unit = fields.Char()
    delivery_start = fields.Date()
    delivery_end = fields.Date()
    expiry_date = fields.Date()
    maturity_type = fields.Char()
    available = fields.Boolean(default=True, index=True)
    reference_date = fields.Date()
    quote_ids = fields.One2many('eex.quote', 'instrument_id')

    _sql_constraints = [('instrument_unique', 'unique(market_id, isin)', 'Instrument already exists in this market.')]

    @api.model
    def _name_search(self, name='', args=None, operator='ilike', limit=100, name_get_uid=None):
        domain = args or []
        if name:
            domain = expression.AND([domain, expression.OR([
                [(field, operator, name)] for field in ('name', 'long_name', 'isin', 'short_code')])])
        return self._search(domain, limit=limit, access_rights_uid=name_get_uid)


class EexQuote(models.Model):
    _name = 'eex.quote'
    _description = 'EEX Shared Quote Cache'

    instrument_id = fields.Many2one('eex.instrument', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='instrument_id.company_id', store=True, index=True)
    kind = fields.Selection(FEEDS, required=True)
    trade_date = fields.Date(required=True)
    values_json = fields.Json()
    source_at = fields.Datetime(string='Source timestamp')
    fetched_at = fields.Datetime(required=True)
    status = fields.Selection([('ok', 'Available'), ('empty', 'No data')], required=True)
    last_sample_at = fields.Datetime()
    _sql_constraints = [('quote_unique', 'unique(instrument_id, kind)', 'A shared quote already exists.')]


class EexSnapshot(models.Model):
    _name = 'eex.snapshot'
    _description = 'EEX Sampled Price History'
    _order = 'captured_at desc'

    instrument_id = fields.Many2one('eex.instrument', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='instrument_id.company_id', store=True, index=True)
    kind = fields.Selection(related='quote_id.kind', store=True)
    quote_id = fields.Many2one('eex.quote', required=True, ondelete='cascade')
    trade_date = fields.Date(required=True)
    captured_at = fields.Datetime(required=True, index=True)
    source_at = fields.Datetime()
    metric = fields.Selection(METRICS, required=True)
    value = fields.Float(digits=(20, 6), group_operator='avg')
    currency = fields.Char(related='instrument_id.currency', store=True)
    price_unit = fields.Char(related='instrument_id.price_unit', store=True)

    @api.autovacuum
    def _gc_history(self):
        for connection in self.env['eex.connection'].sudo().with_context(active_test=False).search([]):
            cutoff = fields.Datetime.now() - timedelta(days=connection.retention_days)
            self.sudo().search([('company_id', '=', connection.company_id.id), ('captured_at', '<', cutoff)]).unlink()


class EexHistorical(models.Model):
    _name = 'eex.historical'
    _description = 'EEX API Daily History'
    _order = 'trade_date desc, instrument_id, metric'

    instrument_id = fields.Many2one('eex.instrument', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='instrument_id.company_id', store=True, index=True)
    market_id = fields.Many2one(related='instrument_id.market_id', store=True, index=True)
    kind = fields.Selection(FEEDS, required=True, index=True)
    trade_date = fields.Date(required=True, index=True)
    metric = fields.Selection(METRICS, required=True, index=True)
    value = fields.Float(digits=(20, 6), group_operator='avg')
    source_at = fields.Datetime(string='Source timestamp')
    fetched_at = fields.Datetime(required=True)
    currency = fields.Char(related='instrument_id.currency', store=True)
    price_unit = fields.Char(related='instrument_id.price_unit', store=True)
    volume_unit = fields.Char(related='instrument_id.volume_unit', store=True)

    _sql_constraints = [('historical_unique', 'unique(instrument_id, kind, trade_date, metric)',
                         'This daily historical value is already stored.')]

    @api.autovacuum
    def _gc_api_history(self):
        for connection in self.env['eex.connection'].sudo().with_context(active_test=False).search([]):
            cutoff = fields.Date.today() - timedelta(days=connection.retention_days)
            self.sudo().search([('company_id', '=', connection.company_id.id), ('trade_date', '<', cutoff)]).unlink()
