
from optional_resources import requires_resource
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication, QDialog
from bmw_bin_editor import gui
from bmw_bin_editor.core import AxisDefinition, CalibrationDefinition, CalibrationDocument, XDFDatabase
from bmw_bin_editor.definition_library import DefinitionMatch, LibraryEntry
from bmw_bin_editor.definition_library_gui import DefinitionProposalDialog
from bmw_bin_editor.xdf_edit import XDFWorkspace
from bmw_bin_editor.xdf_gui import DefinitionPreview

APP = QApplication.instance() or QApplication([])


class LibraryGuiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.old_settings = gui.QSettings
        gui.QSettings = lambda org, name: QSettings(str(self.directory/(name+'.ini')), QSettings.IniFormat)
        gui.QSettings('EmilTools', 'BMWBinEditor').setValue('mpps/auto_launch', False)
        self.data = struct.pack('>4h', -77, 64, 128, 256) + b'\xff'*120
        self.bin = self.directory/'software.bin'; self.bin.write_bytes(self.data)
        workspace = XDFWorkspace(bin_size=len(self.data))
        workspace.set_metadata('Other software', 'Emil Larsson', 'Synthetic library fixture', False)
        workspace.add(CalibrationDefinition('Timing symbol', 'From synthetic pack', 'Timing', 0, 2, 2,
                                           z_signed=True, z_equation='X * 3 / 128', z_units='deg'))
        self.database = workspace.save_as(self.directory/'other.xdf')
        self.entry = LibraryEntry('other', self.database.title, self.database.path, len(self.data), {'map_count': 1})
        self.match = DefinitionMatch(self.entry, self.database.path, 100, 'Exact original image', ['Synthetic hash and layout match'])
        self.windows = []

    def tearDown(self):
        for window in self.windows:
            if getattr(window, 'document', None):
                window.document.dirty = False
            window.close()
        APP.processEvents()
        gui.QSettings = self.old_settings
        self.temp.cleanup()

    def window(self):
        window = gui.MainWindow(); self.windows.append(window)
        return window

    def open(self, window, choice, matches=None):
        def select(dialog):
            dialog.choose(choice)
            return dialog.result()
        with patch.object(gui, 'propose_matches', return_value=matches if matches is not None else [self.match]), \
             patch.object(gui.QFileDialog, 'getOpenFileName', return_value=(str(self.bin), '')), \
             patch.object(DefinitionProposalDialog, 'exec', select):
            window.open_bin()

    def test_proposal_buttons_review_current_compatibility_and_explicit_selection(self):
        dialog = DefinitionProposalDialog(self.data, self.bin.name, [self.match], self.database, True)
        self.windows.append(dialog)
        self.assertIs(dialog.selected_match, self.match)
        self.assertIn('Synthetic hash', dialog.evidence.toPlainText())
        self.assertTrue(dialog.keep_button.isEnabled())
        dialog.choose('library')
        self.assertEqual(dialog.choice, 'library')
        self.assertEqual(dialog.result(), QDialog.Accepted)
        disabled = DefinitionProposalDialog(self.data, self.bin.name, [], self.database, False)
        self.windows.append(disabled)
        self.assertFalse(disabled.use_button.isEnabled())
        self.assertFalse(disabled.keep_button.isEnabled())
        disabled.choose('current')
        self.assertEqual(disabled.result(), QDialog.Rejected)

    def test_bin_open_proposes_and_uses_selected_definition_without_changing_bytes(self):
        window = self.window()
        self.open(window, 'library')
        self.assertEqual(window.xdf.path, self.database.path)
        self.assertEqual(window.current_xdf_path, self.database.path)
        self.assertEqual(bytes(window.document.data), self.data)
        self.assertEqual(window.editor.definition.title, 'Timing symbol')
        self.assertAlmostEqual(window.document.map_values(window.editor.definition)[0][0], -77*3/128)
        self.assertEqual(self.bin.read_bytes(), self.data)

    def test_open_without_definitions_and_open_with_no_preloaded_xdf(self):
        window = self.window()
        self.assertIsNone(window.xdf)
        self.assertIsNone(window.current_xdf_path)
        self.open(window, 'empty')
        self.assertEqual(window.xdf.definitions, [])
        self.assertEqual(bytes(window.document.data), self.data)
        self.assertIsNone(window.current_xdf_path)
        self.assertFalse(self.bin.with_suffix('.xdf').exists())

    @requires_resource('definitions/EDC16C31 361830.xdf')
    def test_retained_car_definition_and_presets_are_loaded_only_after_bin_proposal_selection(self):
        database = XDFDatabase(gui.bundled_definition_path())
        data = b'\xff' * 0x200000
        self.bin.write_bytes(data)
        entry = LibraryEntry('361830', database.title, database.path, len(data), {'map_count': 207})
        match = DefinitionMatch(entry, database.path, 86, 'Software and axes match', ['Retained definition fixture'])
        window = self.window()
        self.assertIsNone(window.xdf)
        self.assertEqual(window.link_groups, [])
        self.assertEqual(window.step_overrides, {})
        def select(dialog):
            self.assertFalse(dialog.keep_button.isEnabled())
            dialog.choose('library')
            return dialog.result()
        with patch.object(gui, 'propose_matches', return_value=[match]), \
             patch.object(gui.QFileDialog, 'getOpenFileName', return_value=(str(self.bin), '')), \
             patch.object(DefinitionProposalDialog, 'exec', select):
            window.open_bin()
        self.assertEqual(window.xdf.profile_id, '361830')
        self.assertEqual(window.current_xdf_path.resolve(), gui.bundled_definition_path().resolve())
        self.assertEqual(bytes(window.document.data), data)
        self.assertEqual({group['name'] for group in window.link_groups}, {'Boost Target', 'N75 / VNT Base'})
        self.assertIn('table:0F7F44', window.step_overrides)

    def test_cancel_proposal_keeps_active_dirty_bin_and_xdf(self):
        window = self.window()
        old = CalibrationDocument(self.database, self.bin)
        old.set_cells(self.database.tables[0], {(0, 0): -5}, 'User edit')
        before = bytes(old.data); history = old.undo_stack.copy()
        window.xdf = self.database; window.document = old
        with patch.object(gui, 'propose_matches', return_value=[self.match]), \
             patch.object(gui.QFileDialog, 'getOpenFileName', return_value=(str(self.bin), '')), \
             patch.object(DefinitionProposalDialog, 'exec', return_value=QDialog.Rejected):
            window.open_bin()
        self.assertIs(window.document, old)
        self.assertIs(window.xdf, self.database)
        self.assertEqual(bytes(old.data), before)
        self.assertTrue(old.dirty)
        self.assertEqual(old.undo_stack, history)

    def test_keep_current_and_unknown_bin_use_explicit_custom_definition(self):
        window = self.window(); window.xdf = self.database; window.current_xdf_path = self.database.path
        self.open(window, 'current')
        self.assertIs(window.xdf, self.database)
        self.open(window, 'library', matches=[])
        self.assertIs(window.xdf, self.database)

    def test_catalog_definition_with_no_match_is_not_silently_retained(self):
        window = self.window(); window.xdf = self.database
        with patch.object(gui, 'discover_entries', return_value=[self.entry]):
            self.open(window, 'library', matches=[])
        self.assertEqual(window.xdf.definitions, [])
        self.assertIsNone(window.current_xdf_path)

    def test_current_bin_proposal_preserves_edits_and_undo(self):
        window = self.window()
        window.xdf = self.database
        window.document = CalibrationDocument(self.database, self.bin)
        definition = self.database.tables[0]
        window.document.set_cells(definition, {(0, 0): -5}, 'User edit')
        old = window.document; before = bytes(old.data); history = old.undo_stack.copy()
        def select(dialog):
            dialog.choose('library'); return dialog.result()
        with patch.object(gui, 'propose_matches', return_value=[self.match]), \
             patch.object(DefinitionProposalDialog, 'exec', select):
            window.suggest_matching_xdf()
        self.assertIs(window.document, old)
        self.assertEqual(bytes(old.data), before)
        self.assertTrue(old.dirty)
        self.assertEqual(old.undo_stack, history)

    @requires_resource('definitions/EDC16C31 361830.xdf')
    def test_software_preferences_and_legacy_aliases_do_not_cross_layouts(self):
        legacy = XDFDatabase(gui.bundled_definition_path())
        original = next(d for d in legacy.tables if d.z_address == 0xF7F44)
        other = self.database.tables[0]
        other.z_address = original.z_address
        self.assertEqual(gui.definition_key(original), 'table:0F7F44')
        self.assertNotEqual(gui.definition_key(other), gui.definition_key(original))
        self.assertIn('N75 A', gui.short_map_name(original))
        self.assertNotIn('N75 A', gui.short_map_name(other))
        self.assertEqual(gui.MapEditor._candidate_family(other), ('', [], None))
        self.assertEqual(gui.MapEditor._candidate_family(original)[0], 'N75')
        window = self.window(); window.xdf = self.database
        window.link_groups = [{'name': 'Legacy', 'mode': 'absolute', 'members': [gui.definition_key(original), 'table:0F8478']}]
        self.assertIsNone(window.resolve_link_group(other))

    def test_numeric_fixed_axes_are_physical_in_document_and_preview(self):
        workspace = XDFWorkspace(bin_size=len(self.data))
        definition = CalibrationDefinition('Fixed axes', '', 'Test', 0, 2, 2, z_signed=True,
            x_axis=AxisDefinition(None, 2, labels=['10', '50']),
            y_axis=AxisDefinition(None, 2, labels=['500', '4000']))
        added = workspace.add(definition)
        database = workspace.save_as(self.directory/'fixed.xdf')
        document = CalibrationDocument(database, self.bin)
        self.assertEqual(document.axis_values(added.x_axis), [10, 50])
        self.assertEqual(document.axis_values(added.y_axis), [500, 4000])
        self.assertEqual(document.axis_values(AxisDefinition(None, 2, labels=['Low', 'High'])), [0, 1])
        preview = DefinitionPreview(self.data); self.windows.append(preview)
        preview.set_definition(added, '>')
        self.assertEqual(preview.table.horizontalHeaderItem(1).text(), '50')
        low, high = preview.figure.axes[0].get_xlim()
        self.assertLess(low, 10)
        self.assertGreater(high, 50)
        self.assertAlmostEqual((low + high) / 2, 30)


if __name__ == '__main__':
    unittest.main(verbosity=2)
