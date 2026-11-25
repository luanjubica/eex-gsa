import base64
import binascii

from odoo import fields, models, _
from odoo.exceptions import ValidationError

from ..services.book_yaml import BookYamlError, parse_book_yaml


from .market import is_rolling_book_symbol


class EexBookImportWizard(models.TransientModel):
    _name = 'eex.book.import.wizard'
    _description = 'Import EEX book YAML'

    file_data = fields.Binary(string='Book YAML file', required=True, attachment=False)
    filename = fields.Char()
    name = fields.Char(string='Board name', help='Leave blank to use the first sheet name.')
    refresh_interval = fields.Integer(string='Collection interval (seconds; 0 = manual)', default=300, required=True)

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
        instrument_ids = set()
        series = set()
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
                symbol = symbol.strip().upper()
                instrument = market and market._resolve_book_symbol(symbol)
                if instrument:
                    if is_rolling_book_symbol(symbol):
                        series.add((market.id, symbol))
                    else:
                        instrument_ids.add(instrument.id)
                else:
                    missing.append('%s (%s / %s)' % (symbol, commodity, area))
        if missing:
            details = '\n'.join('- ' + item for item in missing[:20])
            if len(missing) > 20:
                details += _('\n- and %s more') % (len(missing) - 20)
            raise ValidationError(_('These contracts were not found in the current EEX catalogue:\n%s\n\nSynchronize the catalogue or correct the YAML file.') % details)
        if not instrument_ids and not series:
            raise ValidationError(_('The YAML file does not contain any instruments.'))
        first_name = book['sheets'][0].get('name') if book['sheets'] else False
        board_name = (self.name or first_name or 'Imported board').strip()[:255] or 'Imported board'
        watchlist = self.env['eex.watchlist'].create({
            'name': board_name,
            'company_id': self.env.company.id,
            'owner_id': self.env.user.id,
            'refresh_interval': self.refresh_interval,
            'instrument_ids': [fields.Command.set(sorted(instrument_ids))],
            'series_ids': [fields.Command.create({'market_id': market_id, 'symbol': symbol})
                           for market_id, symbol in sorted(series)],
        })
        return watchlist.action_dashboard()
