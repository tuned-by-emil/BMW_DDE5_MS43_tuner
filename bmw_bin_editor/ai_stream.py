"""Collect Responses SSE output items without executing partial tool calls.

Official event contract:
https://developers.openai.com/api/reference/resources/responses/streaming-events
https://developers.openai.com/api/docs/guides/function-calling

The caller still handles API error codes and connection/cancellation failures.
``feed`` returns a result only after response.completed. A sparse completion
envelope is merged with streamed done items; reasoning from an added event is
never treated as its completed encrypted representation.
"""
from __future__ import annotations
import copy
import json
import re


class ResponsesOutputCollector:
    def __init__(self,max_bytes=12_000_000):
        self._states=[];self._by_index={};self._by_id={};self._by_call={}
        self._events={};self._seen_sequences=set();self._event_count=0
        self._bytes=0;self._max_bytes=max_bytes;self._terminal=False;self._result=None
        self._completion_items=0
        self._anonymous_text=None

    @property
    def diagnostics(self):
        final=self._result.get('output',[]) if self._result else []
        return {'event_count':self._event_count,'event_types':dict(self._events),
                'streamed_item_count':sum(s['streamed'] for s in self._states),
                'streamed_done_item_count':sum(s['done_event'] for s in self._states),
                'completion_item_count':self._completion_items,
                'final_item_count':len(final),'final_item_types':[i.get('type','unknown') for i in final]}

    def _state(self,event,item=None,index=None):
        item=item or {}
        identifier=item.get('id') or event.get('item_id')
        call_id=item.get('call_id') or event.get('call_id')
        index=event.get('output_index',index)
        if type(index) is not int or not 0<=index<=4096:index=None
        state=self._by_id.get(identifier) if identifier else None
        if state is None and call_id:state=self._by_call.get(call_id)
        if state is None and item.get('type')=='message' and self._anonymous_text is not None and not self._anonymous_text['item'].get('id'):
            state=self._anonymous_text
            if index is not None:state['index']=index
        if state is None and index in self._by_index:
            existing=self._by_index[index]
            if not identifier or not existing['item'].get('id') or existing['item'].get('id')==identifier:
                state=existing
        if state is None:
            if len(self._states)>=128:raise ValueError('Too many output items in one response.')
            state={'item':{},'index':index if index is not None else 4097+len(self._states),
                   'order':len(self._states),'arguments':'','arguments_final':None,
                   'parts':{},'done':False,'done_event':False,'reasoning_final':False,'streamed':False}
            self._states.append(state)
        if index is not None:self._by_index.setdefault(index,state)
        if identifier:
            self._by_id[identifier]=state;state['item'].setdefault('id',identifier)
        if call_id:
            self._by_call[call_id]=state;state['item'].setdefault('call_id',call_id)
        return state

    def _merge_item(self,state,item,final=False):
        if not isinstance(item,dict):raise ValueError('Invalid streamed output item.')
        for key,value in item.items():
            if key in {'arguments','content','encrypted_content'}:continue
            if value not in (None,'',[]) or key not in state['item']:
                state['item'][key]=copy.deepcopy(value)
        arguments=item.get('arguments')
        if isinstance(arguments,str) and arguments:
            if final:state['arguments_final']=arguments
            elif not state['arguments']:state['arguments']=arguments
        content=item.get('content')
        if isinstance(content,list) and content:
            if final or not state['item'].get('content'):state['item']['content']=copy.deepcopy(content)
        encrypted=item.get('encrypted_content')
        if isinstance(encrypted,str) and encrypted:
            if final or not state['item'].get('encrypted_content'):state['item']['encrypted_content']=encrypted
        if final:
            state['done']=True
            if item.get('type',state['item'].get('type'))=='reasoning':
                if isinstance(encrypted,str) and encrypted:state['reasoning_final']=True

    def feed(self,event):
        if not isinstance(event,dict):raise ValueError('Invalid Responses event.')
        if self._terminal:
            if self._result is not None and event.get('type')=='response.completed':return copy.deepcopy(self._result)
            raise ValueError('Response stream is already terminal.')
        kind=event.get('type','')
        self._event_count+=1
        label=kind if isinstance(kind,str) and len(kind)<=100 and re.fullmatch(r'(?:response\.[a-z_.]+|error)',kind) else 'other'
        if label not in self._events and len(self._events)>=64:label='other'
        self._events[label]=self._events.get(label,0)+1
        self._bytes+=len(json.dumps(event,ensure_ascii=False,allow_nan=False).encode())
        if self._event_count>100_000 or self._bytes>self._max_bytes:
            raise ValueError('Responses stream exceeds the local collection budget.')
        sequence=event.get('sequence_number')
        if type(sequence) is int:
            if sequence in self._seen_sequences:return None
            self._seen_sequences.add(sequence)
        if kind in {'error','response.failed','response.incomplete'}:
            self._terminal=True;raise ValueError('Response failed or was incomplete; no completed output is available.')
        if kind in {'response.output_item.added','response.output_item.done'}:
            item=event.get('item')
            if not isinstance(item,dict):raise ValueError('Output-item event lacks an item.')
            state=self._state(event,item);state['streamed']=True
            if kind.endswith('.done'):state['done_event']=True
            self._merge_item(state,item,final=kind.endswith('.done'))
        elif kind in {'response.function_call_arguments.delta','response.function_call_arguments.done'}:
            state=self._state(event);state['streamed']=True;state['item'].setdefault('type','function_call')
            for key in ('name','call_id','namespace'):
                if isinstance(event.get(key),str) and event[key]:state['item'][key]=event[key]
            if kind.endswith('.done'):
                if not isinstance(event.get('arguments'),str):raise ValueError('Final tool arguments must be text.')
                state['arguments_final']=event['arguments']
            elif state['arguments_final'] is None:
                delta=event.get('delta','')
                if not isinstance(delta,str):raise ValueError('Tool argument fragment must be text.')
                state['arguments']+=delta
        elif kind in {'response.output_text.delta','response.output_text.done','response.content_part.done'}:
            if not event.get('item_id') and type(event.get('output_index')) is not int:
                if self._anonymous_text is None:self._anonymous_text=self._state(event)
                state=self._anonymous_text
            else:state=self._state(event)
            state['streamed']=True
            state['item'].setdefault('type','message');state['item'].setdefault('role','assistant')
            index=event.get('content_index',0)
            if type(index) is not int or not 0<=index<=1024:raise ValueError('Invalid output-text content index.')
            part=state['parts'].setdefault(index,{'delta':'','final':None,'part':None})
            if kind=='response.content_part.done':
                value=event.get('part',{})
                if isinstance(value,dict):
                    part['part']=copy.deepcopy(value)
                    if value.get('type')=='output_text' and isinstance(value.get('text'),str):part['final']=value['text']
            elif kind.endswith('.done'):
                if isinstance(event.get('text'),str):part['final']=event['text']
            elif isinstance(event.get('delta'),str):part['delta']+=event['delta']
        elif kind=='response.completed':
            response=event.get('response') or {}
            if not isinstance(response,dict) or response.get('status') not in (None,'completed'):
                self._terminal=True;raise ValueError('Response completion did not report success.')
            output=response.get('output') or []
            if not isinstance(output,list):raise ValueError('Completed response output must be an array.')
            self._completion_items=len(output)
            for index,item in enumerate(output):
                if not isinstance(item,dict):raise ValueError('Invalid completed output item.')
                self._merge_item(self._state({},item,index),item,final=True)
            final=[];seen_calls=set();seen_items=set()
            for state in sorted(self._states,key=lambda s:(s['index'],s['order'])):
                item=copy.deepcopy(state['item']);typ=item.get('type')
                if typ=='reasoning' and not state['reasoning_final']:continue
                if typ=='function_call':
                    if not all(isinstance(item.get(k),str) and item[k] for k in ('name','call_id')):
                        raise ValueError('Completed stream lacks complete tool-call identity.')
                    if item['call_id'] in seen_calls:continue
                    seen_calls.add(item['call_id'])
                    item['arguments']=state['arguments_final'] if state['arguments_final'] is not None else state['arguments']
                if typ=='message':
                    if not item.get('content') or not any(p.get('text') for p in item['content'] if isinstance(p,dict)):
                        item['content']=[dict(p['part'] or {},type='output_text',text=p['final'] if p['final'] is not None else p['delta'])
                            for _,p in sorted(state['parts'].items()) if p['final'] is not None or p['delta']]
                if item.get('status')=='in_progress':item['status']='completed'
                if item.get('id') in seen_items:continue
                if item.get('id'):seen_items.add(item['id'])
                if typ:final.append(item)
            text='\n'.join(p.get('text','') for item in final if item.get('type')=='message'
                            for p in item.get('content',[]) if isinstance(p,dict) and p.get('type')=='output_text')
            self._terminal=True;self._result={'text':text,'output':final,'usage':response.get('usage')}
            self._result['stream_diagnostics']=self.diagnostics
            return copy.deepcopy(self._result)
        return None
