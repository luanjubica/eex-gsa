"""Run with: python3 -m unittest discover -s tests -v (requires requests)."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] / 'eex_market_data' / 'services' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


client = load('client')
normalization = load('normalization')


class TestClient(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        self.response = Mock(status_code=200, headers={})
        self.response.json.return_value = []
        self.session.get.return_value = self.response
        self.sleep = Mock()
        self.api = client.EexClient('test-token', self.session, self.sleep)

    def test_exact_trailing_slash_auth_timeout_and_no_redirect(self):
        self.api.get('rd', 'derivatives', 'POWER', listing=True)
        args, kwargs = self.session.get.call_args
        self.assertEqual(args[0], 'https://api.eex-group.com/v2/rd/derivatives/POWER/')
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer test-token')
        self.assertFalse(kwargs['allow_redirects'])
        self.assertEqual(kwargs['timeout'], (5, 20))
        self.sleep.assert_called_once_with(1.1)
        self.api.get('rd', 'derivatives', 'POWER', 'DE', '2026-09-16')
        self.assertFalse(self.session.get.call_args[0][0].endswith('/'))

    def test_status_codes_and_redacted_errors(self):
        for status, retryable in [(401, False), (403, False), (302, False), (500, True), (429, True)]:
            self.response.status_code = status
            with self.assertRaises(client.EexError) as caught:
                self.api.get('rd', 'derivatives', listing=True)
            self.assertEqual(caught.exception.retryable, retryable)
            self.assertNotIn('test-token', str(caught.exception))
        self.response.status_code = 404
        self.assertEqual(self.api.get('rd', 'derivatives', listing=True), [])

    def test_truncation_rejected(self):
        self.response.json.return_value = [{}] * 60000
        with self.assertRaises(client.EexError):
            self.api.get('stat', 'derivatives', 'POWER', 'DE', '2026-09-16')

    def test_invalid_json_and_invalid_structure(self):
        self.response.json.side_effect = ValueError('body contains private data')
        with self.assertRaisesRegex(client.EexError, 'invalid JSON'):
            self.api.get('rd', 'derivatives')
        self.response.json.side_effect = None
        self.response.json.return_value = 'bad'
        with self.assertRaises(client.EexError):
            self.api.get('rd', 'derivatives')

    def test_retry_after_clamped(self):
        self.response.status_code = 429
        self.response.headers = {'Retry-After': '99999'}
        with self.assertRaises(client.EexError) as caught:
            self.api.get('rd', 'derivatives')
        self.assertEqual(caught.exception.retry_after, 3600)

    def test_zero_negative_null_and_nan(self):
        self.assertEqual(normalization.number('0'), 0)
        self.assertEqual(normalization.number('-12.55'), -12.55)
        self.assertIsNone(normalization.number(None))
        with self.assertRaises(ValueError):
            normalization.number('NaN')

    def test_timestamp_normalized_and_naive_rejected(self):
        self.assertEqual(str(normalization.utc_timestamp('2026-09-16T12:30:00+02:00')), '2026-09-16 10:30:00')
        with self.assertRaises(ValueError):
            normalization.utc_timestamp('2026-09-16T12:30:00')

    def test_error_objects_are_not_treated_as_empty_market_data(self):
        with self.assertRaises(ValueError):
            normalization.reference_values({'message': 'Unexpected response'})
        with self.assertRaises(ValueError):
            normalization.quote_values({'message': 'Unexpected response'}, 'stat')


if __name__ == '__main__':
    unittest.main()
