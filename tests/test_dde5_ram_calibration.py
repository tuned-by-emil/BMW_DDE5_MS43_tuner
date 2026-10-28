"""Native RAM bench checks use a stateful in-memory UART, never a COM port."""
from collections import deque
import hashlib
import json
from types import SimpleNamespace
import unittest

from bmw_bin_editor.dde5_patch_test import PATCH_WORDS
from bmw_bin_editor.dde5_ram_calibration import (
    BOUNDARIES, DIRECTORY_ADDRESS, DIRECTORY_BYTES, EXTERNAL_BASE,
    _RamSession, build_ram_plan, run_ram_bench, validate_hardware,
)
from bmw_bin_editor.serial_transport import (
    KlineSession, SerialCommunicationError, encode_request, xor_checksum,
)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def frame(payload):
    body = b'\xB8\xF1\x12' + bytes([len(payload)]) + payload
    return body + bytes([xor_checksum(body)])


def definition(address=0x8EA80C, *, size=4, width=2):
    return SimpleNamespace(profile_id='361830', discovery_unverified=False,
        z_address=address-0x800000, size=size, z_width=width, title='Fixture table')


BIN = bytes(range(256)) * 4096


def hardware(capacity=32768):
    return dict(capacity_bytes=capacity,
                reference='Fixture-only wiring, bank and capacity evidence; no physical ECU',
                confirmed=True)


class Fixture:
    """A model of documented states, with independent original and shadow bytes."""
    def __init__(self, *, table=None, echo=True, response_hook=None,
                 fault=None, close_error=False):
        self.clock, self.created, self.events = Clock(), [], []
        self.plan = build_ram_plan(BIN, table or definition())
        self.echo, self.response_hook, self.fault = echo, response_hook, fault
        self.close_error, self.cancel = close_error, False
        self.memory, self.shadow = {}, {}
        self.checksum_operation = None
        self.on_request = None
        self.checksum_busy_count = 1
        self.terminal_responses = []
        self.store(DIRECTORY_ADDRESS, b''.join(value.to_bytes(4, 'big') for value in BOUNDARIES))
        for address, expected in PATCH_WORDS.items():
            self.store(address, expected.to_bytes(2, 'big'))
        self.put(0x3F9856, 5, 1)
        self.put(0x3F9844, 1)

    def put(self, address, value, count=4):
        self.store(address, value.to_bytes(count, 'big'))

    def get(self, address, count=4):
        return int.from_bytes(self.load(address, count), 'big')

    def store(self, address, blob):
        for offset, value in enumerate(blob):
            self.memory[address+offset] = value

    def load(self, address, count):
        return bytes(self.memory.get(address+i,
            BIN[address+i-0x800000] if 0x800000 <= address+i < 0x900000 else 0)
                     for i in range(count))

    def mapped(self, address, count):
        result = bytearray()
        for target in range(address, address+count):
            index = next(i for i in range(279) if BOUNDARIES[i] <= target < BOUNDARIES[i+1])
            if self.get(0x3FE544) and index in self.shadow:
                result.append(self.shadow[index][target-BOUNDARIES[index]])
            else:
                result.extend(self.load(target, 1))
        return bytes(result)

    def response(self, payload):
        if payload == b'\x1A\x92':
            return b'\x5A\x92' + b'0281010565'
        if payload == b'\x1A\x94':
            return b'\x5A\x94' + b'1037361830'
        if payload[0] == 0x23:
            return b'\x63' + self.load(int.from_bytes(payload[1:4], 'big'), payload[4])
        if payload == b'\x10\x86\x10':
            self.put(0x3F9840, 3, 1)
            self.store(0x3FE540, b'\x01\x00')
            self.put(0x3FE544, 0)
            self.put(0x3FE54C, EXTERNAL_BASE)
            self.put(0x3FE550, 1, 1)
            self.put(0x3FE554, 0x3F9FD4)
            self.put(0x3FE558, EXTERNAL_BASE + DIRECTORY_BYTES)
            return b'\x50\x86\x10'
        if payload[0] == 0x3D:
            assert len(payload) == 6 and payload[4] == 1
            target = int.from_bytes(payload[1:4], 'big')
            index = next(i for i in range(279) if BOUNDARIES[i] <= target < BOUNDARIES[i+1])
            if index not in self.shadow:
                start, end = BOUNDARIES[index:index+2]
                self.shadow[index] = bytearray(self.load(start, end-start))
                # Model the actual two-pool allocator rather than a host assumption.
                internal = self.get(0x3FE554)
                if internal + end-start <= 0x3FA178:
                    self.put(0x3FE554, internal + end-start)
                else:
                    self.put(0x3FE558, self.get(0x3FE558) + end-start)
            self.shadow[index][target-BOUNDARIES[index]] = payload[5]
            return b'\x7D'
        if payload[:2] == b'\x80\x90':
            assert payload[2] in (0, 1)
            self.put(0x3FE544, EXTERNAL_BASE-DIRECTORY_ADDRESS if payload[2] else 0)
            return b'\xC0' + payload[1:]
        if payload[:2] == b'\x31\x01':
            assert len(payload) == 10 and self.checksum_operation is None
            start, end = int.from_bytes(payload[2:5], 'big'), int.from_bytes(payload[5:8], 'big')
            expected = int.from_bytes(payload[8:10], 'big')
            actual = sum(self.mapped(start, end-start+1)) & 0xFFFF
            self.put(0x3FE562, 0, 2)
            self.put(0x3FE541, 6, 1)
            self.checksum_operation = [actual, expected, self.checksum_busy_count]
            return b'\x71\x01'
        if payload == b'\x33\x01':
            assert self.checksum_operation is not None, 'Terminal poll must be consumed exactly once'
            actual, expected, remaining = self.checksum_operation
            if remaining:
                self.checksum_operation[2] -= 1
                return b'\x7F\x33\x23'
            self.put(0x3FE562, actual, 2)
            self.put(0x3FE541, 0, 1)
            self.checksum_operation = None
            response = b'\x73\x01' if actual == expected else b'\x7F\x33\xFA'
            self.terminal_responses.append(response)
            return response
        raise AssertionError('Unexpected fixture request: ' + payload.hex())

    def factory(self, **options):
        result = UART(self, options)
        self.created.append(result)
        self.uart = result
        return result

    def run(self, **options):
        return run_ram_bench('COM6', plan=self.plan, hardware=hardware(),
            serial_factory=self.factory, echo=self.echo,
            clock=self.clock, sleep=self.clock.sleep, cancelled=lambda: self.cancel, **options)


