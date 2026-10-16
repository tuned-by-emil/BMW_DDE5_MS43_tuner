"""Bounded live DDE504 authentication and offline factory-programming contracts.

The live entry point sends identification and routines 07/08 only. It never
enters ECUProgrammingMode, erases, downloads, writes, stamps or resets the ECU.
Programming contracts below are for review and dry runs; a live write executor
remains disabled until image acceptance, preservation and recovery are proven.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import hashlib
import re
import secrets
import time

from .dde5_auth import (authentication_material, calculate_installed_response,
                        prg_authentication_argument, verify_response)
from .dde5_checks import inspect_images
from .ecu_programming import NativeEdiabas, ProgrammingError


TRUSTED_FLS_SHA256 = '8862fb53f9f102d2ca2717338a527546a4da03659b2d8f871856d485cf696e4f'
TRUSTED_PRG_SHA256 = 'fcbb5f3caaa03dfe1bda6b00d27e63ad1a84202c0b7405ffa77347c5e80fdb46'
TARGET_HARDWARE = '0281010565'
TARGET_SOFTWARE = '1037361830'
ECU = '28dde504'


class AuthenticationFailure(ProgrammingError):
    """A fixed, exportable failure message that contains no diagnostic buffers."""
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _require(condition, code, message):
    if not condition:
        raise AuthenticationFailure(code, message)


def _check_cancel(cancelled):
    if cancelled and cancelled():
        raise AuthenticationFailure('cancelled', 'Authentication test cancelled.')


def _safe_failure(exc, stage):
    """Classify known setup failures without copying untrusted diagnostic text."""
    value = str(exc)
    if isinstance(exc, FileNotFoundError) and stage == 'preflight':
        return 'provider_missing', 'The installed WinKFP key provider or DDE504 key dataset was not found.'
    if stage == 'preflight':
        if value.startswith('Unsupported WinKFP provider build'):
            return 'provider_build', 'The installed WinKFP provider build is unsupported by the reviewed decoder.'
        if value.startswith('Unsupported DDE504 authentication dataset'):
            return 'provider_dataset', 'The installed DDE504 authentication dataset differs from the reviewed dataset.'
        return 'key_provider', 'Offline authentication prerequisites are unavailable or incompatible.'
    if isinstance(exc, ProgrammingError):
        if value.startswith('EDIABAS DLL lacks __api'):
            return 'api_exports', 'The installed EDIABAS DLL lacks a required API export for this operation.'
        if value.startswith('EDIABAS API not found:'):
            return 'api_missing', 'The matching EDIABAS API DLL was not found in the selected BIN directory.'
        if value.startswith('EDIABAS DLL architecture does not match'):
            return 'api_architecture', 'The EDIABAS DLL architecture does not match this editor.'
        if value.startswith('Another editor EDIABAS operation is running.'):
            return 'api_busy', 'Another editor EDIABAS operation is running. Stop logging before the test.'
        if value.startswith('Authentication refuses') and 'environment override' in value:
            return 'configuration_override', 'A conflicting EDIABAS configuration environment override must be resolved.'
        if value.startswith('Authentication cannot establish the selected EDIABAS ECU path:'):
            return 'configuration', 'Check the selected EDIABAS.INI: Interface=STD:OBD, Simulation=0 and one absolute EcuPath.'
        if ('reviewed DDE504 PRG' in value or 'PRG differs' in value or 'PRG does not match' in value
                or value.startswith('Authentication requires the exact decoded 28dde504 PRG;')):
            return 'prg_source', 'The configured DDE504 PRG differs from the reviewed implementation.'
        if (value.startswith('Authentication PRG not found') or value.startswith('Authentication cannot read')
                or value.startswith('Authentication PRG cannot be read:')):
            return 'prg_missing', 'The configured 28dde504.prg could not be read.'
        if 'timed out' in value.lower():
            return 'timeout', 'The diagnostic job timed out; no automatic retry was sent.'
        if 'ERROR_AUTHENTICATION' in value:
            return 'authentication_rejected', 'The PRG reported that the ECU rejected authentication.'
        return 'transport', 'EDIABAS stopped the authentication test during ' + stage + '.'
    return 'unexpected_error', 'Authentication stopped during ' + stage + '.'


def _validate_trusted_fls(trusted_fls):
    data = bytes(trusted_fls)
    _require(len(data) == 0x100000, 'trusted_image',
             'Authentication requires the complete preserved 1 MiB FLS baseline.')
    _require(hashlib.sha256(data).hexdigest() == TRUSTED_FLS_SHA256, 'trusted_image',
             'The trusted FLS differs from the preserved 361830 baseline.')
    authentication_material(data)
    return data


def _values(result, name):
    _require(isinstance(result, dict) and isinstance(result.get('result_sets'), list),
             'malformed_result', 'The diagnostic result has an unexpected structure.')
    rows = result['result_sets']
    _require(all(isinstance(row, dict) for row in rows), 'malformed_result',
             'The diagnostic result has an unexpected structure.')
    return [row[name] for row in rows if name in row]


def _job_ok(result):
    statuses = _values(result, 'JOB_STATUS')
    _require(bool(statuses) and all(isinstance(value, str) and value.strip() == 'OKAY'
                                   for value in statuses),
             'job_status', 'The ECU job did not complete with OKAY.')


def _binary(result, name):
    values = _values(result, name)
    _require(len(values) == 1, 'malformed_result',
             'A required binary diagnostic result is missing or ambiguous.')
    value = values[0]
    # NativeEdiabas returns canonical hex. Do not guess how ResultText renders
    # bytes or accept printable nonce strings as a replacement for ResultBinary.
    _require(isinstance(value, str) and bool(re.fullmatch(r'(?:[0-9A-Fa-f]{2})+', value)),
             'malformed_result', 'A binary diagnostic result has an invalid encoding.')
    return bytes.fromhex(value)


def _kwp_payload(frame, *, reply):
    """Parse a STD:OBD KWP telegram and validate source, destination and length.

    xsend result buffers may include or omit the checksum; when present it is
    verified. The B8 transport format has an explicit length, as shown by the
    PRG's KWP2000* branch. Unrecognised framing fails closed.
    """
    _require(4 <= len(frame) <= 260, 'telegram', 'Diagnostic telegram length is invalid.')
    destination, source = (0xf1, 0x12) if reply else (0x12, 0xf1)
    _require(frame[1:3] == bytes((destination, source)), 'telegram',
             'Diagnostic telegram addresses do not match this DDE504 connection.')
    _require(frame[0] & 0xc0 == 0x80, 'telegram',
             'The diagnostic telegram format is unsupported.')
    if frame[0] == 0xb8 or frame[0] & 0x3f == 0:
        length, offset = frame[3], 4
    else:
        length, offset = frame[0] & 0x3f, 3
    _require(length > 0 and len(frame) in (offset + length, offset + length + 1),
             'telegram', 'Diagnostic telegram payload length is invalid.')
    if len(frame) == offset + length + 1:
        _require(sum(frame[:-1]) & 0xff == frame[-1], 'telegram',
                 'Diagnostic telegram checksum is invalid.')
    return frame[offset:offset + length]


def _positive(result, expected, *, request):
    _job_ok(result)
    actual_request = _kwp_payload(_binary(result, '_TEL_AUFTRAG'), reply=False)
    _require(actual_request == request, 'request_mismatch',
             'The PRG generated a different request from the bounded contract.')
    answer = _kwp_payload(_binary(result, '_TEL_ANTWORT'), reply=True)
    if answer[:1] == b'\x7f':
        raise AuthenticationFailure('negative_response',
                                    'The ECU returned a negative diagnostic response.')
    _require(answer == expected, 'positive_response',
             'The ECU did not return the expected positive diagnostic response.')


def _read_bosch_identity(api, identifier, cancelled):
    _check_cancel(cancelled)
    command = bytes((0x1a, identifier))
    result = api.authentication_job(ECU, 'TEL_ROH', binary=command.hex().upper().encode('ascii'),
                                    cancelled=cancelled,
                                    binary_results=('RESPONSE', '_TEL_AUFTRAG', '_TEL_ANTWORT'))
    response = _binary(result, 'RESPONSE')
    _require(len(response) == 12 and response[:2] == bytes((0x5a, identifier))
             and bool(re.fullmatch(rb'[0-9]{10}', response[2:])), 'identity',
             'The Bosch identification response is malformed or unavailable.')
    _positive(result, response, request=command)
    return response[2:].decode('ascii')


def authenticate_ecu(bin_dir, trusted_fls, *, cancelled=None, progress=None,
                     api_factory=NativeEdiabas,
                     response_calculator=calculate_installed_response):
    """Test fresh level-3 authentication; return only a sanitised JSON report.

    All operations use one EDIABAS handle, which is closed on every exit. The
    report proves acceptance only when BOTH PRG OKAY and exact 71/08/01 match.
    A cancellation/timeout after response dispatch may leave the ECU's volatile
    authentication state unknown. No reset or retry is sent to clean it up.
    """
    started = time.monotonic()
    report = dict(format='BMWBinEditor-DDE5-authentication-test-v1',
                  authenticated=False, authentication_sent=False,
                  nonce_requested=False, nonce_request_attempted=False,
                  authentication_dispatch_attempted=False, erase_sent=False, write_sent=False,
                  reset_sent=False, programming_mode_requested=False,
                  programming_enabled=False, flash_ready=False, cancelled=False,
                  ecu_authentication_state='not_requested',
                  transport='Installed EDIABAS / STD:OBD',
                  identity={}, stages=[], error=None, error_code=None)
    stage = 'preflight'

    def record(name, message, status='ok'):
        nonlocal stage
        stage = name
        report['stages'].append(dict(stage=name, status=status, message=message))
        if progress:
            progress(message)

    try:
        _check_cancel(cancelled)
        trusted = _validate_trusted_fls(trusted_fls)
        # Establish availability of the pinned provider/key before opening the
        # ECU connection. The synthetic response is never sent or recorded.
        trial_id, trial_nonce = b'BMWB', bytes(8)
        trial = response_calculator(trial_id, trial_nonce, trusted)
        _require(isinstance(trial, bytes) and len(trial) == 68
                 and verify_response(trial_id, trial_nonce, trial, trusted)['valid'],
                 'key_provider', 'The installed key provider failed its offline preflight.')
        del trial
        report['trusted_fls_sha256'] = TRUSTED_FLS_SHA256
        record('preflight', 'Preserved FLS and installed authentication provider validated offline.')
        _check_cancel(cancelled)
        stage = 'connection'
        with api_factory(bin_dir) as api:
            pin = api.authentication_prg()
            _require(isinstance(pin, dict) and pin.get('sha256') == TRUSTED_PRG_SHA256,
                     'prg_source', 'The configured DDE504 PRG differs from the reviewed implementation.')
            report['prg_source'] = {name: str(pin[name]) for name in
                                    ('path', 'sha256', 'configuration_source') if name in pin}
            record('connection', 'EDIABAS connected with the reviewed DDE504 PRG.')
            _check_cancel(cancelled)
            stage = 'identity'
            ident = api.read_job(ECU, 'IDENT', cancelled=cancelled)
            _job_ok(ident)
            # IDENT fields are BMW part numbers/version triplets. Bosch numbers
            # are read explicitly with 1A92/1A94 instead of guessing from them.
            hardware = _read_bosch_identity(api, 0x92, cancelled)
            report['identity']['hardware'] = hardware
            _require(hardware == TARGET_HARDWARE, 'identity',
                     'The connected Bosch hardware differs from target 0281010565.')
            software = _read_bosch_identity(api, 0x94, cancelled)
            report['identity']['software'] = software
            _require(software == TARGET_SOFTWARE, 'identity',
                     'The connected Bosch software differs from target 1037361830.')
            report['identity']['matched'] = True
            record('identity', 'Connected Bosch hardware 0281010565 and software 1037361830 matched.')
            # Factory metadata and executable contract place routines 07/08 in
            # Default mode. A guessed ECUPM transition or reset is not necessary
            # for this test and is deliberately unavailable to this backend.
            record('session', 'Routines 07 and 08 are documented for Default mode; no session transition was requested.')
            _check_cancel(cancelled)
            # PRG USER_ID is a signed long. Keep the generated ID in its proven
            # positive range and use these exact four BE bytes for the digest.
            caller_value = secrets.randbelow(0x7fffffff) + 1
            caller_id = caller_value.to_bytes(4, 'big')
            stage = 'challenge'
            report['nonce_request_attempted'] = True
            challenge = api.authentication_job(
                ECU, 'AUTHENTISIERUNG_ZUFALLSZAHL_LESEN', parameters=f'3;{caller_value}',
                cancelled=cancelled,
                binary_results=('ZUFALLSZAHL', '_TEL_AUFTRAG', '_TEL_ANTWORT'))
            report['nonce_requested'] = True
            nonce = _binary(challenge, 'ZUFALLSZAHL')
            _require(len(nonce) == 8, 'challenge', 'The ECU challenge is not eight bytes long.')
            _positive(challenge, b'\x71\x07' + nonce,
                      request=b'\x31\x07\x03' + caller_id)
            record('challenge', 'A fresh eight-byte challenge was read and its request verified.')
            _check_cancel(cancelled)
            stage = 'response_calculation'
            response = response_calculator(caller_id, nonce, trusted)
            _require(isinstance(response, bytes) and len(response) == 68
                     and verify_response(caller_id, nonce, response, trusted)['valid'],
                     'response_calculation', 'The calculated response failed the target public-key check.')
            wrapper = prg_authentication_argument(response, wait_seconds=10)
            record('response_calculation', 'Authentication response verified offline for this challenge.')
            _check_cancel(cancelled)
            stage = 'authentication'
            report['authentication_dispatch_attempted'] = True
            report['ecu_authentication_state'] = 'unknown'
            result = api.authentication_job(
                ECU, 'AUTHENTISIERUNG_START', binary=wrapper, timeout=12,
                cancelled=cancelled, binary_results=('_TEL_AUFTRAG', '_TEL_ANTWORT'))
            report['authentication_sent'] = True
            _positive(result, b'\x71\x08\x01', request=b'\x31\x08' + response)
            report['authenticated'] = True
            report['ecu_authentication_state'] = 'accepted_at_response'
            record('authentication', 'The ECU accepted level-3 authentication; no erase or write followed.')
            del response, wrapper, nonce, caller_id, result, challenge
            stage = 'cleanup'
        record('complete', 'EDIABAS handle closed. Authentication test complete.')
    except Exception as exc:
        if isinstance(exc, AuthenticationFailure):
            code, message = exc.code, str(exc)
        elif cancelled and cancelled():
            code, message = 'cancelled', 'Authentication test cancelled.'
        else:
            code, message = _safe_failure(exc, stage)
        # Native transport records only a job name after a successful dispatch,
        # never argument bytes. This distinguishes pre-dispatch setup failure
        # from a timeout/cancellation after a response may have reached the ECU.
        if 'api' in locals():
            dispatched = getattr(api, 'last_authentication_dispatch', None)
            if dispatched == (ECU, 'AUTHENTISIERUNG_START'):
                report['authentication_sent'] = True
            elif dispatched == (ECU, 'AUTHENTISIERUNG_ZUFALLSZAHL_LESEN'):
                report['nonce_requested'] = True
        report['error_code'], report['error'] = code, message
        report['cancelled'] = code == 'cancelled'
        record(stage, message, 'cancelled' if report['cancelled'] else 'failed')
    report['elapsed_ms'] = round((time.monotonic() - started) * 1000)
    report['volatile_authentication_may_remain'] = report['authentication_sent']
    report['session_note'] = ('Closing the API handle does not prove the ECU has relocked. '
                              'The test does not request a reset or an ECUProgrammingMode transition.')
    report['note'] = ('Authentication success is a diagnostic unlock result, not proof that a custom image '
                      'will activate or boot. No ECU memory comparison, erase, write or reset was performed.')
    return report


class ProgrammingPhase(str, Enum):
    PREFLIGHT = 'preflight'
    IDENTITY = 'identity'
    AUTHENTICATION = 'authentication'
    ERASE = 'erase'
    DOWNLOAD = 'download'
    TRANSFER = 'transfer'
    TRANSFER_EXIT = 'transfer_exit'
    SIGNATURE = 'signature'
    PROGRAMMING_STATUS = 'programming_status'
    ACTIVATION = 'activation'
    VERIFICATION = 'verification'


@dataclass(frozen=True)
class ProgrammingStep:
    phase: ProgrammingPhase
    job: str | None
    service: str | None
    contract: str
    live_available: bool = False


@dataclass(frozen=True)
class EraseRegion:
    space: str
    start: int
    end_exclusive: int

    @property
    def byte_count(self):
        return self.end_exclusive - self.start


FACTORY_PROGRAM_ERASE = (
    EraseRegion('MPC', 0x20000, 0x70000),
    EraseRegion('FLS', 0x8c0000, 0x900000),
    EraseRegion('FLS', 0x820000, 0x860000),
)


def factory_erase_regions(address):
    """The normal program erase ignores the caller's requested length."""
    if 0x20000 <= address <= 0x6ffff or 0x820000 <= address <= 0x85ffff:
        return FACTORY_PROGRAM_ERASE
    if 0x8c0000 <= address <= 0x8fffff:
        return (FACTORY_PROGRAM_ERASE[1],)
    raise ValueError('Address does not select a reviewed factory erase path.')


