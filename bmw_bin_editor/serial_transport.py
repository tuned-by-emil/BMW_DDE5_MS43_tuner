"""DDE5 KWP2000* on a transparent BMW K-line cable: 9600 8E1, B8/XOR.

This is the normal measurement transport established by on-car B8 replies.
RAM and baud permissions depend on additional ECU state; the descriptor's
protocol tag3 is not its dispatcher transport state.
Only identification and selective measurement requests are admitted here.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import time
from typing import Callable


class SerialCommunicationError(RuntimeError):
    pass


class SerialCancelled(SerialCommunicationError):
    pass


def validate_port(port: str) -> str:
    if not isinstance(port, str) or not re.fullmatch(r'COM[1-9][0-9]{0,3}', port.strip(), re.I):
        raise ValueError('Choose a Windows COM port, for example COM1.')
    return port.strip().upper()


def xor_checksum(data: bytes) -> int:
    checksum = 0
    for byte in data:
        checksum ^= byte
    return checksum


def encode_request(payload: bytes) -> bytes:
    if not isinstance(payload, bytes) or not 1 <= len(payload) <= 255:
        raise ValueError('Invalid diagnostic payload length.')
    body = bytes((0xB8, 0x12, 0xF1, len(payload))) + payload
    return body + bytes((xor_checksum(body),))


def decode_response(frame: bytes) -> bytes:
    if not isinstance(frame, bytes) or len(frame) < 6:
        raise SerialCommunicationError('Incomplete K-line response.')
    if frame[:3] != b'\xB8\xF1\x12':
        raise SerialCommunicationError('Unexpected K-line response header or ECU address.')
    if frame[3] < 1 or len(frame) != frame[3] + 5:
        raise SerialCommunicationError('K-line response length mismatch.')
    if xor_checksum(frame):
        raise SerialCommunicationError('K-line response checksum mismatch.')
    return frame[4:-1]


@dataclass(frozen=True)
class SerialReply:
    payload: bytes
    request_frame: bytes
    response_frame: bytes
    cached_selection: bool = False


class KlineSession:
    """One COM owner; bounded reads, no connection retries or mode changes."""

    BAUDRATE = 9600
    RESPONSE_TIMEOUT = 0.520  # PRG 500 ms, plus the transport's 20 ms margin.
    INTERBYTE_TIMEOUT = 0.040
    PENDING_TIMEOUT = 5.020
    MAX_PENDING = 2
    REGENERATE_SECONDS = 0.025
    EXPECTED_HARDWARE = '0281010565'
    EXPECTED_SOFTWARE = '1037361830'

    def __init__(self, port: str, *, echo: bool = True,
                 cancelled: Callable[[], bool] | None = None, serial_factory=None,
                 clock=time.monotonic, sleep=time.sleep):
        self.port_name = validate_port(port)
        if type(echo) is not bool:
            raise ValueError('Cable echo must be enabled or disabled.')
        self.echo = echo
        self.cancelled = cancelled or (lambda: False)
        self.serial_factory = serial_factory
        self.clock, self.sleep = clock, sleep
        self.connection = None
        self.identity = None
        self.selected_ids = None
        self.last_response = None
        self.faulted = False

    def __enter__(self):
        if self.connection is not None:
            raise SerialCommunicationError('Serial session is already open.')
        self._check_cancelled()
        factory = self.serial_factory
        if factory is None:
            try:
                import serial
            except ImportError:
                raise SerialCommunicationError('Direct serial requires the pyserial package. Install the editor requirements.') from None
            factory = serial.Serial
        try:
            # Create unopened: suppress DTR/RTS before opening the cable.
            self.connection = factory(port=None, baudrate=self.BAUDRATE,
                bytesize=8, parity='E', stopbits=1, timeout=0.020,
                write_timeout=0.500, xonxoff=False, rtscts=False, dsrdtr=False)
            self.connection.port = self.port_name
            self.connection.dtr = False
            self.connection.rts = False
            self._check_cancelled()
            self.connection.open()
            self.connection.reset_input_buffer()
            self.identity = self.selected_ids = self.last_response = None
            self.faulted = False
            return self
        except SerialCancelled:
            self.close()
            raise
        except Exception as error:
            self.close()
            raise SerialCommunicationError(f'Could not open {self.port_name}: {error}') from error

    def __exit__(self, *_):
        self.close()

    def close(self):
        connection, self.connection = self.connection, None
        self.identity = self.selected_ids = None
        if connection is not None:
            connection.close()

    def _check_cancelled(self):
        if self.cancelled():
            self.faulted = True
            self.selected_ids = None
            raise SerialCancelled('Serial read cancelled.')

    def _require_open(self):
        self._check_cancelled()
        if self.connection is None or not self.connection.is_open:
            raise SerialCommunicationError('Serial session is closed.')
        if self.faulted:
            raise SerialCommunicationError('Serial session failed; reconnect before further requests.')

    def _wait_regenerate(self):
        if self.last_response is None:
            return
        due = self.last_response + self.REGENERATE_SECONDS
        while self.clock() < due:
            self._check_cancelled()
            self.sleep(min(0.010, due - self.clock()))

    def _read_exact(self, length: int, first_timeout: float) -> bytes:
        result = bytearray()
        deadline = self.clock() + first_timeout
        while len(result) < length:
            self._check_cancelled()
            if self.clock() >= deadline:
                kind = 'Incomplete' if result else 'Timed out waiting for'
                raise SerialCommunicationError(f'{kind} K-line response ({len(result)}/{length} bytes).')
            piece = self.connection.read(length - len(result))
            self._check_cancelled()
            if self.clock() >= deadline:
                raise SerialCommunicationError('K-line response arrived after the read deadline.')
            if piece:
                if len(piece) > length - len(result):
                    raise SerialCommunicationError('Serial reader returned too many bytes.')
                result.extend(piece)
                deadline = self.clock() + self.INTERBYTE_TIMEOUT
        return bytes(result)

    def _receive_frame(self, timeout: float) -> tuple[bytes, bytes]:
        header = self._read_exact(4, timeout)
        if header[:3] != b'\xB8\xF1\x12' or header[3] == 0:
            raise SerialCommunicationError('Unexpected K-line response header or ECU address.')
        frame = header + self._read_exact(header[3] + 1, self.INTERBYTE_TIMEOUT)
        return frame, decode_response(frame)

    @staticmethod
    def _admit_payload(payload: bytes):
        if payload in (b'\x1A\x92', b'\x1A\x94', b'\x2C\x10'):
            return
        if (isinstance(payload, bytes) and 4 <= len(payload) <= 22
                and payload[:2] == b'\x2C\x10' and len(payload) % 2 == 0):
            ids = [int.from_bytes(payload[n:n+2], 'big') for n in range(2, len(payload), 2)]
            if all(1 <= value <= 255 for value in ids):
                return
        raise SerialCommunicationError('Direct serial permits DDE identification and measurement reads only.')

    def _validate_response(self, payload: bytes, response: bytes):
        """Validate the normal logger's positive service/subfunction contract."""
        if response[:2] != bytes((payload[0] + 0x40, payload[1])):
            raise SerialCommunicationError('Unexpected diagnostic service or subfunction in response.')

    def _exchange(self, payload: bytes) -> SerialReply:
        self._admit_payload(payload)
        self._require_open()
        request = encode_request(payload)
        try:
            self._wait_regenerate()
            # A late/unrelated frame cannot be associated with a new transaction.
            if self.connection.in_waiting:
                raise SerialCommunicationError('Unexpected data before the next request; reconnect the cable session.')
            self._check_cancelled()
            written = self.connection.write(request)
            if written != len(request):
                raise SerialCommunicationError('Incomplete K-line request transmission.')
            self._check_cancelled()
            if self.echo:
                received_echo = self._read_exact(len(request), self.RESPONSE_TIMEOUT)
                if received_echo != request:
                    raise SerialCommunicationError('Cable echo differs from the request. Check the cable echo setting.')
            pending = 0
            timeout = self.RESPONSE_TIMEOUT
            while True:
                frame, response = self._receive_frame(timeout)
                if response[:1] == b'\x7F':
                    if len(response) != 3 or response[1] != payload[0]:
                        raise SerialCommunicationError('Unrelated or malformed negative ECU response.')
                    if response[2] == 0x78:
                        pending += 1
                        if pending > self.MAX_PENDING:
                            raise SerialCommunicationError('ECU response-pending limit exceeded.')
                        timeout = self.PENDING_TIMEOUT
                        continue
                    raise SerialCommunicationError(f'ECU rejected service 0x{payload[0]:02X}: code 0x{response[2]:02X}.')
                self._validate_response(payload, response)
                self.last_response = self.clock()
                return SerialReply(response, request, frame)
        except SerialCancelled:
            self.faulted = True
            self.selected_ids = None
            raise
        except Exception as error:
            self.faulted = True
            self.selected_ids = None
            if isinstance(error, SerialCommunicationError):
                raise
            raise SerialCommunicationError(f'Serial communication failed: {error}') from error

    def identify(self) -> dict[str, str]:
        self.identity = self.selected_ids = None
        values = {}
        for field, subfunction in [('hardware', 0x92), ('software', 0x94)]:
            reply = self._exchange(bytes((0x1A, subfunction)))
            raw = reply.payload[2:]
            if len(raw) != 10 or not raw.isdigit():
                self.faulted = True
                raise SerialCommunicationError('Invalid DDE identification response.')
            values[field] = raw.decode('ascii')
        if values != {'hardware': self.EXPECTED_HARDWARE, 'software': self.EXPECTED_SOFTWARE}:
            self.faulted = True
            raise SerialCommunicationError('Direct serial logging requires Bosch hardware 0281010565 / software 1037361830; '
                f"received {values['hardware']} / {values['software']}.")
        self.identity = values
        return dict(values)

    def read_measurements(self, ids) -> SerialReply:
        ids = tuple(ids)
        if not 1 <= len(ids) <= 10 or any(type(value) is not int or not 1 <= value <= 255 for value in ids):
            raise ValueError('Select 1–10 valid measurement channel IDs.')
        self._require_open()
        if self.identity is None:
            raise SerialCommunicationError('Identify the supported DDE before reading measurements.')
        cached = ids == self.selected_ids
        payload = b'\x2C\x10' if cached else b'\x2C\x10' + b''.join(value.to_bytes(2, 'big') for value in ids)
        reply = self._exchange(payload)
        if len(reply.payload) != 2 + 2 * len(ids):
            self.selected_ids = None
            self.faulted = True
            raise SerialCommunicationError('Measurement reply length does not match the selected channels.')
        self.selected_ids = ids
        return SerialReply(reply.payload, reply.request_frame, reply.response_frame, cached)
