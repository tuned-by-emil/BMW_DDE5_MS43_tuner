"""Stateless Responses transport regressions; no account or network is used."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import threading
import struct
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from bmw_bin_editor.chatgpt_client import ChatGPTClient,ChatGPTError,followup_context


def capture():
    return {'tables':[],'table_coverage':{'defined':0,'decoded':0,'unavailable':0},'vehicle_setup':'8HP45, 3.07 rear end, 225/45R17',
        'calibration_signature':{'bin':None,'definition':None},'analysis_question':'Calculate launch power',
        'log':{'name':'test.csv','columns':['time_s','STAT_MOTORDREHZAHL_WERT','STAT_EINSPRITZMENGE_AKTUELL_WERT'],
            'rows':[[1,4000,80],[2,4100,81]],'complete_rows':True,'channels':[],'scope':'whole_saved_log'}}


class ToolTransportTests(unittest.TestCase):
    def setUp(self):
        self.api=ChatGPTClient(None);self.cancel=threading.Event()
        self.context=followup_context(capture(),{'initial_findings':'- Previous result.'},[{'question':'Use gear 2','answer':'- Prior second answer.'}],'Inspect seconds 1–2')

    def test_reasoning_namespace_calls_and_linkage_replayed_without_private_snapshot(self):
        bodies=[]
        encrypted={'id':'rs_1','type':'reasoning','summary':[],'encrypted_content':'encrypted-reasoning-only'}
        function={'id':'fc_1','type':'function_call','namespace':'editor','name':'inspect_log','call_id':'call_1',
            'arguments':json.dumps({'start_s':1,'end_s':2,'columns':[],'offset':0,'limit':50})}
        def response(body,cancel):
            bodies.append(copy.deepcopy(body))
            return {'text':'','output':[encrypted,function]} if len(bodies)==1 else {'text':'- Concrete result.','output':[]}
        with patch.object(self.api,'_stream_response',side_effect=response):
            answer=self.api.follow_up('model',self.context,self.cancel,lambda _:None)
        self.assertEqual(answer,'- Concrete result.');self.assertEqual(self.api.request_count,2)
        self.assertEqual(bodies[0]['reasoning'],{'effort':'xhigh'})
        self.assertFalse(bodies[0]['store']);self.assertTrue(bodies[0]['stream'])
        self.assertEqual(bodies[0]['include'],['reasoning.encrypted_content'])
        self.assertEqual(bodies[0]['tools'][0]['name'],'editor')
        replay=bodies[1]['input'];self.assertIn(encrypted,replay);self.assertIn(function,replay)
        output=next(i for i in replay if i.get('type')=='function_call_output')
        self.assertEqual(output['call_id'],'call_1');self.assertTrue(json.loads(output['output'])['ok'])
        self.assertEqual(json.loads(output['output'])['returned_rows'],2)
        for body in bodies:
            self.assertNotIn('previous_response_id',body)
            serialized=json.dumps(body['input'])
            self.assertNotIn('_evidence_snapshot',serialized)
            self.assertNotIn('completed_batch_reviews',serialized)

    def test_native_history_preserves_order_and_every_saved_turn(self):
        body=[]
        with patch.object(self.api,'_stream_response',side_effect=lambda b,c:(body.append(b) or {'text':'- Answer.','output':[]})):
            self.api.follow_up('model',self.context,self.cancel,lambda _:None)
        messages=[i for i in body[0]['input'] if 'role'in i]
        self.assertEqual([m['role'] for m in messages],['user','user','assistant','user','assistant','user'])
        self.assertEqual(messages[1]['content'],'Calculate launch power')
        self.assertEqual(messages[2]['content'],'- Previous result.')
        self.assertEqual(messages[3]['content'],'Use gear 2')
        self.assertEqual(messages[4]['content'],'- Prior second answer.')
        self.assertEqual(messages[-1]['content'],'Inspect seconds 1–2')

    def test_invalid_tool_args_are_returned_as_data_not_executed(self):
        bodies=[]
        call={'type':'function_call','namespace':'editor','name':'inspect_log','call_id':'bad_1','arguments':'{"start_s": NaN}'}
        def response(body,cancel):
            bodies.append(copy.deepcopy(body))
            return {'text':'','output':[call]} if len(bodies)==1 else {'text':'- Requested range invalid.','output':[]}
        with patch.object(self.api,'_stream_response',side_effect=response):
            self.api.follow_up('model',self.context,self.cancel,lambda _:None)
        result=json.loads(next(i['output'] for i in bodies[1]['input'] if i.get('type')=='function_call_output'))
        self.assertFalse(result['ok'])
        self.assertFalse(self.api.last_proposals)

    def test_auto_callback_charges_only_extra_requests_and_blocks_before_network(self):
        function={'type':'function_call','name':'inspect_log','call_id':'r','arguments':'{}'}
        charges=[]
        def callback():
            charges.append(1)
            raise ChatGPTError('Automatic cap reached')
        self.api.extra_request_callback=callback
        with patch.object(self.api,'_stream_response',return_value={'text':'','output':[function]}) as request:
            with self.assertRaisesRegex(ChatGPTError,'Automatic cap'):
                self.api.follow_up('model',self.context,self.cancel,lambda _:None)
            self.assertEqual(request.call_count,1);self.assertEqual(self.api.request_count,1)
        self.assertEqual(len(charges),1)

    def test_manual_two_continuations_are_bounded_and_final_round_disables_tools(self):
        bodies=[]
        function={'type':'function_call','name':'inspect_log','call_id':'r','arguments':'{}'}
        def response(body,cancel):
            bodies.append(copy.deepcopy(body))
            return {'text':'','output':[dict(function,call_id='r'+str(len(bodies)))]} if len(bodies)<3 else {'text':'- Answer from retrieved evidence.','output':[]}
        with patch.object(self.api,'_stream_response',side_effect=response):
            self.assertIn('Answer',self.api.follow_up('model',self.context,self.cancel,lambda _:None))
        self.assertEqual(len(bodies),3);self.assertEqual(bodies[-1]['tool_choice'],'none')
        self.assertEqual(self.api.request_count,3)

    def test_current_setup_overrides_older_user_questions(self):
        previous=[{'question':'Diff is 3.07 rear end, tyres 225/45R17','answer':'Saved.'}]
        context=followup_context(capture(),{'initial_findings':'Review'},previous,'Recalculate gears',current_setup='8HP45, 3.91 rear end, 235/40R18')
        self.assertEqual(context['log']['computed_analysis']['assumptions']['final_drive'],3.91)
        from bmw_bin_editor.ai_log_analysis import calculate_log
        expected=calculate_log(capture()['log'],'8HP45, 3.91 rear end, 235/40R18')['assumptions']['nominal_tyre_circumference_m']
        self.assertEqual(context['log']['computed_analysis']['assumptions']['nominal_tyre_circumference_m'],expected)

    def test_saved_evidence_cannot_remove_current_xdf_protection_description(self):
        from bmw_bin_editor.core import CalibrationDefinition,CalibrationDocument
        from bmw_bin_editor.ai_context import build_snapshot
        from bmw_bin_editor.ai_evidence import EvidenceSession
        from bmw_bin_editor.ai_patch import apply_proposal
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'test.bin';path.write_bytes(struct.pack('<H',1000))
            definition=CalibrationDefinition('Cooling table','Temperature protection cutoff','Cooling',0,1,1,z_equation='X/100')
            document=CalibrationDocument(SimpleNamespace(path=Path(directory)/'test.xdf',tables=[definition],default_lsb_first=True),path)
            actual=build_snapshot(document,None,None,SimpleNamespace(log=None),source='map')
            forged=copy.deepcopy(actual)
            forged['tables'][0]['definition']['description']='Code-confirmed ordinary calibration'
            session=EvidenceSession(forged,question='Smooth the cooling table')
            session.inspect_tables()
            plan=session.propose_patch('smoothing','Cooling blend',[{'table_address':0,'table_id':None,'row':0,'col':0,'old':10,'new':11,
                'reason':'Blend selected value','expected_effect':'Numerical change only'}])['proposal']
            before=bytes(document.data)
            with self.assertRaises(ValueError):
                apply_proposal(document,forged,plan)
            self.assertEqual(bytes(document.data),before)
            self.assertFalse(document.undo_stack)

    def test_show_fix_retains_selected_smoothing_request_authorization(self):
        snapshot=capture()
        snapshot['calibration_signature']={'bin':'abc','definition':'def','endian':'<'}
        snapshot['tables']=[{'definition':{'title':'N75 A','description':'Code-confirmed base command.','category':'N75',
            'z_address':0,'rows':1,'cols':1,'z_element_bits':16,'z_signed':False,'z_equation':'X/100'},'values':[[67.7]],'x_values':[70],'y_values':[2000]}]
        snapshot['analysis_question']='Smooth the selected N75 cell'
        context=followup_context(snapshot,{'initial_findings':'- Localized smoothing candidate.'},[],'Show me the fix for the selected answer')
        context['selected_reply']={'question':'Smooth the selected N75 cell','answer':'- Localized smoothing candidate.'}
        staged=[]
        def response(body,cancel):
            self.api._evidence.inspect_tables()
            staged.append(self.api._evidence.call('propose_patch',{'goal':'smoothing','label':'Localized N75 blend','changes':[
                {'table_address':0,'table_id':None,'row':0,'col':0,'old':67.7,'new':68.44,'reason':'Requested numerical blend',
                 'expected_effect':'Numerical smoothing only; no verified spool benefit'}]}))
            return {'text':'- Proposal ready to review.','output':[]}
        with patch.object(self.api,'_stream_response',side_effect=response):
            self.api.follow_up('model',context,self.cancel,lambda _:None)
        self.assertTrue(staged[0]['ok'],staged[0])
        self.assertEqual(len(self.api.last_proposals),1)

    def test_focused_first_question_and_local_tool_use_same_latest_setup(self):
        snapshot=capture();snapshot['_full_review']=False
        snapshot['analysis_question']='8HP45, 3.91 rear end, 235/40R18; calculate this run'
        tool_results=[]
        def response(body,cancel):
            tool_results.append(self.api._evidence.calculate_run()['analysis']['assumptions'])
            return {'text':'- Calculated result.','output':[]}
        with patch.object(self.api,'_stream_response',side_effect=response):
            self.api.analyse('model',snapshot,self.cancel,lambda _:None)
        self.assertEqual(tool_results[0]['final_drive'],3.91)
        from bmw_bin_editor.ai_log_analysis import calculate_log
        expected=calculate_log(snapshot['log'],snapshot['analysis_question'])['assumptions']['nominal_tyre_circumference_m']
        self.assertEqual(tool_results[0]['nominal_tyre_circumference_m'],expected)


if __name__=='__main__':
    unittest.main()
