"""Modeless live RAM controls use a mocked, thread-bound controller only."""
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
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton
from bmw_bin_editor.core import AxisDefinition, CalibrationDefinition, CalibrationDocument
from bmw_bin_editor.dde5_live_ram_gui import Dde5LiveRamDialog, checked_updates

APP = QApplication.instance() or QApplication([])
MODULE = 'bmw_bin_editor.dde5_live_ram_gui.'


def report(qualified=True, **fields):
    result = {'status': 'passed', 'error': None, 'steps': [], 'frames': [],
              'details': {'qualified': qualified, 'page': 'reference'},
              'restore': {'reference_page': 'verified', 'write': 'not_needed',
                          'power_cycle_required': False, 'restoration_required': False}}
    result.update(fields)
    return result


class Controller:
    def __init__(self, port, original, hardware, *, echo, cancelled, progress):
        self.port, self.original, self.hardware = port, original, hardware
        self.cancelled, self.progress = cancelled, progress
        self.qualified = False
        self.calls = []
        self.qualify_result = None
        self.read_entered = self.read_release = None
        self.updates_override = None
        self.fail_operation = False
        self.disconnect_result = None
        self.record('construct')

    def record(self, action, **payload):
        self.calls.append((action, deepcopy(payload), threading.get_ident()))

    def qualify(self, definition):
        self.record('qualify', definition=definition)
        self.progress('Running the reversible qualification test…')
        self.qualified = True
        return self.qualify_result or report()

    def register_table(self, definition, include_axes=True):
        self.record('register_table', definition=definition, include_axes=include_axes)
        return report()

    def apply(self, definition, edited, include_axes=True):
        self.record('apply', definition=definition, edited=edited, include_axes=include_axes)
        if self.fail_operation:
            self.qualified = False
            return report(False, status='failed', error='Exact verification failed.')
        return report(details={'qualified': True, 'page': 'working', 'working_changes_retained': True},
                      restore={'write': 'retained_verified'})

    def read(self, definition, include_axes=True):
        self.record('read', definition=definition, include_axes=include_axes)
        if self.read_entered:
            self.read_entered.set()
            self.read_release.wait(2)
        updates = [{'file_offset': definition.z_address, 'hex': 'AABBCCDD'}]
        if include_axes:
            updates.append({'file_offset': definition.x_axis.address, 'hex': '01020304'})
        return report(updates=self.updates_override if self.updates_override is not None else updates,
                      details={'qualified': True, 'page': 'working'})

    def restore(self, definition, include_axes=True):
        self.record('restore', definition=definition, include_axes=include_axes)
        return report(details={'qualified': True, 'page': 'working'})

    def select_page(self, page):
        self.record('select_page', page=page)
        return report(details={'qualified': True, 'page': page})

    def disconnect(self):
        self.record('disconnect')
        self.qualified = False
        return self.disconnect_result or report(False, details={'qualified': False,
            'page': 'reference', 'port_closed': True})