def factory_download_payload(address, byte_count, *, data_format=0x06):
    """Pure eight-byte service-34/37 payload, excluding the SID/transport header.

    The protected virtual header window is staged at address +0xD8000. Lower
    flash-driver acceptance, erase state and full-sector preservation are not
    established by this argument builder.
    """
    if (not isinstance(address, int) or isinstance(address, bool)
            or not isinstance(byte_count, int) or isinstance(byte_count, bool)
            or byte_count <= 0 or byte_count > 0xffffff):
        raise ValueError('Download address and positive size must be integer values.')
    end = address + byte_count
    if not (0x20000 <= address < end <= 0x70000
            or 0x808000 <= address < end <= 0x8ff000):
        raise ValueError('Download exceeds the reviewed factory address/length bounds.')
    if data_format not in (0x02, 0x06, 0x09):
        raise ValueError('Unsupported factory data format.')
    return address.to_bytes(3, 'big') + bytes((data_format, 0)) + byte_count.to_bytes(3, 'big')


def transfer_data_payloads(data, *, maximum_data_bytes=245):
    """Pure service-36 payload chunks; no invented caller sequence byte.

    The ECU increments its own 16-bit counter and returns counter-high,
    counter-low, status=1. RAM/physical drivers separately use 64-byte pages.
    """
    if (not isinstance(maximum_data_bytes, int) or isinstance(maximum_data_bytes, bool)
            or not 1 <= maximum_data_bytes <= 245):
        raise ValueError('The evidenced maximum transfer payload is 245 bytes.')
    value = bytes(data)
    return tuple(value[offset:offset + maximum_data_bytes]
                 for offset in range(0, len(value), maximum_data_bytes))


