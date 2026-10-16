"""Spawn-safe UART simulator; cannot discover or open physical ports."""
import time
from bmw_bin_editor.serial_transport import KlineSession, xor_checksum


class TimedPort:
    def __init__(self, **options):
        self.options = options
        self.is_open = False
        self.buffer = bytearray()
        self.started = self.read_so_far = 0
        self.selected = None
    def open(self):
        self.is_open = True
    def close(self):
        self.is_open = False
    def reset_input_buffer(self):
        self.buffer.clear()
    @property
    def in_waiting(self):
        delivered = int((time.monotonic()-self.started)*9600/11)
        return min(len(self.buffer),max(0,delivered-self.read_so_far))
    def write(self, request):
        payload=request[4:-1]
        KlineSession._admit_payload(payload)
        if payload==b'\x1a\x92':
            response=b'\x5a\x92'+b'0281010565'
        elif payload==b'\x1a\x94':
            response=b'\x5a\x94'+b'1037361830'
        elif payload[:2]==b'\x2c\x10':
            if len(payload)>2:
                self.selected=tuple(int.from_bytes(payload[i:i+2],'big') for i in range(2,len(payload),2))
            if self.selected is None:
                raise AssertionError('No measurement selection.')
            if 0xff in self.selected:
                raise RuntimeError('Simulated cable fault')
            response=b'\x6c\x10'+bytes(len(self.selected)*2)
        body=b'\xb8\xf1\x12'+bytes([len(response)])+response
        self.buffer.extend(request+body+bytes([xor_checksum(body)]))
        self.started=time.monotonic()
        self.read_so_far=0
        return len(request)
    def read(self,length):
        deadline=time.monotonic()+self.options['timeout']
        while self.in_waiting<length and time.monotonic()<deadline:
            time.sleep(.001)
        available=min(length,self.in_waiting)
        data=bytes(self.buffer[:available])
        del self.buffer[:available]
        self.read_so_far+=len(data)
        return data


def timed_session_factory(port, *, echo, cancelled):
    return KlineSession(port,echo=echo,cancelled=cancelled,serial_factory=TimedPort)
