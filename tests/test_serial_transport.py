"""Protocol/error-path tests with injected UART and virtual time only."""
from collections import deque
import unittest

from bmw_bin_editor.serial_transport import (
    KlineSession, SerialCancelled, SerialCommunicationError,
    decode_response, encode_request, validate_port,
)


def frame(payload, header=b'\xb8\xf1\x12'):
    body = header + bytes([len(payload)]) + payload
    value = 0
    for byte in body:
        value ^= byte
    return body + bytes([value])


def measurement(count, data=None):
    return frame(b'\x6c\x10' + (bytes(2 * count) if data is None else data))


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class Delayed:
    def __init__(self, delay, data):
        self.delay, self.data = delay, data


class FakeSerial:
    """The factory creates an unopened UART; traffic exists only after write."""
    def __init__(self, clock, scripts=(), echo=True, partial_write=False, **kwargs):
        self.clock, self.scripts = clock, deque(scripts)
        self.options = kwargs
        self.port = kwargs.get('port')
        self.dtr = self.rts = True
        self.is_open = False
        self.closed = False
        self.open_state = None
        self.incoming = deque()
        self.echo = echo
        self.partial_write = partial_write
        self.writes = []
        self.write_times = []
        self.read_count = 0
        self.on_read = None

    def open(self):
        self.open_state = (self.port, self.dtr, self.rts)
        self.is_open = True

    def close(self):
        self.is_open = False
        self.closed = True

    def reset_input_buffer(self):
        self.incoming.clear()

    @property
    def in_waiting(self):
        return sum(len(part) for part in self.incoming if isinstance(part, bytes))

    def write(self, request):
        self.writes.append(request)
        self.write_times.append(self.clock())
        if self.echo:
            self.incoming.append(request)
        if self.scripts:
            script = self.scripts.popleft()
            if callable(script):
                script = script(request)
            if isinstance(script, (bytes, Delayed, Exception)):
                self.incoming.append(script)
            else:
                self.incoming.extend(script)
        return len(request) - 1 if self.partial_write else len(request)

    def read(self, size):
        self.read_count += 1
        if self.on_read is not None:
            self.on_read()
        if not self.incoming:
            self.clock.sleep(0.020)
            return b''
        part = self.incoming.popleft()
        if isinstance(part, Exception):
            raise part
        if isinstance(part, Delayed):
            self.clock.sleep(part.delay)
            part = part.data
        if len(part) > size:
            self.incoming.appendleft(part[size:])
        return part[:size]


class Fixture:
    def __init__(self, scripts=(), *, echo=True, serial_echo=None,
                 partial_write=False, cancelled=None):
        self.clock = Clock()
        self.created = []

        def factory(**kwargs):
            self.uart = FakeSerial(self.clock, scripts,
                echo=echo if serial_echo is None else serial_echo,
                partial_write=partial_write, **kwargs)
            self.created.append(self.uart)
            return self.uart

        self.session = KlineSession('com6', echo=echo, serial_factory=factory,
            clock=self.clock, sleep=self.clock.sleep, cancelled=cancelled)

    def open(self):
        self.session.__enter__()
        return self.session


def identification():
    return [frame(b'\x5a\x92' + b'0281010565'),
            frame(b'\x5a\x94' + b'1037361830')]


