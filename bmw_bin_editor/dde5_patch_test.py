"""Bounded tests for the 361830 read/baud/native-write admission patch.

The ordinary logging transport remains read-only. This separate client has no
raw command console, reset, authentication, flash, token or page-switch command.
The optional four-byte write test requires a USER-verified reservation; source
analysis has not established a safe live scratch address in the stock image.
"""
from __future__ import annotations

import re
import time

from .serial_transport import (KlineSession, SerialCancelled,
    SerialCommunicationError, decode_response, encode_request, validate_port)


PATCH_WORDS = {0x23526: 0xFEFF, 0x2354E: 0xFEFF, 0x23576: 0xFEFF, 0x2358A: 0xFCFF}
SNAPSHOTS = (
    ('execution_mode', 0x3F9856, 1), ('transport_state', 0x3F9CCD, 1),
    ('special_flags', 0x3F9844, 4), ('diagnostic_session_index', 0x3F9840, 1),
    ('directory_ready_and_transaction', 0x3FE540, 2),
    ('application_page_offset', 0x3FE544, 4), ('directory_pointer', 0x3FE54C, 4),
    ('external_ram_present', 0x3FE550, 1), ('internal_allocator_cursor', 0x3FE554, 4),
    ('external_allocator_cursor', 0x3FE558, 4),
    ('main_soi_selector', 0x3FA264, 1), ('main_soi_mode', 0x3FA1D5, 1),
    ('n75_pair_destination', 0x3FA19A, 1), ('n75_ramp_flags', 0x3FDF8C, 1),
    ('alternate_reason', 0x3FA89C, 4), ('n75_weights', 0x3FDF80, 12),
    ('boost_weights', 0x3FDFA0, 12),
)
_READS = {address: size for _, address, size in SNAPSHOTS}
_READS.update({address: 2 for address in PATCH_WORDS})
_NAME = {name: (address, size) for name, address, size in SNAPSHOTS}
_STATE = ('execution_mode', 'transport_state', 'diagnostic_session_index')
_MANAGER = _STATE + ('directory_ready_and_transaction', 'application_page_offset',
                     'directory_pointer', 'internal_allocator_cursor')


def validate_reservation(reservation: dict) -> dict:
    """Validate the user's reservation declaration, not its ECU memory ownership."""
    if not isinstance(reservation, dict):
        raise ValueError('A user-verified RAM reservation is required for the write test.')
    address = reservation.get('address')
    if isinstance(address, str) and re.fullmatch(r'(?:0[xX])?[0-9A-Fa-f]{1,8}', address.strip()):
        address = int(address.strip(), 16)
    if type(address) is not int or address % 4 or not 0x3F9FD4 <= address <= 0x3FA174:
        raise ValueError('Reserve exactly four aligned bytes within 0x3F9FD4–0x3FA177.')
    reference = reservation.get('reference')
    if not isinstance(reference, str):
        raise ValueError('Describe the evidence that establishes exclusive ownership of the reserved bytes.')
    reference = ' '.join(reference.split())
    letters = {c.lower() for c in reference if c.isalnum()}
    if (not 8 <= len(reference) <= 500 or len(letters) < 3
            or reference.lower() in {'reserved', 'verified', 'test only', 'unknown', 'not sure'}):
        raise ValueError('Give a meaningful reservation reference, such as the reviewed memory-map or bench record.')
    if reservation.get('confirmed') is not True:
        raise ValueError('Confirm that you verified exclusive ownership of these four bytes on this ECU.')
    return dict(address=address, reference=reference, confirmed=True, bytes=4,
                ownership='user_verified_not_software_verified')


