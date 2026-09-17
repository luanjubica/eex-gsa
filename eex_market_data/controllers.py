import io

import xlsxwriter
from werkzeug.exceptions import NotFound

from odoo import http
from odoo.http import request, content_disposition


class EexExport(http.Controller):
    @http.route('/eex/watchlist/<int:watchlist_id>/export', type='http', auth='user', methods=['GET'])
    def export_watchlist(self, watchlist_id):
        watchlist = request.env['eex.watchlist'].browse(watchlist_id).exists()
        if not watchlist:
            raise NotFound()
        watchlist._check_read()
        data = watchlist.dashboard_data(watchlist_id)
        output = io.BytesIO()
        book = xlsxwriter.Workbook(output, {'in_memory': True, 'strings_to_formulas': False, 'strings_to_urls': False})
        sheet = book.add_worksheet('Watchlist')
        header = book.add_format({'bold': True, 'bg_color': '#173B49', 'font_color': '#FFFFFF'})
        price = book.add_format({'num_format': '#,##0.0000;[Red]-#,##0.0000'})
        columns = ['Instrument', 'Market', 'Currency', 'Price unit', 'Volume unit', 'Delivery start', 'Delivery end']
        columns += [column['label'] for column in data['columns']]
        for label in ('Statistics', 'Bid / ask', 'Settlement'):
            columns += [label + ' status', label + ' trade date', label + ' source time (UTC)', label + ' fetched (UTC)']
        sheet.write_row(0, 0, columns, header)
        for index, row in enumerate(data['rows'], 1):
            values = [row.get(key) or '' for key in ('name', 'market', 'currency', 'price_unit', 'volume_unit', 'delivery_start', 'delivery_end')]
            sheet.write_row(index, 0, values)
            column_index = len(values)
            for column in data['columns']:
                key = column['key']
                kind = 'tob' if key in ('bid', 'ask') else 'spr' if key == 'settlement' else 'stat'
                value = row['feeds'][kind]['values'].get(key)
                if value is not None:
                    sheet.write_number(index, column_index, value, price)
                column_index += 1
            for kind in ('stat', 'tob', 'spr'):
                feed = row['feeds'][kind]
                sheet.write_row(index, column_index, [feed.get(key) or '' for key in ('status', 'trade_date', 'source_at', 'fetched_at')])
                column_index += 4
        sheet.freeze_panes(1, 2)
        sheet.autofilter(0, 0, len(data['rows']), len(columns) - 1)
        sheet.set_column(0, 1, 26)
        sheet.set_column(2, len(columns) - 1, 20)
        note = book.add_worksheet('About')
        note.set_column(0, 0, 105)
        for index, line in enumerate([
            'EEX watchlist: ' + data['name'],
            'Exported cached observations. Exporting does not fetch fresh market data.',
            'Subscription timing: ' + data['timing'],
            'Blank price cells mean unavailable data. Zero and negative prices are preserved.',
            'Statistics include exchange and trade registration activity. Source time is unavailable for statistics.',
            'Settlement is for the displayed trading date; no previous-day settlement is substituted.',
            'Check each feed status, trading date and fetch time before using its values.',
        ]):
            note.write_string(index, 0, line)
        book.close()
        return request.make_response(output.getvalue(), headers=[
            ('Content-Type', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
            ('Content-Disposition', content_disposition('eex-watchlist-%s.xlsx' % watchlist.id)),
            ('Cache-Control', 'no-store'),
        ])
