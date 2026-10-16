"""Restricted decoder for the bundled MS43X001 ADX and its DS2 data reply.

Packet offsets are relative to the 73-byte data payload, excluding header/XOR.
Per-value flag 0x02 specifies little-endian words (the RPM/gear overlay confirms
this layout); header defaults apply to values without an explicit flags field.
No eval, command execution or arbitrary ADX expressions are allowed.
"""
from __future__ import annotations
import ast
import hashlib
import math
import operator
from pathlib import Path
import xml.etree.ElementTree as ET
from .adx import bundled_ms43_adx_path, load_adx_catalog
from .serial_transport import SerialCommunicationError, xor_checksum

DATA_REQUEST = bytes.fromhex('12050BB0AC')
FAST_REQUEST = bytes.fromhex('12089101E848002A')
SLOW_REQUEST = bytes.fromhex('120891002580002E')
ACK = bytes.fromhex('1204A0B6')
DATA_HEADER = bytes.fromhex('124DA0')
DATA_SIZE = 73
FRAME_SIZE = 77
OPS = {ast.Add:operator.add, ast.Sub:operator.sub, ast.Mult:operator.mul,
       ast.Div:operator.truediv, ast.BitAnd:lambda a,b:int(a)&int(b)}


def compile_expression(expression, names):
    tree = ast.parse(expression, mode='eval')
    def check(node):
        if isinstance(node,ast.Expression): check(node.body)
        elif isinstance(node,ast.Constant) and type(node.value) in (int,float): pass
        elif isinstance(node,ast.Name) and node.id in names: pass
        elif isinstance(node,ast.BinOp) and type(node.op) in OPS:
            check(node.left); check(node.right)
        elif isinstance(node,ast.UnaryOp) and isinstance(node.op,(ast.USub,ast.UAdd)): check(node.operand)
        else: raise ValueError('Unsupported ADX expression: '+expression)
    check(tree)
    def evaluate(values):
        def visit(node):
            if isinstance(node,ast.Expression):return visit(node.body)
            if isinstance(node,ast.Constant):return node.value
            if isinstance(node,ast.Name):return values[node.id]
            if isinstance(node,ast.BinOp):return OPS[type(node.op)](visit(node.left),visit(node.right))
            return -visit(node.operand) if isinstance(node.op,ast.USub) else visit(node.operand)
        try: return float(visit(tree))
        except (ArithmeticError,ValueError,OverflowError):return math.nan
    return evaluate


