import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
import sys
from pathlib import Path
import json
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication, QDialog
from bmw_bin_editor.ecu_programming_gui import EcuProgrammingDialog

APP = QApplication.instance() or QApplication([])


class ProgrammingGuiTests(unittest.TestCase):
    def setUp(self):
        self.files = tempfile.TemporaryDirectory()
        self.companion = Path(self.files.name)/'companion.MPC'
        self.companion.write_bytes(bytes(0x70200))
        self.dialog = EcuProgrammingDialog(bytes(1048576), bytes(1048576), 'test.FLS')

    def tearDown(self):
        if self.dialog.worker:
            self.dialog.worker.wait(5000)
            APP.processEvents()
        self.dialog.close()
        self.files.cleanup()

    def validation_report(self):
        return {
            'changed_bytes': 1, 'flash_ready': False,
            'current': {
                'all_additive_valid': True,
                'program_signature': {'valid': True},
                'calibration_signature': {'valid': False},
            },
            'sector_plan': {'address_space': 'MPC', 'selected_sector_count': 2,
                            'planned_sector_payload_bytes': 65536,
                            'native_program_erase_total_bytes': 851968,
                            'native_partial_supported': False},
        }

    def select_companion(self):
        self.dialog.companion.setText(str(self.companion))

    def stage_correction(self):
        self.select_companion()
        corrected = bytearray(self.dialog.edited)
        corrected[0xfeff8] = 1
        with patch('bmw_bin_editor.ecu_programming_gui.correct_loaded_image',
                   return_value=(bytes(corrected), [{'file_offset': '0xfeff8'}])) as repair, \
             patch('bmw_bin_editor.ecu_programming_gui.review_loaded_image',
                   return_value=self.validation_report()) as check:
            self.dialog.correct_checksums()
        repair.assert_called_once_with(self.dialog.edited, 'FLS', self.companion.read_bytes())
        check.assert_called_once_with(self.dialog.original, bytes(corrected), 'FLS', self.companion.read_bytes())
        return bytes(corrected)

    def finish(self):
        deadline = time.monotonic()+5
        while not self.dialog.read_button.isEnabled() and time.monotonic() < deadline:
            APP.processEvents()
            time.sleep(.01)
        self.assertTrue(self.dialog.read_button.isEnabled())

    def test_review_does_not_enable_writing_or_initialize_ediabas(self):
        with patch('bmw_bin_editor.ecu_programming_gui.probe_ecu') as probe:
            self.dialog.review_changes()
        probe.assert_not_called()
        self.assertFalse(self.dialog.write_button.isEnabled())
        self.assertFalse(self.dialog.report['flash_ready'])
        self.assertTrue(self.dialog.save_button.isEnabled())
        self.dialog.space.setCurrentIndex(1)
        self.assertIsNone(self.dialog.report)
        self.assertFalse(self.dialog.save_button.isEnabled())

    def test_probe_success_is_independent_of_write_readiness(self):
        with patch('bmw_bin_editor.ecu_programming_gui.probe_ecu', return_value={'programming_enabled': False}) as probe:
            self.dialog.read_identity()
            self.assertFalse(self.dialog.space.isEnabled())
            self.assertFalse(self.dialog.companion.isEnabled())
            self.assertFalse(self.dialog.check_button.isEnabled())
            self.assertFalse(self.dialog.correct_button.isEnabled())
            self.assertFalse(self.dialog.apply_button.isEnabled())
            self.finish()
            probe.assert_called_once()
        self.assertTrue(self.dialog.save_button.isEnabled())
        self.assertFalse(self.dialog.write_button.isEnabled())

    def test_failed_probe_discards_stale_success(self):
        self.dialog.review_changes()
        with patch('bmw_bin_editor.ecu_programming_gui.probe_ecu', side_effect=RuntimeError('Cable disconnected')):
            self.dialog.read_identity()
            self.finish()
        self.assertIsNone(self.dialog.report)
        self.assertFalse(self.dialog.save_button.isEnabled())
        self.assertIn('Cable disconnected', self.dialog.output.toPlainText())

    def test_offline_fls_check_allows_optional_companion_and_never_probes_ecu(self):
        self.assertTrue(self.dialog.check_button.isEnabled())
        self.assertTrue(self.dialog.correct_button.isEnabled())
        self.select_companion()
        self.assertTrue(self.dialog.check_button.isEnabled())
        with patch('bmw_bin_editor.ecu_programming_gui.probe_ecu') as probe, \
             patch('bmw_bin_editor.ecu_programming_gui.review_loaded_image',
                   return_value=self.validation_report()) as check:
            self.dialog.check_image()
        probe.assert_not_called()
        check.assert_called_once_with(self.dialog.original, self.dialog.edited, 'FLS', self.companion.read_bytes())
        self.assertFalse(self.dialog.write_button.isEnabled())
        self.assertFalse(self.dialog.report['flash_ready'])
        self.assertIn('arithmetic checksums: pass', self.dialog.summary.text())
        self.assertIn('Calibration signature: invalid', self.dialog.summary.text())
        self.assertIn('65,536 bytes', self.dialog.summary.text())
        self.assertIn('selective erase is unavailable', self.dialog.summary.text())

    def test_correction_stages_bytes_and_apply_returns_them_without_mutating_snapshot(self):
        before_original, before_edited = self.dialog.original, self.dialog.edited
        with patch('bmw_bin_editor.ecu_programming_gui.probe_ecu') as probe:
            corrected = self.stage_correction()
            self.dialog.apply_correction()
        probe.assert_not_called()
        self.assertEqual(self.dialog.result(), QDialog.Accepted)
        self.assertEqual(self.dialog.corrected_image, corrected)
        self.assertEqual(self.dialog.original, before_original)
        self.assertEqual(self.dialog.edited, before_edited)
        self.assertFalse(self.dialog.report['staged_additive_correction']['applied_to_editor'])
        self.assertFalse(self.dialog.write_button.isEnabled())

    def test_changing_companion_or_address_space_discards_staged_correction(self):
        self.stage_correction()
        self.dialog.companion.setText(str(self.companion.with_name('another.MPC')))
        self.assertIsNone(self.dialog.corrected_image)
        self.assertIsNone(self.dialog.report)
        self.assertFalse(self.dialog.apply_button.isEnabled())
        self.stage_correction()
        self.dialog.space.setCurrentIndex(1)
        self.assertIsNone(self.dialog.corrected_image)
        self.assertFalse(self.dialog.save_button.isEnabled())
        self.assertEqual(self.dialog.companion_label.text(), 'Companion FLS image (required)')

    def test_repair_or_validation_failure_does_not_leave_an_applicable_copy(self):
        self.select_companion()
        before = self.dialog.edited
        with patch('bmw_bin_editor.ecu_programming_gui.correct_loaded_image',
                   return_value=(before, [])), \
             patch('bmw_bin_editor.ecu_programming_gui.review_loaded_image',
                   side_effect=ValueError('Layout does not match')):
            self.dialog.correct_checksums()
        self.assertEqual(self.dialog.edited, before)
        self.assertIsNone(self.dialog.corrected_image)
        self.assertIsNone(self.dialog.report)
        self.assertFalse(self.dialog.apply_button.isEnabled())
        self.assertIn('Layout does not match', self.dialog.output.toPlainText())
        with patch('bmw_bin_editor.ecu_programming_gui.correct_loaded_image',
                   return_value=(b'truncated', [])):
            self.dialog.correct_checksums()
        self.assertIn('image size', self.dialog.output.toPlainText())
        self.assertIsNone(self.dialog.corrected_image)

    def test_cancel_discards_staged_copy_and_an_already_correct_image_is_a_noop(self):
        self.stage_correction()
        self.dialog.reject()
        self.assertIsNone(self.dialog.corrected_image)
        self.assertEqual(self.dialog.result(), QDialog.Rejected)
        self.select_companion()
        with patch('bmw_bin_editor.ecu_programming_gui.correct_loaded_image',
                   return_value=(self.dialog.edited, [])), \
             patch('bmw_bin_editor.ecu_programming_gui.review_loaded_image',
                   return_value=self.validation_report()):
            self.dialog.correct_checksums()
        self.assertIsNone(self.dialog.corrected_image)
        self.assertFalse(self.dialog.apply_button.isEnabled())
        self.assertIn('need no correction', self.dialog.summary.text())

    def test_saved_report_keeps_signature_failure_and_sector_plan(self):
        self.stage_correction()
        path = Path(self.files.name)/'review.json'
        with patch('bmw_bin_editor.ecu_programming_gui.QFileDialog.getSaveFileName',
                   return_value=(str(path), 'JSON (*.json)')):
            self.dialog.save_report()
        report = json.loads(path.read_text(encoding='utf-8'))
        self.assertFalse(report['current']['calibration_signature']['valid'])
        self.assertEqual(report['sector_plan']['planned_sector_payload_bytes'], 65536)
        self.assertFalse(report['flash_ready'])
        self.assertFalse(report['staged_additive_correction']['applied_to_editor'])

    def test_summary_separates_loaded_and_companion_checksum_status(self):
        report = self.validation_report()
        report['address_space'] = 'MPC'
        report['current']['selected_space_additive_valid'] = True
        report['current']['all_additive_valid'] = False
        self.dialog.show_report(report)
        self.assertIn('MPC arithmetic checksums: pass', self.dialog.summary.text())
        self.assertIn('companion image still has a failing', self.dialog.summary.text())
        self.assertFalse(self.dialog.write_button.isEnabled())


if __name__ == '__main__':
    unittest.main()
