"""View-file regression checks, including optional use of an installed Odoo reader.

Set ODOO_SOURCE_ROOT to an Odoo checkout to exercise that checkout's exact
get_view_arch_from_file function without starting Odoo or connecting to a database.
"""
import ast
import logging
import os
from pathlib import Path
import unittest
from xml.etree import ElementTree


VIEWS = Path(__file__).resolve().parents[1] / 'eex_market_data' / 'views'


def view_records():
    for path in sorted(VIEWS.glob('*.xml')):
        for record in ElementTree.parse(path).getroot().iter('record'):
            if record.get('model') == 'ir.ui.view':
                yield path, record.get('id'), record.find("field[@name='arch']")


class TestViewFiles(unittest.TestCase):
    def test_architectures_support_legacy_file_reader(self):
        records = list(view_records())
        self.assertTrue(records)
        for path, xmlid, field in records:
            with self.subTest(file=path.name, view=xmlid):
                self.assertIsNotNone(field)
                # Older Odoo 16 combines field_arch.text + serialized children.
                # A child immediately after <field> makes .text None and crashes.
                self.assertIsNotNone(field.text, 'Keep whitespace before the architecture root for Odoo 16 file mode.')
                arch = field.text + ''.join(ElementTree.tostring(child, encoding='unicode') for child in field)
                self.assertIn(ElementTree.fromstring(arch).tag, ('tree', 'form', 'search', 'graph', 'pivot'))

    @unittest.skipUnless(os.environ.get('ODOO_SOURCE_ROOT'), 'Set ODOO_SOURCE_ROOT to test a specific Odoo checkout')
    def test_installed_odoo_file_reader(self):
        from lxml import etree

        root = Path(os.environ['ODOO_SOURCE_ROOT'])
        namespace = {'etree': etree, '_logger': logging.getLogger(__name__),
                     'SKIPPED_ELEMENT_TYPES': (etree._Comment, etree._ProcessingInstruction)}
        functions = [
            (root / 'odoo/tools/convert.py', '_fix_multiple_roots'),
            (root / 'odoo/addons/base/models/ir_ui_view.py', 'get_view_arch_from_file'),
        ]
        for path, function_name in functions:
            parsed = ast.parse(path.read_text())
            function = next(node for node in parsed.body if isinstance(node, ast.FunctionDef) and node.name == function_name)
            module = ast.Module(body=[function], type_ignores=[])
            exec(compile(module, str(path), 'exec'), namespace)
        for path, xmlid, _field in view_records():
            with self.subTest(file=path.name, view=xmlid):
                arch = namespace['get_view_arch_from_file'](str(path), 'eex_market_data.' + xmlid)
                self.assertIsInstance(arch, str)
                self.assertIn(etree.fromstring(arch.encode()).tag, ('tree', 'form', 'search', 'graph', 'pivot'))


if __name__ == '__main__':
    unittest.main()
