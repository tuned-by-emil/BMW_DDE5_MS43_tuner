"""Offscreen continuity/review regressions; mock transport, never an account."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import copy
import json
from pathlib import Path
import struct
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication,QDialog
from bmw_bin_editor import gui
from bmw_bin_editor.ai_panel import AIPanel
from bmw_bin_editor.ai_evidence import EvidenceSession
from bmw_bin_editor.chatgpt_client import AccountStore
from bmw_bin_editor.core import CalibrationDefinition,CalibrationDocument
from bmw_bin_editor.logdata import LiveTestOLog,SIG_RPM,SIG_IQ

APP=QApplication.instance() or QApplication([])


class ChatContinuityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.settings_patch=patch.object(gui,'QSettings',side_effect=lambda *_:QSettings(str(self.root/'settings.ini'),QSettings.IniFormat))
        self.settings_patch.start()
        self.window=gui.MainWindow()
        binary=self.root/'r005.bin';binary.write_bytes(struct.pack('<4H',1000,2000,3000,4000))
        self.definition=CalibrationDefinition('N75 test','Code-confirmed base command','N75',0,2,2,z_equation='X/100',z_units='%')
        self.document=CalibrationDocument(SimpleNamespace(path=self.root/'test.xdf',tables=[self.definition],
            definitions=[self.definition], profile_id='synthetic', title='Synthetic fixture', default_lsb_first=True),binary)
        self.window.document=self.document
        self.window.editor.set_document(self.document);self.window.editor.set_definition(self.definition)
        self.panel=AIPanel(self.window,store=AccountStore(self.root))
        self.panel.models.addItem('Mock model','mock-model')
        self.log=LiveTestOLog()
        for i in range(20):self.log.append_sample(i,{SIG_RPM:1800+i,SIG_IQ:15})
        viewer=self.window.editor.log_panel
        viewer.log=self.log;viewer.live_mode=True;viewer.cursor_index=19
        self.panel.show();APP.processEvents()

    def tearDown(self):
        self.panel.shutdown()
        if self.panel.worker:self.panel.worker.wait(5000);APP.processEvents()
        self.window.editor.log_panel.live_mode=False
        self.document.dirty=False
        self.window.close();self.panel.close();APP.processEvents()
        self.settings_patch.stop();self.temp.cleanup()

    def wait_worker(self):
        deadline=time.monotonic()+8
        while self.panel.worker and time.monotonic()<deadline:
            APP.processEvents();time.sleep(.005)
        self.assertIsNone(self.panel.worker)

    def answer(self,text,action,bodies=None):
        def response(body,cancel):
            if bodies is not None:bodies.append(copy.deepcopy(body))
            return {'text':text,'output':[]}
        with patch.object(self.panel.client,'_stream_response',side_effect=response):
            action();self.wait_worker()

    def initial(self,question='Estimate launch power'):
        self.panel.question.setPlainText(question)
        self.answer('- ORIGINAL_REPLY_MARKER.',lambda:self.panel.analyse('live'))
        return self.panel.records[0]

    def test_fresh_capture_updates_evidence_and_retains_actual_chat_turns(self):
        record=self.initial();origin_question=record['review_context']['initial_question']
        original_signature=copy.deepcopy(record['snapshot']['calibration_signature'])
        self.answer('- TYRE_REPLY_MARKER.',lambda:self.panel.ask_followup('My tyres are 235/40R18'))
        self.document.data[0]^=1
        self.log.append_sample(30,{SIG_RPM:4000,SIG_IQ:80})
        self.panel.question.setPlainText('Analyse the newly acquired run')
        bodies=[]
        self.answer('- UPDATED_REPLY_MARKER.',lambda:self.panel.analyse('live'),bodies)
        self.assertEqual(len(self.panel.records),1);self.assertIs(self.panel.records[0],record)
        self.assertEqual(record['tips'],'- ORIGINAL_REPLY_MARKER.')
        self.assertEqual(len(record['conversation']),2)
        self.assertEqual(record['conversation'][0]['question'],'My tyres are 235/40R18')
        self.assertEqual(record['conversation'][-1]['answer'],'- UPDATED_REPLY_MARKER.')
        self.assertNotEqual(record['snapshot']['calibration_signature'],original_signature)
        self.assertEqual(record['snapshot']['log']['end_s'],30)
        self.assertEqual(record['review_context']['initial_question'],origin_question)
        self.assertIn('ORIGINAL_REPLY_MARKER',json.dumps(bodies[0]['input']))
        self.assertIn('TYRE_REPLY_MARKER',json.dumps(bodies[0]['input']))
        self.assertIn('newly acquired run',bodies[0]['input'][-1]['content'])
        self.assertIn('UPDATED_REPLY_MARKER',self.panel.output.toPlainText())
        self.assertFalse(self.panel.record_stale(record))

    def test_new_chat_keeps_archived_record_but_isolates_new_request(self):
        old=self.initial()
        self.answer('- OLD_FOLLOWUP_MARKER.',lambda:self.panel.ask_followup('Old question'))
        self.panel.new_chat()
        self.assertEqual(self.panel.history.currentIndex(),-1)
        self.assertFalse(self.panel.fix_button.isEnabled());self.assertFalse(self.panel.followup_send.isEnabled())
        self.assertFalse(self.panel.result_displayed);self.assertFalse(self.panel.reply_question.isVisible())
        self.panel.question.setPlainText('Start independently')
        bodies=[];self.answer('- NEW_CHAT_MARKER.',lambda:self.panel.analyse('live'),bodies)
        self.assertEqual(len(self.panel.records),2)
        self.assertIs(self.panel.records[1],old);self.assertEqual(len(old['conversation']),1)
        self.assertFalse(self.panel.records[0]['conversation'])
        serialized=json.dumps(bodies[0]['input'])
        self.assertNotIn('ORIGINAL_REPLY_MARKER',serialized);self.assertNotIn('OLD_FOLLOWUP_MARKER',serialized)
        self.assertIn('NEW_CHAT_MARKER',self.panel.output.toPlainText())

    def test_show_fix_of_initial_reply_uses_its_original_question_after_recapture(self):
        record=self.initial('Smooth the selected N75 cells')
        original=record['review_context']['initial_question']
        self.panel.question.setPlainText('Estimate power in this newer log')
        self.answer('- NEW_CAPTURE_REPLY.',lambda:self.panel.analyse('live'))
        self.panel.reply_picker.setCurrentIndex(0)
        with patch.object(self.panel.client,'follow_up',return_value='- Selected original answer.') as send:
            self.panel.show_fix();self.wait_worker()
        self.assertEqual(send.call_args.args[1]['selected_reply']['question'],original)
        self.assertIn('Smooth',send.call_args.args[1]['selected_reply']['question'])

    def test_applied_cell_journal_exports_restores_and_reaches_next_followup(self):
        record=self.initial('Smooth the selected N75 cells')
        session=EvidenceSession(record['snapshot'],question=record['snapshot']['analysis_question']);session.inspect_tables()
        proposal=session.propose_patch('smoothing','Test single cell',[{'table_address':0,'table_id':None,'row':0,'col':1,'old':20,'new':21,
            'reason':'Explicit requested cell blend','expected_effect':'Numerical experiment; no guaranteed boost benefit'}])['proposal']
        record['proposals']=[proposal];self.panel.show_turn(0)
        with patch.object(QDialog,'exec',return_value=QDialog.Accepted):self.panel.review_patch()
        self.assertEqual(self.document.cell_value(self.definition,0,1),21)
        self.assertEqual(len(self.document.undo_stack),1)
        journal=copy.deepcopy(record['applied_proposals']);self.assertEqual(journal[0]['label'],'Test single cell')
        exported=self.root/'exported-review.json'
        with patch('bmw_bin_editor.ai_panel.QFileDialog.getSaveFileName',return_value=(str(exported),'JSON')):self.panel.export()
        self.assertEqual(json.loads(exported.read_text(encoding='utf-8'))['applied_proposals'],journal)
        self.panel.load_review(exported)
        self.assertEqual(self.panel.records[0]['applied_proposals'],journal)
        bodies=[];self.answer('- Previously applied in memory.',lambda:self.panel.ask_followup('Which cells did I apply?'),bodies)
        public=json.loads(bodies[0]['input'][0]['content'].split('\n',1)[1])
        self.assertEqual(public['editor_actions'],journal)

    def test_forged_displayed_replacement_is_rejected_before_review_dialog(self):
        record=self.initial('Smooth selected N75 cells')
        session=EvidenceSession(record['snapshot'],question=record['snapshot']['analysis_question']);session.inspect_tables()
        plan=session.propose_patch('smoothing','Cell blend',[{'table_address':0,'table_id':None,'row':0,'col':1,'old':20,'new':21,
            'reason':'Requested numeric blend','expected_effect':'Numerical change'}])['proposal']
        plan['changes'][0]['new']=1e10;record['proposals']=[plan];self.panel.show_turn(0)
        before=bytes(self.document.data)
        with patch.object(QDialog,'exec',return_value=QDialog.Accepted) as dialog:self.panel.review_patch()
        self.assertFalse(dialog.called);self.assertEqual(bytes(self.document.data),before)
        self.assertFalse(self.document.undo_stack);self.assertIn('No cells applied',self.panel.status.text())

    def test_reasoning_selector_persists_and_drives_actual_request(self):
        self.panel.effort.setCurrentIndex(self.panel.effort.findData('high'));self.window.settings.sync()
        other=AIPanel(self.window,store=AccountStore(self.root))
        try:
            self.assertEqual(other.effort.currentData(),'high')
        finally:other.shutdown();other.close();APP.processEvents()
        bodies=[];self.answer('- Selected reasoning result.',lambda:self.panel.analyse('live'),bodies)
        self.assertEqual(bodies[0]['reasoning'],{'effort':'high'})
        self.assertEqual(self.panel.records[0]['reasoning_effort'],'high')


if __name__=='__main__':unittest.main()
