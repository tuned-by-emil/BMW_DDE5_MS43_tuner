"""Offscreen RAM bench UI tests; all ECU operations use mocks."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'

from copy import deepcopy
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
from bmw_bin_editor.core import CalibrationDefinition
from bmw_bin_editor.dde5_ram_calibration_gui import Dde5RamCalibrationDialog
from bmw_bin_editor import BUILD_CHANNEL

APP = QApplication.instance() or QApplication([])
MODULE = 'bmw_bin_editor.dde5_ram_calibration_gui.'
BACKEND = MODULE + 'run_ram_bench'


class RamCalibrationGuiTests(unittest.TestCase):
    def setUp(self):
        self.files = tempfile.TemporaryDirectory()
        self.original = bytearray(range(128))
        self.edited = bytearray(self.original)
        self.edited[0x20] ^= 1
        self.edited[0x70] ^= 1  # Outside the whole planned block.
        self.definition = CalibrationDefinition('Mock table', '', 'Test', 0x20, 1, 2,
                                                 profile_id='361830')
        self.plan = {
            'format': 'mock-ram-plan',
            'table': {'title': 'Mock table', 'file_offset': 0x20,
                      'logical_address': 0x8E0020, 'bytes': 4},
            'blocks': [{'index': 9, 'logical_start': 0x8E0018,
                        'logical_end_exclusive': 0x8E0038, 'bytes': 32}],
            'directory_bytes': 1116, 'required_bytes': 1148, 'capacity_bytes': 32768,
            'baseline_sha256': 'mock-baseline',
            'baseline_blocks': [{'index': 9, 'hex': bytes(self.original[0x18:0x38]).hex()}],
            'probe_address': 0x8E0020, 'probe_bytes': 2,
        }
        def build(source, definition, *, capacity_bytes):
            result = deepcopy(self.plan)
            result['capacity_bytes'] = capacity_bytes
            result['table']['title'] = definition.title
            return result
        self.build_patch = patch(MODULE + 'build_ram_plan', side_effect=build)
        self.plan_builder = self.build_patch.start()
        self.dialog = Dde5RamCalibrationDialog(self.original, self.definition,
                                               edited=self.edited, image_name='baseline.FLS')

    def tearDown(self):
        if self.dialog.worker is not None:
            self.dialog.worker.request_cancel()
            self.dialog.worker.wait(5000)
        APP.processEvents()
        self.dialog.close()
        APP.processEvents()
        self.build_patch.stop()
        self.files.cleanup()

    @staticmethod
    def result(**fields):
        result = {'status': 'passed', 'error': None,
                  'steps': ['Copy/page checks completed.'],
                  'frames': [{'request': 'mock frame', 'response': 'mock response'}],
                  'details': {'port_closed': True},
                  'restore': {'reference_page': 'verified', 'write': 'not_needed',
                              'session': '86_left_active', 'power_cycle_required': False,
                              'restoration_required': False}}
        result.update(fields)
        return result

    def ready(self, change=False):
        self.dialog.port.setText('COM3')
        self.dialog.hardware_reference.setText('Mock fitted SRAM evidence for UI tests')
        self.dialog.hardware_confirmed.setChecked(True)
        self.dialog.engine_ack.setChecked(True)
        self.dialog.exclusive_ack.setChecked(True)
        self.dialog.probe_change.setChecked(change)

    def finish(self):
        deadline = time.monotonic() + 5
        while self.dialog._busy and time.monotonic() < deadline:
            APP.processEvents()
            time.sleep(.005)
        APP.processEvents()
        self.assertFalse(self.dialog._busy)
        self.assertIsNone(self.dialog.worker)

    def test_opening_only_builds_offline_plan_and_never_connects(self):
        with patch(BACKEND) as backend:
            self.dialog.show()
            APP.processEvents()
        backend.assert_not_called()
        self.assertEqual(self.plan_builder.call_args.args[0], bytes(self.original))
        self.assertEqual(self.dialog.local_diff_count, 1)
        self.assertIn('1 bytes; excluded', self.dialog.plan_summary.text())
        self.assertIn('1,116 directory bytes', self.dialog.plan_summary.text())
        self.assertIn('0x8E0018–0x8E0037', self.dialog.plan_summary.text())
        self.assertFalse(self.dialog.run_button.isEnabled())
        self.assertFalse(self.dialog.probe_change.isChecked())
        self.assertEqual('local test build' in self.dialog.windowTitle(), BUILD_CHANNEL == 'patch-test')

    def test_snapshots_and_definition_are_frozen_without_changing_editor(self):
        original = bytes(self.original)
        edited = bytes(self.edited)
        self.original[0x20] ^= 2
        self.edited[0x20] ^= 4
        self.definition.title = 'Changed after opening'
        self.dialog.capacity.setCurrentIndex(1)
        self.assertEqual(self.dialog.original, original)
        self.assertEqual(self.dialog.edited, edited)
        self.assertEqual(self.dialog.definition.title, 'Mock table')
        self.assertEqual(self.plan_builder.call_args.args[0], original)
        self.assertEqual(self.plan_builder.call_args.kwargs['capacity_bytes'], 65536)
        self.assertEqual(self.dialog.local_diff_count, 1)
        self.assertEqual(self.original[0x20], original[0x20] ^ 2)

    def test_invalid_offline_plan_blocks_even_programmatic_run(self):
        self.plan_builder.side_effect = ValueError('No complete managed block covers this table.')
        self.dialog._build_plan()
        self.ready()
        with patch(BACKEND) as backend, patch(MODULE + 'QMessageBox.question') as question:
            self.dialog.start_bench()
        backend.assert_not_called()
        question.assert_not_called()
        self.assertFalse(self.dialog.run_button.isEnabled())
        self.assertIn('No complete managed block', self.dialog.status.text())

    def test_excess_ram_budget_is_not_an_enabled_plan(self):
        self.plan['required_bytes'] = 65537
        self.dialog._build_plan()
        self.ready()
        self.assertIsNone(self.dialog.plan)
        self.assertFalse(self.dialog.run_button.isEnabled())
        self.assertIn('exceeds', self.dialog.plan_summary.text())

    def test_invalid_port_hardware_evidence_and_runtime_ack_each_block_run(self):
        with patch(BACKEND) as backend, patch(MODULE + 'QMessageBox.question') as question:
            self.ready()
            self.dialog.port.setText('/dev/tty0')
            self.dialog.start_bench()
            self.ready()
            self.dialog.hardware_reference.setText('short')
            self.dialog.hardware_confirmed.setChecked(True)
            self.dialog.start_bench()
            self.ready()
            self.dialog.hardware_confirmed.setChecked(False)
            self.dialog.start_bench()
            self.ready()
            self.dialog.engine_ack.setChecked(False)
            self.dialog.start_bench()
            self.ready()
            self.dialog.exclusive_ack.setChecked(False)
            self.dialog.start_bench()
        backend.assert_not_called()
        question.assert_not_called()
        self.assertIsNone(self.dialog.worker)

    def test_declining_exact_block_confirmation_never_starts_backend(self):
        self.ready()
        with patch(BACKEND) as backend, \
             patch(MODULE + 'QMessageBox.question', return_value=QMessageBox.No) as question:
            self.dialog.start_bench()
        backend.assert_not_called()
        text = question.call_args.args[2]
        self.assertIn('COM3 at 9600 baud', text)
        self.assertIn('0x8E0018–0x8E0037 (32 bytes)', text)
        self.assertIn('Current editor changes excluded: 1 bytes', text)
        self.assertNotIn('XOR 0x01', text)
        self.assertEqual(question.call_args.args[-1], QMessageBox.No)

    def test_default_run_uses_original_plan_and_never_enables_changed_values(self):
        self.ready()
        with patch(BACKEND, return_value=self.result()) as backend, \
             patch(MODULE + 'QMessageBox.question', return_value=QMessageBox.Yes):
            self.dialog.start_bench()
            self.finish()
        args, kwargs = backend.call_args
        self.assertEqual(args, ('COM3',))
        self.assertEqual(kwargs['plan']['baseline_blocks'], self.plan['baseline_blocks'])
        self.assertEqual(kwargs['hardware']['capacity_bytes'], 32768)
        self.assertTrue(kwargs['hardware']['confirmed'])
        self.assertFalse(kwargs['probe_change'])
        self.assertNotIn('rate', kwargs)
        self.assertTrue(callable(kwargs['cancelled']))
        self.assertTrue(callable(kwargs['progress']))
        self.assertTrue(kwargs['echo'])
        self.assertEqual(self.dialog.report['gui_context']['editor_changed_bytes_in_blocks'], 1)
        self.assertFalse(self.dialog.report['gui_context']['editor_changes_uploaded'])
        self.assertTrue(self.dialog.report['gui_context']['engine_stopped_acknowledged'])
        self.assertTrue(self.dialog.report['gui_context']['exclusive_client_acknowledged'])
        self.assertFalse(self.dialog.hardware_confirmed.isChecked())
        self.assertFalse(self.dialog.engine_ack.isChecked())
        self.assertFalse(self.dialog.exclusive_ack.isChecked())
        self.assertFalse(self.dialog.run_button.isEnabled())
        self.assertIn('Bench checks passed', self.dialog.status.text())
        self.assertNotIn('full copy', self.dialog.status.text().lower())

    def test_changed_byte_is_opt_in_and_prompt_names_first_cell_and_restoration(self):
        self.ready(change=True)
        report = self.result(restore={'reference_page': 'verified', 'write': 'verified'})
        with patch(BACKEND, return_value=report) as backend, \
             patch(MODULE + 'QMessageBox.question', return_value=QMessageBox.Yes) as question:
            self.dialog.start_bench()
            self.finish()
        text = question.call_args.args[2]
        self.assertIn('byte at 0x8E0020 with XOR 0x01', text)
        self.assertIn('first 2-byte cell', text)
        self.assertIn('restore the original byte', text)
        self.assertTrue(backend.call_args.kwargs['probe_change'])
        self.assertFalse(self.dialog.probe_change.isChecked())
        self.assertIn('changed byte is restored and verified', self.dialog.status.text())

    def test_hardware_changes_reset_confirmation_and_optional_changed_byte(self):
        for control, value in ((self.dialog.port, 'COM4'),
                               (self.dialog.hardware_reference, 'Changed fitted chip evidence')):
            self.ready(change=True)
            control.setText(value)
            self.assertFalse(self.dialog.hardware_confirmed.isChecked())
            self.assertFalse(self.dialog.probe_change.isChecked())
            self.assertFalse(self.dialog.run_button.isEnabled())
        self.ready(change=True)
        self.dialog.capacity.setCurrentIndex(1)
        self.assertFalse(self.dialog.hardware_confirmed.isChecked())
        self.assertFalse(self.dialog.probe_change.isChecked())
        self.assertEqual(self.dialog.plan['capacity_bytes'], 65536)

    def test_busy_worker_blocks_parallel_actions_and_close_cancels_without_wait(self):
        entered, release = threading.Event(), threading.Event()
        def backend(*args, **kwargs):
            kwargs['progress']('Inspecting the calibration manager…')
            entered.set()
            release.wait(2)
            return self.result(status='cancelled')
        self.ready()
        self.dialog.show()
        with patch(BACKEND, side_effect=backend) as call, \
             patch(MODULE + 'QMessageBox.question', return_value=QMessageBox.Yes):
            try:
                self.dialog.start_bench()
                self.assertTrue(entered.wait(2))
                APP.processEvents()
                self.assertIn('Inspecting the calibration manager', self.dialog.output.toPlainText())
                for control in (self.dialog.port, self.dialog.echo, self.dialog.capacity,
                                self.dialog.hardware_reference, self.dialog.hardware_confirmed,
                                self.dialog.engine_ack, self.dialog.probe_change,
                                self.dialog.exclusive_ack,
                                self.dialog.run_button, self.dialog.save_button):
                    self.assertFalse(control.isEnabled())
                self.dialog.start_bench()
                started = time.monotonic()
                self.dialog.accept()
                self.dialog.reject()
                self.dialog.close()
                self.assertLess(time.monotonic() - started, .5)
                self.assertTrue(self.dialog.isVisible())
                self.assertTrue(self.dialog.worker.cancel_event.is_set())
                self.assertFalse(self.dialog.cancel_button.isEnabled())
                self.assertEqual(call.call_count, 1)
            finally:
                release.set()
                self.finish()
        self.assertIn('Bench checks cancelled', self.dialog.status.text())

    def test_failed_reference_byte_and_com_restoration_is_visible_and_fully_exported(self):
        self.ready(change=True)
        report = self.result(
            status='failed', error='Readback was interrupted.', details={'port_closed': False},
            restore={'reference_page': 'unknown', 'write': 'unknown', 'session': 'unknown',
                     'power_cycle_required': True, 'restoration_required': True})
        with patch(BACKEND, return_value=report), \
             patch(MODULE + 'QMessageBox.question', return_value=QMessageBox.Yes):
            self.dialog.start_bench()
            self.finish()
        text = self.dialog.status.text()
        for phrase in ('Reference-page restoration is unconfirmed',
                       'Original-byte restoration is unconfirmed', 'power cycle',
                       'diagnostic session is unconfirmed', 'COM port cleanup is unconfirmed'):
            self.assertIn(phrase, text)
        path = Path(self.files.name) / 'ram-bench.json'
        with patch(MODULE + 'QFileDialog.getSaveFileName',
                   return_value=(str(path), 'JSON (*.json)')):
            self.dialog.save_report()
        saved = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(saved['frames'], report['frames'])
        self.assertEqual(saved['restore'], report['restore'])
        self.assertEqual(saved['plan']['baseline_blocks'], self.plan['baseline_blocks'])
        self.assertFalse(saved['gui_context']['editor_changes_uploaded'])

    def test_unexpected_backend_error_retains_exportable_uncertainty(self):
        self.ready()
        with patch(BACKEND, side_effect=RuntimeError('raw details for report')), \
             patch(MODULE + 'QMessageBox.question', return_value=QMessageBox.Yes):
            self.dialog.start_bench()
            self.finish()
        self.assertEqual(self.dialog.report['status'], 'failed')
        self.assertIn('RuntimeError', self.dialog.status.text())
        self.assertIn('Reference-page restoration is unconfirmed', self.dialog.status.text())
        self.assertNotIn('raw details for report', self.dialog.status.text())
        self.assertTrue(self.dialog.save_button.isEnabled())


class RamCalibrationMainWindowTests(unittest.TestCase):
    @staticmethod
    def window(logger=None, thread=None, profile='361830', definition=True):
        table = SimpleNamespace(discovery_unverified=False) if definition else None
        doc = SimpleNamespace(xdf=SimpleNamespace(profile_id=profile), original=b'original',
                              data=bytearray(b'edited'), path=Path('baseline.FLS'))
        panel = SimpleNamespace(direct_logger=logger, live_thread=thread)
        return SimpleNamespace(document=doc,
                               editor=SimpleNamespace(log_panel=panel, definition=table))

    def test_live_logger_or_stopping_thread_blocks_dialog(self):
        from bmw_bin_editor.gui import MainWindow
        for logger, thread in ((object(), None), (None, object())):
            window = self.window(logger=logger, thread=thread)
            with patch('bmw_bin_editor.gui.Dde5RamCalibrationDialog') as dialog, \
                 patch('bmw_bin_editor.gui.QMessageBox.information') as message:
                MainWindow.open_dde5_ram_bench(window)
            dialog.assert_not_called()
            message.assert_called_once()

    def test_missing_document_wrong_profile_or_unverified_table_blocks_dialog(self):
        from bmw_bin_editor.gui import MainWindow
        absent = self.window()
        absent.document = None
        unverified = self.window()
        unverified.editor.definition.discovery_unverified = True
        for window in (absent, self.window(profile='MS43X001'),
                       self.window(definition=False), unverified):
            with patch('bmw_bin_editor.gui.Dde5RamCalibrationDialog') as dialog, \
                 patch('bmw_bin_editor.gui.QMessageBox.information') as message:
                MainWindow.open_dde5_ram_bench(window)
            dialog.assert_not_called()
            message.assert_called_once()

    def test_explicit_menu_handler_passes_original_and_edited_snapshots(self):
        from bmw_bin_editor.gui import MainWindow
        window = self.window()
        with patch('bmw_bin_editor.gui.Dde5RamCalibrationDialog') as dialog:
            MainWindow.open_dde5_ram_bench(window)
        dialog.assert_called_once_with(b'original', window.editor.definition,
                                       edited=b'edited', image_name='baseline.FLS', parent=window)
        dialog.return_value.exec.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