class UART:
    def __init__(self, fixture, options):
        self.fixture, self.options = fixture, options
        self.incoming, self.payloads = deque(), []
        self.port, self.dtr, self.rts = options['port'], True, True
        self.is_open, self.closed, self.baudrate = False, False, options['baudrate']

    def open(self):
        assert self.port == 'COM6' and self.dtr is False and self.rts is False
        assert self.baudrate == 9600
        assert (self.options['bytesize'], self.options['parity'], self.options['stopbits']) == (8, 'E', 1)
        self.is_open = True

    def close(self):
        if self.fixture.close_error:
            raise OSError('Fixture close failure')
        self.closed, self.is_open = True, False

    def reset_input_buffer(self):
        self.incoming.clear()

    @property
    def in_waiting(self):
        return sum(map(len, self.incoming))

    def write(self, request):
        payload = request[4:-1]
        assert request == encode_request(payload) and self.baudrate == 9600
        self.payloads.append(payload)
        self.fixture.events.append(payload)
        if self.fixture.echo:
            self.incoming.append(request)
        response = self.fixture.response(payload)
        if self.fixture.response_hook:
            response = self.fixture.response_hook(payload, response, self.fixture)
        if response is not None:
            response_frame = frame(response)
            fault = self.fixture.fault(payload) if self.fixture.fault else None
            if fault == 'crc':
                response_frame = response_frame[:-1] + bytes([response_frame[-1] ^ 1])
            elif fault == 'partial':
                response_frame = response_frame[:5]
            elif fault == 'echo' and self.fixture.echo:
                self.incoming[0] = bytes([request[0] ^ 1]) + request[1:]
            self.incoming.append(response_frame)
        if self.fixture.on_request:
            self.fixture.on_request(payload)
        return len(request)

    def read(self, size):
        if not self.incoming:
            self.fixture.clock.sleep(0.020)
            return b''
        part = self.incoming.popleft()
        result = part[:size]
        if len(part) > size:
            self.incoming.appendleft(part[size:])
        return result


