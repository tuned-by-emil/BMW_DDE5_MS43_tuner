import os, sys, unittest, tempfile
from pathlib import Path
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QProcess, QSettings
from bmw_bin_editor.mpps_autostart import CableEdge, DEVICE, MppsAutoStart

APP = QApplication.instance() or QApplication([])

class Tests(unittest.TestCase):
    def test_editor_setting(self):
        from bmw_bin_editor import gui
        original = gui.QSettings
        with tempfile.TemporaryDirectory() as td:
            gui.QSettings = lambda *a: QSettings(str(Path(td)/'settings.ini'), QSettings.IniFormat)
            try:
                window = gui.MainWindow()
                self.assertFalse(window.act_mpps.isChecked())
                window.settings.setValue('mpps/auto_launch', False)
                window.act_mpps.setChecked(False)
                self.assertFalse(window.mpps_monitor.enabled)
                self.assertFalse(window.settings.value('mpps/auto_launch', True, type=bool))
                window.close()
                again = gui.MainWindow()
                self.assertFalse(again.act_mpps.isChecked())
                again.close()
            finally:
                gui.QSettings = original

    def test_identity_and_edges(self):
        self.assertTrue(DEVICE.search(r'USB\VID_1C43&PID_0500\5&1c29b00a&0&4'))
        self.assertFalse(DEVICE.search(r'USB\VID_1C43&PID_05001\test'))
        self.assertFalse(DEVICE.search(r'USB\VID_0403&PID_6001\test'))
        edge = CableEdge()
        self.assertTrue(edge.update(True))
        self.assertFalse(edge.update(True))
        self.assertFalse(edge.update(False))
        self.assertFalse(edge.update(True))
        edge.update(False); edge.update(False)
        self.assertTrue(edge.update(True))

    def test_launch_gating(self):
        from unittest.mock import patch
        from bmw_bin_editor import mpps_autostart as module
        monitor = MppsAutoStart()
        real_process = monitor.process
        class Reply:
            output = b''
            def readAllStandardOutput(self): return self.output
        reply = Reply(); monitor.process = reply
        calls = []; monitor._launch = lambda: calls.append('launch')
        monitor.enabled = True
        def finish(output, code=0):
            monitor.stage = 'devices'; reply.output = output
            monitor._finished(code, QProcess.NormalExit)
        with patch.object(module, 'mpps_running', return_value=True):
            finish(br'USB\VID_1C43&PID_0500\test')
            monitor.launch_after = 0
            finish(br'USB\VID_1C43&PID_0500\test')
            self.assertFalse(calls)
        finish(b''); finish(b'')
        with patch.object(module, 'mpps_running', return_value=False):
            finish(br'USB\VID_1C43&PID_0500\test')
            self.assertFalse(calls)
            monitor.launch_after = 0
            finish(br'USB\VID_1C43&PID_0500\test')
            self.assertEqual(calls, ['launch'])
            finish(br'USB\VID_1C43&PID_0500\test')
            self.assertEqual(calls, ['launch'])
        monitor.process = real_process; monitor.stop()

if __name__ == '__main__': unittest.main()
