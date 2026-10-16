
import unittest as _optional_unittest
from pathlib import Path as _OptionalPath
if not (_OptionalPath(__file__).resolve().parents[1] / 'definitions/ms43x/Siemens_MS43_MS43X001_512K.xdf').is_file():
    raise _optional_unittest.SkipTest('Optional local calibration resource not installed: definitions/ms43x/Siemens_MS43_MS43X001_512K.xdf')

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bmw_bin_editor.core import XDFDatabase, CalibrationDocument, EditorError
from bmw_bin_editor.ms43 import database_for_size, export_plan, export_binary, CAL_START, CAL_SIZE, FULL_SIZE
from bmw_bin_editor.adx import load_adx_catalog, bundled_ms43_adx_path

DEFS = Path(__file__).resolve().parents[1] / 'definitions/ms43x'


class MS43Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.partial = XDFDatabase(DEFS / 'Siemens_MS43_MS43X001_64K.xdf')
        cls.full = XDFDatabase(DEFS / 'Siemens_MS43_MS43X001_512K.xdf')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.source = self.folder / 'source.bin'
        self.source.write_bytes(bytes(FULL_SIZE))
        self.doc = CalibrationDocument(self.full, self.source)

    def tearDown(self):
        self.temp.cleanup()

    def test_full_and_partial_definitions_agree_and_round_trip(self):
        full = database_for_size(self.partial, FULL_SIZE)
        partial = database_for_size(full, CAL_SIZE)
        self.assertEqual(len(full.definitions), 3772)
        for x, y, z in zip(self.partial.definitions, full.definitions, self.full.definitions):
            self.assertEqual(x.z_address + CAL_START, y.z_address)
            self.assertEqual(y.z_address, z.z_address)
        self.assertEqual([d.z_address for d in self.partial.definitions],
                         [d.z_address for d in partial.definitions])
        self.assertEqual(full.profile_id, self.partial.profile_id)

    def test_linked_ve_axes_use_real_storage_and_equations(self):
        ve = next(d for d in self.full.definitions if d.title == 'ip_map_ve_1__map__n')
        self.assertEqual(ve.z_address, 0x7EE20)
        self.assertEqual(ve.x_axis.address, 0x77E84)
        self.assertEqual(ve.x_axis.element_bits, 16)
        self.assertEqual(ve.y_axis.address, 0x77E62)
        self.assertEqual(ve.y_axis.equation, '0.0082921489*X')
        missing = next(d for d in self.full.definitions if d.title == 'ip_ectpwm_i__tco_dif')
        self.assertTrue(missing.discovery_unverified)
        self.assertIsNone(missing.y_axis.address)
        self.assertIn('unresolved', missing.y_axis.units)

    def test_partial_export_covers_first_and_last_calibration_byte(self):
        self.doc.apply_bytes('edges', [(CAL_START, b'\x11'), (FULL_SIZE - 1, b'\x22')])
        plan = export_plan(self.doc)
        self.assertTrue(plan.partial)
        self.assertEqual(plan.changed_bytes, 2)
        out = self.folder / 'calibration.bin'
        before_path, saved = self.doc.path, self.doc.saved
        export_binary(self.doc, out)
        self.assertEqual(len(out.read_bytes()), CAL_SIZE)
        self.assertEqual(out.read_bytes(), bytes(self.doc.data[CAL_START:]))
        self.assertEqual(self.doc.path, before_path)
        self.assertEqual(self.doc.saved, saved)
        self.assertTrue(self.doc.dirty)
        self.assertEqual(self.source.read_bytes(), bytes(FULL_SIZE))

    def test_any_code_change_forces_full_even_after_save(self):
        self.doc.apply_bytes('code and calibration', [(CAL_START - 1, b'\x11'), (CAL_START, b'\x22')])
        self.doc.save_as(self.folder / 'working_full.bin')
        plan = export_plan(self.doc)
        self.assertFalse(plan.partial)
        self.assertEqual(plan.code_changed_bytes, 1)
        self.assertEqual(plan.size, FULL_SIZE)
        out = self.folder / 'flash_full.bin'
        export_binary(self.doc, out)
        self.assertEqual(out.read_bytes(), bytes(self.doc.data))

    def test_undo_removes_code_change_from_export_decision(self):
        self.doc.apply_bytes('calibration', [(CAL_START, b'\x11')])
        self.doc.apply_bytes('code', [(0, b'\x22')])
        self.assertFalse(export_plan(self.doc).partial)
        self.doc.undo()
        self.assertTrue(export_plan(self.doc).partial)
        self.doc.redo()
        self.assertFalse(export_plan(self.doc).partial)

    def test_no_changes_or_other_ecu_retains_full_image(self):
        self.assertFalse(export_plan(self.doc).partial)
        self.doc.apply_bytes('calibration', [(CAL_START, b'\x11')])
        self.doc.xdf = SimpleNamespace(title='EDC16C31 361830')
        self.assertFalse(export_plan(self.doc).partial)

    def test_partial_input_and_explicit_full_output(self):
        self.source.write_bytes(bytes(CAL_SIZE))
        partial = CalibrationDocument(self.partial, self.source)
        partial.apply_bytes('calibration', [(0x756, b'\x12')])
        out = self.folder / 'partial.bin'
        self.assertTrue(export_binary(partial, out).partial)
        self.assertEqual(out.read_bytes(), bytes(partial.data))
        self.doc.apply_bytes('calibration', [(CAL_START, b'\x11')])
        self.assertEqual(export_binary(self.doc, self.folder / 'full.bin', full=True).size, FULL_SIZE)

    def test_export_rejects_source_overwrite_and_failed_write_preserves_state(self):
        self.doc.apply_bytes('calibration', [(CAL_START, b'\x11')])
        with self.assertRaises(EditorError):
            export_binary(self.doc, self.doc.path)
        out = self.folder / 'out.bin'
        out.write_bytes(b'previous')
        with patch('bmw_bin_editor.ms43.os.replace', side_effect=PermissionError('locked')):
            with self.assertRaises(PermissionError):
                export_binary(self.doc, out)
        self.assertEqual(out.read_bytes(), b'previous')
        self.assertTrue(self.doc.dirty)
        self.assertFalse(list(self.folder.glob('*.tmp')))

    def test_displacement_cc_and_ve_percent_round_trip_without_changing_encoding(self):
        displacement = next(d for d in self.full.definitions if d.title == 'c_eng_disp')
        ve = next(d for d in self.full.definitions if d.title == 'ip_map_ve_1__map__n')
        self.assertEqual(displacement.z_units, 'cc')
        self.assertEqual(ve.z_units, '%')
        self.doc.set_cells(displacement, {(0,0): 3000}, 'displacement')
        self.assertEqual(self.doc.data[displacement.z_address:displacement.z_address+2], b'\x00\x0c')
        self.assertAlmostEqual(self.doc.map_values(displacement)[0][0], 3000, places=3)
        self.doc.set_cells(ve, {(0,0):100}, 'VE')
        self.assertEqual(self.doc.data[ve.z_address:ve.z_address+2], b'\x00\x04')
        self.assertAlmostEqual(self.doc.map_values(ve)[0][0], 100, places=3)
        reparsed = XDFDatabase.from_tree(self.full.tree, self.full.path)
        again = next(d for d in reparsed.definitions if d.title == 'c_eng_disp')
        self.assertEqual(again.z_equation, displacement.z_equation)

    def test_ve_labels_follow_selector_edits_instead_of_hardcoded_ranges(self):
        from bmw_bin_editor.ms43_navigation import ve_selection_labels
        selector = next(d for d in self.full.definitions if d.title == 'ip_nr_ip_ve__vo')
        points = [3,9,15,24,36,48,60,67.875]
        for i, angle in enumerate(points):
            self.doc.set_axis_value(selector.y_axis, i, angle, 'overlap')
        self.doc.set_cells(selector, {(i,0):i+1 for i in range(8)}, 'selector')
        labels = ve_selection_labels(self.doc)
        self.assertEqual(labels[1], '3° overlap point')
        self.assertEqual(labels[8], '67.875° overlap point')
        self.doc.set_cells(selector, {(i,0):8 for i in range(8)}, 'single VE table')
        labels = ve_selection_labels(self.doc)
        self.assertEqual(labels[1], 'no exact selector point')
        self.assertIn('3°, 9°, 15°', labels[8])

    def test_adx_metadata_is_imported_without_executing_formulas(self):
        catalog, styles = load_adx_catalog(bundled_ms43_adx_path())
        self.assertEqual(len(catalog.definitions), 96)
        self.assertEqual(catalog.label_for('N'), 'Engine Speed')
        self.assertEqual(catalog.unit_for('N'), 'rpm')
        self.assertEqual(styles['N']['upper'], 8160.)


if __name__ == '__main__':
    unittest.main()
