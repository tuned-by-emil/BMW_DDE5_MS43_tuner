"""Engine-off, bounded native RAM calibration experiments for DDE5 361830.

No flash programming or arbitrary command interface. The existing firmware's
mapped byte-sum routine supplies slow, exact single-byte working-page reads.
All mutable experiments end by selecting and checking the reference page.
"""
from __future__ import annotations

from bisect import bisect_right
import hashlib
import re
import time

from .dde5_patch_test import PATCH_WORDS, _PatchSession, _READS
from .serial_transport import (SerialCancelled, SerialCommunicationError,
                               SerialReply, encode_request, validate_port)


# Original 361830 MPC directory: 279 blocks and its exclusive final boundary.
BOUNDARIES = (
    0x8e057c, 0x8e0584, 0x8e05ac, 0x8e05f4, 0x8e0ae0, 0x8e0b2a, 0x8e0b34, 0x8e0b38,
    0x8e0b7c, 0x8e0b94, 0x8e0bd0, 0x8e0bec, 0x8e11fc, 0x8e2378, 0x8e27f8, 0x8e29d4,
    0x8e2c30, 0x8e30c2, 0x8e30f0, 0x8e317c, 0x8e3220, 0x8e325c, 0x8e32cc, 0x8e3544,
    0x8e35fc, 0x8e3640, 0x8e3690, 0x8e3798, 0x8e379c, 0x8e37c0, 0x8e3848, 0x8e3894,
    0x8e38ac, 0x8e38b0, 0x8e3914, 0x8e3948, 0x8e3962, 0x8e3964, 0x8e3984, 0x8e3998,
    0x8e399c, 0x8e39a8, 0x8e39aa, 0x8e39b4, 0x8e3a08, 0x8e3a0c, 0x8e3a18, 0x8e3a20,
    0x8e3a24, 0x8e3a28, 0x8e3ba4, 0x8e3be8, 0x8e3bea, 0x8e3c04, 0x8e3c0c, 0x8e3c6c,
    0x8e3c74, 0x8e3c80, 0x8e3c98, 0x8e3cb6, 0x8e3ce4, 0x8e423c, 0x8e4260, 0x8e4292,
    0x8e433c, 0x8e43b0, 0x8e43b2, 0x8e43b4, 0x8e4508, 0x8e45c0, 0x8e4b80, 0x8e5140,
    0x8e51f8, 0x8e5928, 0x8e5bee, 0x8e5d5e, 0x8e5ece, 0x8e61ae, 0x8e6266, 0x8e626c,
    0x8e62d0, 0x8e62f4, 0x8e6314, 0x8e6350, 0x8e6352, 0x8e6398, 0x8e639e, 0x8e6442,
    0x8e66a8, 0x8e6704, 0x8e6726, 0x8e673e, 0x8e6778, 0x8e6800, 0x8e6a48, 0x8e6f44,
    0x8e6f46, 0x8e718a, 0x8e718e, 0x8e71b8, 0x8e71c0, 0x8e71c8, 0x8e7260, 0x8e7378,
    0x8e7490, 0x8e74e0, 0x8e7cc8, 0x8e7f0c, 0x8e8060, 0x8e806c, 0x8e8074, 0x8e808c,
    0x8e8092, 0x8e8098, 0x8e8774, 0x8e90cc, 0x8e91e8, 0x8e9254, 0x8e9256, 0x8e92a0,
    0x8e9370, 0x8e9378, 0x8e93d4, 0x8e9704, 0x8e970c, 0x8e9738, 0x8e98d0, 0x8e9a5e,
    0x8e9afc, 0x8e9b9c, 0x8e9ba8, 0x8e9bb0, 0x8e9c14, 0x8e9c78, 0x8e9c7a, 0x8e9c92,
    0x8e9e22, 0x8ea0ee, 0x8ea0f4, 0x8ea440, 0x8ea758, 0x8ea7c8, 0x8eb86a, 0x8ec602,
    0x8ec6ce, 0x8ec6e4, 0x8ec6f0, 0x8ed000, 0x8ed770, 0x8edbf8, 0x8edee0, 0x8ee652,
    0x8ee8e2, 0x8ee94c, 0x8ee958, 0x8ef3ba, 0x8ef622, 0x8ef756, 0x8ef78a, 0x8ef8be,
    0x8f0656, 0x8f0f66, 0x8f10ba, 0x8f19ca, 0x8f1e7c, 0x8f1e94, 0x8f23c2, 0x8f26f6,
    0x8f2794, 0x8f2840, 0x8f2f0c, 0x8f3394, 0x8f38a4, 0x8f394e, 0x8f3dd6, 0x8f42a0,
    0x8f44f8, 0x8f4504, 0x8f474c, 0x8f4994, 0x8f4bd8, 0x8f4e1c, 0x8f4f66, 0x8f5050,
    0x8f50b0, 0x8f52f6, 0x8f52fa, 0x8f52fe, 0x8f6d58, 0x8f6d8e, 0x8f6fd6, 0x8f6fdc,
    0x8f7140, 0x8f7164, 0x8f71d8, 0x8f74b8, 0x8f74c8, 0x8f75a4, 0x8f75d4, 0x8f76e8,
    0x8f76ee, 0x8f76f0, 0x8f772c, 0x8f7734, 0x8f7760, 0x8f7784, 0x8f77b0, 0x8f77c8,
    0x8f7830, 0x8f7840, 0x8f7a84, 0x8f7ac0, 0x8f90fc, 0x8f9d38, 0x8fa6bc, 0x8fa6c0,
    0x8fa7dc, 0x8fa7f6, 0x8fa822, 0x8fa828, 0x8fa8b4, 0x8fafc4, 0x8fb094, 0x8fb0a8,
    0x8fb0ac, 0x8fb130, 0x8fb16c, 0x8fb6c0, 0x8fb710, 0x8fb756, 0x8fb768, 0x8fb77e,
    0x8fb7b4, 0x8fb7f8, 0x8fb800, 0x8fba5a, 0x8fbc72, 0x8fbc92, 0x8fbed6, 0x8fbf68,
    0x8fc07e, 0x8fc2a0, 0x8fc2a6, 0x8fc6f0, 0x8fc98c, 0x8fcbd0, 0x8fcbdc, 0x8fd07e,
    0x8fd41a, 0x8fda90, 0x8fdb5c, 0x8fdb7c, 0x8fdba4, 0x8fdbac, 0x8fdbb6, 0x8fdbc0,
    0x8fdbd0, 0x8fdbd2, 0x8fdbda, 0x8fdbf6, 0x8fdc0a, 0x8fdc1e, 0x8fdc22, 0x8fdc8a,
    0x8fded4, 0x8fdedc, 0x8fdf00, 0x8fdf70, 0x8fdf7c, 0x8fdf90, 0x8fdf9a, 0x8fdfa4,
    0x8fdfbc, 0x8fe044, 0x8fe048, 0x8fe054, 0x8fe05c, 0x8fe0e4, 0x8fe10c, 0x8fe110,
)
DIRECTORY_ADDRESS = 0x27250
DIRECTORY_BYTES = 1116
EXTERNAL_BASE = 0x6F0000
STOCK_POOL_BYTES = 0xF000
FORMAT = 'BMWBinEditor-DDE5-361830-RAM-plan-v1'
_STATE_NAMES = ('execution_mode', 'transport_state', 'diagnostic_session_index',
                'directory_ready_and_transaction', 'application_page_offset',
                'directory_pointer', 'external_ram_present',
                'internal_allocator_cursor', 'external_allocator_cursor')


