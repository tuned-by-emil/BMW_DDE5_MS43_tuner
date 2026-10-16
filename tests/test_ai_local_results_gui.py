"""Local-result UX boundaries: no network, exact captured scope, distinct launch."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import copy
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication,QComboBox,QDialog,QTextBrowser
from bmw_bin_editor import gui
from bmw_bin_editor.ai_panel import AIPanel
from bmw_bin_editor.chatgpt_client import AccountStore
from bmw_bin_editor.core import CalibrationDefinition,CalibrationDocument
from bmw_bin_editor.logdata import LiveTestOLog,SIG_RPM,SIG_IQ,SIG_SPEED,SIG_MAF

APP=QApplication.instance() or QApplication([])


class LocalResultsGuiTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.settings_patch=patch.object(gui,'QSettings',side_effect=lambda *_:QSettings(str(self.root/'settings.ini'),QSettings.IniFormat));self.settings_patch.start()
        self.window=gui.MainWindow()
        binary=self.root/'test.bin';binary.write_bytes(struct.pack('<H',1000))
        definition=CalibrationDefinition('Test map','Code-confirmed','N75',0,1,1,z_equation='X/100')
        self.document=CalibrationDocument(SimpleNamespace(path=self.root/'test.xdf',tables=[definition],default_lsb_first=True),binary)
        self.window.document=self.document
        self.panel=AIPanel(self.window,store=AccountStore(self.root))
        self.panel.models.clear()
        log=LiveTestOLog()
        # Loaded standstill, movement/launch peak, sustained lift, larger later pull.
        for t,rpm,iq,speed in [(0,3000,50,0),(.3,3100,50,0),(.6,4100,80,20),(1.2,4410,82,60),
                               (2,4000,60,70),(2.5,2500,10,70),(3,2400,10,70),(20,5000,100,120)]:
            log.append_sample(t,{SIG_RPM:rpm,SIG_IQ:iq,SIG_SPEED:speed,SIG_MAF:iq*14.5*1.15})
        viewer=self.window.editor.log_panel
        viewer.log=log;viewer.live_mode=True;viewer.cursor_index=7
        self.panel.show();APP.processEvents()

    def tearDown(self):
        self.panel.shutdown();self.window.editor.log_panel.live_mode=False
        self.window.close();self.panel.close();APP.processEvents()
        self.settings_patch.stop();self.temp.cleanup()

    @staticmethod
    def dialog_controls(dialog):
        return dialog.findChild(QComboBox),dialog.findChild(QTextBrowser)

    def test_local_results_work_without_client_model_account_or_network(self):
        seen=[];client=self.panel.client;self.panel.client=None;self.panel.update_controls()
        self.assertTrue(self.panel.local_results_button.isEnabled())
        before_requests=list(self.panel.request_times)
        def inspect(dialog):
            scope,view=self.dialog_controls(dialog);seen.append(view.toPlainText())
            self.assertIsNotNone(scope);return QDialog.Accepted
        with (patch.object(client,'analyse',side_effect=AssertionError('No model request')),
              patch.object(client,'follow_up',side_effect=AssertionError('No follow-up')),
              patch('bmw_bin_editor.chatgpt_client.request',side_effect=AssertionError('No network')),
              patch.object(QDialog,'exec',inspect)):
            self.panel.show_local_results()
        self.assertTrue(seen);self.assertIn('Launch 1 peak',seen[0]);self.assertIn('276.1 bhp',seen[0])
        self.assertEqual(self.panel.request_times,before_requests);self.assertFalse(self.panel.records);self.assertIsNone(self.panel.worker)

    def test_launch_and_entire_scope_use_unsampled_peak_rows_not_sent_excerpt(self):
        captured=self.panel.snapshot('live')
        self.assertIn('_captured_log_rows',captured)
        raw=copy.deepcopy(captured['_captured_log_rows'])
        # Represent a heavily reduced outgoing excerpt: neither important peak is sent.
        captured['log']['rows']=[captured['log']['rows'][0]];captured['log']['sent_rows']=1
        self.assertEqual(len(raw),8);seen={}
        def inspect(dialog):
            scope,view=self.dialog_controls(dialog)
            self.assertEqual(scope.currentData(),0)
            seen['launch']=view.toPlainText()
            scope.setCurrentIndex(0);seen['whole']=view.toPlainText()
            scope.setCurrentIndex(1);seen['launch_again']=view.toPlainText()
            return QDialog.Accepted
        with patch.object(self.panel,'snapshot',return_value=captured),patch.object(QDialog,'exec',inspect):self.panel.show_local_results()
        self.assertIn('Launch 1 peak: 276.1 bhp at 1.2 s',seen['launch'])
        self.assertIn('Captured-scope peak: 381.7 bhp at 20 s',seen['whole'])
        self.assertNotIn('381.7 bhp',seen['launch'])
        self.assertEqual(seen['launch'],seen['launch_again'])

    def test_whole_and_launch_scopes_share_declared_question_hardware_setup(self):
        self.panel.vehicle.setPlainText('')
        self.panel.question.setPlainText('8HP45, 3.07 rear end, 225/45R17; calculate this run')
        seen={}
        def inspect(dialog):
            scope,view=self.dialog_controls(dialog)
            seen['launch']=view.toPlainText();scope.setCurrentIndex(0);seen['whole']=view.toPlainText()
            return QDialog.Accepted
        with patch.object(QDialog,'exec',inspect):self.panel.show_local_results()
        self.assertIn('RPM/speed ratio candidates',seen['launch'])
        self.assertIn('RPM/speed ratio candidates',seen['whole'])


if __name__=='__main__':unittest.main()
