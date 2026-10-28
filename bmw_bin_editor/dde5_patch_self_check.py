"""Packaged patch-test smoke check using generated RAM and a virtual UART only."""
from pathlib import Path
import json
import os

from .dde5_patch_test import PATCH_WORDS, run_patch_check
from .serial_transport import xor_checksum


class _Clock:
    def __init__(self):
        self.now = 0.0
    def __call__(self):
        return self.now
    def sleep(self, seconds):
        self.now += seconds


class _MemoryPort:
    """No OS device exists: every byte is held in this Python object's memory."""
    def __init__(self, **options):
        self.is_open = False
        self.buffer = bytearray()
        self.memory = {}
        self._baud = options['baudrate']
        self.pending_rate = None
        self.writes = []
        for address, word in PATCH_WORDS.items():
            self.put(address, word.to_bytes(2, 'big'))
        self.put(0x3F9856, b'\x05')
        self.put(0x3F9844, b'\x00\x00\x00\x01')
        self.put(0x3FA174, bytes.fromhex('12345678'))

    def put(self, address, data):
        self.memory.update({address+i: value for i, value in enumerate(data)})
    def get(self, address, count):
        return bytes(self.memory.get(address+i, 0) for i in range(count))
    def open(self):
        self.is_open = True
    def close(self):
        self.is_open = False
    def reset_input_buffer(self):
        self.buffer.clear()
    @property
    def in_waiting(self):
        return len(self.buffer)
    @property
    def baudrate(self):
        return self._baud
    @baudrate.setter
    def baudrate(self, rate):
        if self.buffer:
            raise AssertionError('Host rate changed before the complete acknowledgement was consumed.')
        if self.pending_rate is not None and self.pending_rate != rate:
            raise AssertionError('Host rate differs from the acknowledged request.')
        self._baud = rate
        self.pending_rate = None
    def read(self, count):
        result = bytes(self.buffer[:count])
        del self.buffer[:count]
        return result
    def write(self, request):
        assert xor_checksum(request) == 0
        payload = request[4:-1]
        self.writes.append(payload)
        if payload == b'\x1A\x92':
            response = b'\x5A\x92' + b'0281010565'
        elif payload == b'\x1A\x94':
            response = b'\x5A\x94' + b'1037361830'
        elif len(payload) == 5 and payload[0] == 0x23:
            response = b'\x63' + self.get(int.from_bytes(payload[1:4], 'big'), payload[4])
        elif payload in (b'\x10\x86\x84', b'\x10\x86\x87', b'\x10\x86\x10'):
            response = b'\x50' + payload[1:]
            self.pending_rate = {0x84: 115200, 0x87: 125000, 0x10: 9600}[payload[2]]
            self.put(0x3F9840, b'\x03')
            self.put(0x3FE554, (0x3F9FD4).to_bytes(4, 'big'))
        elif len(payload) == 9 and payload[:5] == bytes.fromhex('3D3FA17404'):
            self.put(0x3FA174, payload[5:])
            response = b'\x7D'
        else:
            raise AssertionError('Unexpected synthetic patch-check request.')
        body = b'\xB8\xF1\x12' + bytes([len(response)]) + response
        self.buffer.extend(request + body + bytes([xor_checksum(body)]))
        return len(request)


def run_check(path):
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication
    from .dde5_patch_test_gui import Dde5PatchTestDialog
    from . import __version__
    app = QApplication.instance() or QApplication([])
    dialog = Dde5PatchTestDialog()
    dialog.show()
    app.processEvents()
    gui_ok = (dialog.tabs.count() == 3 and not dialog.address.text()
              and not dialog.write_button.isEnabled() and dialog.worker is None)
    dialog.close()
    results = {}
    for mode in ('read', 'baud', 'write'):
        clock = _Clock()
        ports = []
        def factory(**options):
            port = _MemoryPort(**options)
            ports.append(port)
            return port
        reservation = (dict(address=0x3FA174, reference='Synthetic exclusive RAM fixture; no physical ECU',
                            confirmed=True) if mode == 'write' else None)
        report = run_patch_check('COM7', mode=mode, reservation=reservation,
                                 serial_factory=factory, clock=clock, sleep=clock.sleep)
        port = ports[0]
        write_commands = sum(payload[0] == 0x3D for payload in port.writes)
        ok = report['status'] == 'passed' and not port.is_open
        if mode in ('read', 'baud'):
            ok = ok and write_commands == 0
        if mode == 'read':
            ok = ok and all(payload[0] != 0x10 for payload in port.writes)
        if mode == 'baud':
            ok = ok and port.baudrate == 9600 and report['restore']['baud'] == 'verified'
        if mode == 'write':
            ok = (ok and write_commands == 2 and report['restore']['write'] == 'verified'
                  and port.get(0x3FA174, 4) == bytes.fromhex('12345678'))
        results[mode] = dict(ok=ok, report=report)
    output = dict(ok=gui_ok and all(row['ok'] for row in results.values()),
                  version=__version__, gui_no_automatic_connection=gui_ok,
                  physical_port_opened=False, network_used=False, modes=results)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(output, indent=2) + '\n', encoding='utf-8')
    return output['ok']