class LiveRamGuiTests(unittest.TestCase):
    def setUp(self):
        self.files = tempfile.TemporaryDirectory()
        self.definition = CalibrationDefinition('Live fixture', '', 'Test', 0xEA80C, 1, 2,
            x_axis=AxisDefinition(0xEA804, 2), profile_id='361830')
        binary = Path(self.files.name) / 'generated.FLS'
        binary.write_bytes(bytes(0x100000))
        xdf = SimpleNamespace(profile_id='361830', default_lsb_first=False)
        self.doc = CalibrationDocument(xdf, binary)
        self.read_applied = []
        def context():
            return dict(document=self.doc, original=self.doc.original, edited=bytes(self.doc.data),
                        definition=self.definition, profile_id='361830')
        def apply(doc, snapshot, updates):
            self.assertIs(doc, self.doc)
            self.assertEqual(bytes(doc.data), snapshot)
            self.read_applied.append(updates)
            return doc.apply_bytes('Mock RAM readback', updates)
        self.dialog = Dde5LiveRamDialog(context, apply)
        self.controllers = []
        self.configure = lambda controller: None
        def factory(*args, **kwargs):
            controller = Controller(*args, **kwargs)
            self.configure(controller)
            self.controllers.append(controller)
            return controller
        self.backend_patch = patch(MODULE + 'LiveRamController', side_effect=factory)
        self.backend = self.backend_patch.start()

    def tearDown(self):
        if self.controllers and self.controllers[-1].read_release:
            self.controllers[-1].read_release.set()
        if self.dialog.worker is not None:
            self.dialog.disconnect(cancel=True)
            self.wait_for(lambda: self.dialog.worker is None)
        self.dialog.close()
        APP.processEvents()
        self.backend_patch.stop()
        self.files.cleanup()

    def wait_for(self, predicate):
        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            APP.processEvents()
            time.sleep(.005)
        APP.processEvents()
        self.assertTrue(predicate())

    def ready(self):
        self.dialog.port.setText('COM5')
        self.dialog.reference.setText('Synthetic wiring bank and usable capacity evidence')
        self.dialog.hardware_ack.setChecked(True)
        self.dialog.engine_ack.setChecked(True)
        self.dialog.exclusive_ack.setChecked(True)

    def qualify(self):
        self.ready()
        with patch(MODULE + 'QMessageBox.question', return_value=QMessageBox.Yes):
            self.dialog.qualify()
        self.wait_for(lambda: self.dialog._qualified or self.dialog.worker is None)

    def test_modeless_open_does_not_connect_or_unlock_from_existing_report(self):
        self.dialog.show()
        APP.processEvents()
        self.assertFalse(self.dialog.isModal())
        self.backend.assert_not_called()
        self.dialog.reports.append({'action': 'qualify', 'report': report()})
        self.dialog.refresh_controls()
        for button in (self.dialog.upload_button, self.dialog.read_button,
                       self.dialog.restore_button, self.dialog.working_button):
            self.assertFalse(button.isEnabled())

    def test_qualification_requires_declarations_and_explicit_changed_byte_confirmation(self):
        self.dialog.qualify()
        self.backend.assert_not_called()
        self.ready()
        with patch(MODULE + 'QMessageBox.question', return_value=QMessageBox.No) as question:
            self.dialog.qualify()
        self.backend.assert_not_called()
        text = question.call_args.args[2]
        self.assertIn('changed byte at 0x8EA80C', text)
        self.assertIn('restores the original byte', text)
        self.assertEqual(question.call_args.args[-1], QMessageBox.No)

    def test_passing_report_without_live_controller_qualification_does_not_unlock(self):
        def configure(controller):
            def qualify(definition):
                return report()
            controller.qualify = qualify
        self.configure = configure
        self.qualify()
        self.wait_for(lambda: self.dialog.worker is None)
        self.assertFalse(self.dialog._qualified)
        self.assertFalse(self.dialog.upload_button.isEnabled())

    def test_live_controller_without_passing_qualification_report_does_not_unlock(self):
        self.configure = lambda controller: setattr(controller, 'qualify_result', report(False))
        self.qualify()
        self.wait_for(lambda: self.dialog.worker is None)
        self.assertFalse(self.dialog._qualified)
        self.assertFalse(self.dialog.upload_button.isEnabled())

    def test_passed_test_unlocks_only_current_connection_and_uses_one_worker_thread(self):
        owners = []
        self.dialog.ownershipChanged.connect(owners.append)
        self.qualify()
        self.assertTrue(self.dialog.upload_button.isEnabled())
        self.assertFalse(self.dialog.port.isEnabled())
        self.dialog.command('register_table')
        self.wait_for(lambda: not self.dialog._busy)
        self.dialog.command('select_page', page='working')
        self.wait_for(lambda: not self.dialog._busy)
        self.dialog.disconnect()
        self.wait_for(lambda: self.dialog.worker is None)
        self.assertEqual(owners, [True, False])
        controller = self.controllers[0]
        self.assertEqual(len({thread for _, _, thread in controller.calls}), 1)
        self.assertNotEqual(controller.calls[0][2], threading.get_ident())
        self.assertFalse(self.dialog.upload_button.isEnabled())
        self.assertFalse(self.dialog.hardware_ack.isChecked())

    def test_healthy_qualified_connection_handoff_is_not_a_cleanup_warning(self):
        self.configure = lambda controller: setattr(controller, 'qualify_result',
            report(details={'qualified': True, 'page': 'reference', 'port_closed': False,
                            'connection_retained': True}))
        self.qualify()
        self.assertTrue(self.dialog._qualified)
        self.assertIn('Live controls unlocked', self.dialog.status.text())
        self.assertNotIn('cleanup is unconfirmed', self.dialog.status.text())

    def test_upload_is_explicit_captures_edits_and_axes_and_leaves_working_status(self):
        self.qualify()
        self.doc.apply_bytes('User edit', [(0xEA80C, b'\x12\x34')])
        with patch(MODULE + 'QMessageBox.question', return_value=QMessageBox.Yes):
            self.dialog.command('apply')
        captured = bytes(self.doc.data)
        self.doc.apply_bytes('Later edit', [(0xEA80C, b'\x56\x78')])
        self.wait_for(lambda: not self.dialog._busy)
        call = next(payload for action, payload, _ in self.controllers[0].calls if action == 'apply')
        self.assertEqual(call['edited'], captured)
        self.assertTrue(call['include_axes'])
        self.assertIn('working page is active', self.dialog.status.text())
        self.assertEqual(self.doc.data[0xEA80C:0xEA80E], b'\x56\x78')

    def test_declining_upload_or_restore_sends_no_commands(self):
        self.qualify()
        with patch(MODULE + 'QMessageBox.question', return_value=QMessageBox.No):
            self.dialog.command('apply')
            self.dialog.command('restore')
        self.assertFalse(any(action in ('apply', 'restore') for action, _, _ in self.controllers[0].calls))

    def test_ram_read_applies_table_and_axes_as_one_undo_command(self):
        self.qualify()
        self.dialog.command('read')
        self.wait_for(lambda: not self.dialog._busy)
        self.assertEqual(len(self.read_applied), 1)
        self.assertEqual(len(self.doc.undo_stack), 1)
        self.assertEqual(self.doc.data[0xEA80C:0xEA810], bytes.fromhex('AABBCCDD'))
        self.assertEqual(self.doc.data[0xEA804:0xEA808], bytes.fromhex('01020304'))
        self.doc.undo()
        self.assertEqual(bytes(self.doc.data), self.doc.original)

    def test_editor_change_during_readback_preserves_user_work(self):
        self.qualify()
        controller = self.controllers[0]
        controller.read_entered, controller.read_release = threading.Event(), threading.Event()
        self.dialog.command('read')
        self.assertTrue(controller.read_entered.wait(2))
        self.doc.apply_bytes('User edit during read', [(0xEA80C, b'\x99')])
        controller.read_release.set()
        self.wait_for(lambda: not self.dialog._busy)
        self.assertEqual(self.read_applied, [])
        self.assertEqual(self.doc.data[0xEA80C], 0x99)
        self.assertIn('editor changed during RAM readback', self.dialog.status.text())

    def test_closing_during_readback_discards_result_and_cancels_before_disconnect(self):
        self.qualify()
        controller = self.controllers[0]
        controller.read_entered, controller.read_release = threading.Event(), threading.Event()
        self.dialog.command('read')
        self.assertTrue(controller.read_entered.wait(2))
        self.dialog.close()
        self.assertTrue(self.dialog.worker.cancel_event.is_set())
        controller.read_release.set()
        self.wait_for(lambda: self.dialog.worker is None)
        self.assertEqual(self.read_applied, [])
        self.assertEqual(bytes(self.doc.data), self.doc.original)

    def test_outside_partial_and_conflicting_readback_never_replace_editor_bytes(self):
        for updates in ([{'file_offset': 4, 'hex': 'FF'}],
                        [{'file_offset': 0xEA80C, 'hex': 'AA'}],
                        [{'file_offset': 0xEA80C, 'hex': 'AABBCCDD'},
                         {'file_offset': 0xEA80C, 'hex': 'FF'}]):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                checked_updates({'updates': updates}, self.definition, True, self.doc.original)
        self.assertEqual(bytes(self.doc.data), self.doc.original)

    def test_operation_failure_locks_and_disconnects_even_after_earlier_qualification(self):
        self.qualify()
        self.controllers[0].fail_operation = True
        with patch(MODULE + 'QMessageBox.question', return_value=QMessageBox.Yes):
            self.dialog.command('apply')
        self.wait_for(lambda: self.dialog.worker is None)
        self.assertFalse(self.dialog._qualified)
        self.assertFalse(self.dialog.upload_button.isEnabled())
        self.assertTrue(any(row['report']['status'] == 'failed' for row in self.dialog.reports))

    def test_close_waits_asynchronously_for_reference_cleanup_and_reports_remain_exportable(self):
        self.qualify()
        self.dialog.show()
        started = time.monotonic()
        self.dialog.close()
        self.assertLess(time.monotonic() - started, .5)
        self.assertFalse(self.dialog._qualified)
        self.wait_for(lambda: self.dialog.worker is None)
        path = Path(self.files.name) / 'live.json'
        with patch(MODULE + 'QFileDialog.getSaveFileName', return_value=(str(path), 'JSON (*.json)')):
            self.dialog.save_reports()
        saved = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(saved['reports'][-1]['action'], 'disconnect')
        self.assertEqual(saved['reports'][-1]['report']['restore']['reference_page'], 'verified')

    def test_uncertain_disconnect_keeps_report_visible_and_no_unlock(self):
        self.qualify()
        self.dialog.show()
        self.controllers[0].disconnect_result = report(False, status='failed', error='Link lost.',
            details={'qualified': False, 'port_closed': False},
            restore={'reference_page': 'unknown', 'power_cycle_required': True, 'restoration_required': True})
        self.dialog.close()
        self.wait_for(lambda: self.dialog.worker is None)
        self.assertTrue(self.dialog.isVisible())
        self.assertIn('Reference-page restoration is unconfirmed', self.dialog.status.text())
        self.assertIn('COM port cleanup is unconfirmed', self.dialog.status.text())
        self.assertFalse(self.dialog.upload_button.isEnabled())
        self.assertTrue(self.dialog.save_button.isEnabled())

    def test_later_no_session_disconnect_does_not_hide_prior_recovery_requirement(self):
        self.qualify()
        controller = self.controllers[0]
        def fail_apply(**payload):
            controller.qualified = False
            return report(False, status='failed', error='Uncertain RAM write.',
                restore={'reference_page': 'unknown', 'write': 'unknown',
                         'power_cycle_required': True, 'restoration_required': True})
        controller.apply = fail_apply
        with patch(MODULE + 'QMessageBox.question', return_value=QMessageBox.Yes):
            self.dialog.command('apply')
        self.wait_for(lambda: self.dialog.worker is None)
        self.assertIn('Earlier restoration or cleanup remains unconfirmed', self.dialog.status.text())
        self.assertIn('power cycle is still required', self.dialog.status.text())
        self.assertFalse(self.dialog._qualified)