class _PatchSession(KlineSession):
    def __init__(self, *args, mode, report, reservation=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.mode, self.report, self.reservation = mode, report, reservation
        self.allowed_write_data = set()
        self.defer_cancellation = False
        self.cancellation_requested = False
        self.last_negative = None
        self.active_frame = None

    def _check_cancelled(self):
        if self.cancelled():
            self.cancellation_requested = True
            if self.defer_cancellation:
                return
            self.faulted = True
            self.selected_ids = None
            raise SerialCancelled('Serial read cancelled.')

    def close(self):
        try:
            super().close()
            self.report['details']['port_closed'] = True
        except Exception:
            self.report['details']['port_closed'] = False
            raise

    def _admit_payload(self, payload):
        if payload in (b'\x1A\x92', b'\x1A\x94'):
            return
        if self.identity is None:
            raise SerialCommunicationError('Identify the supported ECU before patch-test requests.')
        if isinstance(payload, bytes) and len(payload) == 5 and payload[0] == 0x23:
            address, count = int.from_bytes(payload[1:4], 'big'), payload[4]
            if _READS.get(address) == count:
                return
            if self.reservation and address == self.reservation['address'] and count == 4:
                return
        if self.mode in ('baud', 'write') and payload in (b'\x10\x86\x10',):
            return
        if self.mode == 'baud' and payload in (b'\x10\x86\x84', b'\x10\x86\x87'):
            return
        if (self.mode == 'write' and self.reservation and len(payload) == 9
                and payload[:1] == b'\x3D' and payload[1:4] == self.reservation['address'].to_bytes(3, 'big')
                and payload[4] == 4 and payload[5:] in self.allowed_write_data):
            return
        raise SerialCommunicationError('Request is outside the bounded DDE5 patch-test contract.')

    def _validate_response(self, payload, response):
        if payload[0] == 0x23:
            if len(response) != 1 + payload[4] or response[:1] != b'\x63':
                raise SerialCommunicationError('RAM/code read response has an unexpected service or byte count.')
        elif payload[0] == 0x3D:
            if response != b'\x7D':
                raise SerialCommunicationError('RAM write response must be the single byte 7D.')
        elif payload[0] == 0x10:
            if response != b'\x50' + payload[1:]:
                raise SerialCommunicationError('Baud acknowledgement does not exactly match the requested mode and rate.')
        else:
            super()._validate_response(payload, response)

    def _receive_frame(self, timeout):
        header = self._read_exact(4, timeout)
        if header[:3] != b'\xB8\xF1\x12' or header[3] == 0:
            raise SerialCommunicationError('Unexpected K-line response header or ECU address.')
        frame = header + self._read_exact(header[3] + 1, self.INTERBYTE_TIMEOUT)
        if self.active_frame is not None:
            self.active_frame['response_frames'].append(frame.hex(' ').upper())
        response = decode_response(frame)
        if response[:1] == b'\x7F':
            self.last_negative = response
        return frame, response

    def _exchange(self, payload):
        self._admit_payload(payload)
        entry = dict(payload=payload.hex(' ').upper(), request_frame=encode_request(payload).hex(' ').upper(),
                     host_baud=int(self.connection.baudrate) if self.connection else 9600,
                     response_frames=[], status='attempted')
        self.report['frames'].append(entry)
        self.active_frame, self.last_negative = entry, None
        try:
            reply = super()._exchange(payload)
            entry['status'] = 'passed'
            return reply
        except Exception as error:
            entry.update(status='failed', error=str(error))
            raise
        finally:
            self.active_frame = None

    def read(self, address, count):
        return self._exchange(b'\x23' + address.to_bytes(3, 'big') + bytes([count])).payload[1:]

    def snapshot(self, names):
        values = {}
        for name in names:
            address, size = _NAME[name]
            raw = self.read(address, size)
            values[name] = dict(address=f'0x{address:06X}', bytes=size,
                                hex=raw.hex().upper(), value=int.from_bytes(raw, 'big'))
        return values

    def begin_cleanup(self):
        # Honour cancellation during the regeneration wait, before mutation.
        self._require_open()
        self._wait_regenerate()
        self._check_cancelled()
        self.defer_cancellation = True

    def write_reserved(self, value):
        return self._exchange(b'\x3D' + self.reservation['address'].to_bytes(3, 'big') + b'\x04' + value)


def _inactive(values):
    if values['execution_mode']['value'] != 5 or values['transport_state']['value'] != 0:
        raise SerialCommunicationError('This test requires the normal application and diagnostic transport state 0.')
    if (values['directory_ready_and_transaction']['hex'] != '0000'
            or values['application_page_offset']['value'] != 0):
        raise SerialCommunicationError('Calibration manager must be uninitialized and idle with the reference page selected.')


def _known_state(session):
    state = session.snapshot(_STATE)
    if (state['execution_mode']['value'] != 5 or state['transport_state']['value'] != 0
            or state['diagnostic_session_index']['value'] != 3):
        raise SerialCommunicationError('The ECU did not retain the expected normal application/session-86 state.')
    return state


def _baud_test(session, report, rate, step):
    code = 0x84 if rate == 115200 else 0x87
    step(f'Requesting session 86 at {rate} host baud; waiting for the complete old-rate acknowledgement.')
    session.begin_cleanup()
    current = 'unknown'
    restore = report['restore']
    restore['baud'] = 'required'
    failure = None
    try:
        try:
            session._exchange(bytes((0x10, 0x86, code)))
        except Exception:
            negative = session.last_negative
            if negative and len(negative) == 3 and negative[:2] == b'\x7F\x10' and negative[2] != 0x78:
                current, restore['baud'] = 'baseline', 'unchanged_rejected'
            raise
        restore['session'] = '86_left_active'
        session.connection.baudrate = rate
        current = 'high'
        step('Complete positive acknowledgement received; host rate changed.')
        report['details']['high_rate_state'] = _known_state(session)
    except Exception as error:
        failure = error
    finally:
        if current == 'high' and not session.faulted:
            try:
                step('Restoring nominal 9600 baud; waiting for the complete high-rate acknowledgement.')
                current = 'unknown'
                session._exchange(b'\x10\x86\x10')
                session.connection.baudrate = 9600
                report['details']['restored_rate_state'] = _known_state(session)
                current, restore['baud'] = 'baseline', 'verified'
                step('Baseline baud restored and verified.')
            except Exception as error:
                failure = error
        if current != 'baseline':
            restore.update(baud='unknown', power_cycle_required=True, session='unknown')
    if failure:
        raise failure


def _write_test(session, report, reservation, step):
    step('Entering session 86 at nominal 9600; no high-rate request is sent.')
    session._check_cancelled()
    report['restore']['session'] = 'unknown'
    try:
        session._exchange(b'\x10\x86\x10')
    except Exception:
        negative = session.last_negative
        if negative and len(negative) == 3 and negative[:2] == b'\x7F\x10' and negative[2] != 0x78:
            report['restore']['session'] = 'unchanged'
        else:
            report['restore']['power_cycle_required'] = True
        raise
    report['restore']['session'] = '86_left_active'
    state = session.snapshot(_MANAGER)
    report['details']['pre_write_state'] = state
    _inactive(state)
    if state['internal_allocator_cursor']['value'] not in (0, 0x3F9FD4):
        raise SerialCommunicationError('The internal pool has allocations; this test requires an unused manager pool.')
    if state['diagnostic_session_index']['value'] != 3:
        raise SerialCommunicationError('Session 86 was not established.')
    original = session.read(reservation['address'], 4)
    pattern = bytes.fromhex('A55AC33C')
    if original == pattern:
        pattern = bytes.fromhex('5AA53CC3')
    session.allowed_write_data = {original, pattern}
    report['details']['write_test'] = dict(address_hex=f"0x{reservation['address']:06X}",
        original_hex=original.hex().upper(), pattern_hex=pattern.hex().upper(), bytes=4,
        reservation=reservation, write_attempted=False)
    step('Writing four test bytes to the user-reserved address; cancellation is deferred until restoration finishes.')
    session.begin_cleanup()
    failure = None
    try:
        report['details']['write_test']['write_attempted'] = True
        report['restore']['write'] = 'required'
        session.write_reserved(pattern)
        actual = session.read(reservation['address'], 4)
        if actual != pattern:
            raise SerialCommunicationError('RAM test-pattern readback did not match.')
        report['details']['write_test']['pattern_verified'] = True
    except Exception as error:
        failure = error
    finally:
        if not session.faulted:
            try:
                step('Restoring the original four bytes and verifying readback.')
                session.write_reserved(original)
                if session.read(reservation['address'], 4) != original:
                    raise SerialCommunicationError('Original RAM bytes were not verified after restoration.')
                report['restore']['write'] = 'verified'
                step('Original RAM bytes restored and verified.')
            except Exception as error:
                failure = error
        if report['restore']['write'] != 'verified':
            report['restore'].update(write='unknown', restoration_required=True, power_cycle_required=True)
    if failure:
        raise failure


def run_patch_check(port, *, mode='read', rate=115200, echo=True, reservation=None,
                    cancelled=None, progress=None, serial_factory=None,
                    clock=time.monotonic, sleep=time.sleep):
    """Return a JSON report; the injected factory is used by offline UART tests.

    Read mode never enters86 or writes. Baud mode never sends3D. Write mode
    permits only one user-reserved four-byte location and restores its original
    bytes on a healthy connection. A faulted/ambiguous link is never retried.
    """
    report = dict(format='BMWBinEditor-DDE5-361830-patch-check-v1', mode=mode if isinstance(mode, str) else str(mode),
        status='failed', error=None, details={}, steps=[], frames=[],
        restore=dict(baud='not_needed', write='not_needed', session='unchanged',
                     power_cycle_required=False, restoration_required=False))
    session = None
    def step(message):
        report['steps'].append(message)
        if progress:
            try:
                progress(message)
            except Exception:
                pass  # A UI callback must never interrupt restoration.
    try:
        port = validate_port(port)
        if mode not in ('read', 'baud', 'write'):
            raise ValueError('Choose read, baud or write patch-test mode.')
        if type(rate) is not int or rate not in (115200, 125000):
            raise ValueError('Choose host baud 115200 or 125000.')
        if type(echo) is not bool:
            raise ValueError('Cable echo must be enabled or disabled.')
        if mode == 'write':
            reservation = validate_reservation(reservation)
            report['details']['reservation'] = reservation
        elif reservation is not None:
            raise ValueError('A RAM reservation is used only in write-test mode.')
        report['port'] = port
        report['details']['requested_host_baud'] = rate if mode == 'baud' else 9600
        report['details']['nominal_ecu_wire_baud'] = (113636 if rate == 115200 else 125000) if mode == 'baud' else 9615
        session = _PatchSession(port, mode=mode, report=report, reservation=reservation,
            echo=echo, cancelled=cancelled, serial_factory=serial_factory, clock=clock, sleep=sleep)
        with session:
            step('Identifying Bosch hardware and software at 9600 baud.')
            report['details']['identity'] = session.identify()
            words = {}
            for address, expected in PATCH_WORDS.items():
                actual = int.from_bytes(session.read(address, 2), 'big')
                words[f'0x{address:06X}'] = f'{actual:04X}'
                if actual != expected:
                    raise SerialCommunicationError(f'Patch word at 0x{address:06X} is {actual:04X}; expected {expected:04X}.')
            report['details']['patch_words'] = words
            step('Live admission words match the combined read/baud/native-write patch; this is not a full-image comparison.')
            names = tuple(name for name, _, _ in SNAPSHOTS)
            state = session.snapshot(names)
            report['details']['baseline_snapshot'] = state
            if mode == 'read':
                step('Read-only patch and selector checks completed; no session or write request was sent.')
            else:
                _inactive(state)
                if mode == 'baud':
                    _baud_test(session, report, rate, step)
                else:
                    if state['internal_allocator_cursor']['value'] not in (0, 0x3F9FD4):
                        raise SerialCommunicationError('The internal pool has allocations; this test requires an unused manager pool.')
                    _write_test(session, report, reservation, step)
            if session.cancellation_requested or (cancelled and cancelled()):
                raise SerialCancelled('Patch test cancelled; any required healthy-link restoration completed first.')
            report['status'] = 'passed'
    except SerialCancelled as error:
        report.update(status='cancelled', error=str(error))
    except Exception as error:
        report.update(status='failed', error=str(error))
    return report