def programming_sequence_plan():
    """Typed reviewable sequence with explicit unvalidated activation boundaries."""
    return (
        ProgrammingStep(ProgrammingPhase.PREFLIGHT, None, None,
                        'Pinned PRG, trusted authentication FLS, complete baseline and valid image checks.'),
        ProgrammingStep(ProgrammingPhase.IDENTITY, 'TEL_ROH', '1A/92,1A/94',
                        'Exact Bosch hardware/software reads before state-changing requests.', True),
        ProgrammingStep(ProgrammingPhase.AUTHENTICATION,
                        'AUTHENTISIERUNG_ZUFALLSZAHL_LESEN + AUTHENTISIERUNG_START', '31/07,31/08',
                        'Fresh level-3 caller ID/nonce, offline RSA check, exact positive 71/08/01.', True),
        ProgrammingStep(ProgrammingPhase.ERASE, 'FLASH_LOESCHEN', '31/02',
                        'Factory program vector erases MPC 20000..6FFFF, FLS 8C0000..8FFFFF and 820000..85FFFF.'),
        ProgrammingStep(ProgrammingPhase.DOWNLOAD, 'FLASH_SCHREIBEN_ADRESSE', '34',
                        'Eight payload bytes: address BE24, format, zero, length BE24; virtual header stages +D8000.'),
        ProgrammingStep(ProgrammingPhase.TRANSFER, 'FLASH_SCHREIBEN', '36',
                        'At most 245 data bytes; require exact ECU counter progression and status 1; no automatic retry.'),
        ProgrammingStep(ProgrammingPhase.TRANSFER_EXIT, 'FLASH_SCHREIBEN_ENDE', '37',
                        'Eight address/format/length payload bytes; does not automatically invoke signature routine 09.'),
        ProgrammingStep(ProgrammingPhase.SIGNATURE, 'FLASH_SIGNATUR_PRUEFEN', '31/09',
                        'Fresh RSA image verification and marker stamping must match the selected acceptance strategy.'),
        ProgrammingStep(ProgrammingPhase.PROGRAMMING_STATUS, 'FLASH_PROGRAMMIER_STATUS_LESEN', '31/0A',
                        'Interpret the ECU programming-status byte against the proven write/stamp/commit lifecycle.'),
        ProgrammingStep(ProgrammingPhase.ACTIVATION, None, None,
                        'Full programming-session entry, preserved data restoration, marker/commit lifecycle and reset remain unvalidated.'),
        ProgrammingStep(ProgrammingPhase.VERIFICATION, None, None,
                        'Reconnection, changed-image readback, boot verification and bench recovery remain unvalidated.'),
    )


