import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from bmw_bin_editor.core import safe_math
from bmw_bin_editor.logdata import profile_for_definition, SIG_RPM
from bmw_bin_editor.map_classification import FamilySuggestion
from bmw_bin_editor.xdf_gui import PotentialMapsDialog
from test_map_classification import family_blob

APP = QApplication.instance() or QApplication([])


def table_fixture():
    data, candidate = family_blob('Main SOI')
    suggestion = FamilySuggestion('Main SOI', 93, ['Signed timing surface and independent library evidence'],
        'mg/cycle', 'rpm', 'deg', 'X * 0.01', 'X', 'X * 3 / 128', True,
        candidate.rows, candidate.cols, candidate.x_address, candidate.y_address)
    suggestion.scaling_sources = ['Reference SW A', 'Reference SW B']
    suggestion.scaling_profile_count = 2
    suggestion.scaling_label = 'Library-derived hypothesis'
    suggestion.source_scales = ['A: X * 3 / 128 deg', 'B: X * 3 / 128 deg']
    alternate = replace(suggestion, score=78, z_equation='X * 0.01',
        reasons=['Alternative remains plausible without library support'], scaling_sources=[], scaling_profile_count=0,
        scaling_label='', source_scales=[])
    candidate.suggestions = [suggestion, alternate]
    return data, candidate


class ScalingGuiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dialogs = []

    def tearDown(self):
        for dialog in self.dialogs:
            dialog.reject()
        APP.processEvents()
        self.temp.cleanup()

    def dialog(self, data, candidates):
        with patch.object(PotentialMapsDialog, 'start_scan', lambda self: None):
            dialog = PotentialMapsDialog(data, None)
            APP.processEvents()
        self.dialogs.append(dialog)
        dialog.scan_endian = '>'
        dialog.show_results(candidates)
        return dialog

    def test_learned_scale_visible_by_default_with_signed_preview_and_sources(self):
        data, candidate = table_fixture()
        dialog = self.dialog(data, [candidate])
        self.assertTrue(dialog.use_interpretation.isChecked())
        self.assertEqual(dialog.preview.definition.z_equation, 'X * 3 / 128')
        self.assertAlmostEqual(float(dialog.preview.table.item(0, 0).text()), -85*3/128, places=2)
        self.assertIn('X * 3 / 128', dialog.scaling_summary.text())
        self.assertIn('signed 16-bit', dialog.scaling_summary.text())
        self.assertIn('mg/cycle', dialog.scaling_summary.text())
        self.assertIn('2 reference software profiles', dialog.scaling_summary.text())
        self.assertIn('Reference SW A', dialog.evidence.toPlainText())
        self.assertIn('X * 0.01', dialog.interpretation.itemText(1))
        self.assertIn('X * 3 / 128', dialog.results.currentItem().text(4))
        self.assertEqual(data, table_fixture()[0])

    def test_raw_toggle_retains_negative_integers_and_added_xdf_choice(self):
        data, candidate = table_fixture()
        dialog = self.dialog(data, [candidate])
        dialog.use_interpretation.setChecked(False)
        self.assertEqual(dialog.preview.definition.z_equation, 'X')
        self.assertTrue(dialog.preview.definition.z_signed)
        self.assertEqual(float(dialog.preview.table.item(0, 0).text()), -85)
        self.assertIn('Raw preview', dialog.scaling_summary.text())
        dialog.accept_candidates()
        self.assertEqual(dialog.selected_definitions[0].z_equation, 'X')
        self.assertTrue(dialog.selected_definitions[0].z_signed)

    def test_family_filter_displays_and_adds_the_matching_scaling(self):
        data, candidate = table_fixture()
        candidate.suggestions.insert(0, replace(candidate.suggestions[0], family='Boost target',
            score=97, z_units='hPa', z_equation='X', z_signed=False))
        dialog = self.dialog(data, [candidate])
        self.assertEqual(dialog.preview.definition.z_units, 'hPa')
        dialog.family_filter.setCurrentText('Main SOI')
        self.assertIn('Main SOI', dialog.results.currentItem().text(3))
        self.assertIn('X * 3 / 128', dialog.results.currentItem().text(4))
        self.assertEqual(dialog.interpretation.currentIndex(), 1)
        self.assertEqual(dialog.preview.definition.z_units, 'deg')
        self.assertTrue(dialog.preview.definition.z_signed)
        dialog.accept_candidates()
        self.assertEqual(dialog.selected_definitions[0].z_equation, 'X * 3 / 128')
        self.assertEqual(dialog.selected_definitions[0].z_units, 'deg')

    def test_scale_choice_is_remembered_per_table_and_bulk_add_uses_each_choice(self):
        data, first = table_fixture()
        shift = len(data) + 32
        other = replace(first, address=first.address+shift, x_address=first.x_address+shift,
            y_address=first.y_address+shift, suggestions=[replace(s, x_address=s.x_address+shift, y_address=s.y_address+shift)
                                                         for s in first.suggestions])
        dialog = self.dialog(data+b'\xff'*32+data, [first, other])
        first_item, second_item = dialog.results.topLevelItem(0), dialog.results.topLevelItem(1)
        dialog.interpretation.setCurrentIndex(1)
        self.assertAlmostEqual(float(dialog.preview.table.item(0, 0).text()), -.85, places=2)
        dialog.results.setCurrentItem(second_item)
        self.assertEqual(dialog.preview.definition.z_equation, 'X * 3 / 128')
        dialog.use_interpretation.setChecked(False)
        dialog.results.setCurrentItem(first_item)
        self.assertEqual(dialog.interpretation.currentIndex(), 1)
        self.assertTrue(dialog.use_interpretation.isChecked())
        first_item.setSelected(True); second_item.setSelected(True)
        dialog.accept_candidates()
        self.assertEqual([d.z_equation for d in dialog.selected_definitions], ['X * 0.01', 'X'])

    def test_fallback_stays_raw_and_findings_include_scaling_provenance(self):
        data, candidate = table_fixture()
        candidate.suggestions = [candidate.suggestions[1]]
        dialog = self.dialog(data, [candidate])
        self.assertFalse(dialog.use_interpretation.isChecked())
        self.assertIn('no compatible library evidence', dialog.scaling_summary.text())
        dialog.reject()
        data, candidate = table_fixture()
        dialog = self.dialog(data, [candidate])
        target = Path(self.temp.name)/'findings.json'
        self.assertTrue(dialog.save_findings(path=target))
        suggestion = json.loads(target.read_text(encoding='utf-8'))['candidates'][0]['suggestions'][0]
        self.assertEqual(suggestion['scaling_profile_count'], 2)
        self.assertEqual(suggestion['scaling_sources'], ['Reference SW A', 'Reference SW B'])
        self.assertEqual(suggestion['z_equation'], 'X * 3 / 128')

    def test_transferred_quantity_units_do_not_implicitly_bind_log_positions(self):
        data, candidate = table_fixture()
        definition = candidate.suggestions[0].to_definition(candidate)
        profile = profile_for_definition(definition)
        self.assertIsNone(profile.x_signal)
        self.assertEqual(profile.y_signal, SIG_RPM)
        self.assertIn('configure physical axis bindings explicitly', ' '.join(profile.notes))


if __name__ == '__main__':
    unittest.main(verbosity=2)
