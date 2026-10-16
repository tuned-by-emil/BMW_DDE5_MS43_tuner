import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication
from bmw_bin_editor import gui
from public_build_resources import public_data

APP = QApplication.instance() or QApplication([])
ROOT = Path(__file__).resolve().parents[1]


class PublicCheckoutTests(unittest.TestCase):
    def test_build_data_allowlist_ignores_installed_reference_packs(self):
        data = public_data(ROOT)
        names = [Path(source).suffix.lower() for source, _ in data]
        self.assertEqual(names.count('.xdf'), 9)
        self.assertEqual(names.count('.adx'), 1)
        self.assertTrue(all(Path(source).is_file() for source, _ in data))
        self.assertEqual(len(data), 20)

    def test_missing_local_resources_keep_startup_and_ms43_editor_available(self):
        with tempfile.TemporaryDirectory() as temp:
            missing = Path(temp) / 'absent.xdf'
            settings = QSettings(str(Path(temp) / 'test.ini'), QSettings.IniFormat)
            settings.setValue('settings/migrated', True)
            with patch.object(gui, 'QSettings', lambda *args: settings), \
                 patch.object(gui, 'bundled_definition_path', return_value=missing), \
                 patch('bmw_bin_editor.adx.bundled_ms43_adx_path', return_value=missing), \
                 patch.object(gui.QMessageBox, 'information') as message:
                window = gui.MainWindow()
                try:
                    self.assertIsNone(window.document)
                    self.assertIn('BMW DDE5 MS43 Tuner', window.windowTitle())
                    self.assertFalse(hasattr(window, 'act_mpps'))
                    self.assertFalse(hasattr(window, 'mpps_monitor'))
                    self.assertFalse(any('MPPS' in a.text() for a in window.findChildren(gui.QAction)))
                    self.assertFalse(window.act_bundled.isEnabled())
                    panel = window.editor.log_panel
                    panel.set_ecu_profile('public-ms43-test', 'MS43X001')
                    self.assertFalse(panel.catalog.definitions)
                    panel.open_live_dialog()
                    message.assert_called_once()
                    self.assertIn('ADX', message.call_args.args[2])
                    self.assertIsNone(panel.direct_logger)
                finally:
                    window.close()
