"""Responses SSE item lifecycle and real client-continuation regressions."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
try:
    from bmw_bin_editor.ai_stream import ResponsesOutputCollector
except ImportError:
    spec=importlib.util.spec_from_file_location('staged_ai_stream',Path(__file__).with_name('ai_stream.py'))
    staged=importlib.util.module_from_spec(spec);spec.loader.exec_module(staged)
    ResponsesOutputCollector=staged.ResponsesOutputCollector
from bmw_bin_editor import chatgpt_client as client
from bmw_bin_editor.ai_evidence import EvidenceSession


def item_event(item,index=0,done=False):
    return {'type':'response.output_item.done' if done else 'response.output_item.added','output_index':index,'item':copy.deepcopy(item)}


def completed(output=None):
    return {'type':'response.completed','response':{'status':'completed','output':[] if output is None else output,
        'usage':{'input_tokens':100,'output_tokens':85}}}


def function(identifier='fc_1',call_id='call_1',name='inspect_tables',arguments=''):
    return {'type':'function_call','id':identifier,'call_id':call_id,'namespace':'editor','name':name,'arguments':arguments}


def tool_events():
    args=json.dumps({'query':'','table_ids':[],'offset':0,'limit':1,'include_values':False},separators=(',',':'))
    reason={'type':'reasoning','id':'rs_1','summary':[],'encrypted_content':'PARTIAL_REASONING'}
    final_reason=dict(reason,encrypted_content='FINAL_ENCRYPTED_REASONING')
    call=function()
    events=[item_event(reason,0),item_event(final_reason,0,done=True),item_event(call,1),
        {'type':'response.function_call_arguments.delta','item_id':'fc_1','output_index':1,'delta':args[:25]},
        {'type':'response.function_call_arguments.delta','item_id':'fc_1','output_index':1,'delta':args[25:]},
        {'type':'response.function_call_arguments.done','item_id':'fc_1','output_index':1,'arguments':args},
        item_event(dict(call,arguments=args),1,done=True)]
    return events,final_reason,dict(call,arguments=args)


def stream(events):
    return io.BytesIO(b''.join(('data: '+json.dumps(e)+'\n\n').encode() for e in events))


class CollectorTests(unittest.TestCase):
    def test_sparse_completion_recovers_done_call_and_final_encrypted_reasoning(self):
        collector=ResponsesOutputCollector();events,reason,call=tool_events()
        for event in events:self.assertIsNone(collector.feed(event))
        result=collector.feed(completed())
        self.assertEqual(result['output'],[reason,call]);self.assertEqual(result['text'],'')
        diagnostic=json.dumps(result['stream_diagnostics'])
        self.assertNotIn('ENCRYPTED_REASONING',diagnostic);self.assertNotIn('"query"',diagnostic)
        self.assertEqual(result['stream_diagnostics']['streamed_item_count'],2)
        self.assertEqual(result['stream_diagnostics']['completion_item_count'],0)
        self.assertEqual(result['stream_diagnostics']['final_item_types'],['reasoning','function_call'])

    def test_full_completion_and_repeated_done_do_not_duplicate_calls_or_arguments(self):
        events,reason,call=tool_events();collector=ResponsesOutputCollector()
        for event in events+[events[-1]]:collector.feed(event)
        # Sparse final reasoning entry must not erase its completed encrypted data.
        result=collector.feed(completed([{'type':'reasoning','id':'rs_1','summary':[]},call,copy.deepcopy(call)]))
        self.assertEqual(len(result['output']),2)
        self.assertEqual(result['output'][0]['encrypted_content'],'FINAL_ENCRYPTED_REASONING')
        self.assertEqual(result['output'][1]['arguments'],call['arguments'])

    def test_interleaved_call_fragments_and_arguments_done_are_kept_separately(self):
        collector=ResponsesOutputCollector();a=function();b=function('fc_2','call_2','inspect_log')
        collector.feed(item_event(a,0));collector.feed(item_event(b,1))
        for index,identifier,delta in [(0,'fc_1','{"query":'),(1,'fc_2','{"limit":'),(0,'fc_1','"N75"}'),(1,'fc_2','50}')]:
            collector.feed({'type':'response.function_call_arguments.delta','output_index':index,'item_id':identifier,'delta':delta})
        collector.feed({'type':'response.function_call_arguments.done','output_index':0,'item_id':'fc_1','arguments':'{"query":"N75"}'})
        result=collector.feed(completed())
        self.assertEqual([json.loads(i['arguments']) for i in result['output']],[{'query':'N75'},{'limit':50}])

    def test_text_delta_done_and_full_message_are_not_concatenated_twice(self):
        collector=ResponsesOutputCollector()
        message={'type':'message','id':'msg_1','role':'assistant','content':[],'status':'in_progress'}
        collector.feed(item_event(message))
        collector.feed({'type':'response.output_text.delta','item_id':'msg_1','output_index':0,'content_index':0,'delta':'- Actual '})
        collector.feed({'type':'response.output_text.delta','item_id':'msg_1','output_index':0,'content_index':0,'delta':'answer.'})
        collector.feed({'type':'response.output_text.done','item_id':'msg_1','output_index':0,'content_index':0,'text':'- Actual answer.'})
        final=dict(message,status='completed',content=[{'type':'output_text','text':'- Actual answer.','annotations':[]}])
        collector.feed(item_event(final,done=True));result=collector.feed(completed([final]))
        self.assertEqual(result['text'],'- Actual answer.');self.assertEqual(len(result['output']),1)

    def test_added_reasoning_is_not_replayed_as_final_without_done_content(self):
        collector=ResponsesOutputCollector()
        collector.feed(item_event({'type':'reasoning','id':'rs','encrypted_content':'PARTIAL','summary':[]}))
        collector.feed(item_event(function(arguments='{}'),1,done=True))
        self.assertEqual([i['type'] for i in collector.feed(completed())['output']],['function_call'])

    def test_no_outputs_are_published_before_overall_completion_or_after_failure(self):
        collector=ResponsesOutputCollector()
        self.assertIsNone(collector.feed(item_event(function(arguments='{}'),done=True)))
        with self.assertRaises(ValueError):collector.feed({'type':'response.failed','response':{'error':{'code':'failed'}}})
        self.assertEqual(collector.diagnostics['final_item_count'],0)
        with self.assertRaises(ValueError):collector.feed(completed())

    def test_delta_only_legacy_text_and_done_only_content_are_supported(self):
        collector=ResponsesOutputCollector();collector.feed({'type':'response.output_text.delta','delta':'Legacy '})
        collector.feed({'type':'response.output_text.delta','delta':'text'})
        self.assertEqual(collector.feed({'type':'response.completed'})['text'],'Legacy text')
        collector=ResponsesOutputCollector();collector.feed({'type':'response.output_text.done','text':'Done text','output_index':0})
        self.assertEqual(collector.feed(completed())['text'],'Done text')

    def test_unidentified_text_fragments_merge_with_final_identified_message(self):
        collector=ResponsesOutputCollector()
        collector.feed({'type':'response.output_text.delta','delta':'Check '})
        collector.feed({'type':'response.output_text.delta','delta':'boost.'})
        message={'type':'message','id':'msg_final','role':'assistant','status':'completed',
            'content':[{'type':'output_text','text':'Check boost.','annotations':[]}]}
        result=collector.feed(completed([message]))
        self.assertEqual(result['text'],'Check boost.');self.assertEqual(len(result['output']),1)

    def test_collection_budget_and_missing_tool_identity_fail_closed(self):
        collector=ResponsesOutputCollector(max_bytes=100)
        with self.assertRaises(ValueError):collector.feed(item_event(function(arguments='x'*200)))
        collector=ResponsesOutputCollector();collector.feed(item_event({'type':'function_call','name':'inspect_log','arguments':'{}'}))
        with self.assertRaisesRegex(ValueError,'identity'):collector.feed(completed())

    def test_completion_only_items_are_not_counted_as_streamed_done_events(self):
        result=ResponsesOutputCollector().feed(completed([function(arguments='{}')]))
        self.assertEqual(result['stream_diagnostics']['completion_item_count'],1)
        self.assertEqual(result['stream_diagnostics']['streamed_item_count'],0)
        self.assertEqual(result['stream_diagnostics']['streamed_done_item_count'],0)


class ClientSSETests(unittest.TestCase):
    def setUp(self):
        self.api=client.ChatGPTClient(None);self.cancel=threading.Event()
        self.token=patch.object(self.api,'access_token',return_value='LOCAL_TEST_ONLY');self.token.start()
        self.context={'follow_up':True,'analysis_question':'Inspect captured tables','completed_review':{'initial_findings':'Prior review'},
            'previous_turns':[],'_evidence_snapshot':{'tables':[],'calibration_signature':{'bin':None,'definition':None}}}

    def tearDown(self):self.token.stop()

    def test_actual_stream_response_recovers_sparse_completion_tools_and_reasoning(self):
        events,reason,call=tool_events()
        with patch.object(client,'request',return_value=stream(events+[completed()])):
            result=self.api._stream_response({'model':'LOCAL_TEST_ONLY'},self.cancel)
        self.assertEqual(result['output'],[reason,call])
        self.assertEqual(result['stream_diagnostics']['final_item_types'],['reasoning','function_call'])

    def test_followup_roundtrip_executes_once_after_completion_and_replays_reasoning(self):
        events,reason,call=tool_events()
        answer={'type':'message','id':'msg_answer','role':'assistant','status':'completed',
            'content':[{'type':'output_text','text':'- Captured tables checked.','annotations':[]}]}
        streams=[stream(events+[completed()]),stream([item_event(answer,done=True),completed()])]
        seen=[]
        with patch.object(client,'request',side_effect=streams) as request:
            result=self.api.follow_up('LOCAL_TEST_ONLY',self.context,self.cancel,seen.append)
        self.assertEqual(result,'- Captured tables checked.');self.assertEqual(request.call_count,2)
        inputs=request.call_args.kwargs['body']['input']
        self.assertIn(reason,inputs);self.assertIn(call,inputs)
        outputs=[i for i in inputs if i.get('type')=='function_call_output']
        self.assertEqual(len(outputs),1);self.assertEqual(outputs[0]['call_id'],'call_1')
        self.assertTrue(json.loads(outputs[0]['output'])['ok'])

    def test_interrupted_or_failed_stream_never_dispatches_even_done_tool(self):
        events,_,_=tool_events()
        for ending in ([],[{'type':'response.failed','response':{'error':{'code':'subscription_sharing_usage_limit_exceeded'}}}]):
            with patch.object(client,'request',return_value=stream(events+ending)),patch.object(EvidenceSession,'call') as execute:
                with self.assertRaises(client.ChatGPTError):self.api.follow_up('LOCAL_TEST_ONLY',self.context,self.cancel,lambda _:None)
                self.assertFalse(execute.called);self.assertFalse(self.api.last_proposals)


if __name__=='__main__':unittest.main()
