import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication
from bmw_bin_editor import gui
from bmw_bin_editor.definition_library import LibraryEntry, propose_matches, sha256

APP = QApplication.instance() or QApplication([])
ROOT = Path(__file__).resolve().parents[1]


def fixture(size):
    base = 0x70000 if size == 0x80000 else 0
    data = bytearray(size)
    data[base + 8:base + 14] = b'43X001'
    data[base + 64:base + 64 + len(b'ca43X001.DAT')] = b'ca43X001.DAT'
    axes = [{'offset': base + 0x4000 + i * 16, 'length': 16,
             'sha256': sha256(bytes(16))} for i in range(8)]
    code = [{'offset': 0x1000 + i * 16, 'length': 16,
             'sha256': sha256(bytes(16))} for i in range(8)] if base else []
    filename = 'Siemens_MS43_MS43X001_' + ('512K' if base else '64K') + '.xdf'
    metadata = {'family': 'ms43x001', 'identity_anchors': [
        {'offset': base + 8, 'bytes': b'43X001'.hex()},
        {'offset': base + 64, 'bytes': b'ca43X001.DAT'.hex()}],
        'axis_fingerprints': axes, 'code_fingerprints': code}
    return data, LibraryEntry('synthetic-ms43', 'MS43X001', ROOT / 'definitions/ms43x' / filename, size, metadata)


class DetectionTests(unittest.TestCase):
    def test_edited_full_and_partial_match_with_evidence(self):
        for size in (0x80000, 0x10000):
            with self.subTest(size=size):
                data, entry = fixture(size)
                data[-20] = 42
                matches = propose_matches(bytes(data), entries=[entry])
                self.assertEqual(len(matches), 1)
                self.assertEqual(matches[0].score, 93)

    def test_wrong_firmware_wrong_size_and_missing_layout_do_not_match(self):
        for size in (0x80000, 0x10000):
            data, entry = fixture(size)
            stock = bytearray(data); base = 0x70000 if size == 0x80000 else 0
            stock[base + 8:base + 14] = b'430069'
            self.assertEqual(propose_matches(bytes(stock), entries=[entry]), [])
            self.assertEqual(propose_matches(bytes(data[:-1]), entries=[entry]), [])
            data[base + 0x4000:base + 0x4080] = b'\xff' * 128
            self.assertEqual(propose_matches(bytes(data), entries=[entry]), [])

    def test_bin_open_automatically_loads_full_or_partial_xdf_without_dialog(self):
        with tempfile.TemporaryDirectory() as tmp:
            for size in (0x80000, 0x10000):
                data, entry = fixture(size)
                path = Path(tmp) / ('image-' + str(size) + '.bin'); path.write_bytes(data)
                with patch.object(gui, 'QSettings', lambda *args: QSettings(str(Path(tmp) / (str(size) + '.ini')), QSettings.IniFormat)):
                    window = gui.MainWindow()
                    try:
                        with patch.object(gui, 'propose_matches', return_value=propose_matches(bytes(data), entries=[entry])), \
                             patch.object(gui.QFileDialog, 'getOpenFileName', return_value=(str(path), '')), \
                             patch.object(gui.DefinitionProposalDialog, 'exec', side_effect=AssertionError('Unexpected proposal dialog')), \
                             patch.object(gui.QMessageBox, 'critical') as error:
                            window.open_bin()
                        error.assert_not_called()
                        self.assertIsNotNone(window.document)
                        self.assertEqual(window.xdf.title, 'MS43X001')
                        self.assertEqual(len(window.xdf.definitions), 3772)
                        self.assertEqual(window.current_xdf_path, entry.path)
                        self.assertEqual(bytes(window.document.data), bytes(data))
                        self.assertEqual(window.editor.log_panel.ecu_profile_label, 'MS43X001')
                        stock = bytearray(data)
                        base = 0x70000 if size == 0x80000 else 0
                        stock[base + 8:base + 14] = b'430069'
                        self.assertFalse(window._current_xdf_compatible(stock, []))
                    finally:
                        window.close()