def dry_run_programming_sequence(mpc, fls):
    """Inspect supplied images and sequence scope without opening EDIABAS.

    This deliberately emits no immediately executable write plan: the erased
    data/staging sector includes persistent contents whose restoration order,
    coverage and activation contract are still unresolved.
    """
    mpc, fls = bytes(mpc), bytes(fls)
    checks = inspect_images(mpc, fls, trusted_mpc=mpc, trusted_fls=fls)
    steps = [dict(asdict(step), phase=step.phase.value) for step in programming_sequence_plan()]
    return dict(format='BMWBinEditor-DDE5-factory-sequence-dry-run-v1', dry_run=True,
                erase_sent=False, write_sent=False, programming_enabled=False, flash_ready=False,
                steps=steps, image_checks=checks,
                factory_erase_regions=[dict(asdict(region), bytes=region.byte_count)
                                       for region in FACTORY_PROGRAM_ERASE],
                factory_program_erased_bytes=sum(region.byte_count for region in FACTORY_PROGRAM_ERASE),
                maximum_transfer_data_bytes=245, native_partial_supported=False,
                blockers=[
                    'Live programming-session entry and sustained authentication have not been tested.',
                    'Factory erase removes the whole 256 KiB data/staging sector; preserved-data restoration is unresolved.',
                    'Factory download stops at 8FF000, so coverage of the rest of the erased sector requires a proven preservation route.',
                    'Custom image signature/marker acceptance, commit, verification and recovery remain unvalidated.',
                    'Selective MPC-sector access uses a separate unvalidated boot programming path.',
                ])


def execute_programming_sequence(*_args, **_kwargs):
    """Fail before any dispatch, even when handed an already authenticated API."""
    raise ProgrammingError('Live erase/write/activation is disabled; only the bounded authentication test is available.')