def _capacity(value):
    if type(value) is not int or value not in (32768, 65536):
        raise ValueError('Choose verified 32 KiB or 64 KiB external RAM capacity.')
    return value


def validate_hardware(hardware):
    if not isinstance(hardware, dict):
        raise ValueError('Verified external RAM hardware is required.')
    capacity = _capacity(hardware.get('capacity_bytes'))
    reference = hardware.get('reference')
    if not isinstance(reference, str):
        raise ValueError('Give the wiring, bank configuration and capacity-test evidence.')
    reference = ' '.join(reference.split())
    if not 8 <= len(reference) <= 500 or len(set(re.findall('[a-z0-9]', reference.lower()))) < 3:
        raise ValueError('Give a meaningful hardware verification reference (8–500 characters).')
    if hardware.get('confirmed') is not True:
        raise ValueError('Confirm the SRAM wiring, bank configuration and capacity were verified.')
    return dict(capacity_bytes=capacity, reference=reference, confirmed=True,
                ownership='user_verified_not_software_verified')


def _span_blocks(address, count):
    if (type(address) is not int or type(count) is not int or count <= 0
            or address < BOUNDARIES[0] or address + count > BOUNDARIES[-1]):
        raise ValueError('The entire table must lie inside the actual 361830 calibration directory.')
    first = bisect_right(BOUNDARIES, address) - 1
    last = bisect_right(BOUNDARIES, address + count - 1) - 1
    return [dict(index=i, logical_start=BOUNDARIES[i], logical_end_exclusive=BOUNDARIES[i+1],
                 bytes=BOUNDARIES[i+1]-BOUNDARIES[i]) for i in range(first, last+1)]


