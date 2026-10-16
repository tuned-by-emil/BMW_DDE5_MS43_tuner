"""Offline focused serialization checks; no account, file or network calls."""
import copy,json,sys,threading,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
if not (ROOT/'bmw_bin_editor').is_dir():
    ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from bmw_bin_editor.ai_prompt import compact_provider_context
from bmw_bin_editor.ai_evidence import EvidenceSession
from bmw_bin_editor.chatgpt_client import ChatGPTClient,followup_context


def capture():
    return {'analysis_question':'Original launch question','vehicle_setup':'8HP45, diff 3.07',
        'calibration_signature':{'bin':'same','definition':'same'},
        'table_coverage':{'defined':2,'decoded':2,'unavailable':0},
        'tables':[{'definition':{'title':'N75 A','description':'Captured base','z_address':0xF7F44,'rows':1,'cols':1},'values':[[67.7]],'x_values':[70],'y_values':[2000]},
                  {'definition':{'title':'Gear torque selector1','z_address':0xE945A,'rows':1,'cols':1},'values':[[510]],'x_values':[2000],'y_values':[0]}],
        'log':{'name':'test.csv','columns':['time_s','STAT_MOTORDREHZAHL_WERT','STAT_EINSPRITZMENGE_AKTUELL_WERT','STAT_GESCHWINDIGKEIT_WERT'],
            'rows':[[0,3500,60,0],[.3,4000,70,0],[.6,4500,80,20]],'total_rows':3,'complete_rows':True},
        'confirmed_gear_bindings':{'1':{'table_address':0xE945A,'source':'User-confirmed CAN'}}}


class PromptTests(unittest.TestCase):
    def context(self):
        return followup_context(capture(),{'initial_question':'Original launch question','initial_findings':'Original reply'},
            [{'question':'User correction remains','answer':'Assistant history remains'}],'Recalculate launch')

    def test_compaction_preserves_calculations_capture_and_native_history(self):
        context=self.context();before=copy.deepcopy(context)
        public={k:v for k,v in context.items() if not k.startswith('_') and k not in {'previous_turns','completed_review'}}
        result=compact_provider_context(public)
        self.assertEqual(context,before)
        self.assertEqual(result['log']['computed_analysis'],public['log']['computed_analysis'])
        self.assertEqual(result['table_coverage'],public['table_coverage'])
        self.assertEqual(result['confirmed_gear_bindings'],public['confirmed_gear_bindings'])
        self.assertEqual(result['table_catalog']['available_tables'],2)
        self.assertEqual(result['table_catalog']['addresses'],['0xF7F44','0xE945A'])
        self.assertEqual(result['log']['available_captured_rows'],3)
        self.assertTrue(result['log']['captured_complete_rows'])
        self.assertFalse(result['log']['complete_rows'])
        self.assertEqual(result['log']['rows'],[])
        self.assertNotIn('tables',result)
        session=EvidenceSession(context['_evidence_snapshot'],setup=context['resolved_vehicle_setup'])
        self.assertEqual(session.inspect_tables(query='N75',include_values=True)['tables'][0]['values'],[[67.7]])
        self.assertEqual(session.inspect_log()['returned_rows'],3)
        self.assertEqual(session.calculate_run(launch_index=0)['analysis']['peaks']['power']['time_s'],.6)

    def request(self,snapshot,allow_tools=True):
        client=ChatGPTClient.__new__(ChatGPTClient)
        client._allow_tools=allow_tools;client._evidence=EvidenceSession(capture())
        client._chat_review={};client._chat_turns=[];client._reserved_requests=1
        client.request_count=0;client.extra_request_callback=None;client.reasoning_effort='xhigh'
        with patch.object(client,'_stream_response',return_value={'text':'- Answer.','output':[]}) as stream:
            client._analyse_request('mock',snapshot,threading.Event(),lambda _:None)
            body=stream.call_args.args[0]
        public=json.loads(body['input'][0]['content'].split('\n',1)[1])
        return body,public

    def test_actual_client_serialization_compacts_only_private_tool_context(self):
        context=self.context();before=copy.deepcopy(context)
        body,public=self.request(context)
        self.assertNotIn('tables',public)
        self.assertEqual(public['log']['rows'],[])
        self.assertEqual(public['log']['computed_analysis'],context['log']['computed_analysis'])
        actual=[(item.get('role'),item.get('content'))for item in body['input'][1:]]
        self.assertIn(('user','Original launch question'),actual)
        self.assertIn(('assistant','Original reply'),actual)
        self.assertIn(('user','User correction remains'),actual)
        self.assertIn(('assistant','Assistant history remains'),actual)
        self.assertEqual(context,before)

    def test_full_audit_without_private_context_keeps_raw_table_and_log_payload(self):
        snap=capture();before=copy.deepcopy(snap)
        body,public=self.request(snap)
        self.assertEqual(public['tables'],snap['tables'])
        self.assertEqual(public['log']['rows'],snap['log']['rows'])
        self.assertNotIn('table_catalog',public)
        self.assertEqual(snap,before)

    def test_tools_disabled_preserves_payload_even_with_private_context(self):
        context=self.context()
        body,public=self.request(context,allow_tools=False)
        self.assertEqual(public['tables'],context['tables'])
        self.assertEqual(public['log']['rows'],context['log']['rows'])
        self.assertNotIn('tools',body)


if __name__=='__main__':unittest.main(verbosity=2)
