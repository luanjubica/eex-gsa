import io
from collections import defaultdict

import xlsxwriter
from werkzeug.exceptions import NotFound

from odoo import http
from odoo.http import request, content_disposition


class EexExport(http.Controller):
    def _watchlist(self, watchlist_id):
        watchlist = request.env['eex.watchlist'].browse(watchlist_id).exists()
        if not watchlist:
            raise NotFound()
        watchlist._check_read()
        return watchlist

    @http.route('/eex/watchlist/<int:watchlist_id>/export', type='http', auth='user', methods=['GET'])
    def export_watchlist(self, watchlist_id):
        watchlist = self._watchlist(watchlist_id)
        data = watchlist.dashboard_data(watchlist_id)
        output = io.BytesIO()
        book = xlsxwriter.Workbook(output, {'in_memory': True, 'strings_to_formulas': False, 'strings_to_urls': False})
        sheet = book.add_worksheet('Watchlist')
        header = book.add_format({'bold': True, 'bg_color': '#173B49', 'font_color': '#FFFFFF'})
        price = book.add_format({'num_format': '#,##0.0000;[Red]-#,##0.0000'})
        columns = [column['label'] for column in data['columns']]
        sheet.write_row(0, 0, columns, header)
        for index, row in enumerate(data['rows'], 1):
            for column_index, column in enumerate(data['columns']):
                value = self._board_value(row, column)
                if value is not None:
                    if column['type'] == 'number':
                        sheet.write_number(index, column_index, value, price)
                    else:
                        sheet.write(index, column_index, value)
        sheet.freeze_panes(1, 1)
        sheet.autofilter(0, 0, len(data['rows']), len(columns) - 1)
        sheet.set_column(0, max(0, len(columns) - 1), 20)
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
        return self._response(output, 'eex-watchlist-%s.xlsx' % watchlist.id)

    @staticmethod
    def _board_value(row, column):
        if column['source'] == 'instrument':
            value = row.get(column['field'])
        else:
            feed = row['feeds'][column['source']]
            value = (feed.get('values') or {}).get(column['field']) if column['type'] == 'number' \
                    else feed.get(column['field'])
        if column['type'] == 'status':
            return {'ok': 'Available', 'waiting': 'Awaiting data', 'empty': 'No data',
                    'stale': 'Stale cache', 'previous_day': 'Earlier trading day',
                    'error': 'Collection error', 'disabled': 'Feed disabled',
                    'paused': 'Collection paused'}.get(value, value or '')
        if column['type'] == 'availability':
            return 'Available' if value else 'Outside latest catalogue'
        if column['type'] == 'datetime' and value:
            return value + ' UTC'
        return value if value not in (None, '') else None

    @http.route('/eex/watchlist/<int:watchlist_id>/history/export', type='http', auth='user', methods=['GET'])
    def export_history(self, watchlist_id):
        watchlist = self._watchlist(watchlist_id)
        connection = request.env['eex.connection'].sudo().search([('company_id', '=', watchlist.company_id.id)], limit=1)
        days = connection.history_backfill_days if connection else 10
        History = request.env['eex.historical']
        rows = History.search([('instrument_id', 'in', watchlist.instrument_ids.ids)],
                              order='trade_date, instrument_id, metric')
        output = io.BytesIO()
        book = xlsxwriter.Workbook(output, {'in_memory': True, 'strings_to_formulas': False, 'strings_to_urls': False})
        title = book.add_format({'bold': True, 'font_size': 14, 'font_color': '#173B49'})
        header = book.add_format({'bold': True, 'bg_color': '#173B49', 'font_color': '#FFFFFF', 'text_wrap': True})
        subheader = book.add_format({'bold': True, 'bg_color': '#E8F0F2', 'font_color': '#173B49'})
        date_format = book.add_format({'num_format': 'yyyy-mm-dd'})
        timestamp_format = book.add_format({'num_format': 'yyyy-mm-dd hh:mm:ss'})
        price = book.add_format({'num_format': '#,##0.0000;[Red]-#,##0.0000'})
        used_names = {'History data', 'About'}
        by_market = defaultdict(list)
        for instrument in watchlist.instrument_ids:
            by_market[instrument.market_id].append(instrument)
        for market, instruments in sorted(by_market.items(), key=lambda item: item[0].name):
            sheet_name = self._sheet_name(market.name, used_names)
            sheet = book.add_worksheet(sheet_name)
            sheet.write(0, 0, '%s — daily close' % market.name, title)
            sheet.write(1, 0, 'Date', header)
            dates = sorted({row.trade_date for row in rows if row.market_id == market})[-days:]
            values = {(row.trade_date, row.instrument_id.id): row.value for row in rows
                      if row.market_id == market and row.metric == 'last'}
            for column, instrument in enumerate(instruments, 1):
                sheet.write(1, column, instrument.name, header)
                sheet.write(2, column, '%s %s' % (instrument.short_code, instrument.maturity), subheader)
            for index, trade_date in enumerate(dates, 3):
                sheet.write_datetime(index, 0, trade_date, date_format)
                for column, instrument in enumerate(instruments, 1):
                    value = values.get((trade_date, instrument.id))
                    if value is not None:
                        sheet.write_number(index, column, value, price)
            sheet.freeze_panes(3, 1)
            sheet.set_column(0, 0, 13)
            sheet.set_column(1, max(1, len(instruments)), 24)
        detail = book.add_worksheet('History data')
        detail_columns = ['Date', 'Market', 'Instrument', 'Short code', 'Maturity', 'Metric', 'Value',
                          'Currency', 'Unit', 'Source time (UTC)', 'Fetched (UTC)']
        detail.write_row(0, 0, detail_columns, header)
        allowed_dates = {}
        for market in by_market:
            allowed_dates[market.id] = set(sorted({r.trade_date for r in rows if r.market_id == market})[-days:])
        detail_row = 1
        for row in rows:
            if row.trade_date not in allowed_dates.get(row.market_id.id, set()):
                continue
            unit = row.volume_unit if row.metric == 'volume' else row.price_unit
            detail.write_datetime(detail_row, 0, row.trade_date, date_format)
            detail.write_row(detail_row, 1, [row.market_id.name, row.instrument_id.name,
                row.instrument_id.short_code, row.instrument_id.maturity, row.metric])
            detail.write_number(detail_row, 6, row.value, price)
            detail.write_row(detail_row, 7, [row.currency or '', unit or ''])
            if row.source_at:
                detail.write_datetime(detail_row, 9, row.source_at, timestamp_format)
            if row.fetched_at:
                detail.write_datetime(detail_row, 10, row.fetched_at, timestamp_format)
            detail_row += 1
        detail.freeze_panes(1, 2)
        detail.autofilter(0, 0, max(1, detail_row - 1), len(detail_columns) - 1)
        detail.set_column(0, 0, 13)
        detail.set_column(1, 4, 24)
        detail.set_column(5, 8, 14)
        detail.set_column(9, 10, 22)
        about = book.add_worksheet('About')
        about.set_column(0, 0, 105)
        for index, line in enumerate([
            'EEX API daily history: ' + watchlist.name,
            'The market sheets follow the supplied add-in pattern: dates in rows and contracts in columns.',
            'Daily close uses LastPx from the EEX /stats derivatives endpoint.',
            'The detail sheet includes every stored metric returned by enabled historical feeds.',
            'This export uses the latest %s stored trading days per market and does not call EEX.' % days,
            'Blank cells mean EEX returned no value for that contract and date.',
        ]):
            about.write_string(index, 0, line)
        book.close()
        return self._response(output, 'eex-history-%s.xlsx' % watchlist.id)

    @staticmethod
    def _sheet_name(name, used):
        base = ''.join('_' if char in '[]:*?/\\' else char for char in name)[:31] or 'Market'
        candidate = base
        suffix = 2
        while candidate in used:
            tail = ' %s' % suffix
            candidate = base[:31 - len(tail)] + tail
            suffix += 1
        used.add(candidate)
        return candidate

    @staticmethod
    def _response(output, filename):
        return request.make_response(output.getvalue(), headers=[
            ('Content-Type', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
            ('Content-Disposition', content_disposition(filename)),
            ('Cache-Control', 'no-store'),
        ])