def build_ram_plan(bin_data, definition, *, capacity_bytes=32768):
    """Plan against a frozen original full FLS; edits are not uploaded."""
    capacity = _capacity(capacity_bytes)
    if not isinstance(bin_data, bytes) or len(bin_data) != 0x100000:
        raise ValueError('Load an original full 1 MiB 361830 FLS BIN for the RAM bench plan.')
    if definition is None or getattr(definition, 'profile_id', '') not in ('', '361830'):
        raise ValueError('Select a 361830 calibration table.')
    if getattr(definition, 'discovery_unverified', True):
        raise ValueError('An unverified discovered table cannot be used for the RAM bench test.')
    offset, count, width = definition.z_address, definition.size * definition.z_width, definition.z_width
    if type(offset) is not int or type(count) is not int or width not in (1, 2, 4):
        raise ValueError('The selected table must have a contiguous 8-, 16- or 32-bit value array.')
    address = 0x800000 + offset
    blocks = _span_blocks(address, count)
    required = DIRECTORY_BYTES + sum(row['bytes'] for row in blocks)
    if required > min(capacity, STOCK_POOL_BYTES):
        raise ValueError(f'The directory and complete blocks need {required:,} bytes; the selected capacity is insufficient.')
    baseline = [dict(index=row['index'], hex=bin_data[row['logical_start']-0x800000:
                        row['logical_end_exclusive']-0x800000].hex().upper()) for row in blocks]
    return dict(format=FORMAT, table=dict(title=str(definition.title), file_offset=offset,
                logical_address=address, bytes=count), blocks=blocks, directory_bytes=DIRECTORY_BYTES,
                required_bytes=required, capacity_bytes=capacity, baseline_blocks=baseline,
                baseline_sha256=hashlib.sha256(bin_data).hexdigest(), probe_address=address,
                probe_bytes=width)