class FrameTests(unittest.TestCase):
    def test_golden_vectors_use_explicit_length_and_xor(self):
        self.assertEqual(encode_request(b'\x2c\x10'), bytes.fromhex('B8 12 F1 02 2C 10 65'))
        self.assertEqual(encode_request(b'\x1a\x94'), bytes.fromhex('B8 12 F1 02 1A 94 D7'))
        self.assertEqual(decode_response(bytes.fromhex('B8 F1 12 04 6C 10 12 34 05')),
                         bytes.fromhex('6C 10 12 34'))

    def test_response_strict_header_addresses_length_and_checksum(self):
        good = frame(b'\x6c\x10\x12\x34')
        failures = [good[:-1], good + b'\x00', good[:3] + b'\x03' + good[4:],
            good[:-1] + bytes([good[-1] ^ 1]),
            frame(b'\x6c\x10', b'\x80\xf1\x12'),
            frame(b'\x6c\x10', b'\xb8\x12\xf1'),
            frame(b'\x6c\x10', b'\xb8\xf1\x13'), frame(b'')]
        for value in failures:
            with self.subTest(value=value.hex()), self.assertRaises(SerialCommunicationError):
                decode_response(value)

    def test_payload_and_port_validation(self):
        self.assertEqual(validate_port('  com12  '), 'COM12')
        for port in ['COM0', 'COM10000', 'socket://localhost:1', '', '../COM1']:
            with self.subTest(port=port), self.assertRaises(ValueError):
                validate_port(port)
        for payload in [b'', b'\x00' * 256, bytearray(b'\x1a\x94'), '1A94']:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                encode_request(payload)


