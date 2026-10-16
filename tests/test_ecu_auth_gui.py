"""Offscreen authentication UI tests. All ECU calls are replaced by mocks."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication, QDialog
from bmw_bin_editor.ecu_programming_gui import EcuProgrammingDialog

APP = QApplication.instance() or QApplication([])
AUTH_CALL = 'bmw_bin_editor.ecu_programming_gui.authenticate_ecu'


class AuthenticationGuiTests(unittest.TestCase):
    def setUp(self):
        self.files = tempfile.TemporaryDirectory()
        self.baseline = bytes(0x100000)
        edited = bytearray(self.baseline)
        edited[0x90000] = 1
        self.dialog = EcuProgrammingDialog(self.baseline, bytes(edited), 'preserved.FLS')
        self.dialog.api_dir.setText(self.files.name)

    def tearDown(self):
        if self.dialog.worker is not None:
            if hasattr(self.dialog.worker, 'request_cancel'):
                self.dialog.worker.request_cancel()
            self.dialog.worker.wait(5000)
        APP.processEvents()
        self.dialog.close()
        APP.processEvents()
        self.files.cleanup()

    def finish(self):
        deadline = time.monotonic()+5
        while self.dialog._busy and time.monotonic() < deadline:
            APP.processEvents()
            time.sleep(.005)
        APP.processEvents()
        self.assertFalse(self.dialog._busy)
        self.assertIsNone(self.dialog.worker)

    @staticmethod
    def result(**fields):
        result = {'authenticated': True, 'cancelled': False, 'erase_sent': False,
                  'write_sent': False, 'stages': [{'stage': 'authenticate',
                                                 'status': 'passed',
                                                 'message': 'Authentication accepted.'}],
                  'identity': {'hardware': '0281010565', 'software': '361830'}}
        result.update(fields)
        return result

    def test_opening_and_offline_review_never_start_authentication(self):
        with patch(AUTH_CALL) as authenticate, \
             patch('bmw_bin_editor.ecu_programming_gui.probe_ecu') as probe:
            self.dialog.show()
            APP.processEvents()
            self.dialog.review_changes()
            companion = Path(self.files.name)/'preserved.MPC'
            companion.write_bytes(bytes(0x70200))
            self.dialog.companion.setText(str(companion))
            with patch('bmw_bin_editor.ecu_programming_gui.review_loaded_image',
                       return_value={'flash_ready': False}), \
                 patch('bmw_bin_editor.ecu_programming_gui.correct_loaded_image',
                       return_value=(self.dialog.edited, [])):
                self.dialog.check_image()
                self.dialog.correct_checksums()
        authenticate.assert_not_called()
        probe.assert_not_called()
        self.assertFalse(self.dialog.write_button.isEnabled())
        self.assertTrue(self.dialog.auth_button.isEnabled())
        self.assertFalse(self.dialog.cancel_auth_button.isEnabled())

    def test_prerequisites_are_checked_before_a_worker_or_ecu_call(self):
        with patch(AUTH_CALL) as authenticate:
            self.dialog.api_dir.setText(str(Path(self.files.name)/'missing'))
            self.dialog.test_authentication()
            self.assertIn('EDIABAS Bin folder', self.dialog.output.toPlainText())
            self.dialog.api_dir.setText(self.files.name)
            self.dialog.original = b'partial FLS'
            self.dialog.test_authentication()
            self.assertIn('complete 1 MiB', self.dialog.output.toPlainText())
            self.dialog.original = None
            self.dialog.test_authentication()
            self.assertIn('preserved MPC or FLS', self.dialog.output.toPlainText())
        authenticate.assert_not_called()
        self.assertIsNone(self.dialog.worker)
        self.assertFalse(self.dialog._busy)
        self.assertIsNone(self.dialog.report)

    def test_mpc_requires_a_readable_complete_fls_companion(self):
        self.dialog.space.setCurrentIndex(1)
        self.dialog.original = self.dialog.edited = bytes(0x70200)
        with patch(AUTH_CALL) as authenticate:
            self.dialog.test_authentication()
            self.assertIn('companion FLS', self.dialog.output.toPlainText())
            self.dialog.companion.setText(str(Path(self.files.name)/'missing.FLS'))
            self.dialog.test_authentication()
            self.assertIn('could not be read', self.dialog.output.toPlainText())
            companion = Path(self.files.name)/'truncated.FLS'
            companion.write_bytes(bytes(0x100))
            self.dialog.companion.setText(str(companion))
            self.dialog.test_authentication()
            self.assertIn('complete 1 MiB', self.dialog.output.toPlainText())
        authenticate.assert_not_called()
        self.assertIsNone(self.dialog.worker)

    def test_explicit_fls_test_uses_original_baseline_and_clears_correction(self):
        self.dialog.corrected_image = b'not the authentication evidence'
        with patch(AUTH_CALL, return_value=self.result()) as authenticate:
            self.dialog.auth_button.click()
            self.finish()
        args, kwargs = authenticate.call_args
        self.assertEqual(args, (self.files.name, self.baseline))
        self.assertTrue(callable(kwargs['cancelled']))
        self.assertTrue(callable(kwargs['progress']))
        self.assertIsNone(self.dialog.corrected_image)
        self.assertTrue(self.dialog.report['authenticated'])
        self.assertFalse(self.dialog.report['flash_ready'])
        self.assertFalse(self.dialog.report['programming_enabled'])
        self.assertIn('succeeded during this test', self.dialog.auth_status.text())
        self.assertFalse(self.dialog.write_button.isEnabled())

    def test_mpc_companion_bytes_are_frozen_before_background_work(self):
        self.dialog.space.setCurrentIndex(1)
        self.dialog.original = self.dialog.edited = bytes(0x70200)
        companion = Path(self.files.name)/'preserved.FLS'
        companion.write_bytes(self.baseline)
        self.dialog.companion.setText(str(companion))
        entered, release = threading.Event(), threading.Event()
        received = []
        def authenticate(bin_dir, trusted_fls, **kwargs):
            received.append(trusted_fls)
            entered.set()
            release.wait(2)
            return self.result()
        with patch(AUTH_CALL, side_effect=authenticate):
            try:
                self.dialog.test_authentication()
                self.assertTrue(entered.wait(2))
                companion.write_bytes(b'replaced after the snapshot')
            finally:
                release.set()
                self.finish()
        self.assertEqual(received, [self.baseline])

    def test_busy_worker_blocks_shared_controls_other_actions_and_close(self):
        entered, release = threading.Event(), threading.Event()
        def authenticate(*args, **kwargs):
            entered.set()
            release.wait(2)
            return self.result()
        with patch(AUTH_CALL, side_effect=authenticate) as authentication, \
             patch('bmw_bin_editor.ecu_programming_gui.probe_ecu') as probe:
            try:
                self.dialog.show()
                self.dialog.test_authentication()
                self.assertTrue(entered.wait(2))
                for name in ('read_button', 'review_button', 'save_button', 'check_button',
                             'correct_button', 'apply_button', 'auth_button', 'api_dir',
                             'space', 'companion', 'companion_browse', 'write_button'):
                    self.assertFalse(getattr(self.dialog, name).isEnabled(), name)
                self.assertTrue(self.dialog.cancel_auth_button.isEnabled())
                self.dialog.read_identity()
                self.dialog.test_authentication()
                self.dialog.review_changes()
                self.dialog.correct_checksums()
                self.dialog.accept()
                self.dialog.reject()
                self.dialog.close()
                APP.processEvents()
                self.assertTrue(self.dialog.isVisible())
                self.assertEqual(self.dialog.result(), QDialog.Rejected)
                probe.assert_not_called()
                self.assertEqual(authentication.call_count, 1)
            finally:
                release.set()
                self.finish()
        self.assertTrue(self.dialog.read_button.isEnabled())
        self.assertFalse(self.dialog.cancel_auth_button.isEnabled())
        self.assertFalse(self.dialog.write_button.isEnabled())

    def test_cancel_is_cooperative_and_keeps_controls_blocked_until_finished(self):
        entered, release = threading.Event(), threading.Event()
        def authenticate(*args, cancelled, progress):
            progress('Requesting the ECU challenge.')
            entered.set()
            release.wait(2)
            return self.result(authenticated=False, cancelled=cancelled())
        with patch(AUTH_CALL, side_effect=authenticate):
            try:
                self.dialog.test_authentication()
                self.assertTrue(entered.wait(2))
                APP.processEvents()
                self.assertIn('Requesting the ECU challenge', self.dialog.output.toPlainText())
                self.dialog.cancel_auth_button.click()
                self.assertTrue(self.dialog.worker.cancel_event.is_set())
                self.assertFalse(self.dialog.cancel_auth_button.isEnabled())
                self.assertFalse(self.dialog.read_button.isEnabled())
                self.assertIn('Cancellation requested', self.dialog.auth_status.text())
            finally:
                release.set()
                self.finish()
        self.assertTrue(self.dialog.report['cancelled'])
        self.assertIn('test cancelled', self.dialog.auth_status.text())
        self.assertFalse(self.dialog.report['flash_ready'])

    def test_backend_failure_report_is_exportable_and_does_not_enable_write(self):
        failure = self.result(authenticated=False, error='The ECU identity does not match 361830.',
                              error_code='identity_mismatch')
        with patch(AUTH_CALL, return_value=failure):
            self.dialog.test_authentication()
            self.finish()
        self.assertEqual(self.dialog.report['error_code'], 'identity_mismatch')
        self.assertIn('does not match', self.dialog.output.toPlainText())
        self.assertTrue(self.dialog.save_button.isEnabled())
        self.assertFalse(self.dialog.write_button.isEnabled())
        path = Path(self.files.name)/'authentication.json'
        with patch('bmw_bin_editor.ecu_programming_gui.QFileDialog.getSaveFileName',
                   return_value=(str(path), 'JSON (*.json)')):
            self.dialog.save_report()
        saved = json.loads(path.read_text(encoding='utf-8'))
        self.assertFalse(saved['authenticated'])
        self.assertFalse(saved['erase_sent'])
        self.assertFalse(saved['write_sent'])
        self.assertFalse(saved['programming_enabled'])

    def test_unexpected_exception_arguments_are_not_displayed(self):
        secret = 'RAW_RESPONSE_PRIVATE_EXPONENT_SECRET'
        with patch(AUTH_CALL, side_effect=RuntimeError(secret)):
            self.dialog.test_authentication()
            self.finish()
        self.assertNotIn(secret, self.dialog.output.toPlainText())
        self.assertIn('Unexpected authentication error (RuntimeError)', self.dialog.output.toPlainText())
        self.assertIsNone(self.dialog.report)
        self.assertFalse(self.dialog.save_button.isEnabled())
        self.assertFalse(self.dialog.write_button.isEnabled())

    def test_accepted_authentication_plus_cleanup_error_preserves_both_facts(self):
        result = self.result(error='EDIABAS handle cleanup failed.', error_code='api_cleanup',
                             volatile_authentication_may_remain=True)
        with patch(AUTH_CALL, return_value=result):
            self.dialog.test_authentication()
            self.finish()
        self.assertTrue(self.dialog.report['authenticated'])
        self.assertEqual(self.dialog.report['error_code'], 'api_cleanup')
        self.assertIn('authentication was accepted', self.dialog.auth_status.text())
        self.assertIn('test reported an error', self.dialog.auth_status.text())
        self.assertIn('EDIABAS handle cleanup failed', self.dialog.auth_status.text())
        self.assertIn('may retain volatile authentication', self.dialog.auth_status.text())
        self.assertEqual(self.dialog.summary.text(), self.dialog.auth_status.text())
        self.assertFalse(self.dialog.report['flash_ready'])
        self.assertFalse(self.dialog.report['programming_enabled'])
        self.assertFalse(self.dialog.write_button.isEnabled())
        self.assertTrue(self.dialog.save_button.isEnabled())

    def test_authentication_success_is_never_write_readiness_and_selection_invalidates_it(self):
        with patch(AUTH_CALL, return_value=self.result(flash_ready=True, programming_enabled=True,
                                                     volatile_authentication_may_remain=True)):
            self.dialog.test_authentication()
            self.finish()
        self.assertFalse(self.dialog.report['flash_ready'])
        self.assertFalse(self.dialog.report['programming_enabled'])
        self.assertFalse(self.dialog.write_button.isEnabled())
        self.assertIn('may retain volatile authentication', self.dialog.auth_status.text())
        self.dialog.companion.setText('new selection')
        self.assertIsNone(self.dialog.report)
        self.assertIn('has not been tested for this image selection', self.dialog.auth_status.text())
        self.assertFalse(self.dialog.save_button.isEnabled())


if __name__ == '__main__':
    unittest.main()