def _validate_plan(plan):
    # Reconstruct every admitted span. A supplied/modified JSON plan cannot turn
    # this client into an arbitrary memory writer.
    if not isinstance(plan, dict) or plan.get('format') != FORMAT:
        raise ValueError('Create a RAM plan from the loaded original BIN and table.')
    table = plan.get('table', {})
    blocks = _span_blocks(table.get('logical_address'), table.get('bytes'))
    capacity = _capacity(plan.get('capacity_bytes'))
    required = DIRECTORY_BYTES + sum(row['bytes'] for row in blocks)
    if (blocks != plan.get('blocks') or plan.get('directory_bytes') != DIRECTORY_BYTES
            or plan.get('required_bytes') != required or required > min(capacity, STOCK_POOL_BYTES)
            or table.get('file_offset') != table['logical_address'] - 0x800000
            or plan.get('probe_address') != table['logical_address']
            or type(plan.get('probe_bytes')) is not int or plan['probe_bytes'] not in (1, 2, 4)
            or plan['probe_bytes'] > table['bytes']):
        raise ValueError('RAM plan boundaries, byte widths or capacity have changed; rebuild the plan.')
    baseline = plan.get('baseline_blocks')
    if not isinstance(baseline, list) or len(baseline) != len(blocks):
        raise ValueError('RAM plan needs the complete original bytes of every affected block.')
    for block, source in zip(blocks, baseline):
        if (not isinstance(source, dict) or source.get('index') != block['index']
                or not isinstance(source.get('hex'), str)
                or not re.fullmatch('[0-9A-Fa-f]+', source['hex'])
                or len(source['hex']) != block['bytes']*2):
            raise ValueError('RAM plan baseline block is invalid.')
    if not isinstance(plan.get('baseline_sha256'), str) or not re.fullmatch('[0-9a-f]{64}', plan['baseline_sha256']):
        raise ValueError('RAM plan must retain its original BIN SHA-256.')
    return blocks


