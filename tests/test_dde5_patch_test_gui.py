"""Offscreen patch-check UI tests. No test opens a physical COM port."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'

import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication, QMessageBox
from bmw_bin_editor.dde5_patch_test_gui import Dde5PatchTestDialog
from bmw_bin_editor import BUILD_CHANNEL

APP = QApplication.instance() or QApplication([])
BACKEND = 'bmw_bin_editor.dde5_patch_test_gui.run_patch_check'
QUESTION = 'bmw_bin_editor.dde5_patch_test_gui.QMessageBox.question'


class PatchCheckGuiTests(unittest.TestCase):
    def setUp(self):
        self.files = tempfile.TemporaryDirectory()
        self.dialog = Dde5PatchTestDialog()

    def tearDown(self):
        if self.dialog.worker is not None:
            self.dialog.worker.request_cancel()
            self.dialog.worker.wait(5000)
        APP.processEvents()
        self.dialog.close()
        APP.processEvents()
        self.files.cleanup()

    @staticmethod
    def result(**fields):
        report = {
            'status': 'passed', 'error': None,
            'steps': ['Identity and patch words checked.'],
            'frames': [{'request': 'retained request', 'response': 'retained response'}],
            'details': {},
            'restore': {'baud': 'not_needed', 'write': 'not_needed',
                        'power_cycle_required': False, 'restoration_required': False,
                        'session': 'unchanged'},
        }
        report.update(fields)
        return report

    def finish(self):
        deadline = time.monotonic() + 5
        while self.dialog._busy and time.monotonic() < deadline:
            APP.processEvents()
            time.sleep(.005)
        APP.processEvents()
        self.assertFalse(self.dialog._busy)
        self.assertIsNone(self.dialog.worker)

    def reservation(self, address='0x3F9FD4'):
        # Synthetic test evidence only; never use as an ECU reservation.
        self.dialog.address.setText(address)
        self.dialog.reference.setText('Mock firmware reservation for UI tests')
        self.dialog.reservation_confirmed.setChecked(True)
        self.dialog.write_ack.setChecked(True)

    def test_opening_does_not_connect_and_address_has_no_default(self):
        with patch(BACKEND) as backend:
            self.dialog.show()
            APP.processEvents()
            for i in range(self.dialog.tabs.count()):
                self.dialog.tabs.setCurrentIndex(i)
            APP.processEvents()
        backend.assert_not_called()
        self.assertEqual(self.dialog.address.text(), '')
        self.assertFalse(self.dialog.reservation_confirmed.isChecked())
        self.assertFalse(self.dialog.read_button.isEnabled())
        self.assertFalse(self.dialog.baud_button.isEnabled())
        self.assertFalse(self.dialog.write_button.isEnabled())
        self.assertEqual('local test build' in self.dialog.windowTitle(), BUILD_CHANNEL == 'patch-test')

    def test_explicit_read_checks_do_not_pass_a_write_reservation(self):
        self.dialog.port.setText('com7')
        self.reservation()
        with patch(BACKEND, return_value=self.result()) as backend:
            self.dialog.read_button.click()
            self.finish()
        args, kwargs = backend.call_args
        self.assertEqual(args, ('COM7',))
        self.assertEqual(kwargs['mode'], 'read')
        self.assertIsNone(kwargs['reservation'])
        self.assertTrue(kwargs['echo'])
        self.assertTrue(callable(kwargs['cancelled']))
        self.assertTrue(callable(kwargs['progress']))
        self.assertIn('Check passed', self.dialog.status.text())
        self.assertTrue(self.dialog.save_button.isEnabled())

    def test_invalid_port_blocks_even_programmatic_start(self):
        self.dialog.port.setText('/dev/ttyUSB0')
        with patch(BACKEND) as backend:
            self.dialog.start_check('read')
        backend.assert_not_called()
        self.assertIsNone(self.dialog.worker)
        self.assertIn('Windows COM port', self.dialog.status.text())

    def test_baud_requires_acknowledgement_and_defaults_to_115200(self):
        self.dialog.port.setText('COM3')
        self.assertEqual(self.dialog.rate.currentData(), 115200)
        self.assertFalse(self.dialog.baud_button.isEnabled())
        with patch(BACKEND, return_value=self.result()) as backend:
            self.dialog.start_check('baud')
            backend.assert_not_called()
            self.dialog.baud_ack.setChecked(True)
            self.assertTrue(self.dialog.baud_button.isEnabled())
            self.dialog.baud_button.click()
            self.finish()
        self.assertEqual(backend.call_args.kwargs['mode'], 'baud')
        self.assertEqual(backend.call_args.kwargs['rate'], 115200)
        self.assertIsNone(backend.call_args.kwargs['reservation'])
        self.assertFalse(self.dialog.baud_ack.isChecked())

    def test_selected_baud_and_echo_are_passed_to_worker(self):
        self.dialog.port.setText('COM3')
        self.dialog.rate.setCurrentIndex(1)
        self.dialog.echo.setChecked(False)
        self.dialog.baud_ack.setChecked(True)
        with patch(BACKEND, return_value=self.result()) as backend:
            self.dialog.start_check('baud')
            self.finish()
        self.assertEqual(backend.call_args.kwargs['rate'], 125000)
        self.assertFalse(backend.call_args.kwargs['echo'])

    def test_write_requires_valid_exact_reservation(self):
        self.dialog.port.setText('COM3')
        with patch(BACKEND) as backend, patch(QUESTION) as question:
            self.dialog.start_check('write')
            self.assertFalse(self.dialog.write_button.isEnabled())
            for address in ('0x3F9FD5', '0x3F9FD0', '0x3FA178', '0x3FA176'):
                self.reservation(address)
                self.assertFalse(self.dialog.write_button.isEnabled(), address)
                self.dialog.start_check('write')
            self.reservation()
            self.dialog.reference.setText('short')
            self.dialog.reservation_confirmed.setChecked(True)
            self.assertFalse(self.dialog.write_button.isEnabled())
            self.dialog.start_check('write')
        backend.assert_not_called()
        question.assert_not_called()

    def test_write_needs_runtime_acknowledgement(self):
        self.dialog.port.setText('COM3')
        self.reservation()
        self.dialog.write_ack.setChecked(False)
        with patch(BACKEND) as backend, patch(QUESTION) as question:
            self.dialog.start_check('write')
        backend.assert_not_called()
        question.assert_not_called()
        self.assertFalse(self.dialog.write_button.isEnabled())

    def test_write_prompt_names_exact_four_bytes_and_can_be_declined(self):
        self.dialog.port.setText('COM3')
        self.reservation('0x3FA174')
        self.assertTrue(self.dialog.write_button.isEnabled())
        with patch(BACKEND) as backend, patch(QUESTION, return_value=QMessageBox.No) as question:
            self.dialog.start_check('write')
        backend.assert_not_called()
        prompt = question.call_args.args[2]
        self.assertIn('exactly 4 bytes at 0x3FA174–0x3FA177 on COM3', prompt)
        self.assertIn('Mock firmware reservation', prompt)
        self.assertEqual(question.call_args.args[-1], QMessageBox.No)

    def test_accepted_write_passes_verified_reservation_and_resets_confirmation(self):
        self.dialog.port.setText('COM3')
        self.reservation()
        with patch(BACKEND, return_value=self.result()) as backend, \
             patch(QUESTION, return_value=QMessageBox.Yes):
            self.dialog.start_check('write')
            self.finish()
        kwargs = backend.call_args.kwargs
        self.assertEqual(kwargs['mode'], 'write')
        self.assertEqual(kwargs['reservation']['address'], 0x3F9FD4)
        self.assertEqual(kwargs['reservation']['bytes'], 4)
        self.assertEqual(kwargs['reservation']['ownership'], 'user_verified_not_software_verified')
        self.assertTrue(kwargs['reservation']['confirmed'])
        self.assertFalse(self.dialog.reservation_confirmed.isChecked())
        self.assertFalse(self.dialog.write_button.isEnabled())

    def test_reservation_confirmation_resets_after_each_relevant_input_change(self):
        self.dialog.port.setText('COM3')
        for control, value in ((self.dialog.address, '0x3FA174'),
                               (self.dialog.reference, 'Changed reservation reference'),
                               (self.dialog.port, 'COM4')):
            self.reservation()
            self.assertTrue(self.dialog.reservation_confirmed.isChecked())
            control.setText(value)
            self.assertFalse(self.dialog.reservation_confirmed.isChecked())
            self.assertFalse(self.dialog.write_button.isEnabled())

    def test_worker_blocks_parallel_actions_and_close_requests_cancel_without_waiting(self):
        entered, release = threading.Event(), threading.Event()
        def backend(*args, **kwargs):
            kwargs['progress']('Reading ECU identity…')
            entered.set()
            release.wait(2)
            return self.result(status='cancelled')
        self.dialog.port.setText('COM3')
        self.dialog.show()
        with patch(BACKEND, side_effect=backend) as call:
            try:
                self.dialog.start_check('read')
                self.assertTrue(entered.wait(2))
                APP.processEvents()
                self.assertIn('Reading ECU identity', self.dialog.output.toPlainText())
                for control in (self.dialog.port, self.dialog.echo, self.dialog.rate,
                                self.dialog.address, self.dialog.reference,
                                self.dialog.read_button, self.dialog.baud_button,
                                self.dialog.write_button, self.dialog.save_button):
                    self.assertFalse(control.isEnabled())
                self.dialog.start_check('read')
                started = time.monotonic()
                self.dialog.accept()
                self.dialog.reject()
                self.dialog.close()
                self.assertLess(time.monotonic() - started, .5)
                self.assertTrue(self.dialog.worker.cancel_event.is_set())
                self.assertTrue(self.dialog.isVisible())
                self.assertTrue(self.dialog._busy)
                self.assertFalse(self.dialog.cancel_button.isEnabled())
                self.assertEqual(call.call_count, 1)
            finally:
                release.set()
                self.finish()
        self.assertIn('Check cancelled', self.dialog.status.text())
        self.assertTrue(self.dialog.read_button.isEnabled())

    def test_failure_and_uncertain_restoration_are_visible_and_exported(self):
        failure = self.result(
            status='failed', error='Verification failed.',
            details={'write_test': {'address_hex': '0x3F9FD4', 'original_hex': '01020304'}},
            restore={'baud': 'unknown', 'write': 'unknown', 'restoration_required': True,
                     'power_cycle_required': True, 'session': '86_left_active'})
        self.dialog.port.setText('COM3')
        with patch(BACKEND, return_value=failure):
            self.dialog.start_check('read')
            self.finish()
        summary = self.dialog.status.text()
        self.assertIn('RAM restoration is unconfirmed', summary)
        self.assertIn('baud restoration is unconfirmed', summary)
        self.assertIn('power cycle', summary)
        self.assertIn('session 86 remains active', summary)
        path = Path(self.files.name) / 'failure.json'
        with patch('bmw_bin_editor.dde5_patch_test_gui.QFileDialog.getSaveFileName',
                   return_value=(str(path), 'JSON (*.json)')):
            self.dialog.save_report()
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')), failure)

    def test_unexpected_error_keeps_exportable_report_and_uncertainty(self):
        self.dialog.port.setText('COM3')
        with patch(BACKEND, side_effect=RuntimeError('diagnostic details for report')):
            self.dialog.start_check('read')
            self.finish()
        self.assertEqual(self.dialog.report['status'], 'failed')
        self.assertIn('RuntimeError', self.dialog.status.text())
        self.assertIn('cleanup and any restoration are unconfirmed', self.dialog.status.text())
        self.assertNotIn('diagnostic details for report', self.dialog.status.text())
        self.assertTrue(self.dialog.save_button.isEnabled())

    def test_unconfirmed_session_is_visible_after_an_ambiguous_entry(self):
        self.dialog.port.setText('COM3')
        failure = self.result(status='failed', error='Acknowledgement was not received.',
                              restore={'session': 'unknown'})
        with patch(BACKEND, return_value=failure):
            self.dialog.start_check('read')
            self.finish()
        self.assertIn('diagnostic session is unconfirmed', self.dialog.status.text())
        self.assertTrue(self.dialog.save_button.isEnabled())

    def test_com_port_cleanup_failure_is_visible(self):
        self.dialog.port.setText('COM3')
        failure = self.result(status='failed', error='Could not close the serial port.',
                              details={'port_closed': False})
        with patch(BACKEND, return_value=failure):
            self.dialog.start_check('read')
            self.finish()
        self.assertIn('COM port cleanup is unconfirmed', self.dialog.status.text())
        self.assertEqual(self.dialog.report, failure)
        self.assertTrue(self.dialog.save_button.isEnabled())


class PatchCheckMainWindowTests(unittest.TestCase):
    def test_live_logger_or_stopping_thread_blocks_dialog(self):
        from bmw_bin_editor.gui import MainWindow
        for logger, thread in ((object(), None), (None, object())):
            panel = SimpleNamespace(direct_logger=logger, live_thread=thread)
            window = SimpleNamespace(editor=SimpleNamespace(log_panel=panel))
            with patch('bmw_bin_editor.gui.Dde5PatchTestDialog') as dialog, \
                 patch('bmw_bin_editor.gui.QMessageBox.information') as info:
                MainWindow.open_dde5_patch_checks(window)
            dialog.assert_not_called()
            info.assert_called_once()

    def test_explicit_menu_handler_opens_modal_dialog_when_logger_is_idle(self):
        from bmw_bin_editor.gui import MainWindow
        panel = SimpleNamespace(direct_logger=None, live_thread=None)
        window = SimpleNamespace(editor=SimpleNamespace(log_panel=panel))
        with patch('bmw_bin_editor.gui.Dde5PatchTestDialog') as dialog:
            MainWindow.open_dde5_patch_checks(window)
        dialog.assert_called_once_with(window)
        dialog.return_value.exec.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
