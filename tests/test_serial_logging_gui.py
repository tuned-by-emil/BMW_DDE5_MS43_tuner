"""Direct serial UI tests; fake loggers never open a cable or vendor library."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from types import ModuleType, SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QObject, QSettings, Signal, Qt
from PySide6.QtWidgets import QApplication, QDialog
import bmw_bin_editor.live_logging as logging

APP = QApplication.instance() or QApplication([])
CAT = logging.load_parameters(Path(__file__).resolve().parents[1] / 'definitions/live_parameters.json')
PROFILE = dict(version=1, ecu='D50M47A', name='Serial UI test', parameters=logging.FAST, interval_ms=0)


class FakeLogger(QObject):
    sampleReady = Signal(float, object)
    status = Signal(str)
    error = Signal(str)
    stopped = Signal()

    def __init__(self, config, catalog, parent=None):
        super().__init__(parent)
        self.config = config
        self.catalog = catalog
        self.started = False
        self.count = 0
        self.capture = None
        self.pending_stop = False
        self.stop_calls = 0

    def start(self):
        self.started = True

    def stop(self):
        self.stop_calls += 1
        if self.pending_stop:
            return False
        self.stopped.emit()
        return True

    def finish(self):
        self.pending_stop = False
        self.stopped.emit()


class SerialLoggingGuiTests(unittest.TestCase):
    def setUp(self):
        self.files = tempfile.TemporaryDirectory()
        self.addCleanup(self.files.cleanup)
        self.settings = QSettings(str(Path(self.files.name) / 'ui.ini'), QSettings.IniFormat)
        self.dialog_patch = patch.object(logging, 'editor_settings', lambda _: self.settings)
        self.dialog_patch.start()

    def tearDown(self):
        self.dialog_patch.stop()
        APP.processEvents()

    def dialog(self):
        dialog = logging.LoggingDialog(CAT)
        dialog.save_csv.setChecked(False)
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(dialog.close)
        return dialog

    def test_existing_indices_and_serial_defaults_without_discovery(self):
        with patch.object(logging, 'available_serial_ports') as enumerate_ports:
            dialog = self.dialog()
            self.assertEqual([dialog.transport.itemData(i) for i in range(3)], ['ediabas', 'testo', 'serial'])
            self.assertEqual(dialog.transport.currentData(), 'ediabas')
            self.assertEqual(dialog.port.currentText(), 'COM1')
            self.assertTrue(dialog.echo.isChecked())
            self.assertFalse(dialog.port.isEnabled())
            dialog.transport.setCurrentIndex(2)
            self.assertTrue(dialog.port.isEnabled())
            self.assertTrue(dialog.echo.isEnabled())
            self.assertFalse(dialog.bin_dir.isEnabled())
            self.assertFalse(dialog.endpoint.isEnabled())
            self.assertIn('9,600 baud', dialog.note.text())
            enumerate_ports.assert_not_called()

    def test_serial_config_and_settings_round_trip(self):
        dialog = self.dialog()
        dialog.transport.setCurrentIndex(2)
        dialog.port.setCurrentText(' com12 ')
        dialog.echo.setChecked(False)
        dialog.endpoint.setText('not a websocket')
        dialog.bin_dir.setText('not an installed folder')
        config = dialog.config()
        self.assertEqual(config['transport'], 'serial')
        self.assertEqual(config['port'], 'COM12')
        self.assertIs(config['echo'], False)
        self.assertEqual(config['output'], '')
        self.assertEqual(config['profile']['ecu'], 'D50M47A')
        dialog.accept()
        self.assertEqual(dialog.result(), QDialog.Accepted)
        second = self.dialog()
        self.assertEqual(second.transport.currentData(), 'serial')
        self.assertEqual(second.port.currentText(), 'COM12')
        self.assertFalse(second.echo.isChecked())

    def test_port_validation_and_unknown_transport(self):
        dialog = self.dialog()
        dialog.transport.setCurrentIndex(2)
        for invalid in ['', 'COM0', 'COM-1', 'COM1/other', 'COM1\x00', 'serial']:
            dialog.port.setCurrentText(invalid)
            with self.assertRaisesRegex(ValueError, 'serial port'):
                dialog.config()
        dialog.transport.addItem('Unknown', 'unknown')
        dialog.transport.setCurrentIndex(3)
        with self.assertRaisesRegex(ValueError, 'supported logging connection'):
            dialog.config()

    def test_refresh_preserves_typed_port_and_handles_optional_discovery(self):
        dialog = self.dialog()
        dialog.transport.setCurrentIndex(2)
        dialog.port.setCurrentText('COM77')
        with patch.object(logging, 'available_serial_ports', return_value=['COM2', 'COM5']) as discover:
            dialog.refresh_ports_button.click()
        discover.assert_called_once_with()
        self.assertEqual([dialog.port.itemText(i) for i in range(dialog.port.count())], ['COM2', 'COM5'])
        self.assertEqual(dialog.port.currentText(), 'COM77')
        with patch.object(logging, 'available_serial_ports', side_effect=ImportError('pyserial is unavailable')):
            dialog.refresh_serial_ports()
        self.assertEqual(dialog.port.currentText(), 'COM77')
        self.assertIn('unavailable', dialog.refresh_ports_button.toolTip())
        self.assertEqual(dialog.config()['port'], 'COM77')
        with patch.object(logging, 'available_serial_ports', return_value=['COM7']):
            dialog.refresh_serial_ports()
        self.assertEqual(dialog.refresh_ports_button.toolTip(), '')
        self.assertEqual(dialog.port.currentText(), 'COM77')

    def test_conflicting_result_names_rejected_before_accepting_or_persisting(self):
        dialog = self.dialog()
        dialog.transport.setCurrentIndex(2)
        for item in dialog.items():
            if item.data(0, Qt.UserRole) in ('IWSTO', 'RWSTR'):
                item.setCheckState(0, Qt.Checked)
        self.assertTrue({'IWSTO', 'RWSTR'}.issubset(dialog.selected()))
        previous_settings = {key: self.settings.value(key) for key in self.settings.allKeys()}
        with self.assertRaisesRegex(ValueError, 'conflicting result names'):
            dialog.config()
        with patch.object(logging.QMessageBox, 'warning') as warning:
            dialog.accept()
        warning.assert_called_once()
        self.assertIn('conflicting result names', warning.call_args.args[2])
        self.assertEqual(dialog.result(), QDialog.Rejected)
        self.assertEqual({key: self.settings.value(key) for key in self.settings.allKeys()}, previous_settings)

    def test_enumeration_never_constructs_or_opens_a_serial_device(self):
        fake_serial = ModuleType('serial')
        fake_serial.Serial = Mock()
        fake_tools = ModuleType('serial.tools')
        comports = Mock(return_value=[SimpleNamespace(device='COM5'), SimpleNamespace(device='COM2'),
                                     SimpleNamespace(device='COM5')])
        fake_tools.list_ports = SimpleNamespace(comports=comports)
        with patch.dict(sys.modules, {'serial': fake_serial, 'serial.tools': fake_tools}):
            self.assertEqual(logging.available_serial_ports(), ['COM2', 'COM5'])
        comports.assert_called_once_with()
        fake_serial.Serial.assert_not_called()

    def window(self):
        import bmw_bin_editor.gui as gui
        import bmw_bin_editor.settings as settings_module
        gui_patch = patch.object(gui, 'editor_settings', lambda _: self.settings)
        settings_patch = patch.object(settings_module, 'editor_settings', lambda _: self.settings)
        gui_patch.start(); settings_patch.start()
        self.addCleanup(gui_patch.stop); self.addCleanup(settings_patch.stop)
        power_patch = patch.object(gui, 'LoggingPowerGuard')
        factory = power_patch.start(); self.addCleanup(power_patch.stop)
        factory.return_value.recover.return_value = ''
        factory.return_value.start.return_value = 'Power: fake logging protection'
        factory.return_value.stop.return_value = 'Power: fake settings restored'
        factory.return_value.refresh.return_value = 'Power: fake logging protection'
        window = gui.MainWindow()
        self.addCleanup(window.deleteLater)
        self.addCleanup(window.close)
        return gui, window

    def test_dispatch_existing_backends_and_serial_sample_stop(self):
        gui, window = self.window()
        panel = window.editor.log_panel
        for transport, backend, label in [('ediabas', 'NativeLiveLogger', 'EDIABAS LIVE'),
                                          ('testo', 'LiveLogger', 'TestO LIVE'),
                                          ('serial', 'SerialLiveLogger', 'SERIAL LIVE')]:
            with self.subTest(transport=transport), patch.object(gui, backend, FakeLogger):
                config = dict(profile=PROFILE, transport=transport, port='COM12', echo=True, output='')
                panel.start_direct_live(config, CAT)
                logger = panel.direct_logger
                self.assertIsInstance(logger, FakeLogger)
                self.assertTrue(logger.started)
                self.assertEqual(logger.config, config)
                self.assertEqual(panel.log.path.name, label)
                logger.count = 1
                logger.sampleReady.emit(0.0, {CAT['INMOT']['result']: 1200.0, CAT['IMEIA']['result']: 8.0})
                self.assertEqual(panel.log.sample_count, 1)
                self.assertTrue(panel.stop_live())
                self.assertEqual(logger.stop_calls, 1)
                self.assertIsNone(panel.direct_logger)
                self.assertFalse(panel.live_mode)
                self.assertTrue(panel.live_button.isEnabled())
                self.assertTrue(panel.play_button.isEnabled())

    def test_pending_serial_stop_keeps_owner_until_finished(self):
        gui, window = self.window()
        panel = window.editor.log_panel
        with patch.object(gui, 'SerialLiveLogger', FakeLogger):
            panel.start_direct_live(dict(profile=PROFILE, transport='serial', port='COM1', echo=True, output=''), CAT)
        logger = panel.direct_logger
        logger.pending_stop = True
        self.assertFalse(panel.stop_live())
        self.assertIs(panel.direct_logger, logger)
        self.assertFalse(panel.live_button.isEnabled())
        logger.finish()
        self.assertIsNone(panel.direct_logger)
        self.assertTrue(panel.live_button.isEnabled())

    def test_power_options_persist_in_logging_config(self):
        dialog=self.dialog()
        dialog.transport.setCurrentIndex(dialog.transport.findData('serial'))
        dialog.port.setCurrentText('COM12')
        self.assertTrue(dialog.config()['keep_awake'])
        self.assertTrue(dialog.config()['lid_closed'])
        dialog.keep_awake.setChecked(False);dialog.lid_closed.setChecked(False)
        dialog.accept()
        second=self.dialog()
        self.assertFalse(second.config()['keep_awake']);self.assertFalse(second.config()['lid_closed'])

    def test_power_lifetime_covers_all_direct_backends_and_pending_stop(self):
        gui,window=self.window();panel=window.editor.log_panel
        for transport,backend in [('ediabas','NativeLiveLogger'),('serial','SerialLiveLogger'),('testo','LiveLogger')]:
            with self.subTest(transport=transport),patch.object(gui,backend,FakeLogger):
                panel.power_guard.reset_mock()
                config=dict(profile=PROFILE,transport=transport,port='COM12',output='',keep_awake=True,lid_closed=True)
                panel.start_direct_live(config,CAT)
                panel.power_guard.start.assert_called_once_with(keep_awake=True,lid_closed=True)
                self.assertTrue(panel._power_timer.isActive())
                self.assertIn('fake logging protection',panel.power_status.text())
                panel.power_guard.stop.reset_mock()
                logger=panel.direct_logger;logger.pending_stop=True
                self.assertFalse(panel.stop_live())
                panel.power_guard.stop.assert_not_called()
                logger.finish()
                panel.power_guard.stop.assert_called_once()
                self.assertFalse(panel._power_timer.isActive())

    def test_logger_start_exception_and_connection_error_release_power(self):
        gui,window=self.window();panel=window.editor.log_panel
        config=dict(profile=PROFILE,transport='serial',port='COM12',output='',keep_awake=True,lid_closed=True)
        with patch.object(gui,'SerialLiveLogger',FakeLogger),patch.object(FakeLogger,'start',side_effect=OSError('start failed')):
            with self.assertRaises(OSError):panel.start_direct_live(config,CAT)
        self.assertIsNone(panel.direct_logger);self.assertFalse(panel._power_timer.isActive())
        with patch.object(gui,'SerialLiveLogger',FakeLogger):panel.start_direct_live(config,CAT)
        panel.power_guard.stop.reset_mock()
        with patch.object(gui.QMessageBox,'warning'):panel.direct_logger.error.emit('connection lost')
        panel.power_guard.stop.assert_called_once()
        self.assertFalse(panel._power_timer.isActive())
        panel.direct_logger.finish()

    def test_unknown_transport_preserves_current_logger(self):
        gui, window = self.window()
        panel = window.editor.log_panel
        with patch.object(gui, 'SerialLiveLogger', FakeLogger):
            panel.start_direct_live(dict(profile=PROFILE, transport='serial', port='COM1', echo=True, output=''), CAT)
        logger = panel.direct_logger
        with self.assertRaisesRegex(ValueError, 'Unsupported live logging transport'):
            panel.start_direct_live(dict(profile=PROFILE, transport='unknown', output=''), CAT)
        self.assertIs(panel.direct_logger, logger)
        self.assertEqual(logger.stop_calls, 0)

    def test_constructor_failure_preserves_inactive_ui_and_preferences(self):
        gui, window = self.window()
        panel = window.editor.log_panel
        def ui_state():
            return (panel.log, panel.live_mode, panel.live_button.text(), panel.live_button.isEnabled(),
                    panel.play_button.text(), panel.play_button.isEnabled(), panel.file_label.text(),
                    panel.file_label.toolTip(), panel._pending_status, panel.live_status.text(),
                    panel.custom_graphs, panel.cursor_index, panel.range_seconds, panel.view_xlim)
        before = ui_state()
        previous_settings = {key: self.settings.value(key) for key in self.settings.allKeys()}
        with patch.object(gui, 'SerialLiveLogger', side_effect=ValueError('Verified serial definitions missing')) as backend:
            with self.assertRaisesRegex(ValueError, 'Verified serial definitions missing'):
                panel.start_direct_live(dict(profile=PROFILE, transport='serial', port='COM1', echo=True, output=''), CAT)
        backend.assert_called_once()
        self.assertEqual(ui_state(), before)
        self.assertIsNone(panel.direct_logger)
        self.assertFalse(panel.live_mode)
        self.assertEqual({key: self.settings.value(key) for key in self.settings.allKeys()}, previous_settings)

    def test_dialog_reports_constructor_race_without_false_live_state(self):
        gui, window = self.window()
        panel = window.editor.log_panel
        dialog = Mock()
        dialog.exec.return_value = QDialog.Accepted
        dialog.config.return_value = dict(profile=PROFILE, transport='serial', port='COM1', echo=True, output='')
        previous_button = panel.live_button.text()
        previous_log = panel.log
        with patch.object(gui, 'LoggingDialog', return_value=dialog), \
             patch.object(gui, 'SerialLiveLogger', side_effect=ValueError('Serial definitions disappeared')), \
             patch.object(gui.QMessageBox, 'warning') as warning:
            panel.open_live_dialog()
        warning.assert_called_once()
        self.assertEqual(warning.call_args.args[2], 'Could not start live logging:\nSerial definitions disappeared')
        self.assertIsNone(panel.direct_logger)
        self.assertIs(panel.log, previous_log)
        self.assertFalse(panel.live_mode)
        self.assertEqual(panel.live_button.text(), previous_button)


if __name__ == '__main__':
    unittest.main()