class _RamSession(_PatchSession):
    def __init__(self, *args, plan, **kwargs):
        super().__init__(*args, mode='ram', **kwargs)
        self.plan = plan
        self.write_values = {}
        self.checksum_ranges = {(b['logical_start'], b['logical_end_exclusive']-1) for b in plan['blocks']}
        self.checksum_ranges.update((plan['probe_address']+i, plan['probe_address']+i)
                                   for i in range(plan['probe_bytes']))
        self.cleanup = None
        self.retain_open = False
        self.live_write_spans = []
        self.live_write_payload = None

    def __exit__(self, exc_type, exc, traceback):
        try:
            if exc is not None:
                self.report['details']['primary_error'] = str(exc)
            if self.cleanup is not None:
                self.cleanup()
        finally:
            if exc is None and self.retain_open:
                self.report['details'].update(port_closed=False, connection_retained=True)
            else:
                self.close()

    def _admit_payload(self, payload):
        if payload in (b'\x1A\x92', b'\x1A\x94'):
            return
        if self.identity is None:
            raise SerialCommunicationError('Identify 361830 before RAM calibration requests.')
        if isinstance(payload, bytes) and len(payload) == 5 and payload[0] == 0x23:
            address, count = int.from_bytes(payload[1:4], 'big'), payload[4]
            if _READS.get(address) == count or (address == 0x3FE562 and count == 2):
                return
            if 1 <= count <= 254 and DIRECTORY_ADDRESS <= address and address+count <= DIRECTORY_ADDRESS+1120:
                return
            if 1 <= count <= 254 and any(b['logical_start'] <= address and address+count <= b['logical_end_exclusive']
                                        for b in self.plan['blocks']):
                return
        if payload in (b'\x10\x86\x10', b'\x80\x90\x00', b'\x80\x90\x01', b'\x33\x01'):
            return
        if (len(payload) == 10 and payload[:2] == b'\x31\x01'
                and (int.from_bytes(payload[2:5], 'big'), int.from_bytes(payload[5:8], 'big')) in self.checksum_ranges):
            return
        if (len(payload) == 10 and payload[:2] == b'\x31\x01'
                and payload[2:5] == payload[5:8]
                and any(b['logical_start'] <= int.from_bytes(payload[2:5], 'big') < b['logical_end_exclusive']
                        for b in self.plan['blocks'])):
            return
        if (payload == self.live_write_payload and len(payload) >= 6 and payload[0] == 0x3D
                and 1 <= payload[4] <= 250 and len(payload) == payload[4]+5
                and any(start <= int.from_bytes(payload[1:4], 'big')
                        and int.from_bytes(payload[1:4], 'big')+payload[4] <= end
                        for start, end in self.live_write_spans)):
            return
        if (len(payload) == 6 and payload[0] == 0x3D and payload[4] == 1
                and payload[5:] in self.write_values.get(int.from_bytes(payload[1:4], 'big'), ())):
            return
        raise SerialCommunicationError('Request is outside the bounded RAM calibration bench plan.')

    def _validate_response(self, payload, response):
        if payload[0] == 0x80:
            if response != b'\xC0' + payload[1:]:
                raise SerialCommunicationError('Page-selection reply does not match the requested page.')
        elif payload[:2] == b'\x31\x01':
            if response != b'\x71\x01':
                raise SerialCommunicationError('Mapped-checksum start reply must be 71 01.')
        else:
            super()._validate_response(payload, response)

    def poll_checksum(self):
        """33/01 has native busy23/mismatchFA statuses, unlike KWP pending78."""
        payload = b'\x33\x01'
        self._admit_payload(payload)
        self._require_open()
        request = encode_request(payload)
        entry = dict(payload=payload.hex(' ').upper(), request_frame=request.hex(' ').upper(),
                     host_baud=9600, response_frames=[], status='attempted')
        self.report['frames'].append(entry)
        self.active_frame = entry
        try:
            self._wait_regenerate()
            if self.connection.in_waiting:
                raise SerialCommunicationError('Unexpected data before checksum status request.')
            if self.connection.write(request) != len(request):
                raise SerialCommunicationError('Incomplete checksum status request transmission.')
            if self.echo and self._read_exact(len(request), self.RESPONSE_TIMEOUT) != request:
                raise SerialCommunicationError('Cable echo differs from checksum status request.')
            pending = 0
            while True:
                frame, response = self._receive_frame(self.RESPONSE_TIMEOUT if not pending else self.PENDING_TIMEOUT)
                if response == b'\x7F\x33\x78' and pending < self.MAX_PENDING:
                    pending += 1
                    continue
                if response not in (b'\x73\x01', b'\x7F\x33\x23', b'\x7F\x33\xFA'):
                    raise SerialCommunicationError('Unexpected mapped-checksum status: ' + response.hex(' ').upper())
                self.last_response = self.clock()
                entry.update(status='passed', checksum_status={b'\x73\x01':'match',
                    b'\x7F\x33\x23':'busy', b'\x7F\x33\xFA':'mismatch'}[response])
                return SerialReply(response, request, frame)
        except Exception:
            self.faulted = True
            entry['status'] = 'failed'
            raise
        finally:
            self.active_frame = None

    def state(self):
        return {name: row['value'] for name, row in self.snapshot(_STATE_NAMES).items()}

    def checkpoint(self):
        self._check_cancelled()
        if self.cancellation_requested:
            raise SerialCancelled('RAM bench cancelled; healthy-link cleanup is being completed.')

    def read_span(self, address, count):
        result = bytearray()
        while len(result) < count:
            size = min(254, count-len(result))
            for block in self.plan['blocks']:
                if block['logical_start'] <= address+len(result) < block['logical_end_exclusive']:
                    size = min(size, block['logical_end_exclusive']-address-len(result))
                    break
            result.extend(self.read(address+len(result), size))
        return bytes(result)

    def checksum(self, start, end, expected):
        self._exchange(b'\x31\x01' + start.to_bytes(3, 'big') + end.to_bytes(3, 'big')
                       + expected.to_bytes(2, 'big'))
        deadline = self.clock() + 10.0
        for _ in range(400):
            reply = self.poll_checksum().payload
            if reply != b'\x7F\x33\x23':
                actual = int.from_bytes(self.read(0x3FE562, 2), 'big')
                if (reply == b'\x73\x01') != (actual == expected):
                    raise SerialCommunicationError('Checksum status and computed result disagree.')
                return actual
            if self.clock() >= deadline:
                break
            self.sleep(0.025)
        self.faulted = True  # Do not issue page/write commands into an unfinished manager operation.
        raise SerialCommunicationError('Mapped-checksum operation did not finish within its bounded wait.')

    def mapped_bytes(self, address, count):
        values = []
        for target in range(address, address+count):
            value = self.checksum(target, target, 0)
            if value > 255:
                raise SerialCommunicationError('A single-byte checksum returned a value outside 0..255.')
            values.append(value)
        return bytes(values)

    def write_byte(self, address, value):
        self._exchange(b'\x3D' + address.to_bytes(3, 'big') + b'\x01' + value)

    def write_managed(self, address, data):
        if not isinstance(data, bytes) or not 1 <= len(data) <= 250:
            raise ValueError('A managed write must contain 1–250 bytes.')
        self.live_write_payload = b'\x3D' + address.to_bytes(3, 'big') + bytes([len(data)]) + data
        try:
            self._exchange(self.live_write_payload)
        finally:
            self.live_write_payload = None