class LiveRamMainWindowTests(unittest.TestCase):
    def test_all_other_ecu_and_source_replacement_handlers_are_guarded(self):
        from bmw_bin_editor.gui import MainWindow, LogViewerPanel
        window = SimpleNamespace(_dde5_live_ram_owned=True)
        with patch('bmw_bin_editor.gui.QMessageBox.information') as message:
            for name in ('open_live_logging', 'open_ecu_programming', 'open_dde5_patch_checks',
                         'open_dde5_ram_bench', 'open_bin', 'open_xdf', 'load_bundled_definition',
                         'show_xdf_editor', 'suggest_matching_xdf'):
                getattr(MainWindow, name)(window)
            MainWindow._apply_xdf_database(window, None)
            self.assertFalse(MainWindow._load_xdf_path(window, 'unused.xdf'))
            LogViewerPanel.open_live_dialog(SimpleNamespace(external_com_owner='RAM'))
        self.assertEqual(message.call_count, 12)
        with self.assertRaises(RuntimeError):
            LogViewerPanel.start_direct_live(SimpleNamespace(external_com_owner='RAM'), {}, {})
        with self.assertRaises(RuntimeError):
            LogViewerPanel.start_live(SimpleNamespace(external_com_owner='RAM'), '', '', '', '', {}, 0)

    def test_owner_flag_disables_connection_actions_and_restores_previous_enabled_states(self):
        from bmw_bin_editor.gui import MainWindow
        panel = SimpleNamespace(live_button=QPushButton())
        window = SimpleNamespace(editor=SimpleNamespace(log_panel=panel),
            act_bin=QAction(None), act_bundled=QAction(None), act_ecu_direct=QAction(None))
        window.act_bundled.setEnabled(False)
        MainWindow._set_dde5_live_ram_owner(window, True)
        MainWindow._set_dde5_live_ram_owner(window, True)
        self.assertFalse(window.act_bin.isEnabled())
        self.assertFalse(panel.live_button.isEnabled())
        self.assertTrue(panel.external_com_owner)
        MainWindow._set_dde5_live_ram_owner(window, False)
        self.assertTrue(window.act_bin.isEnabled())
        self.assertFalse(window.act_bundled.isEnabled())
        self.assertTrue(panel.live_button.isEnabled())

    def test_editor_readback_callback_is_atomic_and_rejects_stale_snapshot(self):
        from bmw_bin_editor.gui import MainWindow
        doc = SimpleNamespace(data=bytearray(b'abc'), apply_bytes=Mock(return_value=True))
        window = SimpleNamespace(document=doc, editor=SimpleNamespace(refresh=Mock()), _document_changed=Mock())
        MainWindow._apply_dde5_ram_readback(window, doc, b'abc', [(1, b'Z')])
        doc.apply_bytes.assert_called_once_with('Read DDE5 RAM table and axes', [(1, b'Z')])
        with self.assertRaises(ValueError):
            MainWindow._apply_dde5_ram_readback(window, doc, b'old', [(1, b'Z')])
        self.assertEqual(doc.apply_bytes.call_count, 1)

    def test_main_close_requests_cleanup_and_defers_without_blocking(self):
        from bmw_bin_editor.gui import MainWindow
        dialog = SimpleNamespace(shutdown=Mock(return_value=False), stopped=SimpleNamespace(connect=Mock()))
        window = SimpleNamespace(document=None, _dde5_live_ram_dialog=dialog,
                                 _dde5_ram_shutdown_finished=Mock())
        event = Mock()
        MainWindow.closeEvent(window, event)
        event.ignore.assert_called_once()
        dialog.stopped.connect.assert_called_once()
        self.assertTrue(window._closing_after_ram)


if __name__ == '__main__':
    unittest.main()