class PlanTests(unittest.TestCase):
    def test_directory_and_whole_block_cost_instead_of_cell_cost(self):
        self.assertEqual((len(BOUNDARIES), BOUNDARIES[0], BOUNDARIES[-1]),
                         (280, 0x8E057C, 0x8FE110))
        self.assertTrue(all(a < b for a, b in zip(BOUNDARIES, BOUNDARIES[1:])))
        # Independently recorded from reviewed MPC SHA40ca5ca2..., not derived
        # from the production tuple at test-writing time.
        directory_bytes = b''.join(value.to_bytes(4, 'big') for value in BOUNDARIES)
        self.assertEqual(hashlib.sha256(directory_bytes).hexdigest(),
                         '48724cea6aceb3fe3ead599fad5db2a661aaaa211d70bf60ba798f6ce8dc503b')
        plan = build_ram_plan(BIN, definition())
        self.assertEqual(plan['blocks'], [dict(index=141, logical_start=0x8EA7C8,
            logical_end_exclusive=0x8EB86A, bytes=4258)])
        self.assertEqual(plan['required_bytes'], 1116 + 4258)
        self.assertEqual(len(bytes.fromhex(plan['baseline_blocks'][0]['hex'])), 4258)
        json.dumps(plan)

    def test_cross_boundary_plan_includes_every_complete_block(self):
        plan = build_ram_plan(BIN, definition(0x8EB868, size=4, width=1))
        self.assertEqual([block['index'] for block in plan['blocks']], [141, 142])
        self.assertEqual(plan['required_bytes'], 1116 + 4258 + 3480)

    def test_capacity_and_real_directory_guards(self):
        for table in (definition(0x8E057B), definition(0x8FE10F),
                      definition(0x8E057C, size=40000, width=1)):
            with self.subTest(table=table), self.assertRaises(ValueError):
                build_ram_plan(BIN, table)
        with self.assertRaises(ValueError):
            build_ram_plan(BIN, definition(), capacity_bytes=32000)
        with self.assertRaises(ValueError):
            build_ram_plan(BIN[:-1], definition())
        invalid = definition()
        invalid.discovery_unverified = True
        with self.assertRaises(ValueError):
            build_ram_plan(BIN, invalid)
        larger = build_ram_plan(BIN, definition(0x8E057C, size=40000, width=1),
                                capacity_bytes=65536)
        self.assertGreater(larger['required_bytes'], 32768)
        self.assertLessEqual(larger['required_bytes'], 0xF000)

    def test_hardware_is_user_declaration_not_software_proof(self):
        self.assertEqual(validate_hardware(hardware())['ownership'],
                         'user_verified_not_software_verified')
        for declaration in (None, {}, dict(hardware(), confirmed=1),
                            dict(hardware(), reference=''), dict(hardware(), capacity_bytes=0)):
            with self.subTest(declaration=declaration), self.assertRaises(ValueError):
                validate_hardware(declaration)

    def test_tampered_or_over_capacity_plan_never_opens_uart(self):
        for mutate in (lambda p: p['blocks'][0].update(bytes=1),
                       lambda p: p['table'].update(logical_address=0x3F9844),
                       lambda p: p.update(probe_address=0x3F9844),
                       lambda p: p.update(required_bytes=1),
                       lambda p: p['baseline_blocks'][0].update(hex='00')):
            fixture = Fixture()
            mutate(fixture.plan)
            report = fixture.run()
            self.assertEqual(report['status'], 'failed', report)
            self.assertEqual(fixture.created, [])