def _require_manager(state, *, ready, cursor=None, internal_cursor=None, page=None):
    if state['execution_mode'] != 5 or state['transport_state'] != 0:
        raise SerialCommunicationError('Normal application / diagnostic transport 0 is required.')
    if state['directory_ready_and_transaction'] != (0x0100 if ready else 0):
        raise SerialCommunicationError('The calibration directory is not in the required ready/idle state.')
    if page is not None and state['application_page_offset'] != page:
        raise SerialCommunicationError('The actual application page offset does not match the requested page.')
    if ready:
        if (state['diagnostic_session_index'] != 3 or state['external_ram_present'] != 1
                or state['directory_pointer'] != EXTERNAL_BASE):
            raise SerialCommunicationError('Session86 / external shadow-directory preparation was not verified.')
        if cursor is not None and state['external_allocator_cursor'] != cursor:
            raise SerialCommunicationError('The external allocation cursor does not match this bounded plan.')
    allowed_internal = (0, 0x3F9FD4) if not ready else (internal_cursor,)
    if state['internal_allocator_cursor'] not in allowed_internal:
        raise SerialCommunicationError('Unexpected internal manager-pool allocation; exclusive bench ownership is required.')


def run_ram_bench(port, *, plan, hardware, probe_change=False, echo=True,
                  cancelled=None, progress=None, serial_factory=None,
                  clock=time.monotonic, sleep=time.sleep, _handoff=None):
    """Return an exportable report; never upload the editor's changed table."""
    report = dict(format='BMWBinEditor-DDE5-361830-RAM-bench-v1', status='failed', error=None,
        details={}, steps=[], frames=[], restore=dict(reference_page='not_needed', write='not_needed',
        session='unchanged', baud='not_needed', power_cycle_required=False, restoration_required=False))
    session = None
    restore_byte = None
    reference_required = False
    failure = None
    cursor = EXTERNAL_BASE + DIRECTORY_BYTES
    internal_cursor = 0x3F9FD4

    def cleanup():
        nonlocal restore_byte, reference_required
        errors = []
        if restore_byte and not session.faulted:
            try:
                address, original_byte, original, expected_cursor, expected_internal, working_offset = restore_byte
                _require_manager(session.state(), ready=True, cursor=expected_cursor,
                                 internal_cursor=expected_internal, page=working_offset)
                step('Restoring the original RAM byte and checking the complete first cell.')
                session.write_byte(address, original_byte)
                if session.mapped_bytes(address, len(original)) != original:
                    raise SerialCommunicationError('The original working-page first cell was not restored exactly.')
                report['restore']['write'] = 'verified'
                restore_byte = None
            except Exception as error:
                errors.append(str(error))
        if reference_required and not session.faulted:
            try:
                state = session.state()
                if (state['execution_mode'] != 5 or state['transport_state'] != 0
                        or state['directory_ready_and_transaction'] & 0xFF):
                    raise SerialCommunicationError('Cannot verify an idle manager for reference-page restoration.')
                if state['application_page_offset']:
                    session._exchange(b'\x80\x90\x00')
                    state = session.state()
                if (state['application_page_offset'] != 0
                        or state['directory_ready_and_transaction'] & 0xFF):
                    raise SerialCommunicationError('Reference-page selection was not verified.')
                report['details']['after'] = state
                report['restore']['reference_page'] = 'verified'
                reference_required = False
                step('Reference-page selection verified; session86 remains active and RAM allocations remain.')
            except Exception as error:
                errors.append(str(error))
        if errors:
            report['details']['cleanup_errors'] = errors
            raise SerialCommunicationError('RAM bench cleanup: ' + '; '.join(errors))

    def step(message):
        report['steps'].append(message)
        if progress:
            try:
                progress(message)
            except Exception:
                pass

    try:
        port = validate_port(port)
        import copy
        plan = copy.deepcopy(plan)
        blocks = _validate_plan(plan)
        hardware = validate_hardware(hardware)
        if hardware['capacity_bytes'] != plan['capacity_bytes']:
            raise ValueError('Hardware capacity and offline plan must match; rebuild the plan.')
        if type(probe_change) is not bool or type(echo) is not bool:
            raise ValueError('Probe and cable echo options must be enabled or disabled.')
        if _handoff is not None and (not callable(_handoff) or not probe_change):
            raise ValueError('A retained live connection requires the complete changed-byte qualification.')
        report['port'] = port
        report['details'].update(plan=plan, hardware=hardware, probe_change=probe_change,
            hardware_validated_by_software=False, flash_programming=False,
            capacity_limit='host-enforced for this fresh, exclusive experiment; stock ECU allocator remains 60 KiB',
            verification=dict(whole_block='16-bit byte sums; collisions possible',
                              first_cell='exact bytes through one-byte mapped checksums'))
        session = _RamSession(port, plan=plan, report=report, echo=echo, cancelled=cancelled,
                              serial_factory=serial_factory, clock=clock, sleep=sleep)
        session.cleanup = cleanup
        with session:
            step('Identifying the ECU and checking the installed patch at 9600 baud.')
            report['details']['identity'] = session.identify()
            for address, expected in PATCH_WORDS.items():
                if int.from_bytes(session.read(address, 2), 'big') != expected:
                    raise SerialCommunicationError(f'Installed MPC admission word does not match at 0x{address:06X}.')
            directory = session.read_span(DIRECTORY_ADDRESS, 1120)
            if directory != b''.join(value.to_bytes(4, 'big') for value in BOUNDARIES):
                raise SerialCommunicationError('The live 279-block directory differs from the reviewed 361830 layout.')
            state = session.state()
            report['details']['before'] = state
            _require_manager(state, ready=False, page=0)
            if state['diagnostic_session_index'] != 0:
                raise SerialCommunicationError('A fresh ECU boot in the default diagnostic session is required.')
            if state['external_allocator_cursor'] not in (0, EXTERNAL_BASE):
                raise SerialCommunicationError('Existing external allocations are present; start from a fresh ECU boot.')
            live_blocks = {}
            for block, source in zip(blocks, plan['baseline_blocks']):
                actual = session.read_span(block['logical_start'], block['bytes'])
                if actual != bytes.fromhex(source['hex']):
                    raise SerialCommunicationError('The live flash block differs from the loaded original BIN; no RAM write was sent.')
                live_blocks[block['index']] = actual
            step('Live directory and complete original flash blocks match the offline plan.')
            session.begin_cleanup()
            report['restore']['session'] = 'unknown'
            reference_required = True
            report['restore']['reference_page'] = 'required'
            session._exchange(b'\x10\x86\x10')
            report['restore']['session'] = '86_left_active'
            state = session.state()
            cursor = EXTERNAL_BASE + DIRECTORY_BYTES
            _require_manager(state, ready=True, cursor=cursor, internal_cursor=internal_cursor, page=0)
            report['details']['prepared'] = state
            step('External directory readiness and fresh allocation cursor verified; reference page remains active.')
            session.checkpoint()
            for block in blocks:
                state = session.state()
                _require_manager(state, ready=True, cursor=cursor, internal_cursor=internal_cursor, page=0)
                use_internal = internal_cursor + block['bytes'] <= 0x3FA178
                next_internal = internal_cursor + block['bytes'] if use_internal else internal_cursor
                next_cursor = cursor if use_internal else cursor + block['bytes']
                if next_cursor > EXTERNAL_BASE + min(hardware['capacity_bytes'], STOCK_POOL_BYTES):
                    raise SerialCommunicationError('The complete next block would exceed the verified host capacity limit.')
                target = max(plan['probe_address'], block['logical_start'])
                value = live_blocks[block['index']][target-block['logical_start']:target-block['logical_start']+1]
                session.write_values[target] = {value}
                session.write_byte(target, value)
                state = session.state()
                _require_manager(state, ready=True, cursor=next_cursor, internal_cursor=next_internal, page=0)
                cursor, internal_cursor = next_cursor, next_internal
                session.checkpoint()
            report['details']['copied'] = state
            step('Unchanged-byte managed writes completed with the expected whole-block allocation cost.')
            session._exchange(b'\x80\x90\x01')
            working_offset = EXTERNAL_BASE - DIRECTORY_ADDRESS
            _require_manager(session.state(), ready=True, cursor=cursor, internal_cursor=internal_cursor,
                             page=working_offset)
            sums = []
            for block in blocks:
                expected = sum(live_blocks[block['index']]) & 0xFFFF
                actual = session.checksum(block['logical_start'], block['logical_end_exclusive']-1, expected)
                sums.append(dict(index=block['index'], expected=expected, actual=actual))
                if actual != expected:
                    raise SerialCommunicationError('The working-page whole-block byte sum differs from the original flash block.')
                session.checkpoint()
            report['details']['working_block_sums'] = sums
            address, width = plan['probe_address'], plan['probe_bytes']
            original = session.read_span(address, width)
            working = session.mapped_bytes(address, width)
            if working != original:
                raise SerialCommunicationError('Exact working-page first-cell bytes differ from the original flash cell.')
            report['details']['first_cell'] = dict(address=address, original_hex=original.hex().upper(),
                                                  working_hex=working.hex().upper())
            step('Working-page block sums and exact first-cell bytes match the original calibration.')
            session.checkpoint()
            if probe_change:
                changed = bytes([original[0] ^ 1])
                session.write_values[address] = {original[:1], changed}
                restore_byte = (address, original[:1], original, cursor, internal_cursor, working_offset)
                report['restore']['write'] = 'required'
                report['details']['changed_probe'] = dict(address=address, original_hex=original[:1].hex().upper(),
                                                          changed_hex=changed.hex().upper())
                session.write_byte(address, changed)
                _require_manager(session.state(), ready=True, cursor=cursor, internal_cursor=internal_cursor,
                                 page=working_offset)
                if session.mapped_bytes(address, width) != changed + original[1:]:
                    raise SerialCommunicationError('Changed working-page first byte did not read back exactly.')
                if session.read_span(address, width) != original:
                    raise SerialCommunicationError('Reference flash bytes unexpectedly changed during the RAM-only probe.')
                report['details']['changed_probe']['exact_readback_verified'] = True
                step('One changed RAM byte read back exactly; original flash bytes are unchanged.')
            report['status'] = 'passed'
            cleanup()
            if session.cancellation_requested or (cancelled and cancelled()):
                raise SerialCancelled('RAM bench cancelled after healthy-link restoration completed.')
            if _handoff is not None:
                _handoff(session, report)
                session.retain_open = True
                session.cleanup = None
    except Exception as error:
        failure = error
        report.update(status='cancelled' if isinstance(error, SerialCancelled) else 'failed', error=str(error))
    finally:
        # The guarded session context performs healthy-link cleanup before
        # closing; ambiguous/faulted links get explicit recovery status here.
        if restore_byte:
            report['restore'].update(write='unknown', restoration_required=True, power_cycle_required=True)
        if reference_required:
            report['restore'].update(reference_page='unknown', restoration_required=True, power_cycle_required=True)
        if failure and report['restore']['session'] == 'unknown':
            report['restore']['power_cycle_required'] = True
    return report