class SessionTests(unittest.TestCase):
    def fixture(self, *args, **kwargs):
        fixture = Fixture(*args, **kwargs)
        fixture.open()
        self.addCleanup(fixture.session.close)
        return fixture

    def test_unopened_factory_and_control_signals_before_open(self):
        f = self.fixture()
        self.assertIsNone(f.uart.options['port'])
        self.assertEqual(f.uart.open_state, ('COM6', False, False))
        self.assertEqual((f.uart.options['baudrate'], f.uart.options['bytesize'],
            f.uart.options['parity'], f.uart.options['stopbits']), (9600, 8, 'E', 1))
        for option in ['xonxoff', 'rtscts', 'dsrdtr']:
            self.assertFalse(f.uart.options[option])
        self.assertEqual(f.uart.writes, [])

    def test_identification_gates_measurements_without_sending(self):
        f = self.fixture()
        with self.assertRaisesRegex(SerialCommunicationError, 'Identify'):
            f.session.read_measurements([0x2d])
        self.assertEqual(f.uart.writes, [])

    def test_supported_identity_and_exact_digit_fields(self):
        f = self.fixture(identification())
        self.assertEqual(f.session.identify(), {'hardware': '0281010565', 'software': '1037361830'})
        self.assertEqual([value[4:-1] for value in f.uart.writes], [b'\x1a\x92', b'\x1a\x94'])
        self.assertGreaterEqual(f.uart.write_times[1] - f.uart.write_times[0], 0.025)

    def test_wrong_and_malformed_identity_block_all_measurements(self):
        for hardware, software in [(b'0281010566', b'1037361830'),
                (b'0281010565', b'1037361858'), (b'0281010565', b'103736183'),
                (b'0281010565', b'103736183X')]:
            with self.subTest(hardware=hardware, software=software):
                f = self.fixture([frame(b'\x5a\x92' + hardware), frame(b'\x5a\x94' + software)])
                with self.assertRaises(SerialCommunicationError):
                    f.session.identify()
                count = len(f.uart.writes)
                with self.assertRaises(SerialCommunicationError):
                    f.session.read_measurements([0x2d])
                self.assertEqual(len(f.uart.writes), count)
                self.assertIsNone(f.session.identity)

    def test_ram_baud_and_programming_rejected_before_any_write(self):
        f = self.fixture()
        rejected = [bytes.fromhex(value) for value in
            ['233FA19A01', '108684', '1085', '3107', '3108', '310902',
             '3400000000', '3600', '37', '3D3FA19A01', '1101', '2C11002D']]
        for payload in rejected:
            with self.subTest(payload=payload.hex()), self.assertRaises(SerialCommunicationError):
                f.session._exchange(payload)
        self.assertEqual(f.uart.writes, [])

    def test_fragmented_echo_and_response(self):
        def pieces(request):
            reply = frame(b'\x5a\x94' + b'1037361830')
            return [request[:1], request[1:4], request[4:],
                    reply[:1], reply[1:3], reply[3:7], reply[7:]]
        f = self.fixture([pieces], serial_echo=False)
        self.assertEqual(f.session._exchange(b'\x1a\x94').payload[2:], b'1037361830')
        self.assertGreater(f.uart.read_count, 4)

    def test_explicit_no_echo_adapter(self):
        f = self.fixture([frame(b'\x5a\x94' + b'1037361830')], echo=False)
        self.assertEqual(f.session._exchange(b'\x1a\x94').payload[2:], b'1037361830')

    def test_wrong_or_missing_echo_faults_session(self):
        for scripts in [[lambda request: bytes([request[0] ^ 1]) + request[1:]],
                        [frame(b'\x5a\x94' + b'1037361830')], []]:
            with self.subTest(scripts=scripts):
                f = self.fixture(scripts, serial_echo=False)
                with self.assertRaises(SerialCommunicationError):
                    f.session._exchange(b'\x1a\x94')
                self.assertTrue(f.session.faulted)

    def test_wrong_service_or_subfunction_and_negative_sid(self):
        for payload in [b'\x5b\x94', b'\x5a\x92', b'\x7f\x2c\x78', b'\x7f\x1a']:
            with self.subTest(payload=payload):
                f = self.fixture([frame(payload)])
                with self.assertRaises(SerialCommunicationError):
                    f.session._exchange(b'\x1a\x94')
                self.assertTrue(f.session.faulted)

    def test_two_pending_responses_then_success_never_resend(self):
        f = self.fixture([[frame(b'\x7f\x1a\x78'), frame(b'\x7f\x1a\x78'),
                           Delayed(0.8, frame(b'\x5a\x94' + b'1037361830'))]])
        self.assertEqual(f.session._exchange(b'\x1a\x94').payload[:2], b'\x5a\x94')
        self.assertEqual(len(f.uart.writes), 1)

    def test_third_pending_response_exceeds_bound_without_resend(self):
        f = self.fixture([[frame(b'\x7f\x1a\x78')] * 3])
        with self.assertRaisesRegex(SerialCommunicationError, 'pending limit'):
            f.session._exchange(b'\x1a\x94')
        self.assertEqual(len(f.uart.writes), 1)

    def test_busy_errors_fail_without_replay(self):
        for code in [0x21, 0x23, 0x12]:
            with self.subTest(code=code):
                f = self.fixture([frame(bytes([0x7f, 0x1a, code]))])
                with self.assertRaises(SerialCommunicationError):
                    f.session._exchange(b'\x1a\x94')
                self.assertEqual(len(f.uart.writes), 1)

    def test_cache_only_after_correct_reply_and_group_rotation(self):
        f = self.fixture(identification() + [measurement(2), measurement(2),
            measurement(1), measurement(2), measurement(10)])
        f.session.identify()
        first = f.session.read_measurements([0x2d, 0x66])
        self.assertFalse(first.cached_selection)
        self.assertTrue(f.session.read_measurements([0x2d, 0x66]).cached_selection)
        self.assertFalse(f.session.read_measurements([0x55]).cached_selection)
        self.assertFalse(f.session.read_measurements([0x2d, 0x66]).cached_selection)
        self.assertFalse(f.session.read_measurements(range(1, 11)).cached_selection)
        self.assertEqual([value[4:-1] for value in f.uart.writes[2:6]],
            [bytes.fromhex('2C10002D0066'), b'\x2c\x10', bytes.fromhex('2C100055'),
             bytes.fromhex('2C10002D0066')])

    def test_invalid_group_never_writes(self):
        f = self.fixture(identification())
        f.session.identify()
        count = len(f.uart.writes)
        for ids in [[], range(1, 12), [0], [256], [-1], [True], ['45']]:
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                f.session.read_measurements(ids)
        self.assertEqual(len(f.uart.writes), count)

    def test_truncated_channel_response_never_arms_or_preserves_cache(self):
        for replies in [[measurement(1)], [measurement(2), measurement(1)]]:
            with self.subTest(replies=replies):
                f = self.fixture(identification() + replies)
                f.session.identify()
                if len(replies) == 2:
                    f.session.read_measurements([0x2d, 0x66])
                with self.assertRaisesRegex(SerialCommunicationError, 'reply length'):
                    f.session.read_measurements([0x2d, 0x66])
                self.assertIsNone(f.session.selected_ids)
                self.assertTrue(f.session.faulted)

    def test_timeout_invalidates_cache_and_blocks_late_reply_reuse(self):
        f = self.fixture(identification() + [measurement(1)])
        f.session.identify()
        f.session.read_measurements([0x2d])
        with self.assertRaises(SerialCommunicationError):
            f.session.read_measurements([0x2d])
        self.assertIsNone(f.session.selected_ids)
        count = len(f.uart.writes)
        f.uart.incoming.append(measurement(1))
        with self.assertRaises(SerialCommunicationError):
            f.session.read_measurements([0x2d])
        self.assertEqual(len(f.uart.writes), count)

    def test_unsolicited_frame_before_request_faults_without_write(self):
        f = self.fixture(identification() + [measurement(1)])
        f.session.identify()
        f.session.read_measurements([0x2d])
        count = len(f.uart.writes)
        f.uart.incoming.append(measurement(1))
        with self.assertRaisesRegex(SerialCommunicationError, 'Unexpected data'):
            f.session.read_measurements([0x2d])
        self.assertEqual(len(f.uart.writes), count)
        self.assertIsNone(f.session.selected_ids)

    def test_partial_uart_write_faults_before_receiving(self):
        f = self.fixture(partial_write=True)
        with self.assertRaisesRegex(SerialCommunicationError, 'Incomplete K-line request'):
            f.session._exchange(b'\x1a\x94')
        self.assertEqual(f.uart.read_count, 0)
        self.assertTrue(f.session.faulted)

    def test_read_exception_invalidates_selected_group(self):
        f = self.fixture(identification() + [measurement(1), OSError('fake unplug')])
        f.session.identify()
        f.session.read_measurements([0x2d])
        with self.assertRaisesRegex(SerialCommunicationError, 'fake unplug'):
            f.session.read_measurements([0x2d])
        self.assertIsNone(f.session.selected_ids)

    def test_cancellation_during_read_invalidates_cache_and_context_closes(self):
        state = {'cancel': False}
        f = Fixture(identification() + [measurement(1)], cancelled=lambda: state['cancel'])
        with f.session:
            f.session.identify()
            f.session.read_measurements([0x2d])
            f.uart.on_read = lambda: state.update(cancel=True)
            with self.assertRaises(SerialCancelled):
                f.session.read_measurements([0x2d])
            self.assertIsNone(f.session.selected_ids)
        self.assertTrue(f.uart.closed)
        self.assertIsNone(f.session.connection)

    def test_cancellation_before_request_invalidates_existing_cache(self):
        state = {'cancel': False}
        f = self.fixture(identification() + [measurement(1)], cancelled=lambda: state['cancel'])
        f.session.identify()
        f.session.read_measurements([0x2d])
        count = len(f.uart.writes)
        state['cancel'] = True
        with self.assertRaises(SerialCancelled):
            f.session.read_measurements([0x2d])
        self.assertEqual(len(f.uart.writes), count)
        self.assertIsNone(f.session.selected_ids)

    def test_frame_returning_after_response_deadline_is_rejected(self):
        f = self.fixture([Delayed(0.540, frame(b'\x5a\x94' + b'1037361830'))], echo=False)
        with self.assertRaises(SerialCommunicationError):
            f.session._exchange(b'\x1a\x94')
        self.assertTrue(f.session.faulted)

    def test_explicit_close_clears_identity_and_selection(self):
        f = self.fixture(identification() + [measurement(1)])
        f.session.identify()
        f.session.read_measurements([0x2d])
        f.session.close()
        self.assertTrue(f.uart.closed)
        self.assertIsNone(f.session.connection)
        self.assertIsNone(f.session.identity)
        self.assertIsNone(f.session.selected_ids)


if __name__ == '__main__':
    unittest.main()