class MS43Definition:
    def __init__(self, path=None):
        self.path = Path(path or bundled_ms43_adx_path())
        raw = self.path.read_bytes()
        self.sha256 = hashlib.sha256(raw).hexdigest()
        root = ET.fromstring(raw)
        if root.tag != 'ADXFORMAT' or 'MS43X001' not in root.findtext('ADXHEADER/desc',''):
            raise ValueError('This logger requires the MS43X001 ADX.')
        commands={n.get('id'):bytes.fromhex(n.findtext('bytestring','')) for n in root.findall('ADXCSENDCOMMAND')}
        if (commands.get('DATAREQUEST'),commands.get('125000BAUD'),commands.get('9600BAUD')) != (DATA_REQUEST,FAST_REQUEST,SLOW_REQUEST):
            raise ValueError('Unsupported MS43X ADX command sequence.')
        response = next((n for n in root.findall('ADXCLISTENPACKET') if n.get('id')=='DATAREPLY'),None)
        if response is None or bytes.fromhex(response.findtext('headerstring','')) != DATA_HEADER:
            raise ValueError('Unexpected MS43X ADX packet header.')
        if int(response.findtext('packetsize','0')) != DATA_SIZE:
            raise ValueError('Unsupported MS43X ADX packet size.')
        self.signal_catalog, self.styles = load_adx_catalog(self.path, include_states=True)
        self.nodes={n.get('id'):n for n in root if n.tag in ('ADXVALUE','ADXBITMASK')}
        self.hashes={int(n.get('idhash'),0):n.get('id') for n in root if n.get('idhash') and n.tag in ('ADXVALUE','ADXBITMASK','ADXLOOKUPTABLE')}
        self.lookups={int(n.get('idhash'),0):{float(e.get('input')):float(e.get('output')) for e in n.findall('tableentry')}
                      for n in root.findall('ADXLOOKUPTABLE') if n.findtext('lookupmode')=='3'}
        self.compiled={}
        self.links={}
        self.catalog={}
        defaults=root.find('ADXHEADER/DEFAULTS')
        self.layout={}
        for ident,n in self.nodes.items():
            offset=int(n.findtext('packetoffset','0'),0)
            bits=int(n.findtext('sizeinbits',defaults.get('datasizeinbits','8')))
            if bits not in (8,16):raise ValueError('Unsupported ADX value width.')
            width=bits//8
            if offset<0 or offset+width>DATA_SIZE:raise ValueError('ADX value outside MS43X data packet.')
            flags=n.findtext('flags')
            little = bool(int(flags,0)&2) if flags is not None else defaults.get('lsbfirst')=='1'
            self.layout[ident]=(offset,width,'little' if little else 'big')
            if n.tag=='ADXBITMASK':
                if n.findtext('bitop')!='AND':raise ValueError('Unsupported ADX digital-state operation.')
                if int(n.findtext('operand','0'),0)>255:raise ValueError('Unsupported ADX digital-state width.')
            else:
                math_node=n.find('MATH')
                if math_node is None:raise ValueError('Missing ADX conversion.')
                variables={v.get('varID'):v for v in math_node.findall('VAR')}
                links={}
                for name,v in variables.items():
                    if v.get('type')=='link':
                        target=self.hashes.get(int(v.get('linkIDHash'),0))
                        if target not in self.nodes:raise ValueError('Unresolved ADX linked value.')
                        links[name]=target
                    elif v.get('type')!='native':raise ValueError('Unsupported ADX variable type.')
                self.links[ident]=links
                self.compiled[ident]=compile_expression(math_node.get('equation','X'),set(variables))
                lookup=math_node.get('lookupidhash')
                if lookup and int(lookup,0) not in self.lookups:raise ValueError('Unresolved ADX lookup table.')
            self.catalog[ident]={'arg':ident,'result':ident,'label':self.signal_catalog.label_for(ident),
                'unit':self.signal_catalog.unit_for(ident),'offset':offset,'bits':bits,
                'description':n.findtext('desc',n.get('title',''))}
        # Validate dependency cycles before opening a cable.
        seen=set()
        def check(ident,stack):
            if ident in stack:raise ValueError('Cyclic ADX value links.')
            if ident in seen:return
            for target in self.links.get(ident,{}).values():check(target,stack|{ident})
            seen.add(ident)
        for ident in self.nodes:check(ident,set())

    def decode_frame(self,frame):
        if len(frame)!=FRAME_SIZE or frame[:3]!=DATA_HEADER:
            raise SerialCommunicationError('Unexpected MS43X response header/length; expected 12 4D A0 and 77 bytes.')
        if xor_checksum(frame):raise SerialCommunicationError('MS43X response XOR checksum mismatch.')
        return self.decode_payload(frame[3:-1])

    def decode_payload(self,payload):
        if len(payload)!=DATA_SIZE:raise SerialCommunicationError('MS43X data payload must contain 73 bytes.')
        cache={}
        def value(ident):
            if ident in cache:return cache[ident]
            node=self.nodes[ident]
            offset,width,endian=self.layout[ident]
            raw=int.from_bytes(payload[offset:offset+width],endian)
            if node.tag=='ADXBITMASK':
                result=float((raw & int(node.findtext('operand','0'),0)) == int(node.findtext('result','0'),0))
            else:
                variables={v.get('varID'):raw for v in node.findall('MATH/VAR') if v.get('type')=='native'}
                variables.update({name:value(target) for name,target in self.links[ident].items()})
                result=self.compiled[ident](variables)
                lookup=node.find('MATH').get('lookupidhash')
                if lookup:
                    # All bundled lookups are discrete state selectors. Unknown
                    # keys remain unavailable rather than extrapolating a state.
                    result=self.lookups[int(lookup,0)].get(result,math.nan)
            cache[ident]=result
            return result
        return {ident:value(ident) for ident in self.nodes}
