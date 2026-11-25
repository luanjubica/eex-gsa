import re
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import AccessError
from odoo.osv import expression

METRICS = [(x, x.title()) for x in ('last', 'open', 'high', 'low', 'volume', 'bid', 'ask', 'settlement')]
FEEDS = [('stat', 'Statistics'), ('tob', 'Bid / ask'), ('spr', 'Settlement')]


MONTH_CODES = {'F': 1, 'G': 2, 'H': 3, 'J': 4, 'K': 5, 'M': 6,
               'N': 7, 'Q': 8, 'U': 9, 'V': 10, 'X': 11, 'Z': 12}
FIXED_SYMBOL = re.compile(r'^/E\.([A-Z0-9_]+?)([FGHJKMNQUVXZ])(\d{2})$', re.I)
ROLLING_SYMBOL = re.compile(r'^/E\.([A-Z0-9_]+?)(?:(?:<(\d+)>|\[(\d+)\]))?$', re.I)
CONTINUOUS_SYMBOLS = {
    '/E.DB_DAILY': ('DB', 'day'),
    '/E.DEB_WEEK': ('DEB', 'week'),
    '/E.DWB_WEEK': ('DWB', 'weekend'),
    '/E.FF_DAILY': ('FF', 'day'),
    '/E.FFB_WEEK': ('FFB', 'week'),
    '/E.FFW_WEEK': ('FFW', 'weekend'),
    '/E.FD_DAILY': ('FD', 'day'),
    '/E.FDB_WEEK': ('FDB', 'week'),
    '/E.FDW_WEEK': ('FDW', 'weekend'),
    '/E.FZB_WEEK': ('FZB', 'week'),
    '/E.F9_DAILY': ('F9', 'day'),
    '/E.F9B_WEEK': ('F9B', 'week'),
    '/E.W9B_WEEK': ('W9B', 'weekend'),
}


def is_rolling_book_symbol(symbol):
    return symbol in CONTINUOUS_SYMBOLS or (
        not FIXED_SYMBOL.fullmatch(symbol) and bool(ROLLING_SYMBOL.fullmatch(symbol)))


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

    @staticmethod
    def _series_matches(instrument, series):
        maturity_type = (instrument.maturity_type or '').strip().lower()
        if series == 'weekend':
            return 'weekend' in maturity_type
        if series == 'week':
            return maturity_type in ('week', 'weekly') or ('week' in maturity_type and 'weekend' not in maturity_type)
        return maturity_type in ('day', 'daily')

    def _continuous_instrument(self, prefix, series):
        candidates = self.env['eex.instrument'].search([
            ('market_id', '=', self.id), ('available', '=', True),
            ('short_code', '=ilike', prefix + '%'),
        ], order='delivery_start, maturity, short_code, id')
        candidates = candidates.filtered(lambda instrument: self._series_matches(instrument, series))
        if not candidates:
            return self.env['eex.instrument']
        trade_date = self.trade_date or fields.Date.today()
        covering = candidates.filtered(
            lambda instrument: instrument.delivery_start and instrument.delivery_end
            and instrument.delivery_start <= trade_date <= instrument.delivery_end)
        if covering:
            return covering[:1]
        future = candidates.filtered(lambda instrument: instrument.delivery_start
                                     and instrument.delivery_start >= trade_date)
        return future[:1]

    def _resolve_book_symbol(self, symbol):
        self.ensure_one()
        normalized = symbol.strip().upper()
        continuous = CONTINUOUS_SYMBOLS.get(normalized)
        if continuous:
            return self._continuous_instrument(*continuous)

        fixed = FIXED_SYMBOL.fullmatch(normalized)
        if fixed:
            root, month_code, short_year = fixed.groups()
            maturity = '%04d%02d' % (2000 + int(short_year), MONTH_CODES[month_code])
            return self.env['eex.instrument'].search([
                ('market_id', '=', self.id), ('short_code', '=', root), ('maturity', '=', maturity),
            ], limit=1)

        rolling = ROLLING_SYMBOL.fullmatch(normalized)
        if rolling:
            root, angle_offset, square_offset = rolling.groups()
            offset_text = angle_offset or square_offset or '0'
            if len(offset_text) > 3:
                return self.env['eex.instrument']
            offset = int(offset_text)
            candidates = self.env['eex.instrument'].search([
                ('market_id', '=', self.id), ('available', '=', True), ('short_code', '=', root),
            ], order='delivery_start, maturity, id')
            trade_date = self.trade_date or fields.Date.today()
            by_maturity = {}
            for instrument in candidates:
                if not instrument.expiry_date or instrument.expiry_date >= trade_date:
                    by_maturity.setdefault(instrument.maturity, instrument)
            eligible = list(by_maturity.values())
            if offset < len(eligible):
                return eligible[offset]
        return self.env['eex.instrument']

    def _price_channel(self):
        self.ensure_one()
        return 'eex_market_data.market.%s' % self.id

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