class BenchTests(unittest.TestCase):
    def assert_no_mutation(self, fixture):
        self.assertFalse(any(payload[0] in (0x10, 0x3D, 0x80, 0x31)
                             for payload in fixture.events), fixture.events)

    def test_unchanged_copy_and_native_oracle_leave_reference_selected(self):
        fixture = Fixture()
        report = fixture.run()
        self.assertEqual(report['status'], 'passed', report)
        self.assertEqual(report['restore']['reference_page'], 'verified')
        self.assertEqual(report['restore']['write'], 'not_needed')
        self.assertEqual(report['restore']['session'], '86_left_active')
        self.assertFalse(report['restore']['power_cycle_required'])
        self.assertEqual(fixture.get(0x3FE544), 0)
        self.assertTrue(fixture.uart.closed)
        writes = [payload for payload in fixture.events if payload[0] == 0x3D]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][5:], fixture.load(0x8EA80C, 1))
        self.assertIn(b'\x7F\x33\xFA', fixture.terminal_responses)
        self.assertIn(b'\x73\x01', fixture.terminal_responses)
        self.assertFalse(any(payload[0] in (0x11, 0x27, 0x34, 0x36, 0x37) for payload in fixture.events))
        self.assertEqual({entry['host_baud'] for entry in report['frames']}, {9600})
        json.dumps(report)

    def test_explicit_changed_byte_is_exactly_checked_and_restored(self):
        fixture = Fixture()
        original = fixture.load(0x8EA80C, 2)
        report = fixture.run(probe_change=True)
        self.assertEqual(report['status'], 'passed', report)
        self.assertEqual(report['restore']['write'], 'verified')
        writes = [payload for payload in fixture.events if payload[0] == 0x3D]
        self.assertEqual([payload[5:] for payload in writes],
                         [original[:1], bytes([original[0] ^ 1]), original[:1]])
        self.assertEqual(bytes(fixture.shadow[141][0x44:0x46]), original)
        self.assertEqual(fixture.load(0x8EA80C, 2), original)
        self.assertTrue(report['details']['changed_probe']['exact_readback_verified'])
        self.assertEqual(fixture.get(0x3FE544), 0)

    def test_zero_byte_oracle_and_echo_off(self):
        fixture = Fixture(table=definition(0x8EA800), echo=False)
        report = fixture.run()
        self.assertEqual(report['status'], 'passed', report)
        self.assertEqual(report['details']['first_cell']['working_hex'], '0001')

    def test_small_and_mixed_blocks_follow_actual_internal_first_allocator(self):
        for table in (definition(BOUNDARIES[0], size=1, width=1),
                      definition(BOUNDARIES[0], size=200, width=1)):
            with self.subTest(size=table.size):
                fixture = Fixture(table=table)
                report = fixture.run()
                self.assertEqual(report['status'], 'passed', report)
                internal, external = 0x3F9FD4, EXTERNAL_BASE + DIRECTORY_BYTES
                for block in fixture.plan['blocks']:
                    if internal+block['bytes'] <= 0x3FA178:
                        internal += block['bytes']
                    else:
                        external += block['bytes']
                self.assertEqual(fixture.get(0x3FE554), internal)
                self.assertEqual(fixture.get(0x3FE558), external)
                self.assertEqual(fixture.get(0x3FE544), 0)

    def test_cross_block_runtime_copies_each_block_once(self):
        fixture = Fixture(table=definition(0x8EB868, size=4, width=1))
        report = fixture.run()
        self.assertEqual(report['status'], 'passed', report)
        self.assertEqual(set(fixture.shadow), {141, 142})
        writes = [payload for payload in fixture.events if payload[0] == 0x3D]
        self.assertEqual([int.from_bytes(payload[1:4], 'big') for payload in writes],
                         [0x8EB868, 0x8EB86A])

    def test_wrong_identity_stops_before_memory_or_mutation(self):
        fixture = Fixture(response_hook=lambda request, response, _:
            b'\x5A\x94'+b'1037361858' if request == b'\x1A\x94' else response)
        report = fixture.run()
        self.assertEqual(report['status'], 'failed')
        self.assertEqual([p[0] for p in fixture.events], [0x1A, 0x1A])

    def test_patch_directory_fresh_state_and_full_baseline_preflight(self):
        for mutate in (lambda f: f.put(0x2354E, 0xFCFF, 2),
                       lambda f: f.put(DIRECTORY_ADDRESS+4*279, 0x8FEFFF),
                       lambda f: f.store(0x3FE540, b'\x01\x00'),
                       lambda f: f.put(0x3FE544, 1),
                       lambda f: f.put(0x3FE558, EXTERNAL_BASE+4000),
                       lambda f: f.store(0x8EB869, b'\xEE')):
            fixture = Fixture()
            mutate(fixture)
            report = fixture.run()
            self.assertEqual(report['status'], 'failed', report)
            self.assert_no_mutation(fixture)

    def test_positive_prepare_ack_does_not_prove_successful_directory(self):
        def fail_prepare(request, response, fixture):
            if request == b'\x10\x86\x10':
                fixture.store(0x3FE540, b'\x00\x00')
                fixture.put(0x3FE54C, 0)
            return response
        fixture = Fixture(response_hook=fail_prepare)
        report = fixture.run()
        self.assertEqual(report['status'], 'failed', report)
        self.assertFalse(any(payload[0] in (0x3D, 0x80, 0x31) for payload in fixture.events))
        self.assertEqual(report['restore']['session'], '86_left_active')

    def test_positive_copy_ack_requires_expected_allocation_state(self):
        def fail_copy(request, response, fixture):
            if request[0] == 0x3D:
                fixture.put(0x3FE558, EXTERNAL_BASE + DIRECTORY_BYTES)
            return response
        fixture = Fixture(response_hook=fail_copy)
        report = fixture.run()
        self.assertEqual(report['status'], 'failed', report)
        self.assertEqual(len([p for p in fixture.events if p[0] == 0x3D]), 1)
        self.assertFalse(any(p in (b'\x80\x90\x01',) or p[0] == 0x31 for p in fixture.events))
        self.assertEqual(report['restore']['reference_page'], 'verified')

    def test_semantic_changed_readback_failure_still_restores_byte_and_page(self):
        changed = False
        def corrupt_change(request, response, fixture):
            nonlocal changed
            if request[0] == 0x3D and request[5] != fixture.load(0x8EA80C, 1)[0]:
                changed = True
                fixture.shadow[141][0x44] ^= 2
            return response
        fixture = Fixture(response_hook=corrupt_change)
        report = fixture.run(probe_change=True)
        self.assertTrue(changed)
        self.assertEqual(report['status'], 'failed', report)
        self.assertEqual(report['restore']['write'], 'verified')
        self.assertEqual(report['restore']['reference_page'], 'verified')
        self.assertEqual(bytes(fixture.shadow[141][0x44:0x46]), fixture.load(0x8EA80C, 2))

    def test_partial_crc_or_echo_fault_stops_without_blind_restore_traffic(self):
        for kind in ('partial', 'crc', 'echo'):
            fixture = Fixture(fault=lambda payload, kind=kind: kind if payload == b'\x80\x90\x01' else None)
            report = fixture.run()
            self.assertEqual(report['status'], 'failed', report)
            self.assertEqual(fixture.events[-1], b'\x80\x90\x01')
            self.assertEqual(report['restore']['reference_page'], 'unknown')
            self.assertTrue(report['restore']['power_cycle_required'])
            self.assertTrue(fixture.uart.closed)

    def test_ambiguous_changed_write_retains_original_and_requires_recovery(self):
        original = BIN[0xEA80C]
        fixture = Fixture(fault=lambda payload: 'crc'
            if payload[0] == 0x3D and payload[5] == original ^ 1 else None)
        report = fixture.run(probe_change=True)
        self.assertEqual(report['status'], 'failed', report)
        self.assertEqual(report['restore']['write'], 'unknown')
        self.assertTrue(report['restore']['restoration_required'])
        self.assertEqual(fixture.events[-1], b'\x3D\x8E\xA8\x0C\x01'+bytes([original ^ 1]))
        self.assertEqual(report['details']['changed_probe']['original_hex'], f'{original:02X}')

    def test_lost_prepare_ack_reports_unknown_session_and_sends_no_cleanup(self):
        fixture = Fixture(response_hook=lambda request, normal, _:
                          None if request == b'\x10\x86\x10' else normal)
        report = fixture.run()
        self.assertEqual(report['status'], 'failed', report)
        self.assertEqual(fixture.events[-1], b'\x10\x86\x10')
        self.assertEqual(report['restore']['session'], 'unknown')
        self.assertTrue(report['restore']['power_cycle_required'])

    def test_restricted_writer_requires_exact_single_byte_positive(self):
        fixture = Fixture(response_hook=lambda request, normal, _:
            b'\x7D'+request[1:4] if request[0] == 0x3D else normal)
        report = fixture.run()
        self.assertEqual(report['status'], 'failed', report)
        self.assertEqual(fixture.events[-1][0], 0x3D)
        self.assertEqual(report['restore']['reference_page'], 'unknown')

    def test_out_of_range_single_byte_sum_is_not_silently_truncated(self):
        def bad_byte_sum(request, normal, fixture):
            if request[:2] == b'\x31\x01' and request[2:5] == request[5:8]:
                fixture.checksum_operation[0] = 0x100
            return normal
        fixture = Fixture(response_hook=bad_byte_sum)
        report = fixture.run()
        self.assertEqual(report['status'], 'failed', report)
        self.assertIn('outside', report['error'])
        self.assertEqual(report['restore']['reference_page'], 'verified')

    def test_full_block_sum_failure_on_healthy_link_still_restores_reference(self):
        def corrupt_copy(request, normal, fixture):
            if request[0] == 0x3D:
                fixture.shadow[141][-1] ^= 1
            return normal
        fixture = Fixture(response_hook=corrupt_copy)
        report = fixture.run()
        self.assertEqual(report['status'], 'failed', report)
        self.assertIn('whole-block byte sum', report['error'])
        self.assertEqual(report['restore']['reference_page'], 'verified')
        self.assertFalse(report['restore']['power_cycle_required'])
        self.assertEqual(fixture.get(0x3FE544), 0)

    def test_terminal_status_must_agree_with_retained_computed_sum(self):
        fixture = Fixture(response_hook=lambda request, normal, _:
            b'\x73\x01' if request == b'\x33\x01' and normal == b'\x7F\x33\xFA' else normal)
        report = fixture.run()
        self.assertEqual(report['status'], 'failed', report)
        self.assertIn('status and computed result disagree', report['error'])
        self.assertEqual(report['restore']['reference_page'], 'verified')
        self.assertEqual(fixture.get(0x3FE544), 0)

    def test_restore_byte_semantic_failure_does_not_prevent_reference_selection(self):
        writes = 0
        def corrupt_restoration(request, normal, fixture):
            nonlocal writes
            if request[0] == 0x3D:
                writes += 1
                if writes == 3:
                    fixture.shadow[141][0x44] ^= 2
            return normal
        fixture = Fixture(response_hook=corrupt_restoration)
        report = fixture.run(probe_change=True)
        self.assertEqual(report['status'], 'failed', report)
        self.assertEqual(writes, 3)
        self.assertEqual(report['restore']['write'], 'unknown')
        self.assertEqual(report['restore']['reference_page'], 'verified')
        self.assertTrue(report['restore']['restoration_required'])
        self.assertTrue(report['details']['cleanup_errors'])
        self.assertEqual(fixture.get(0x3FE544), 0)

    def test_close_failure_changes_final_pass_to_failure_and_is_reported(self):
        fixture = Fixture(close_error=True)
        report = fixture.run()
        self.assertEqual(report['status'], 'failed', report)
        self.assertFalse(report['details']['port_closed'])
        self.assertEqual(report['restore']['reference_page'], 'verified')

    def test_pending_poll_is_bounded_and_does_not_inject_cleanup_into_active_manager(self):
        fixture = Fixture()
        fixture.checksum_busy_count = 10000
        report = fixture.run()
        self.assertEqual(report['status'], 'failed', report)
        self.assertLessEqual(len([p for p in fixture.events if p == b'\x33\x01']), 400)
        self.assertEqual(fixture.events[-1], b'\x33\x01')
        self.assertEqual(report['restore']['reference_page'], 'unknown')

    def test_unrelated_or_malformed_poll_negative_is_not_accepted_as_busy(self):
        for response in (b'\x7F\x31\x23', b'\x7F\x33\x22', b'\x7F\x33\xFA\x00', b'\x73\x02'):
            fixture = Fixture(response_hook=lambda request, normal, _, response=response:
                response if request == b'\x33\x01' else normal)
            report = fixture.run()
            self.assertEqual(report['status'], 'failed', report)
            self.assertEqual(fixture.events[-1], b'\x33\x01')

    def test_standard_pending78_is_waited_for_inside_single_status_exchange(self):
        fixture = Fixture()
        sent_pending = False
        def pending(request, response, fixture):
            nonlocal sent_pending
            if request == b'\x33\x01' and not sent_pending:
                sent_pending = True
                fixture.uart.incoming.append(frame(b'\x7F\x33\x78'))
            return response
        fixture.response_hook = pending
        report = fixture.run()
        self.assertEqual(report['status'], 'passed', report)
        self.assertTrue(any(len(entry['response_frames']) == 2 for entry in report['frames']))

    def test_cancel_before_mutation_and_deferred_cancel_after_changed_write(self):
        fixture = Fixture()
        fixture.cancel = True
        report = fixture.run()
        self.assertEqual(report['status'], 'cancelled')
        self.assertEqual(fixture.created, [])
        fixture = Fixture()
        fixture.on_request = lambda payload: setattr(fixture, 'cancel', True) if (
            payload[0] == 0x3D and payload[5] != BIN[0xEA80C]) else None
        report = fixture.run(probe_change=True)
        self.assertEqual(report['status'], 'cancelled', report)
        self.assertEqual(report['restore']['write'], 'verified')
        self.assertEqual(report['restore']['reference_page'], 'verified')

    def test_cancel_after_copy_uses_healthy_cleanup_without_starting_changed_probe(self):
        fixture = Fixture()
        fixture.on_request = lambda payload: setattr(fixture, 'cancel', True) if payload[0] == 0x3D else None
        report = fixture.run(probe_change=True)
        self.assertEqual(report['status'], 'cancelled', report)
        self.assertEqual(report['restore']['reference_page'], 'verified')
        self.assertEqual(len([p for p in fixture.events if p[0] == 0x3D]), 1)
        self.assertNotIn('changed_probe', report['details'])


