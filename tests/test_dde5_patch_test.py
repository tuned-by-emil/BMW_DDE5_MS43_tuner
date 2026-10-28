"""Bounded patch checks against an injected UART; never opens a physical port."""
from collections import deque
import json
import unittest

from bmw_bin_editor.dde5_patch_test import (
    PATCH_WORDS, SNAPSHOTS, run_patch_check, validate_reservation,
)
from bmw_bin_editor.serial_transport import encode_request, xor_checksum


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def response_frame(payload):
    body = b'\xB8\xF1\x12' + bytes([len(payload)]) + payload
    return body + bytes([xor_checksum(body)])


class Fixture:
    """Device state, wire bytes and rate changes are confined to Python objects."""
    ADDRESS = 0x3F9FD4
    ORIGINAL = bytes.fromhex('11223344')

    def __init__(self, *, echo=True, response_hook=None, crc_fault=None,
                 echo_fault=False, read_delay=0, close_error=False):
        self.clock, self.created, self.events = Clock(), [], []
        self.response_hook, self.crc_fault = response_hook, crc_fault
        self.echo, self.echo_fault, self.read_delay = echo, echo_fault, read_delay
        self.close_error = close_error
        self.cancel = False
        self.on_write = None
        self.memory = {}
        for address, value in PATCH_WORDS.items():
            self.store(address, value.to_bytes(2, 'big'))
        self.store(0x3F9856, b'\x05')
        self.store(0x3F9844, b'\0\0\0\x01')
        self.store(self.ADDRESS, self.ORIGINAL)

    def store(self, address, blob):
        for offset, value in enumerate(blob):
            self.memory[address+offset] = value

    def load(self, address, count):
        return bytes(self.memory.get(address+offset, 0) for offset in range(count))

    def factory(self, **options):
        self.uart = UART(self, options)
        self.created.append(self.uart)
        return self.uart

    def run(self, mode='read', **kwargs):
        return run_patch_check('COM6', mode=mode, serial_factory=self.factory,
            clock=self.clock, sleep=self.clock.sleep, cancelled=lambda: self.cancel,
            echo=self.echo, **kwargs)

    @staticmethod
    def reservation():
        return dict(address='0x3F9FD4', reference='Fixture-only reservation, never ECU evidence', confirmed=True)


class UART:
    def __init__(self, fixture, options):
        self.fixture, self.options = fixture, options
        self.incoming = deque()
        self.port, self.dtr, self.rts = options['port'], True, True
        self.is_open, self.closed = False, False
        self._baudrate = options['baudrate']
        self.payloads, self.write_rates = [], []
        self.device_baud = 9600
        self.pending_rate = None

    @property
    def baudrate(self):
        return self._baudrate

    @baudrate.setter
    def baudrate(self, value):
        if self.incoming:
            raise AssertionError('Host rate changed before complete response consumption')
        self.fixture.events.append(('host_baud', value))
        self._baudrate = value

    def open(self):
        assert self.port == 'COM6' and self.dtr is False and self.rts is False
        assert self.options['bytesize'] == 8 and self.options['parity'] == 'E' and self.options['stopbits'] == 1
        self.is_open = True

    def close(self):
        if self.fixture.close_error:
            raise OSError('Fixture close failure')
        self.is_open, self.closed = False, True

    def reset_input_buffer(self):
        self.incoming.clear()

    @property
    def in_waiting(self):
        return sum(len(part) for part in self.incoming)

    def write(self, request):
        payload = request[4:-1]
        assert request == encode_request(payload)
        assert self.baudrate == self.device_baud
        self.payloads.append(payload)
        self.write_rates.append(self.baudrate)
        self.fixture.events.append(('request', payload.hex().upper(), self.baudrate))
        if self.fixture.echo:
            self.incoming.append(bytes([request[0] ^ 1])+request[1:] if self.fixture.echo_fault else request)
        if payload == b'\x1A\x92':
            response = b'\x5A\x92' + b'0281010565'
        elif payload == b'\x1A\x94':
            response = b'\x5A\x94' + b'1037361830'
        elif payload[0] == 0x23:
            response = b'\x63' + self.fixture.load(int.from_bytes(payload[1:4], 'big'), payload[4])
        elif payload[:2] == b'\x10\x86':
            response = b'\x50' + payload[1:]
            self.pending_rate = {0x10: 9600, 0x84: 115200, 0x87: 125000}[payload[2]]
            self.fixture.store(0x3F9840, b'\x03')
        elif payload[0] == 0x3D:
            assert payload[4] == 4 and len(payload) == 9
            self.fixture.store(int.from_bytes(payload[1:4], 'big'), payload[5:])
            response = b'\x7D'
        else:
            raise AssertionError('Unexpected fixture request '+payload.hex())
        if self.fixture.response_hook:
            response = self.fixture.response_hook(payload, response)
        if response is not None:
            frame = response_frame(response)
            if self.fixture.crc_fault and self.fixture.crc_fault(payload):
                frame = frame[:-1] + bytes([frame[-1] ^ 1])
            self.incoming.append(frame)
            # A rejected baud request does not change the mocked device rate.
            if response[:2] == b'\x7F\x10':
                self.pending_rate = None
        else:
            self.pending_rate = None
        if self.fixture.on_write:
            self.fixture.on_write(payload)
        return len(request)

    def read(self, size):
        self.fixture.clock.sleep(self.fixture.read_delay)
        if not self.incoming:
            self.fixture.clock.sleep(0.020)
            return b''
        part = self.incoming.popleft()
        answer = part[:size]
        if len(part) > size:
            self.incoming.appendleft(part[size:])
        if not self.incoming and self.pending_rate is not None:
            self.device_baud, self.pending_rate = self.pending_rate, None
            self.fixture.events.append(('complete_baud_ack', self.device_baud))
        return answer


