"""Check the MainWindow checksum result handoff without opening ECU sessions."""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bmw_bin_editor.gui import MainWindow
from bmw_bin_editor.core import CalibrationDocument
from bmw_bin_editor.dde5_flash import correct_loaded_image

SOURCE = Path(__file__).resolve().parents[1]/'local_resources'/'dde5'


@unittest.skipUnless((SOURCE/'read kessv3.MPC').is_file(), 'Target fixtures not installed')
class EditorApplyTests(unittest.TestCase):
    def setUp(self):
        self.doc = CalibrationDocument(None, SOURCE/'read kessv3.MPC')
        self.doc.apply_bytes('CAN edit', [(0x208ee, b'\x06')])
        fls = (SOURCE/'read kessv3.FLS').read_bytes()
        self.corrected, _ = correct_loaded_image(self.doc.data, 'MPC', fls)
        self.window = SimpleNamespace(document=self.doc,
            editor=SimpleNamespace(log_panel=SimpleNamespace(direct_logger=None, live_thread=None), refresh=Mock()),
            _document_changed=Mock(), _status=Mock())

    def test_accepted_correction_uses_one_undo_command_and_refreshes(self):
        dialog = Mock(corrected_image=self.corrected)
        with patch('bmw_bin_editor.gui.EcuProgrammingDialog', return_value=dialog):
            MainWindow.open_ecu_programming(self.window)
        self.assertEqual(bytes(self.doc.data), self.corrected)
        self.assertEqual(len(self.doc.undo_stack), 2)
        self.window.editor.refresh.assert_called_once()
        self.window._document_changed.assert_called_once()
        self.assertIn('Undo', self.window._status.call_args.args[0])

    def test_cancel_keeps_current_edits_unchanged(self):
        snapshot = bytes(self.doc.data)
        with patch('bmw_bin_editor.gui.EcuProgrammingDialog', return_value=Mock(corrected_image=None)):
            MainWindow.open_ecu_programming(self.window)
        self.assertEqual(bytes(self.doc.data), snapshot)
        self.assertEqual(len(self.doc.undo_stack), 1)
        self.window.editor.refresh.assert_not_called()

    def test_changed_document_rejects_stale_result(self):
        dialog = Mock(corrected_image=self.corrected)
        dialog.exec.side_effect = lambda: self.doc.data.__setitem__(0x208ee, 9)
        with patch('bmw_bin_editor.gui.EcuProgrammingDialog', return_value=dialog), \
             patch('bmw_bin_editor.gui.QMessageBox.warning') as warning:
            MainWindow.open_ecu_programming(self.window)
        warning.assert_called_once()
        self.assertEqual(self.doc.data[0x208ee], 9)
        self.assertNotEqual(bytes(self.doc.data), self.corrected)
        self.assertEqual(len(self.doc.undo_stack), 1)
        self.window.editor.refresh.assert_not_called()


@unittest.skipUnless((SOURCE/'read kessv3.FLS').is_file(), 'Target fixtures not installed')
class ConfigEditorApplyTests(unittest.TestCase):
    def test_standalone_correction_handoff_undo_redo_and_exact_save(self):
        fls = (SOURCE/'read kessv3.FLS').read_bytes()
        for size, space in ((0x100000, 'FLS'), (0x100000, 'CONFIG'),
                            (0x1f000, 'CONFIG'), (0x20000, 'CONFIG')):
            with self.subTest(size=size, space=space), tempfile.TemporaryDirectory() as folder:
                original = fls if size == 0x100000 else fls[0xe0000:0xe0000+size]
                source = Path(folder)/'input.bin'
                source.write_bytes(original)
                doc = CalibrationDocument(None, source)
                offset = 0xee940 if size == 0x100000 else 0xe940
                doc.apply_bytes('Calibration edit', [(offset, bytes([doc.data[offset]^1]))])
                edited = bytes(doc.data)
                corrected, corrections = correct_loaded_image(doc.data, space)
                self.assertEqual(len(corrections), 1)
                window = SimpleNamespace(document=doc,
                    editor=SimpleNamespace(log_panel=SimpleNamespace(direct_logger=None, live_thread=None),
                                           refresh=Mock()),
                    _document_changed=Mock(), _status=Mock())
                with patch('bmw_bin_editor.gui.EcuProgrammingDialog',
                           return_value=Mock(corrected_image=corrected)):
                    MainWindow.open_ecu_programming(window)
                self.assertEqual(bytes(doc.data), corrected)
                self.assertEqual(len(doc.undo_stack), 2)
                window.editor.refresh.assert_called_once()
                doc.undo()
                self.assertEqual(bytes(doc.data), edited)
                doc.redo()
                self.assertEqual(bytes(doc.data), corrected)
                output = Path(folder)/'corrected.bin'
                doc.save_as(output)
                self.assertEqual(output.read_bytes(), corrected)
                self.assertEqual(output.stat().st_size, size)
                self.assertFalse(doc.dirty)
                self.assertEqual(doc.original, original)
                self.assertEqual(source.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
