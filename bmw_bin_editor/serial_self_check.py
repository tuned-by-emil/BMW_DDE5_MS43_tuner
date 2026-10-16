"""Offline portable-release verification: the UART is entirely in memory."""
from .serial_transport import KlineSession, xor_checksum
from .serial_process import ProcessKlineSession


class _MemoryPort:
    def __init__(self, **_):
        self.is_open=False
        self.buffer=bytearray()
        self.selected=None
    def open(self):
        self.is_open=True
    def close(self):
        self.is_open=False
    def reset_input_buffer(self):
        self.buffer.clear()
    @property
    def in_waiting(self):
        return len(self.buffer)
    def read(self,length):
        data=bytes(self.buffer[:length])
        del self.buffer[:length]
        return data
    def write(self,request):
        payload=request[4:-1]
        KlineSession._admit_payload(payload)
        if payload==b'\x1a\x92':
            response=b'\x5a\x92'+b'0281010565'
        elif payload==b'\x1a\x94':
            response=b'\x5a\x94'+b'1037361830'
        else:
            if len(payload)>2:
                self.selected=tuple(int.from_bytes(payload[i:i+2],'big') for i in range(2,len(payload),2))
            if self.selected is None:
                raise RuntimeError('No measurement selection in offline check.')
            response=b'\x6c\x10'+b''.join(channel.to_bytes(2,'big') for channel in self.selected)
        body=b'\xb8\xf1\x12'+bytes([len(response)])+response
        self.buffer.extend(request+body+bytes([xor_checksum(body)]))
        return len(request)


def _memory_session(port, *, echo, cancelled):
    return KlineSession(port,echo=echo,cancelled=cancelled,serial_factory=_MemoryPort)


def run_serial_self_check():
    import os
    session=ProcessKlineSession('COM7',session_factory=_memory_session)
    with session:
        child_pid=session.process.pid
        identity=session.identify()
        full=session.read_measurements([0x2d,0x55])
        cached=session.read_measurements([0x2d,0x55])
        changed=session.read_measurements([0x2d,0x3a])
        correct=(identity==dict(hardware='0281010565',software='1037361830')
            and full.payload==b'\x6c\x10\x00\x2d\x00\x55'
            and not full.cached_selection and cached.cached_selection
            and cached.request_frame[4:-1]==b'\x2c\x10'
            and changed.payload==b'\x6c\x10\x00\x2d\x00\x3a'
            and not changed.cached_selection and child_pid!=os.getpid())
    closed=session.process is None and session.connection is None
    return dict(ok=correct and closed,child_pid=child_pid,separate_process=child_pid!=os.getpid(),
        process_closed=closed,physical_port_opened=False,measurement_replies=3)
