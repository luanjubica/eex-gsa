from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

from .market import is_rolling_book_symbol


class EexWatchlistSeries(models.Model):
    _name = 'eex.watchlist.series'
    _description = 'EEX Watchlist Rolling Contract'
    _order = 'market_id, symbol'
    _check_company_auto = True

    watchlist_id = fields.Many2one('eex.watchlist', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='watchlist_id.company_id', store=True)
    market_id = fields.Many2one('eex.market', required=True, ondelete='cascade', index=True, check_company=True)
    symbol = fields.Char(required=True)
    instrument_id = fields.Many2one('eex.instrument', compute='_compute_instrument', store=True)
    _sql_constraints = [
        ('series_unique', 'unique(watchlist_id, market_id, symbol)', 'This rolling symbol is already on the watchlist.'),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        cleaned = []
        for values in vals_list:
            values = {key: value for key, value in values.items() if key not in {'instrument_id', 'company_id'}}
            if values.get('symbol'):
                values['symbol'] = values['symbol'].strip().upper()
            cleaned.append(values)
        return super().create(cleaned)

    @api.depends('symbol', 'market_id', 'market_id.catalogue_checked_at')
    def _compute_instrument(self):
        resolved = {}
        for line in self:
            key = (line.market_id, line.symbol)
            if key not in resolved:
                resolved[key] = line.market_id._resolve_book_symbol(line.symbol) if line.market_id and line.symbol else False
            line.instrument_id = resolved[key]

    @api.constrains('symbol', 'watchlist_id', 'market_id')
    def _check_series(self):
        for line in self:
            if not is_rolling_book_symbol(line.symbol):
                raise ValidationError(_('Use a rolling or continuous EEX symbol.'))
        self.watchlist_id._validate_watchlist()
