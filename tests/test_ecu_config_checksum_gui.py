"""Standalone calibration checking in the UI; no ECU or vendor code is run."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication, QDialog
from bmw_bin_editor.ecu_programming_gui import EcuProgrammingDialog

APP = QApplication.instance() or QApplication([])
SOURCE = Path(__file__).resolve().parents[1]/'local_resources'/'dde5'/'read kessv3.FLS'
AUTH_CALL = 'bmw_bin_editor.ecu_programming_gui.authenticate_ecu'
PROBE_CALL = 'bmw_bin_editor.ecu_programming_gui.probe_ecu'


class ConfigSelectionTests(unittest.TestCase):
    def test_known_slice_sizes_select_config_and_never_request_authentication(self):
        for size in (0x20000, 0x1F000):
            with self.subTest(size=size):
                dialog = EcuProgrammingDialog(bytes(size), bytes(size), 'config.bin')
                try:
                    self.assertEqual(dialog.space.currentIndex(), 2)
                    self.assertEqual(dialog.address_space(), 'CONFIG')
                    self.assertTrue(dialog.check_button.isEnabled())
                    self.assertTrue(dialog.correct_button.isEnabled())
                    self.assertFalse(dialog.companion.isEnabled())
                    self.assertFalse(dialog.companion_browse.isEnabled())
                    self.assertFalse(dialog.auth_button.isEnabled())
                    self.assertFalse(dialog.write_button.isEnabled())
                    # Programmatic invocation must still refuse partial-image auth.
                    dialog.api_dir.setText(str(Path(__file__).parent))
                    with patch(AUTH_CALL) as authenticate:
                        dialog.test_authentication()
                    authenticate.assert_not_called()
                    self.assertIsNone(dialog.worker)
                    self.assertIn('complete 1 MiB', dialog.output.toPlainText())
                finally:
                    dialog.close()

    def test_mpc_keeps_its_required_companion_and_fls_makes_it_optional(self):
        dialog = EcuProgrammingDialog(bytes(0x70200), bytes(0x70200), 'original.MPC')
        try:
            self.assertEqual(dialog.space.currentIndex(), 1)
            self.assertFalse(dialog.check_button.isEnabled())
            self.assertFalse(dialog.correct_button.isEnabled())
            with self.assertRaisesRegex(ValueError, 'companion FLS'):
                dialog._read_companion()
            self.assertIn('required', dialog.companion_label.text())
            dialog.space.setCurrentIndex(0)
            self.assertIn('optional', dialog.companion_label.text())
            self.assertIsNone(dialog._read_companion())
        finally:
            dialog.close()

    def test_partial_size_alone_cannot_make_an_unknown_layout_correctable(self):
        dialog = EcuProgrammingDialog(bytes(0x20000), bytes(0x20000), 'unknown.bin')
        try:
            with patch(AUTH_CALL) as authenticate, patch(PROBE_CALL) as probe:
                dialog.correct_checksums()
            authenticate.assert_not_called()
            probe.assert_not_called()
            self.assertIsNone(dialog.report)
            self.assertIsNone(dialog.corrected_image)
            self.assertFalse(dialog.apply_button.isEnabled())
            self.assertIn('Calibration header or byte order differs', dialog.output.toPlainText())
            self.assertEqual(dialog.edited, bytes(0x20000))
        finally:
            dialog.close()


@unittest.skipUnless(SOURCE.is_file(), 'Preserved 361830 FLS fixture is unavailable')
class StandaloneConfigGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fls = SOURCE.read_bytes()
        assert hashlib.sha256(cls.fls).hexdigest() == '8862fb53f9f102d2ca2717338a527546a4da03659b2d8f871856d485cf696e4f'

    def setUp(self):
        self.files = tempfile.TemporaryDirectory()
        self.dialog = None

    def tearDown(self):
        if self.dialog is not None:
            self.dialog.close()
        APP.processEvents()
        self.files.cleanup()

    def open_slice(self, size=0x20000):
        baseline = self.fls[0xe0000:0xe0000+size]
        edited = bytearray(baseline)
        edited[0x300] ^= 1
        self.dialog = EcuProgrammingDialog(baseline, bytes(edited), 'config.bin')
        return baseline, bytes(edited)

    def test_raw_slices_check_and_correct_without_mpc_and_preserve_all_other_bytes(self):
        for size in (0x20000, 0x1F000):
            with self.subTest(size=size):
                if self.dialog is not None:
                    self.dialog.close()
                baseline, edited = self.open_slice(size)
                with patch(AUTH_CALL) as authenticate, patch(PROBE_CALL) as probe:
                    self.dialog.check_image()
                    self.assertIsNotNone(self.dialog.report, self.dialog.output.toPlainText())
                    self.assertFalse(self.dialog.report['current']['selected_space_additive_valid'])
                    self.dialog.correct_checksums()
                authenticate.assert_not_called()
                probe.assert_not_called()
                self.assertIsNotNone(self.dialog.corrected_image, self.dialog.output.toPlainText())
                corrected = self.dialog.corrected_image
                corrections = self.dialog.report['staged_additive_correction']['changes']
                self.assertEqual([row['file_offset'] for row in corrections], [0x1eff8])
                self.assertTrue(self.dialog.report['current']['selected_space_additive_valid'])
                for offset, (before, after) in enumerate(zip(edited, corrected)):
                    self.assertTrue(before == after or 0x1eff8 <= offset < 0x1effc)
                self.assertEqual(self.dialog.original, baseline)
                self.assertEqual(self.dialog.edited, edited)
                self.assertFalse(self.dialog.report['flash_ready'])
                self.assertFalse(self.dialog.write_button.isEnabled())
                self.assertIn('Config / calibration flash arithmetic checksums: pass', self.dialog.summary.text())
                self.assertIn('0x8E0180', self.dialog.summary.text())
                self.assertIn('only', self.dialog.summary.text())
                self.assertIn('does not contain complete external-flash sector bytes', self.dialog.summary.text())
                self.assertIn('Program signature: not checked', self.dialog.summary.text())
                self.assertIn('Calibration signature: not checked', self.dialog.summary.text())
                self.assertNotIn('Calibration signature: invalid', self.dialog.summary.text())

    def test_full_fls_checks_and_repairs_without_an_mpc_image(self):
        edited = bytearray(self.fls)
        edited[0xee940] ^= 1
        self.dialog = EcuProgrammingDialog(self.fls, bytes(edited), 'read.FLS')
        self.assertEqual(self.dialog.address_space(), 'FLS')
        self.assertIn('optional', self.dialog.companion_label.text())
        with patch(AUTH_CALL) as authenticate, patch(PROBE_CALL) as probe:
            self.dialog.check_image()
            self.assertIsNotNone(self.dialog.report, self.dialog.output.toPlainText())
            self.assertEqual(len(self.dialog.report['current']['additive_checks']), 4)
            self.dialog.correct_checksums()
        authenticate.assert_not_called()
        probe.assert_not_called()
        self.assertIsNotNone(self.dialog.corrected_image, self.dialog.output.toPlainText())
        self.assertTrue(self.dialog.report['current']['selected_space_additive_valid'])
        self.assertIn('Program signature: not checked', self.dialog.summary.text())
        self.assertIn('Calibration signature: invalid', self.dialog.summary.text())
        corrections = self.dialog.report['staged_additive_correction']['changes']
        self.assertEqual([row['file_offset'] for row in corrections], [0xfeff8])
        self.assertEqual(self.dialog.report['sector_plan']['planned_sector_payload_bytes'], 0x40000)

    def test_config_scope_on_full_fls_does_not_repair_a_program_checksum(self):
        edited = bytearray(self.fls)
        edited[0x38e03] ^= 1
        edited[0xee940] ^= 1
        self.dialog = EcuProgrammingDialog(self.fls, bytes(edited), 'read.FLS')
        self.dialog.space.setCurrentIndex(2)
        # Config scope must not attempt to open an unrelated optional path.
        self.dialog.companion.setText(str(Path(self.files.name)/'missing.MPC'))
        self.dialog.correct_checksums()
        self.assertIsNotNone(self.dialog.corrected_image, self.dialog.output.toPlainText())
        self.assertEqual(len(self.dialog.report['current']['additive_checks']), 1)
        corrections = self.dialog.report['staged_additive_correction']['changes']
        self.assertEqual([row['file_offset'] for row in corrections], [0xfeff8])
        self.assertEqual(self.dialog.corrected_image[0x38e03], edited[0x38e03])
        self.assertTrue(self.dialog.auth_button.isEnabled())
        self.assertIn('Config / calibration flash arithmetic checksums: pass', self.dialog.summary.text())
        self.assertIn('Program signature: not checked', self.dialog.summary.text())

    def test_raw_report_is_exportable_and_apply_returns_staged_copy(self):
        baseline, edited = self.open_slice(0x1F000)
        self.dialog.correct_checksums()
        self.assertIsNotNone(self.dialog.corrected_image, self.dialog.output.toPlainText())
        corrected = self.dialog.corrected_image
        path = Path(self.files.name)/'config_review.json'
        with patch('bmw_bin_editor.ecu_programming_gui.QFileDialog.getSaveFileName',
                   return_value=(str(path), 'JSON (*.json)')):
            self.dialog.save_report()
        saved = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(saved['address_space'], 'CONFIG')
        self.assertFalse(saved['flash_ready'])
        self.assertFalse(saved['staged_additive_correction']['applied_to_editor'])
        self.assertIsNone(saved['current']['calibration_signature']['valid'])
        self.dialog.apply_correction()
        self.assertEqual(self.dialog.result(), QDialog.Accepted)
        self.assertEqual(self.dialog.corrected_image, corrected)
        self.assertEqual(self.dialog.original, baseline)
        self.assertEqual(self.dialog.edited, edited)

    def test_cancel_and_scope_change_discard_staged_config_correction(self):
        self.open_slice()
        self.dialog.correct_checksums()
        self.assertIsNotNone(self.dialog.corrected_image)
        self.dialog.reject()
        self.assertIsNone(self.dialog.corrected_image)
        self.assertEqual(self.dialog.result(), QDialog.Rejected)
        self.dialog.correct_checksums()
        self.assertIsNotNone(self.dialog.corrected_image)
        self.dialog.space.setCurrentIndex(0)
        self.assertIsNone(self.dialog.corrected_image)
        self.assertIsNone(self.dialog.report)
        self.assertFalse(self.dialog.apply_button.isEnabled())

    def test_raw_current_change_review_uses_the_calibration_runtime_base(self):
        self.open_slice()
        with patch(AUTH_CALL) as authenticate, patch(PROBE_CALL) as probe:
            self.dialog.review_changes()
        authenticate.assert_not_called()
        probe.assert_not_called()
        self.assertEqual(self.dialog.report['address_space'], 'CONFIG')
        self.assertEqual(self.dialog.report['changed_ranges'][0]['runtime_address'], 0x8e0300)
        self.assertFalse(self.dialog.report['flash_ready'])


if __name__ == '__main__':
    unittest.main()