class ReservationTests(unittest.TestCase):
    def test_exact_aligned_range_and_explicit_meaningful_user_reference(self):
        normalized = validate_reservation(Fixture.reservation())
        self.assertEqual(normalized['address'], Fixture.ADDRESS)
        self.assertEqual(normalized['ownership'], 'user_verified_not_software_verified')
        for address in (None, True, 0x3F9FD3, 0x3F9FD5, 0x3FA178, 'COM1', '0x3FA175'):
            with self.subTest(address=address), self.assertRaises(ValueError):
                validate_reservation(dict(Fixture.reservation(), address=address))
        for changes in ({'confirmed': False}, {'confirmed': 1}, {'reference': ''},
                        {'reference': 'reserved'}, {'reference': '!!!!!!!!!!!!'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_reservation(dict(Fixture.reservation(), **changes))

    def test_invalid_reservation_does_not_create_or_open_uart(self):
        for reservation in (None, {}, dict(Fixture.reservation(), address='3FA177')):
            fixture = Fixture()
            report = fixture.run('write', reservation=reservation)
            self.assertEqual(report['status'], 'failed')
            self.assertEqual(fixture.created, [])


class PatchCheckTests(unittest.TestCase):
    def test_read_checks_identity_live_words_and_selectors_without_mode_or_write(self):
        fixture = Fixture()
        report = fixture.run()
        self.assertEqual(report['status'], 'passed', report)
        self.assertTrue(fixture.uart.closed)
        self.assertEqual({payload[0] for payload in fixture.uart.payloads}, {0x1A, 0x23})
        self.assertEqual(len(report['details']['baseline_snapshot']), len(SNAPSHOTS))
        self.assertEqual(len(report['details']['patch_words']), 4)
        self.assertFalse(report['restore']['power_cycle_required'])
        json.dumps(report)

    def test_wrong_identity_stops_before_any_memory_read(self):
        fixture = Fixture(response_hook=lambda request, response:
            b'\x5A\x94'+b'1037361858' if request == b'\x1A\x94' else response)
        report = fixture.run()
        self.assertEqual(report['status'], 'failed')
        self.assertEqual([p[0] for p in fixture.uart.payloads], [0x1A, 0x1A])

    def test_wrong_patch_mask_stops_without_mode_or_write(self):
        fixture = Fixture()
        fixture.store(0x23576, b'\xFC\xFF')
        report = fixture.run('baud')
        self.assertEqual(report['status'], 'failed')
        self.assertFalse(any(p[0] in (0x10, 0x3D) for p in fixture.uart.payloads))

    def test_crc_echo_and_deadline_fail_without_retry(self):
        for kwargs in (dict(crc_fault=lambda payload: True), dict(echo_fault=True), dict(read_delay=0.6)):
            with self.subTest(kwargs=kwargs):
                fixture = Fixture(**kwargs)
                report = fixture.run()
                self.assertEqual(report['status'], 'failed')
                self.assertEqual(len(fixture.uart.payloads), 1)
                self.assertTrue(fixture.uart.closed)

    def test_no_echo_is_explicit_and_works(self):
        self.assertEqual(Fixture(echo=False).run()['status'], 'passed')

    def test_baud_waits_for_each_full_ack_before_host_change_and_restores(self):
        for rate, code in ((115200, '84'), (125000, '87')):
            with self.subTest(rate=rate):
                fixture = Fixture()
                report = fixture.run('baud', rate=rate)
                self.assertEqual(report['status'], 'passed', report)
                self.assertEqual(report['restore']['baud'], 'verified')
                events = fixture.events
                self.assertLess(events.index(('complete_baud_ack', rate)), events.index(('host_baud', rate)))
                self.assertLess(events.index(('complete_baud_ack', 9600)), events.index(('host_baud', 9600)))
                self.assertIn(('request', '1086'+code, 9600), events)
                self.assertIn(('request', '108610', rate), events)
                self.assertEqual(fixture.uart.baudrate, 9600)
                self.assertFalse(any(p[0] == 0x3D for p in fixture.uart.payloads))

    def test_rejected_baud_never_changes_host_speed(self):
        fixture = Fixture(response_hook=lambda request, response:
            b'\x7F\x10\x12' if request[:2] == b'\x10\x86' else response)
        report = fixture.run('baud')
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['restore']['baud'], 'unchanged_rejected')
        self.assertFalse(report['restore']['power_cycle_required'])
        self.assertFalse(any(event[0] == 'host_baud' for event in fixture.events))

    def test_wrong_or_missing_baud_ack_stops_at_old_host_rate_and_reports_uncertainty(self):
        for bad in (None, b'\x50\x86\x87', b'\x50\x86\x84\x00'):
            with self.subTest(bad=bad):
                fixture = Fixture(response_hook=lambda request, response:
                    bad if request == b'\x10\x86\x84' else response)
                report = fixture.run('baud')
                self.assertEqual(report['status'], 'failed')
                self.assertEqual(report['restore']['baud'], 'unknown')
                self.assertTrue(report['restore']['power_cycle_required'])
                self.assertFalse(any(e[0] == 'host_baud' for e in fixture.events))
                self.assertEqual(sum(p[0] == 0x10 for p in fixture.uart.payloads), 1)

    def test_communication_failure_at_high_rate_sends_no_blind_restore(self):
        fixture = Fixture()
        fixture.crc_fault = lambda payload: fixture.uart.baudrate == 125000 and payload[0] == 0x23
        report = fixture.run('baud', rate=125000)
        self.assertEqual(report['status'], 'failed')
        self.assertTrue(report['restore']['power_cycle_required'])
        self.assertEqual(sum(p[0] == 0x10 for p in fixture.uart.payloads), 1)

    def test_working_page_or_busy_manager_blocks_mutating_modes(self):
        for address, value in ((0x3FE540, b'\x01'), (0x3FE541, b'\x01'), (0x3FE544, b'\0\0\0\x04')):
            for mode in ('baud', 'write'):
                with self.subTest(address=address, mode=mode):
                    fixture = Fixture()
                    fixture.store(address, value)
                    report = fixture.run(mode, **({'reservation': fixture.reservation()} if mode == 'write' else {}))
                    self.assertEqual(report['status'], 'failed')
                    self.assertFalse(any(p[0] in (0x10, 0x3D) for p in fixture.uart.payloads))

    def test_write_pattern_readback_restore_and_originals_are_reported(self):
        fixture = Fixture()
        report = fixture.run('write', reservation=fixture.reservation())
        self.assertEqual(report['status'], 'passed', report)
        self.assertEqual(report['restore']['write'], 'verified')
        writes = [p for p in fixture.uart.payloads if p[0] == 0x3D]
        self.assertEqual([p[5:] for p in writes], [bytes.fromhex('A55AC33C'), fixture.ORIGINAL])
        self.assertEqual(fixture.load(fixture.ADDRESS, 4), fixture.ORIGINAL)
        self.assertEqual(report['details']['write_test']['original_hex'], fixture.ORIGINAL.hex().upper())
        self.assertEqual({e[2] for e in fixture.events if e[0] == 'request'}, {9600})
        self.assertEqual(report['restore']['session'], '86_left_active')

    def test_wrong_7d_address_echo_is_rejected_not_treated_as_write_success(self):
        fixture = Fixture(response_hook=lambda request, response:
            b'\x7D'+request[1:4] if request[0] == 0x3D else response)
        report = fixture.run('write', reservation=fixture.reservation())
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['restore']['write'], 'unknown')
        self.assertTrue(report['restore']['restoration_required'])
        self.assertEqual(report['details']['write_test']['original_hex'], '11223344')
        self.assertEqual(sum(p[0] == 0x3D for p in fixture.uart.payloads), 1)

    def test_pattern_mismatch_on_healthy_link_still_restores_original(self):
        fixture = Fixture()
        fixture.response_hook = lambda request, response: (b'\x63\x00\x00\x00\x00'
            if request == bytes.fromhex('233F9FD404') and fixture.load(fixture.ADDRESS, 4) == bytes.fromhex('A55AC33C') else response)
        report = fixture.run('write', reservation=fixture.reservation())
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['restore']['write'], 'verified')
        self.assertEqual(fixture.load(fixture.ADDRESS, 4), fixture.ORIGINAL)

    def test_write_timeout_retains_original_and_does_not_retry_or_guess_cleanup(self):
        fixture = Fixture(response_hook=lambda request, response: None if request[0] == 0x3D else response)
        report = fixture.run('write', reservation=fixture.reservation())
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['restore']['write'], 'unknown')
        self.assertTrue(report['restore']['restoration_required'])
        self.assertEqual(report['details']['write_test']['original_hex'], '11223344')
        self.assertEqual(sum(p[0] == 0x3D for p in fixture.uart.payloads), 1)

    def test_cancellation_before_mutation_and_during_write_cleanup(self):
        fixture = Fixture()
        fixture.cancel = True
        report = fixture.run('write', reservation=fixture.reservation())
        self.assertEqual(report['status'], 'cancelled')
        self.assertEqual(fixture.created, [])
        fixture = Fixture()
        fixture.on_write = lambda payload: setattr(fixture, 'cancel', True) if payload[0] == 0x3D else None
        report = fixture.run('write', reservation=fixture.reservation())
        self.assertEqual(report['status'], 'cancelled', report)
        self.assertEqual(report['restore']['write'], 'verified')
        self.assertEqual(fixture.load(fixture.ADDRESS, 4), fixture.ORIGINAL)
        self.assertEqual(sum(p[0] == 0x3D for p in fixture.uart.payloads), 2)

    def test_cancellation_during_baud_still_restores_known_link(self):
        fixture = Fixture()
        fixture.on_write = lambda payload: setattr(fixture, 'cancel', True) if payload == bytes.fromhex('108684') else None
        report = fixture.run('baud')
        self.assertEqual(report['status'], 'cancelled', report)
        self.assertEqual(report['restore']['baud'], 'verified')
        self.assertEqual(fixture.uart.baudrate, 9600)

    def test_ambiguous_write_session_entry_reports_unknown_without_ram_writes(self):
        fixture = Fixture(response_hook=lambda request, response: None if request == bytes.fromhex('108610') else response)
        report = fixture.run('write', reservation=fixture.reservation())
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['restore']['session'], 'unknown')
        self.assertTrue(report['restore']['power_cycle_required'])
        self.assertFalse(any(p[0] == 0x3D for p in fixture.uart.payloads))

    def test_session_entry_preparation_is_rechecked_before_writes(self):
        fixture = Fixture()
        fixture.on_write = lambda payload: fixture.store(0x3FE540, b'\x01') if payload == bytes.fromhex('108610') else None
        report = fixture.run('write', reservation=fixture.reservation())
        self.assertEqual(report['status'], 'failed')
        self.assertFalse(any(p[0] == 0x3D for p in fixture.uart.payloads))

    def test_internal_pool_allocations_block_write(self):
        fixture = Fixture()
        fixture.store(0x3FE554, bytes.fromhex('003FA000'))
        report = fixture.run('write', reservation=fixture.reservation())
        self.assertEqual(report['status'], 'failed')
        self.assertFalse(any(p[0] in (0x10, 0x3D) for p in fixture.uart.payloads))

    def test_cancellation_from_progress_before_mutation_sends_no_mutating_request(self):
        for mode, prefix, forbidden in (('baud', 'Requesting session 86', 0x10),
                                       ('write', 'Writing four test bytes', 0x3D)):
            with self.subTest(mode=mode):
                fixture = Fixture()
                def progress(message):
                    if message.startswith(prefix):
                        fixture.cancel = True
                report = fixture.run(mode, progress=progress,
                    **({'reservation': fixture.reservation()} if mode == 'write' else {}))
                self.assertEqual(report['status'], 'cancelled', report)
                self.assertFalse(any(p[0] == forbidden for p in fixture.uart.payloads))

    def test_ambiguous_baud_restoration_never_changes_host_to_baseline(self):
        fixture = Fixture(response_hook=lambda request, response:
            None if request == bytes.fromhex('108610') else response)
        report = fixture.run('baud')
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['restore']['baud'], 'unknown')
        self.assertTrue(report['restore']['power_cycle_required'])
        self.assertNotIn(('host_baud', 9600), fixture.events)
        self.assertEqual(sum(p == bytes.fromhex('108610') for p in fixture.uart.payloads), 1)

    def test_progress_callback_failure_cannot_interrupt_restoration(self):
        fixture = Fixture()
        def progress(_):
            raise RuntimeError('Fixture UI callback')
        report = fixture.run('write', reservation=fixture.reservation(), progress=progress)
        self.assertEqual(report['status'], 'passed', report)
        self.assertEqual(report['restore']['write'], 'verified')

    def test_close_failure_is_visible(self):
        fixture = Fixture(close_error=True)
        report = fixture.run()
        self.assertEqual(report['status'], 'failed')
        self.assertFalse(report['details']['port_closed'])


if __name__ == '__main__':
    unittest.main()
