import os
from datetime import timedelta
from unittest.mock import patch
from unittest import TestCase

from odoo import fields
from odoo.exceptions import AccessError, ValidationError, UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase, new_test_user

from ..services.client import EexError


@tagged('post_install', '-at_install')
class TestEexMarketData(TransactionCase):
    def setUp(self):
        super().setUp()
        self.connection = self.env['eex.connection'].create({'name': 'Test EEX', 'enabled': True, 'collect_tob': True})
        self.market = self.env['eex.market'].create({'connection_id': self.connection.id, 'commodity': 'POWER',
                                                  'area': 'DE', 'enabled': True, 'trade_date': fields.Date.today()})
        self.instrument = self.env['eex.instrument'].create({'name': 'DEBM OCT26', 'market_id': self.market.id,
            'isin': 'DE000TEST001', 'short_code': 'DEBM', 'maturity': '202610', 'currency': 'EUR', 'price_unit': 'MWh'})
        self.alice = new_test_user(self.env, login='eex_alice', groups='eex_market_data.group_eex_user')
        self.bob = new_test_user(self.env, login='eex_bob', groups='eex_market_data.group_eex_user')
        self.watchlist = self.env['eex.watchlist'].with_user(self.alice).create({
            'name': 'Private', 'instrument_ids': [(6, 0, self.instrument.ids)]})

    def row(self, **values):
        return dict({'InstrumentISIN': self.instrument.isin, 'InstrumentType': 'Simple Instrument',
            'Cmdty': 'POWER', 'Area': 'DE', 'TrdDate': str(self.market.trade_date)}, **values)

    def job(self, kind='stat', history_from=None, history_to=None, repeat=True):
        return self.env['eex.job']._enqueue(self.connection, kind, self.market,
                                            instrument=self.instrument if kind.startswith('hist_') else None,
                                            history_from=history_from, history_to=history_to,
                                            repeat=repeat)

    def test_private_shared_and_read_only_access(self):
        other = self.watchlist.with_user(self.bob)
        with self.assertRaises(AccessError):
            other.read(['name'])
        with self.assertRaises(AccessError):
            other.dashboard_data(self.watchlist.id)
        with self.assertRaises(AccessError):
            other.action_refresh()
        self.watchlist.write({'shared': True})
        self.assertEqual(other.read(['name'])[0]['name'], 'Private')
        other.action_refresh()
        with self.assertRaises(AccessError):
            other.write({'name': 'Hijacked'})
        with self.assertRaises(AccessError):
            other.unlink()
        with self.assertRaises(AccessError):
            self.watchlist.write({'owner_id': self.bob.id})
        with self.assertRaises(AccessError):
            self.env['eex.connection'].with_user(self.bob).search([])

    def test_company_isolation(self):
        company = self.env['res.company'].create({'name': 'Other company'})
        other_connection = self.env['eex.connection'].create({'name': 'Other', 'company_id': company.id})
        other_market = self.env['eex.market'].create({'connection_id': other_connection.id, 'commodity': 'POWER', 'area': 'FR'})
        instrument = self.env['eex.instrument'].create({'name': 'Other', 'market_id': other_market.id,
            'isin': 'OTHER', 'short_code': 'F7BM', 'maturity': '202610'})
        self.assertNotIn(instrument, self.env['eex.instrument'].with_user(self.alice).search([]))
        with TestCase.assertRaises(self, (AccessError, UserError)):
            with self.env.cr.savepoint():
                self.watchlist.write({'instrument_ids': [(4, instrument.id)]})

    def test_null_zero_negative_and_history(self):
        job = self.job()
        job._store_quotes([self.row(LastPx=0, LowPx=-20.5, TotTrdVol=None)])
        quote = self.instrument.quote_ids
        self.assertEqual(quote.values_json['last'], 0)
        self.assertEqual(quote.values_json['low'], -20.5)
        self.assertIsNone(quote.values_json['volume'])
        samples = self.env['eex.snapshot'].search([('instrument_id', '=', self.instrument.id)])
        self.assertEqual(len(samples), 2)
        job._store_quotes([self.row(LastPx=1)])
        self.assertEqual(self.env['eex.snapshot'].search_count([('instrument_id', '=', self.instrument.id)]), 2)
        job._store_quotes([])
        self.assertEqual(quote.status, 'empty')
        self.assertFalse(quote.values_json)

    def test_api_history_backfill_and_upsert(self):
        history_date = fields.Date.today() - timedelta(days=1)
        job = self.job('hist_stat', history_from=history_date, history_to=history_date, repeat=False)
        row = self.row(TrdDate=str(history_date), LastPx=75.5, OpenPx=70, TotTrdVol=None)
        job._store_history([row], 'stat')
        values = self.env['eex.historical'].search([('instrument_id', '=', self.instrument.id)])
        self.assertEqual(set(values.mapped('metric')), {'last', 'open'})
        self.assertEqual(values.filtered(lambda value: value.metric == 'last').value, 75.5)
        job._store_history([dict(row, LastPx=76.25)], 'stat')
        last = self.env['eex.historical'].search([('instrument_id', '=', self.instrument.id),
                                                  ('metric', '=', 'last')])
        self.assertEqual(len(last), 1)
        self.assertEqual(last.value, 76.25)

    def test_dates_enqueue_latest_historical_trading_days_once(self):
        self.connection.history_backfill_days = 2
        today = fields.Date.today()
        dates = [str(today - timedelta(days=value)) for value in (4, 3, 2, 0)]
        with patch('odoo.addons.eex_market_data.models.job.EexClient.get', return_value=dates):
            self.job('dates')._execute()
        historical = self.env['eex.job'].search([('kind', 'like', 'hist_%')])
        self.assertEqual(len(historical), 2)
        self.assertEqual(set(historical.mapped('history_from')), {today - timedelta(days=3)})
        self.assertEqual(set(historical.mapped('history_to')), {today - timedelta(days=2)})
        historical.write({'state': 'done'})
        self.market.history_requested = False
        with patch('odoo.addons.eex_market_data.models.job.EexClient.get', return_value=dates):
            self.job('dates')._execute()
        self.assertTrue(all(job.state == 'done' for job in historical))
        newer_dates = [str(today - timedelta(days=value)) for value in (3, 2, 1, 0)]
        with patch('odoo.addons.eex_market_data.models.job.EexClient.get', return_value=newer_dates):
            self.job('dates')._execute()
        self.assertTrue(all(job.state == 'pending' for job in historical))
        self.assertEqual(set(historical.mapped('history_from')), {today - timedelta(days=2)})
        self.assertEqual(set(historical.mapped('history_to')), {today - timedelta(days=1)})

    def test_history_action_queues_refresh_and_opens_api_history(self):
        self.market.history_requested = False
        result = self.watchlist.action_history_refresh()
        self.assertTrue(self.market.history_requested)
        self.assertEqual(result['params']['type'], 'success')
        action = self.watchlist.action_history()
        self.assertEqual(action['res_model'], 'eex.historical')
        self.assertEqual([view_type for _view_id, view_type in action['views']], ['graph', 'tree', 'pivot'])
        self.assertEqual(action['context'], {'search_default_last_price': 1, 'fill_temporal': False})
        self.assertIn(self.instrument.id, action['domain'][0][2])
        self.assertIn('/history/export', self.watchlist.action_history_export()['url'])

    def test_shared_queue_deduplicates(self):
        self.env['eex.watchlist'].with_user(self.bob).create({'name': 'Bob', 'instrument_ids': [(6, 0, self.instrument.ids)]})
        self.market.dates_checked_at = fields.Datetime.now()
        self.env['eex.job']._schedule()
        self.assertEqual(self.env['eex.job'].search_count([('market_id', '=', self.market.id), ('kind', '=', 'stat')]), 1)
        self.watchlist.action_refresh()
        previous = self.watchlist.last_scheduled
        self.env['eex.job']._schedule()
        self.assertEqual(self.watchlist.last_scheduled, previous)

    def test_missing_token_job_requeues_after_restore(self):
        job = self.job()
        job.write({'state': 'failed', 'message': 'Configure a valid EEX access token on the server.',
                   'last_attempt': fields.Datetime.now() - timedelta(minutes=10)})
        with patch.dict(os.environ, {'EEX_API_TOKEN': ''}):
            self.job()
            self.assertEqual(job.state, 'failed')
        with patch.dict(os.environ, {'EEX_API_TOKEN': 'restored'}):
            self.job()
            self.assertEqual(job.state, 'pending')
            self.assertFalse(job.message)
        job.write({'state': 'failed', 'message': 'The EEX subscription does not permit this request.'})
        with patch.dict(os.environ, {'EEX_API_TOKEN': 'restored'}):
            self.job()
            self.assertEqual(job.state, 'failed')

    def test_worker_keeps_last_good_data_on_error(self):
        job = self.job()
        job._store_quotes([self.row(LastPx=45)])
        fetched = self.instrument.quote_ids.fetched_at
        self.market.dates_checked_at = fields.Datetime.now()
        self.watchlist.sudo().write({'refresh_interval': 0, 'refresh_requested': False})
        with patch('odoo.addons.eex_market_data.models.job.EexClient.get', side_effect=EexError('Not entitled')):
            self.env['eex.job']._cron_collect()
        self.assertEqual(job.state, 'failed')
        self.assertEqual(self.instrument.quote_ids.fetched_at, fetched)
        self.assertEqual(self.instrument.quote_ids.values_json['last'], 45)
        data = self.watchlist.dashboard_data(self.watchlist.id)
        self.assertEqual(data['rows'][0]['feeds']['stat']['status'], 'error')

    def test_stale_previous_day_and_source_times(self):
        self.market.trade_date = fields.Date.today() - timedelta(days=1)
        job = self.job('tob')
        job._store_quotes([self.row(BidPx=0, AskPx=-1, Tm='2026-09-15T12:00:00+02:00')])
        data = self.watchlist.dashboard_data(self.watchlist.id)
        feed = data['rows'][0]['feeds']['tob']
        self.assertEqual(feed['status'], 'previous_day')
        self.assertEqual(feed['source_at'], '2026-09-15 10:00:00')
        self.instrument.quote_ids.fetched_at = fields.Datetime.now() - timedelta(hours=2)
        data = self.watchlist.dashboard_data(self.watchlist.id)
        self.assertEqual(data['rows'][0]['feeds']['tob']['status'], 'stale')

    def test_catalogue_import_excludes_options_and_spreads(self):
        row = self.row(ShortCode='DEBM', Maturity=202610, ProductType='Future', Currency='EUR', UOM='MWh')
        with patch('odoo.addons.eex_market_data.models.job.EexClient.get', return_value=[row,
                   dict(row, InstrumentISIN='OPTION', ProductType='Option'),
                   dict(row, InstrumentISIN='SPREAD', InstrumentType='Futures Spread')]):
            self.job('catalogue')._execute()
        self.assertEqual(self.env['eex.instrument'].search_count([('market_id', '=', self.market.id)]), 1)

    def test_empty_catalogue_does_not_remove_instruments(self):
        with patch('odoo.addons.eex_market_data.models.job.EexClient.get', return_value=[]):
            with self.assertRaises(EexError):
                self.job('catalogue')._execute()
        self.assertTrue(self.instrument.available)

    def test_bad_trade_date_preserves_cache(self):
        job = self.job()
        job._store_quotes([self.row(LastPx=50)])
        with self.assertRaises(EexError):
            job._store_quotes([self.row(LastPx=99, TrdDate='2020-01-01')])
        self.assertEqual(self.instrument.quote_ids.values_json['last'], 50)

    def test_manual_refresh_and_interval_validation(self):
        self.watchlist.write({'refresh_interval': 0})
        self.watchlist.action_refresh()
        self.assertTrue(self.watchlist.refresh_requested)
        with self.assertRaises(ValidationError):
            with self.env.cr.savepoint():
                self.watchlist.write({'refresh_interval': 2})
        with self.assertRaises(ValidationError):
            with self.env.cr.savepoint():
                self.connection.write({'history_backfill_days': 0})
