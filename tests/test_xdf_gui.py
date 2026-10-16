
from optional_resources import requires_resource
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import hashlib
import json
import struct
import sys
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication, QAbstractItemView, QMessageBox
from bmw_bin_editor import gui
from bmw_bin_editor.core import AxisDefinition, CalibrationDefinition, CalibrationDocument, XDFDatabase
from bmw_bin_editor.xdf_edit import XDFWorkspace
from bmw_bin_editor.xdf_gui import XDFEditorDialog, PotentialMapsDialog
from bmw_bin_editor.bin_analysis import analyze_maps, candidate_definition
from bmw_bin_editor.logdata import profile_for_definition, SIG_RPM
from test_map_classification import family_blob

APP = QApplication.instance() or QApplication([])


def example():
    data = bytearray(b'\xFF' * 1024)
    words = [3, 4, 100, 200, 300, 0, 1000, 2000, 3000] + [500 + r * 50 + c * 10 for r in range(3) for c in range(4)]
    data[64:64+len(words)*2] = struct.pack('>'+'H'*len(words), *words)
    d = CalibrationDefinition('Demo', 'Synthetic table', 'Test', 82, 3, 4,
                              x_axis=AxisDefinition(74, 4), y_axis=AxisDefinition(68, 3))
    workspace = XDFWorkspace(bin_size=len(data))
    workspace.add(d)
    return bytes(data), workspace.database


def finish_scan(dialog):
    APP.processEvents()
    deadline = time.monotonic() + 5
    while dialog.worker is not None and time.monotonic() < deadline:
        APP.processEvents()
        time.sleep(0.005)
    if dialog.worker is not None:
        dialog.cancel_scan()
        dialog.worker.wait(2000)
        APP.processEvents()
        raise AssertionError('Scanner did not finish')
    APP.processEvents()


def sized_family_blob(family):
    data, _candidate = family_blob(family)
    return data[:128] + struct.pack('>HH', 8, 8) + data[128:]


class XDFGuiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data, self.database = example()
        self.old_settings = gui.QSettings
        gui.QSettings = lambda org, name: QSettings(str(Path(self.temp.name)/(name+'.ini')), QSettings.IniFormat)
        self.dialogs = []

    def tearDown(self):
        for dialog in self.dialogs:
            if isinstance(dialog, PotentialMapsDialog):
                finish_scan(dialog)
                dialog.reject()
            else:
                with patch.object(QMessageBox, 'question', return_value=QMessageBox.Yes):
                    dialog.reject()
        APP.processEvents()
        gui.QSettings = self.old_settings
        self.temp.cleanup()

    def editor(self):
        dialog = XDFEditorDialog(self.database, self.data)
        self.dialogs.append(dialog)
        return dialog

    def test_staged_edits_preview_and_saved_definition_application(self):
        original = ET.tostring(self.database.root)
        dialog = self.editor()
        self.assertEqual(float(dialog.preview.table.item(0, 0).text()), 500)
        self.assertEqual(dialog.preview.table.editTriggers(), QAbstractItemView.NoEditTriggers)
        dialog.form.name.setText('Reviewed map')
        dialog.form.equation.setText('X * 0.1')
        dialog.form.units.setText('Test units')
        dialog.update_preview()
        self.assertEqual(float(dialog.preview.table.item(0, 0).text()), 50)
        self.assertTrue(dialog.commit_current())
        self.assertEqual(ET.tostring(self.database.root), original)
        self.assertEqual(dialog.preview.data, self.data)
        self.assertTrue(dialog.save_xdf(path=Path(self.temp.name)/'edited.xdf'))
        self.assertTrue(dialog.use_button.isEnabled())
        dialog.use_saved()
        self.assertEqual(dialog.applied_database.definitions[0].title, 'Reviewed map')
        dialog.form.units.setText('Unstored changes')
        self.assertFalse(dialog.use_button.isEnabled())

    def test_invalid_address_does_not_commit_and_revert_restores_preview(self):
        dialog = self.editor()
        dialog.form.address.setText('0x1000')
        dialog.update_preview()
        self.assertIn('outside', dialog.preview.note.text())
        self.assertFalse(dialog.commit_current())
        self.assertEqual(dialog.workspace.database.definitions[0].z_address, 82)
        self.assertIn('outside BIN', dialog.status.text())
        dialog.revert_form()
        self.assertEqual(dialog.form.address.text(), '0x52')
        self.assertEqual(float(dialog.preview.table.item(0, 0).text()), 500)

    def test_new_definition_duplicate_remove_and_header_properties(self):
        dialog = self.editor()
        dialog.new_definition('constant')
        self.assertEqual(dialog.form.original.kind, 'constant')
        self.assertFalse(dialog.form.rows.isEnabled())
        dialog.duplicate_definition()
        self.assertEqual(len(dialog.workspace.database.constants), 2)
        dialog.remove_definition()
        self.assertEqual(len(dialog.workspace.database.constants), 1)
        dialog.header_title.setText('Custom definition')
        dialog.header_author.setText('Emil Larsson')
        dialog.header_endian.setCurrentIndex(1)
        self.assertTrue(dialog.save_xdf(path=Path(self.temp.name)/'custom.xdf'))
        self.assertTrue(dialog.workspace.database.default_lsb_first)
        self.assertEqual(dialog.workspace.database.title, 'Custom definition')

    def test_new_xdf_without_bin_can_be_saved_and_used(self):
        dialog = XDFEditorDialog(None)
        self.dialogs.append(dialog)
        self.assertFalse(dialog.form.isEnabled())
        dialog.new_definition('table')
        self.assertTrue(dialog.form.isEnabled())
        self.assertIn('without a BIN', dialog.preview.note.text())
        self.assertTrue(dialog.save_xdf(path=Path(self.temp.name)/'new.xdf'))
        dialog.use_saved()
        self.assertEqual(len(dialog.applied_database.tables), 1)

    def test_title_edit_preserves_legacy_storage_and_unedited_xml_fields(self):
        tree = ET.ElementTree(ET.fromstring(ET.tostring(self.database.root)))
        node = tree.getroot().find('XDFTABLE')
        embedded = node.find("./XDFAXIS[@id='z']/EMBEDDEDDATA")
        embedded.set('mmedelementsizebits', '24'); embedded.set('mmedrowcount', '5000')
        node.find("./XDFAXIS[@id='z']/decimalpl").text = '10'
        node.find("./XDFAXIS[@id='z']/MATH").set('equation', ' (X * 0.1) ')
        node.find("./XDFAXIS[@id='x']/MATH").set('equation', ' X * 10 ')
        node.find("./XDFAXIS[@id='x']/units").text = ' rpm '
        database = XDFDatabase.from_tree(tree, Path('Legacy.xdf'))
        dialog = XDFEditorDialog(database)
        self.dialogs.append(dialog)
        self.assertIn('24 (unsupported)', dialog.form.bits.currentText())
        self.assertEqual(dialog.form.rows.value(), 5000)
        self.assertEqual(dialog.form.decimals.value(), 10)
        dialog.form.name.setText('Name only')
        self.assertTrue(dialog.commit_current())
        changed = dialog.workspace.database.definitions[0]
        self.assertEqual(changed.z_element_bits, 24)
        self.assertEqual(changed.rows, 5000)
        self.assertEqual(changed.decimal_places, 10)
        self.assertEqual(changed.z_equation, ' (X * 0.1) ')
        self.assertEqual(changed.x_axis.equation, ' X * 10 ')
        self.assertEqual(changed.x_axis.units, ' rpm ')
        node.find('title').text = 'Name only'
        self.assertEqual(ET.tostring(node), ET.tostring(dialog.workspace.database.root.find('XDFTABLE')))

    def test_selection_change_updates_staged_list_name(self):
        dialog = self.editor()
        dialog.new_definition('constant')
        changed_id = dialog.current_id
        dialog.form.name.setText('Renamed constant')
        other = next(dialog.listing.topLevelItem(i) for i in range(dialog.listing.topLevelItemCount())
                     if dialog.listing.topLevelItem(i).data(0, Qt.UserRole) != changed_id)
        dialog.listing.setCurrentItem(other)
        renamed = next(dialog.listing.topLevelItem(i) for i in range(dialog.listing.topLevelItemCount())
                       if dialog.listing.topLevelItem(i).data(0, Qt.UserRole) == changed_id)
        self.assertEqual(renamed.text(0), 'Renamed constant')

    def test_apply_xdf_keeps_bin_bytes_dirty_state_and_undo(self):
        path = Path(self.temp.name)/'demo.bin'; path.write_bytes(self.data)
        document = CalibrationDocument(self.database, path)
        d = self.database.definitions[0]
        document.set_cells(d, {(0, 0): 777}, 'User edit')
        before = bytes(document.data)
        original = document.original
        history = document.undo_stack.copy()
        workspace = XDFWorkspace(self.database, len(self.data))
        changed = workspace.update(d.unique_id, replace(d, title='New definition name', z_equation='X * 0.1'))
        updated = workspace.save_as(Path(self.temp.name)/'changed.xdf')
        window = gui.MainWindow()
        try:
            window.xdf = self.database; window.document = document
            window.editor.set_document(document); window.editor.set_definition(d)
            window._apply_xdf_database(updated, changed.unique_id)
            self.assertIs(window.document, document)
            self.assertEqual(bytes(document.data), before)
            self.assertEqual(document.original, original)
            self.assertTrue(document.dirty)
            self.assertEqual(document.undo_stack, history)
            self.assertEqual(window.editor.definition.title, 'New definition name')
            self.assertAlmostEqual(document.map_values(changed)[0][0], 77.7)
            document.undo()
            self.assertEqual(document.map_raw(changed)[0][0], 500)
        finally:
            document.dirty = False
            window.close()

    def test_finder_reviews_candidate_and_hidden_filter_cannot_add(self):
        dialog = PotentialMapsDialog(self.data, None)
        self.dialogs.append(dialog)
        finish_scan(dialog)
        self.assertTrue(dialog.candidates)
        self.assertEqual(dialog.candidates[0].address, 82)
        self.assertIn('UNVERIFIED', dialog.candidates[0].to_definition().description)
        self.assertEqual(dialog.preview.table.item(0, 0).text(), '500')
        self.assertTrue(dialog.add_button.isEnabled())
        dialog.search.setText('no matching result')
        self.assertFalse(dialog.add_button.isEnabled())
        dialog.accept_candidates()
        self.assertEqual(dialog.selected_definitions, [])
        dialog.search.clear()
        dialog.accept_candidates()
        self.assertEqual(dialog.selected_definitions[0].z_address, 82)
        self.assertEqual(dialog.selected_definitions[0].z_equation, 'X')

    def test_finder_known_regions_and_byte_order_mismatch(self):
        dialog = PotentialMapsDialog(self.data, self.database)
        self.dialogs.append(dialog)
        finish_scan(dialog)
        self.assertFalse(dialog.candidates)
        dialog.include_known.setChecked(True)
        dialog.start_scan(); finish_scan(dialog)
        self.assertTrue(dialog.candidates[0].known)
        dialog.scan_endian = '<'
        dialog.accept_candidates()
        self.assertEqual(dialog.selected_definitions, [])
        self.assertIn('byte order differs', dialog.status.text())

    def test_family_filter_selection_preview_and_interpretation_export(self):
        data = sized_family_blob("Driver's wish") + b'\xff' * 64 + sized_family_blob('Boost target')
        dialog = PotentialMapsDialog(data, None)
        self.dialogs.append(dialog)
        finish_scan(dialog)
        self.assertTrue(dialog.candidates[0].suggestions)
        dialog.family_filter.setCurrentText("Driver's wish")
        candidate = dialog.candidates[dialog.results.currentItem().data(0, Qt.UserRole)]
        self.assertEqual(candidate.suggestions[0].family, "Driver's wish")
        self.assertEqual(dialog.use_interpretation.isChecked(), bool(candidate.suggestions[0].scaling_profile_count))
        dialog.use_interpretation.setChecked(False)
        self.assertFalse(dialog.use_interpretation.isChecked())
        raw = dialog.preview.definition
        self.assertEqual(raw.z_equation, 'X')
        self.assertEqual(raw.x_axis.units, '')
        dialog.use_interpretation.setChecked(True)
        interpreted = dialog.preview.definition
        self.assertEqual(interpreted.z_units, 'Nm')
        self.assertEqual(interpreted.x_axis.equation, 'X * 100 / 8192')
        dialog.family_filter.setCurrentText('Boost target')
        self.assertFalse(dialog.results.currentItem().isHidden())
        self.assertIn('Boost target', dialog.preview.definition.title)
        self.assertIn('Boost target', dialog.interpretation.currentText())
        dialog.search.setText('no matching result')
        self.assertIsNone(dialog.results.currentItem())
        self.assertIsNone(dialog.preview.definition)
        self.assertEqual(dialog.evidence.toPlainText(), '')
        self.assertFalse(dialog.interpretation.isEnabled())
        dialog.search.clear()
        self.assertIn('Boost target', dialog.preview.definition.title)
        report_path = Path(self.temp.name)/'families.json'
        self.assertTrue(dialog.save_findings(path=report_path))
        report = json.loads(report_path.read_text(encoding='utf-8'))
        self.assertEqual(report['sha256'], hashlib.sha256(data).hexdigest())
        self.assertIn("Driver's wish", report['family_counts'])
        self.assertFalse(report['partial_scan'])
        # Explicit opt-in is required when no local scaling library is installed.
        dialog.use_interpretation.setChecked(True)
        dialog.accept_candidates()
        self.assertEqual(len(dialog.selected_definitions), 1)
        self.assertIn('UNVERIFIED semantic', dialog.selected_definitions[0].description)
        self.assertIn(report['sha256'], dialog.selected_definitions[0].description)

    def test_unverified_maps_avoid_address_aliases_and_raw_axis_log_binding(self):
        data = sized_family_blob('Boost target')
        candidate = next(c for c in analyze_maps(data) if c.suggestions)
        raw = candidate_definition(candidate)
        aliased_address = replace(raw, z_address=0xF7F44)
        self.assertNotIn('N75 A', gui.short_map_name(aliased_address))
        self.assertEqual(gui.MapEditor._candidate_family(aliased_address), ('', [], None))
        profile = profile_for_definition(raw)
        self.assertIsNone(profile.x_signal)
        self.assertIsNone(profile.y_signal)
        self.assertIn('physical axis bindings', ' '.join(profile.notes))
        interpreted = candidate_definition(candidate, interpreted=True)
        profile = profile_for_definition(interpreted)
        self.assertIsNone(profile.x_signal)  # load remains ambiguous, even after opting into scaling
        self.assertEqual(profile.y_signal, SIG_RPM)
        window = gui.MainWindow()
        try:
            window.editor.definition = aliased_address
            self.assertFalse(window.editor._is_n75_definition())
        finally:
            window.close()

    def test_raw_soi_preview_and_added_definition_preserve_negative_integers(self):
        data = sized_family_blob('Main SOI')
        dialog = PotentialMapsDialog(data, None)
        self.dialogs.append(dialog)
        finish_scan(dialog)
        dialog.family_filter.setCurrentText('Main SOI')
        candidate = dialog.candidates[dialog.results.currentItem().data(0, Qt.UserRole)]
        self.assertEqual(dialog.use_interpretation.isChecked(), bool(candidate.suggestions[0].scaling_profile_count))
        dialog.use_interpretation.setChecked(False)
        self.assertFalse(dialog.use_interpretation.isChecked())
        self.assertTrue(dialog.preview.definition.z_signed)
        self.assertEqual(dialog.preview.definition.z_equation, 'X')
        self.assertIn('16-bit signed', dialog.preview.note.text())
        self.assertEqual(float(dialog.preview.table.item(0, 0).text()), -85)
        dialog.use_interpretation.setChecked(True)
        self.assertAlmostEqual(float(dialog.preview.table.item(0, 0).text()), -85*3/128, places=2)
        dialog.use_interpretation.setChecked(False)
        self.assertEqual(float(dialog.preview.table.item(0, 0).text()), -85)
        dialog.accept_candidates()
        self.assertTrue(dialog.selected_definitions[0].z_signed)
        self.assertEqual(dialog.selected_definitions[0].z_equation, 'X')

    @requires_resource('definitions/EDC16C31 361830.xdf')
    def test_other_bin_size_or_software_gets_fresh_bundled_definition_workspace(self):
        for size in (0x100000, 0x200000):
            with self.subTest(size=size):
                data = bytearray(b'\xff' * size)
                identity = b'BOSCH EDC16CP35/C 1037387658 0281013500'
                data[128:128+len(identity)] = identity
                data = bytes(data)
                path = Path(self.temp.name)/f'other-{size}.bin'; path.write_bytes(data)
                window = gui.MainWindow()
                try:
                    self.assertIsNone(window.xdf)
                    window.load_bundled_definition()
                    self.assertEqual(window.xdf.path.resolve(), gui.bundled_definition_path().resolve())
                    with patch.object(gui.QFileDialog, 'getOpenFileName', return_value=(str(path), '')):
                        window.open_bin()
                    self.assertEqual(bytes(window.document.data), data)
                    self.assertEqual(window.document.original, data)
                    self.assertFalse(window.document.dirty)
                    self.assertEqual(window.xdf.tables, [])
                    self.assertIs(window.document.xdf, window.xdf)
                    self.assertIsNone(window.current_xdf_path)
                    self.assertIsNone(window.editor.definition)
                    self.assertIn(hashlib.sha256(data).hexdigest(), window.xdf.root.find('./XDFHEADER/description').text)
                    self.assertFalse(path.with_suffix('.xdf').exists())
                    self.assertEqual(path.read_bytes(), data)
                finally:
                    window.close()

    def test_explicit_custom_definition_is_kept_for_other_bin(self):
        data = bytearray(b'\xff' * 0x200000)
        identity = b'BOSCH EDC16CP35/C 1037387658'
        data[128:128+len(identity)] = identity
        path = Path(self.temp.name)/'custom.bin'; path.write_bytes(data)
        workspace = XDFWorkspace(self.database, len(data))
        custom = workspace.save_as(Path(self.temp.name)/'custom.xdf')
        window = gui.MainWindow()
        try:
            window.xdf = custom
            window.populate_tree()
            with patch.object(gui.QFileDialog, 'getOpenFileName', return_value=(str(path), '')):
                window.open_bin()
            self.assertIs(window.xdf, custom)
            self.assertIs(window.document.xdf, custom)
            self.assertEqual(window.editor.definition.z_address, 82)
            self.assertEqual(bytes(window.document.data), bytes(data))
        finally:
            window.close()

    def test_finder_cancel_close_does_not_destroy_running_thread(self):
        dialog = PotentialMapsDialog(self.data * 10000, None)
        self.dialogs.append(dialog)
        dialog.start_scan()
        dialog.reject()
        finish_scan(dialog)
        self.assertTrue(dialog._closing)
        self.assertIsNone(dialog.worker)

    def test_finder_immediate_close_skips_deferred_scan(self):
        dialog = PotentialMapsDialog(self.data, None)
        self.dialogs.append(dialog)
        dialog.reject()
        APP.processEvents()
        self.assertTrue(dialog._closing)
        self.assertIsNone(dialog.worker)
        self.assertEqual(dialog.candidates, [])

    def test_finder_cancel_status_and_worker_guard_survive_completion(self):
        dialog = PotentialMapsDialog(self.data * 10000, None)
        self.dialogs.append(dialog)
        dialog.start_scan()
        worker = dialog.worker
        dialog.start_scan()
        self.assertIs(dialog.worker, worker)
        dialog.cancel_scan()
        finish_scan(dialog)
        self.assertIn('Scan cancelled', dialog.status.text())
        self.assertFalse(dialog._closing)
        self.assertTrue(dialog.scan_button.isEnabled())


if __name__ == '__main__':
    unittest.main(verbosity=2)
