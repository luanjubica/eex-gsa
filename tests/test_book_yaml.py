import importlib.util
from pathlib import Path
from unittest import TestCase


path = Path(__file__).parents[1] / 'eex_market_data' / 'services' / 'book_yaml.py'
spec = importlib.util.spec_from_file_location('book_yaml', path)
book_yaml = importlib.util.module_from_spec(spec)
spec.loader.exec_module(book_yaml)


class TestBookYaml(TestCase):
    def test_generated_book_structure(self):
        parsed = book_yaml.parse_book_yaml('''# comment
history_days: 10
history_keep_days: 400
commodity: POWER
market: derivatives
sheets:
  - name: "Germany"
    area: DE
    instruments:
      - /E.DEBMV26
      - /E.DB_DAILY
''')
        self.assertEqual(parsed['commodity'], 'POWER')
        self.assertEqual(parsed['sheets'][0]['name'], 'Germany')
        self.assertEqual(parsed['sheets'][0]['instruments'], ['/E.DEBMV26', '/E.DB_DAILY'])

    def test_rejects_tags_and_nested_structures(self):
        with self.assertRaises(book_yaml.BookYamlError):
            book_yaml.parse_book_yaml('sheets:\n  - name: !!python/object:bad\n    area: DE\n    instruments: []\n')