class AdmissionTests(unittest.TestCase):
    def test_normal_logger_stays_measurements_only(self):
        for payload in (b'\x23\x3F\xE5\x62\x02', b'\x10\x86\x10',
                        b'\x80\x90\x01', b'\x33\x01',
                        b'\x3D\x8E\xA8\x0C\x01\x00'):
            with self.subTest(payload=payload), self.assertRaises(SerialCommunicationError):
                KlineSession._admit_payload(payload)

    def test_ram_session_has_no_raw_sram_or_arbitrary_checksum_write_console(self):
        fixture = Fixture()
        session = _RamSession('COM6', plan=fixture.plan, report={'details': {}, 'frames': []})
        session.identity = dict(hardware='0281010565', software='1037361830')
        for payload in (b'\x23\x6F\x00\x00\x01', b'\x23\x3F\xFB\x1C\x04',
                        b'\x3D\x3F\x98\x44\x01\x00', b'\x3D\x8E\xA8\x0C\x01\x00',
                        b'\x31\x01\x8E\xA8\x0D\x8E\xA8\x0E\x00\x00',
                        b'\x10\x86\x84', b'\x11\x01'):
            with self.subTest(payload=payload), self.assertRaises(SerialCommunicationError):
                session._admit_payload(payload)
        session.write_values[0x8EA80C] = {b'\x0C'}
        session._admit_payload(b'\x3D\x8E\xA8\x0C\x01\x0C')
        with self.assertRaises(SerialCommunicationError):
            session._admit_payload(b'\x3D\x8E\xA8\x0C\x01\x0D')


if __name__ == '__main__':
    unittest.main()
