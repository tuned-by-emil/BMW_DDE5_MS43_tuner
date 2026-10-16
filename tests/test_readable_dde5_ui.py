import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication
from bmw_bin_editor import gui
from bmw_bin_editor.core import CalibrationDocument, XDFDatabase
from bmw_bin_editor.dde5_labels import PARAMETER_NAMES, table_name
from bmw_bin_editor.live_logging import LoggingDialog, load_parameters

APP = QApplication.instance() or QApplication([])
ROOT = Path(__file__).resolve().parents[1]


def leaves(tree):
    def visit(item):
        if not item.childCount():
            yield item
        for n in range(item.childCount()):
            yield from visit(item.child(n))
    for n in range(tree.topLevelItemCount()):
        yield from visit(tree.topLevelItem(n))


class ReadableMapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.database = XDFDatabase(ROOT / 'definitions/EDC16C31 361830.xdf')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = QSettings(str(Path(self.temp.name) / 'ui.ini'), QSettings.IniFormat)
        patcher = patch.object(gui, 'QSettings', lambda *args: self.settings)
        patcher.start(); self.addCleanup(patcher.stop)
        self.window = gui.MainWindow()
        self.addCleanup(self.window.close)
        self.window.xdf = self.database
        self.window.populate_tree()

    def test_names_keep_original_evidence_and_do_not_cross_ecus(self):
        for definition in self.database.definitions:
            self.assertTrue(table_name(definition))
            self.assertFalse(table_name(definition).startswith('0x'))
            if '[UV]' in definition.title:
                self.assertIn('unverified', table_name(definition))
        boost = next(d for d in self.database.tables if d.z_address == 0xF95D8)
        self.assertEqual(table_name(boost), 'Boost Target B')
        self.assertIsNone(table_name(replace(boost, profile_id='different-ecu')))
        self.assertIsNone(table_name(replace(boost, description='UNVERIFIED pattern candidate. Synthetic fixture')))
        self.assertIn('[CC]', boost.title)

    def test_two_columns_search_name_address_and_original_description(self):
        tree = self.window.tree
        self.assertEqual([tree.headerItem().text(i) for i in range(2)], ['Name', 'Address'])
        item = next(i for i in leaves(tree) if i.text(1) == '0xF95D8')
        self.assertEqual(item.text(0), 'Boost Target B')
        self.assertIn('[CC]', item.toolTip(0))
        for query in ('boost target b', 'f95d8', 'boost-pressure target map b'):
            self.window.search.setText(query)
            self.assertFalse(item.isHidden())
        self.window.search.setText('no such table')
        self.assertTrue(item.isHidden())

    def test_favorite_address_selection_keeps_storage_identity_and_bin(self):
        definition = next(d for d in self.database.tables if d.z_address == 0xF95D8)
        key = gui.definition_key(definition)
        self.assertEqual(key, 'table:0F95D8')
        self.window.favorites = {key}; self.window.populate_favorites()
        item = self.window.favorite_tree.topLevelItem(0)
        self.assertEqual(item.text(1), '0xF95D8')
        for query in ('Boost Target B', 'F95D8'):
            self.window.favorite_search.setText(query)
            self.assertFalse(item.isHidden())
        path = Path(self.temp.name) / 'synthetic.bin'; path.write_bytes(bytes(0x100000))
        doc = CalibrationDocument(self.database, path)
        self.window.document = doc; self.window.editor.set_document(doc)
        self.window.open_tree_item(item, 1)
        self.assertIs(self.window.editor.definition, definition)
        self.assertFalse(doc.dirty)
        self.assertEqual(bytes(doc.data), path.read_bytes())


class ReadableParameterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.catalog = load_parameters(ROOT / 'definitions/live_parameters.json')
        self.settings = QSettings(str(Path(self.temp.name) / 'log.ini'), QSettings.IniFormat)
        self.dialog = LoggingDialog(self.catalog, settings=self.settings)
        self.addCleanup(self.dialog.close)

    def test_all_parameters_have_english_names_and_three_identity_columns(self):
        self.assertEqual(set(self.catalog) - set(PARAMETER_NAMES), set())
        self.assertEqual(self.dialog.tree.columnCount(), 3)
        self.assertEqual([self.dialog.tree.headerItem().text(i) for i in range(3)],
                         ['English name', 'BMW name', 'Hex code'])
        for item in self.dialog.items():
            row = self.catalog[item.data(0, Qt.UserRole)]
            self.assertEqual(item.text(1), row['result'])
            self.assertEqual(item.text(2), row['id'])
            self.assertIn('Units:', item.toolTip(0))

    def test_search_every_column_and_original_argument_preserves_selection(self):
        item = next(i for i in self.dialog.items() if i.data(0, Qt.UserRole) == 'INMOT')
        before = self.dialog.profile()
        for query in ('engine speed', item.text(1).lower(), item.text(2).lower(), 'INMOT'):
            self.dialog.search.setText(query)
            self.assertFalse(item.isHidden())
            self.assertEqual(self.dialog.profile(), before)
        self.dialog.search.setText('no such parameter')
        self.assertTrue(item.isHidden())

    def test_group_updates_never_overwrite_hex_codes_or_change_profile_identity(self):
        item = next(i for i in self.dialog.items() if i.data(0, Qt.UserRole) == 'SPRDR')
        item.setCheckState(0, Qt.Checked)
        self.dialog.update_summary()
        self.assertEqual(item.text(2), self.catalog['SPRDR']['id'])
        self.assertIn('SPRDR', self.dialog.profile()['parameters'])
        self.assertIn('Capture groups:', item.toolTip(2))
        self.dialog.selected_only.setChecked(True)
        self.assertFalse(item.isHidden())
