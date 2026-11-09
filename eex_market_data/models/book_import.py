import base64
import binascii
import re

from odoo import fields, models, _
from odoo.exceptions import ValidationError

from ..services.book_yaml import BookYamlError, parse_book_yaml


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


class EexBookImportWizard(models.TransientModel):
    _name = 'eex.book.import.wizard'
    _description = 'Import EEX book YAML'

    file_data = fields.Binary(string='Book YAML file', required=True, attachment=False)
    filename = fields.Char()
    name = fields.Char(string='Board name', help='Leave blank to use the first sheet name.')
    refresh_interval = fields.Integer(string='Collection interval (seconds; 0 = manual)', default=300, required=True)

    @staticmethod
    def _series_matches(instrument, series):
        maturity_type = (instrument.maturity_type or '').strip().lower()
        if series == 'weekend':
            return 'weekend' in maturity_type
        if series == 'week':
            return maturity_type in ('week', 'weekly') or ('week' in maturity_type and 'weekend' not in maturity_type)
        return maturity_type in ('day', 'daily')

    def _continuous_instrument(self, market, prefix, series):
        candidates = self.env['eex.instrument'].search([
            ('market_id', '=', market.id), ('available', '=', True),
            ('short_code', '=ilike', prefix + '%'),
        ], order='delivery_start, maturity, short_code, id')
        candidates = candidates.filtered(lambda instrument: self._series_matches(instrument, series))
        if not candidates:
            return self.env['eex.instrument']
        trade_date = market.trade_date or fields.Date.today()
        covering = candidates.filtered(
            lambda instrument: instrument.delivery_start and instrument.delivery_end
            and instrument.delivery_start <= trade_date <= instrument.delivery_end)
        if covering:
            return covering[:1]
        future = candidates.filtered(lambda instrument: instrument.delivery_start
                                     and instrument.delivery_start >= trade_date)
        return (future or candidates)[:1]

    def _resolve_symbol(self, market, symbol):
        normalized = symbol.strip().upper()
        continuous = CONTINUOUS_SYMBOLS.get(normalized)
        if continuous:
            return self._continuous_instrument(market, *continuous)

        fixed = FIXED_SYMBOL.fullmatch(normalized)
        if fixed:
            root, month_code, short_year = fixed.groups()
            maturity = '%04d%02d' % (2000 + int(short_year), MONTH_CODES[month_code])
            return self.env['eex.instrument'].search([
                ('market_id', '=', market.id), ('short_code', '=', root), ('maturity', '=', maturity),
            ], limit=1)

        rolling = ROLLING_SYMBOL.fullmatch(normalized)
        if rolling:
            root, angle_offset, square_offset = rolling.groups()
            offset = int(angle_offset or square_offset or 0)
            candidates = self.env['eex.instrument'].search([
                ('market_id', '=', market.id), ('available', '=', True), ('short_code', '=', root),
            ], order='delivery_start, maturity, id')
            maturities = []
            for instrument in candidates:
                if instrument.maturity not in maturities:
                    maturities.append(instrument.maturity)
            if offset < len(maturities):
                return candidates.filtered(lambda instrument: instrument.maturity == maturities[offset])[:1]
        return self.env['eex.instrument']

    def _decoded_book(self):
        self.ensure_one()
        try:
            payload = base64.b64decode(self.file_data or b'', validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValidationError(_('The uploaded file is not valid base64 data.')) from exc
        if len(payload) > 1024 * 1024:
            raise ValidationError(_('The YAML file must be no larger than 1 MB.'))
        try:
            text = payload.decode('utf-8-sig')
        except UnicodeDecodeError as exc:
            raise ValidationError(_('The YAML file must use UTF-8 encoding.')) from exc
        try:
            return parse_book_yaml(text)
        except BookYamlError as exc:
            raise ValidationError(_('Invalid book YAML: %s') % exc) from exc

    def action_import(self):
        self.ensure_one()
        book = self._decoded_book()
        default_commodity = (book.get('commodity') or 'POWER').upper()
        default_market = (book.get('market') or 'derivatives').lower()
        instrument_ids = []
        missing = []
        for sheet in book['sheets']:
            area = (sheet.get('area') or '').upper()
            commodity = (sheet.get('commodity') or default_commodity).upper()
            market_segment = (sheet.get('market') or default_market).lower()
            if not area:
                raise ValidationError(_('Every sheet must define an area.'))
            if market_segment != 'derivatives':
                raise ValidationError(_('Only derivatives book sheets can be imported.'))
            market = self.env['eex.market'].search([
                ('company_id', '=', self.env.company.id), ('commodity', '=', commodity), ('area', '=', area),
            ], limit=1)
            for symbol in sheet['instruments']:
                instrument = market and self._resolve_symbol(market, symbol)
                if instrument:
                    if instrument.id not in instrument_ids:
                        instrument_ids.append(instrument.id)
                else:
                    missing.append('%s (%s / %s)' % (symbol, commodity, area))
        if missing:
            details = '\n'.join('- ' + item for item in missing[:20])
            if len(missing) > 20:
                details += _('\n- and %s more') % (len(missing) - 20)
            raise ValidationError(_('These contracts were not found in the current EEX catalogue:\n%s\n\nSynchronize the catalogue or correct the YAML file.') % details)
        if not instrument_ids:
            raise ValidationError(_('The YAML file does not contain any instruments.'))
        first_name = book['sheets'][0].get('name') if book['sheets'] else False
        board_name = (self.name or first_name or 'Imported board').strip()[:255] or 'Imported board'
        watchlist = self.env['eex.watchlist'].create({
            'name': board_name,
            'company_id': self.env.company.id,
            'owner_id': self.env.user.id,
            'refresh_interval': self.refresh_interval,
            'instrument_ids': [(6, 0, instrument_ids)],
        })
        return watchlist.action_dashboard()
