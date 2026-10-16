"""Direct EDIABAS diagnostics and offline DDE5 write preparation.

Offline checksum, signature and authentication calculations are implemented
in the DDE5 modules. A complete custom programming/activation/recovery session
has not been validated. Identification/logging and a narrowly bounded,
non-erasing authentication transport are exposed separately.
"""
from __future__ import annotations

import ctypes as ct
import configparser
import hashlib
import math
import os
from pathlib import Path
import re
import struct
import threading
import time


class ProgrammingError(RuntimeError):
    pass


READ_JOBS = frozenset({
    ('28dde504', 'IDENT'), ('28dde504', 'PHYSIKALISCHE_HW_NR_LESEN'),
    ('28dde504', 'HARDWARE_REFERENZ_LESEN'), ('28dde504', 'DATEN_REFERENZ_LESEN'),
    ('28dde504', 'FLASH_BLOCKLAENGE_LESEN'),
    ('dde50k47', 'IDENT'), ('dde50k47', 'STATUS_UBATT'),
})
WRITE_BLOCKERS = (
    'DDE504 live programming-session entry and authentication are unvalidated.',
    'Custom-image signature/marker acceptance and activation remain unvalidated.',
    'Factory program erase selects whole ranges; selective boot-mode erase is unvalidated.',
    'ECU-to-image identity checks, post-write verification and recovery are unvalidated.',
)

# This is intentionally not a generic programming-job allowlist. The binary
# call can carry embedded NUL bytes, but only a fixed authentication response
# is admitted. Erase, download, data transfer, reset and raw jobs are excluded.
AUTHENTICATION_JOBS = frozenset({
    'AUTHENTISIERUNG_ZUFALLSZAHL_LESEN', 'AUTHENTISIERUNG_START', 'TEL_ROH',
})
_AUTH_BINARY_LIMITS = {'ZUFALLSZAHL': 8, '_TEL_AUFTRAG': 512, '_TEL_ANTWORT': 512, 'RESPONSE': 12}
_AUTH_PRG_SHA256 = 'fcbb5f3caaa03dfe1bda6b00d27e63ad1a84202c0b7405ffa77347c5e80fdb46'


def pe_machine(path):
    data = Path(path).read_bytes()
    try:
        pe = struct.unpack_from('<I', data, 0x3c)[0]
        if data[:2] != b'MZ' or data[pe:pe+4] != b'PE\0\0':
            raise ValueError()
        return struct.unpack_from('<H', data, pe+4)[0]
    except (ValueError, struct.error):
        raise ProgrammingError('The selected file is not a Windows DLL.') from None


def prepare_change_report(original, edited, space):
    """List differences only; no sector selection or flashable-image claim."""
    original, edited = bytes(original), bytes(edited)
    if space not in {'MPC', 'FLS', 'CONFIG'}:
        raise ProgrammingError('Select MPC, external FLS or config/calibration explicitly.')
    if len(original) != len(edited):
        raise ProgrammingError('Original and edited image sizes differ.')
    sizes = {'FLS': {0x100000}, 'MPC': {0x70000, 0x70200},
             'CONFIG': {0x100000, 0x20000, 0x1f000}}
    valid = len(original) in sizes[space]
    if not valid:
        raise ProgrammingError('Image size does not match the selected 361830 address space.')
    base = (0 if space == 'MPC' else
            0x8e0000 if space == 'CONFIG' and len(original) != 0x100000 else 0x800000)
    changed = []
    for offset, (before, after) in enumerate(zip(original, edited)):
        if before == after:
            continue
        if changed and changed[-1]['end_exclusive'] == offset:
            changed[-1]['end_exclusive'] += 1
            changed[-1]['bytes'] += 1
        else:
            changed.append({'file_offset': offset, 'end_exclusive': offset+1,
                            'runtime_address': base+offset, 'bytes': 1})
    ids = lambda data: sorted(set(x.decode() for x in re.findall(rb'1037\d{6}', data)))
    outside = []
    # MPC range evidenced by the matching factory update. External-FLS program
    # and calibration ranges are not inferred from FF bytes in sparse exports.
    if space == 'MPC':
        outside = [r for r in changed if r['file_offset'] < 0x20000 or r['end_exclusive'] > 0x70000]
    return dict(format='BMWBinEditor-DDE5-change-review-v1', address_space=space,
                image_size=len(original), original_sha256=hashlib.sha256(original).hexdigest(),
                edited_sha256=hashlib.sha256(edited).hexdigest(),
                original_software_ids=ids(original), edited_software_ids=ids(edited),
                changed_bytes=sum(r['bytes'] for r in changed), changed_ranges=changed,
                outside_factory_MPC_application=outside,
                baseline_matches_connected_ECU=False, checksums_corrected=False,
                flash_ready=False, blockers=list(WRITE_BLOCKERS),
                note='Editor original is a file baseline, not a verified read of the connected ECU. '
                     'FF-filled areas are not proof of erase state or complete sector contents.')


class NativeEdiabas:
    """Handle-aware BMW __api* ABI. Loads the installed API; no GUI tools."""
    _lock = threading.Lock()

    def __init__(self, bin_dir=r'C:\EDIABAS\BIN', *, library=None):
        self.bin_dir = Path(bin_dir)
        self.handle = ct.c_uint32(0)
        self.dll = library
        self._directory = None
        self._locked = False
        self._f = {}
        # Records API dispatch only, never arguments, nonce or RSA material.
        self.last_authentication_dispatch = None

    def _bind(self):
        u, w, p, s, i = ct.c_uint32, ct.c_uint16, ct.c_void_p, ct.c_char_p, ct.c_int
        signatures = {
            'InitExt': (i, [ct.POINTER(u), s, s, s, s]),
            'End': (None, [u]), 'Break': (None, [u]), 'State': (i, [u]),
            'ErrorCode': (i, [u]), 'ErrorText': (None, [u, p, i]),
            'Job': (None, [u, s, s, s, s]),
            'ResultSets': (i, [u, ct.POINTER(w)]),
            'ResultNumber': (i, [u, ct.POINTER(w), w]),
            'ResultName': (i, [u, p, w, w]),
            'ResultText': (i, [u, p, s, w, s]),
        }
        for name, (result, args) in signatures.items():
            try:
                fn = getattr(self.dll, '__api'+name)
            except AttributeError:
                raise ProgrammingError('EDIABAS DLL lacks __api'+name) from None
            fn.restype, fn.argtypes = result, args
            self._f[name] = fn

    def _bind_optional(self, name, result, arguments):
        """Bind only on use; old read-only APIs/mocks need no binary exports."""
        if name not in self._f:
            try:
                function = getattr(self.dll, '__api'+name)
            except AttributeError:
                raise ProgrammingError('EDIABAS DLL lacks __api'+name+'.') from None
            function.restype, function.argtypes = result, arguments
            self._f[name] = function
        return self._f[name]

    def __enter__(self):
        if not self._lock.acquire(blocking=False):
            raise ProgrammingError('Another editor EDIABAS operation is running.')
        self._locked = True
        self.last_authentication_dispatch = None
        try:
            if self.dll is None:
                if os.name != 'nt':
                    raise ProgrammingError('Direct EDIABAS requires Windows.')
                bits64 = ct.sizeof(ct.c_void_p) == 8
                path = self.bin_dir / ('api64.dll' if bits64 else 'api32.dll')
                if not path.is_file():
                    raise ProgrammingError(f'EDIABAS API not found: {path}')
                if pe_machine(path) != (0x8664 if bits64 else 0x14c):
                    raise ProgrammingError('EDIABAS DLL architecture does not match this editor.')
                self._directory = os.add_dll_directory(str(self.bin_dir.resolve()))
                self.dll = ct.WinDLL(str(path.resolve()))
            self._bind()
            if not self._f['InitExt'](ct.byref(self.handle), b'STD:OBD', b'', b'BMWBinEditor', b''):
                raise ProgrammingError('EDIABAS initialization failed. Check its installed configuration.')
            if not self.handle.value:
                raise ProgrammingError('EDIABAS returned no session handle.')
            return self
        except Exception:
            self.close()
            raise

    def close(self):
        try:
            if self.handle.value and 'End' in self._f:
                self._f['End'](self.handle.value)
        finally:
            self.handle.value = 0
            try:
                if self._directory:
                    self._directory.close()
            finally:
                self._directory = None
                if self._locked:
                    self._locked = False
                    self._lock.release()

    def __exit__(self, *_):
        self.close()

    def ecu_path(self):
        """Resolve the selected installation's explicit EcuPath without API guesses.

        The documented native wrapper has no __apiConfigGet export. We admit
        only a local STD:OBD configuration with an absolute single ECU directory
        and reject conflicting environment overrides. This does not inspect the DLL's
        internal effective configuration; alternative installations must first
        establish their configuration source rather than bypassing this check.
        """
        for variable in ('ECU', 'EDIABASINIPATH', 'EDIABAS_INI'):
            if os.environ.get(variable):
                raise ProgrammingError('Authentication refuses an EDIABAS configuration environment override: '+variable)
        configured_directory = os.environ.get('EDIABAS_CONFIG_DIR')
        if configured_directory:
            selected = os.path.normcase(str(self.bin_dir.resolve()))
            effective = os.path.normcase(str(Path(configured_directory).resolve()))
            if effective != selected:
                raise ProgrammingError('Authentication refuses a conflicting EDIABAS_CONFIG_DIR environment override.')
        path = self.bin_dir / 'EDIABAS.INI'
        parser = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(';', '#'))
        try:
            parser.read_string(path.read_text(encoding='cp1252'))
            configuration = parser['Configuration']
            if configuration.get('Interface', '').strip().upper() != 'STD:OBD':
                raise ValueError('Interface must be STD:OBD')
            if configuration.get('Simulation', '').strip() != '0':
                raise ValueError('Simulation must be explicitly disabled')
            directory = configuration.get('EcuPath', '').strip()
            ecu_path = Path(directory)
            if (not directory or not ecu_path.is_absolute() or any(c in directory for c in ('%', '$', ';'))
                    or not ecu_path.is_dir()):
                raise ValueError('EcuPath must name one existing absolute directory')
        except (OSError, KeyError, ValueError, configparser.Error) as error:
            raise ProgrammingError('Authentication cannot establish the selected EDIABAS ECU path: '+str(error)) from None
        return ecu_path.resolve()

    def authentication_prg(self):
        """Pin the decoded job implementation before any authentication dispatch."""
        path = self.ecu_path() / '28dde504.prg'
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as error:
            raise ProgrammingError('Authentication PRG cannot be read: '+str(error)) from None
        if digest != _AUTH_PRG_SHA256:
            raise ProgrammingError('Authentication requires the exact decoded 28dde504 PRG; installed file differs.')
        return {'path': str(path), 'sha256': digest,
                'configuration_source': str(self.bin_dir / 'EDIABAS.INI')}

    def read_job(self, ecu, job, timeout=12, *, parameters="", cancelled=None):
        # Check policy BEFORE any call, including for a caller using this class
        # outside the GUI. Programming, reset, raw telegram and output jobs fail.
        ecu, job = ecu.lower(), job.upper()
        measurement = (ecu, job) == ('d50m47a', 'STATUS_MESSWERTBLOCK_LESEN')
        if measurement:
            if not isinstance(parameters, str) or not re.fullmatch(r'JA;[A-Z0-9_]+(?:;[A-Z0-9_]+){0,9}', parameters):
                raise ProgrammingError('Measurement reads require JA and 1â€“10 channel names.')
        elif parameters:
            raise ProgrammingError('Parameters are allowed only for measurement reads.')
        if (ecu, job) not in READ_JOBS and not measurement:
            raise ProgrammingError('This development interface permits identification/read jobs only.')
        if not self.handle.value:
            raise ProgrammingError('EDIABAS session is closed.')
        if not 0 < timeout <= 60:
            raise ProgrammingError('Invalid diagnostic timeout.')
        if cancelled and cancelled():
            raise ProgrammingError('Diagnostic read cancelled.')
        binary = None
        if measurement:
            binary = getattr(self.dll, '__apiResultBinary', None)
            if binary is None:
                raise ProgrammingError('EDIABAS DLL lacks __apiResultBinary.')
            binary.restype = ct.c_int
            binary.argtypes = [ct.c_uint32, ct.c_void_p, ct.POINTER(ct.c_uint16), ct.c_char_p, ct.c_uint16]
        self._f['Job'](self.handle.value, ecu.encode('ascii'), job.encode('ascii'), parameters.encode('ascii'), b'')
        deadline = time.monotonic()+timeout
        while True:
            if cancelled and cancelled():
                self._f['Break'](self.handle.value)
                raise ProgrammingError('Diagnostic read cancelled.')
            state = self._f['State'](self.handle.value)
            if state != 0:
                break
            if time.monotonic() >= deadline:
                self._f['Break'](self.handle.value)
                raise ProgrammingError(f'{job} timed out; no automatic retry was sent.')
            time.sleep(0.005 if measurement else 0.02)
        if state != 1:
            buf = ct.create_string_buffer(1024)
            code = self._f['ErrorCode'](self.handle.value)
            self._f['ErrorText'](self.handle.value, buf, len(buf))
            raise ProgrammingError(f'{job}: EDIABAS {code}: '+buf.value.decode('cp1252', 'replace'))
        sets = ct.c_uint16()
        if not self._f['ResultSets'](self.handle.value, ct.byref(sets)) or sets.value > 256:
            raise ProgrammingError('EDIABAS result-set enumeration failed.')
        results = []
        for rset in range(sets.value+1):
            count = ct.c_uint16()
            if not self._f['ResultNumber'](self.handle.value, ct.byref(count), rset) or count.value > 1024:
                raise ProgrammingError('EDIABAS result enumeration failed.')
            row = {}
            for index in range(1, count.value+1):
                name, value = ct.create_string_buffer(64), ct.create_string_buffer(1024)
                if not self._f['ResultName'](self.handle.value, name, index, rset):
                    raise ProgrammingError('EDIABAS result name unavailable.')
                if binary is not None and name.value in (b'_TEL_AUFTRAG', b'_TEL_ANTWORT'):
                    raw, size = ct.create_string_buffer(65536), ct.c_uint16()
                    if not binary(self.handle.value, raw, ct.byref(size), name.value, rset):
                        raise ProgrammingError('Diagnostic telegram unavailable.')
                    row[name.value.decode('ascii')] = raw.raw[:size.value].hex().upper()
                elif self._f['ResultText'](self.handle.value, value, name.value, rset, b''):
                    row[name.value.decode('cp1252', 'replace')] = value.value.decode('cp1252', 'replace')
            results.append(row)
        statuses = [row['JOB_STATUS'] for row in results if 'JOB_STATUS' in row]
        if not statuses or any(s.strip() != 'OKAY' for s in statuses):
            raise ProgrammingError(f'{job}: unexpected JOB_STATUS: {statuses!r}')
        return {'ecu': ecu, 'job': job, 'result_sets': results}

    def authentication_job(self, ecu, job, *, parameters='', binary=None,
                           timeout=12, cancelled=None, binary_results=()):
        """Send only the evidenced DDE504 challenge or authentication response.

        No job retries or fallback to textual binary conversion are performed.
        Result byte strings are returned as uppercase hex for JSON reports.
        Success means the PRG returned OKAY; the session layer must independently
        validate the positive routine response and its echoed routine number.
        """
        self.last_authentication_dispatch = None
        if not isinstance(ecu, str) or not isinstance(job, str):
            raise ProgrammingError('Authentication ECU/job names must be strings.')
        ecu, job = ecu.lower(), job.upper()
        if ecu != '28dde504' or job not in AUTHENTICATION_JOBS:
            raise ProgrammingError('Only the bounded DDE504 authentication jobs are permitted.')
        if not isinstance(parameters, str):
            raise ProgrammingError('Authentication text arguments must be a string.')
        if job == 'TEL_ROH':
            # The decoded PRG uses hex2y on its binary argument. These ASCII
            # strings are the only admitted raw-job arguments, and represent
            # read-only Bosch hardware/software identification (1A92/1A94).
            if parameters or not isinstance(binary, (bytes, bytearray, memoryview)):
                raise ProgrammingError('Only the two fixed binary identity-read arguments are permitted.')
            binary = bytes(binary)
            if binary not in (b'1A92', b'1A94'):
                raise ProgrammingError('Only the two fixed binary identity-read arguments are permitted.')
        elif job == 'AUTHENTISIERUNG_ZUFALLSZAHL_LESEN':
            match = re.fullmatch(r'3;(0|[1-9][0-9]{0,9})', parameters)
            if binary is not None or not match or int(match.group(1)) > 0xffffffff:
                raise ProgrammingError('Challenge requires level 3 and an unsigned 32-bit caller ID.')
        else:
            if parameters or not isinstance(binary, (bytes, bytearray, memoryview)):
                raise ProgrammingError('Authentication start requires only its binary response wrapper.')
            binary = bytes(binary)
            if len(binary) != 90:
                raise ProgrammingError('Authentication response wrapper must contain exactly 90 bytes.')
            header = bytearray(21)
            header[0], header[4] = 1, binary[4]
            header[13:15] = (68).to_bytes(2, 'little')
            if (binary[:21] != bytes(header) or not 1 <= binary[4] <= 60
                    or binary[21:25] != (16).to_bytes(4, 'big') or binary[-1] != 3):
                raise ProgrammingError('Authentication response wrapper has an invalid type, length, count or terminator.')
        if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
                or not math.isfinite(timeout) or not 0 < timeout <= 60):
            raise ProgrammingError('Invalid authentication timeout.')
        if (not isinstance(binary_results, (tuple, list, set, frozenset))
                or any(not isinstance(name, str) or name not in _AUTH_BINARY_LIMITS
                       for name in binary_results)):
            raise ProgrammingError('Unsupported authentication binary result name.')
        required_binary = set(binary_results)
        if job == 'AUTHENTISIERUNG_ZUFALLSZAHL_LESEN':
            required_binary.add('ZUFALLSZAHL')
        elif job == 'TEL_ROH':
            required_binary.add('RESPONSE')
        if not self.handle.value:
            raise ProgrammingError('EDIABAS session is closed.')
        if cancelled and cancelled():
            raise ProgrammingError('Authentication cancelled before dispatch.')
        self.authentication_prg()
        # Bind every required export before invoking an ECU job. A deficient API
        # must not leave a challenge/session action running without result access.
        result_binary = self._bind_optional('ResultBinary', ct.c_int, [
            ct.c_uint32, ct.c_void_p, ct.POINTER(ct.c_uint16), ct.c_char_p, ct.c_uint16])
        if binary is None:
            self._f['Job'](self.handle.value, ecu.encode('ascii'), job.encode('ascii'),
                           parameters.encode('ascii'), b'')
        else:
            job_data = self._bind_optional('JobData', None, [
                ct.c_uint32, ct.c_char_p, ct.c_char_p, ct.POINTER(ct.c_ubyte),
                ct.c_int, ct.c_char_p])
            buffer = (ct.c_ubyte * len(binary)).from_buffer_copy(binary)
            job_data(self.handle.value, ecu.encode('ascii'), job.encode('ascii'),
                     buffer, len(binary), b'')
        self.last_authentication_dispatch = (ecu, job)
        deadline = time.monotonic()+timeout
        while True:
            if cancelled and cancelled():
                self._f['Break'](self.handle.value)
                raise ProgrammingError('Authentication cancelled; no automatic retry was sent.')
            state = self._f['State'](self.handle.value)
            if state != 0:
                break
            if time.monotonic() >= deadline:
                self._f['Break'](self.handle.value)
                raise ProgrammingError(f'{job} timed out; no automatic retry was sent.')
            time.sleep(0.02)
        if state != 1:
            error = ct.create_string_buffer(1024)
            code = self._f['ErrorCode'](self.handle.value)
            self._f['ErrorText'](self.handle.value, error, len(error))
            raise ProgrammingError(f'{job}: EDIABAS {code}: '+error.value.decode('cp1252', 'replace'))
        sets = ct.c_uint16()
        if not self._f['ResultSets'](self.handle.value, ct.byref(sets)) or sets.value > 32:
            raise ProgrammingError('Authentication result-set enumeration failed or exceeded its limit.')
        results, seen_binary = [], set()
        for rset in range(sets.value+1):
            count = ct.c_uint16()
            if not self._f['ResultNumber'](self.handle.value, ct.byref(count), rset) or count.value > 128:
                raise ProgrammingError('Authentication result enumeration failed or exceeded its limit.')
            row = {}
            for index in range(1, count.value+1):
                if cancelled and cancelled():
                    raise ProgrammingError('Authentication cancelled while collecting results.')
                name_buffer = ct.create_string_buffer(64)
                if not self._f['ResultName'](self.handle.value, name_buffer, index, rset):
                    raise ProgrammingError('Authentication result name unavailable.')
                raw_name = name_buffer.value
                if not re.fullmatch(rb'[A-Z][A-Z0-9_]{0,62}|_[A-Z0-9_]{1,62}', raw_name):
                    raise ProgrammingError('Malformed authentication result name.')
                name = raw_name.decode('ascii')
                if name in row:
                    raise ProgrammingError('Duplicate authentication result name.')
                if name in _AUTH_BINARY_LIMITS:
                    # apiResultBinary has no input-capacity argument. Allocate
                    # the whole uint16 length domain, then enforce our contract.
                    raw, size = ct.create_string_buffer(65536), ct.c_uint16()
                    if not result_binary(self.handle.value, raw, ct.byref(size), raw_name, rset):
                        raise ProgrammingError(f'Authentication binary result {name} unavailable.')
                    if not 0 < size.value <= _AUTH_BINARY_LIMITS[name]:
                        raise ProgrammingError(f'Authentication binary result {name} has an invalid length.')
                    if name == 'ZUFALLSZAHL' and size.value != 8:
                        raise ProgrammingError('Authentication nonce must contain exactly eight bytes.')
                    row[name] = raw.raw[:size.value].hex().upper()
                    seen_binary.add(name)
                else:
                    value = ct.create_string_buffer(1024)
                    if not self._f['ResultText'](self.handle.value, value, raw_name, rset, b''):
                        raise ProgrammingError(f'Authentication text result {name} unavailable.')
                    row[name] = value.value.decode('cp1252', 'replace')
            results.append(row)
        statuses = [row['JOB_STATUS'] for row in results if 'JOB_STATUS' in row]
        if not statuses or any(status.strip() != 'OKAY' for status in statuses):
            raise ProgrammingError(f'{job}: unexpected JOB_STATUS: {statuses!r}')
        missing = required_binary - seen_binary
        if missing:
            raise ProgrammingError('Required authentication binary results are missing: '+', '.join(sorted(missing)))
        return {'ecu': ecu, 'job': job, 'result_sets': results}


def probe_ecu(bin_dir):
    results = []
    with NativeEdiabas(bin_dir) as api:
        for ecu, job in [('28dde504', 'IDENT'), ('28dde504', 'PHYSIKALISCHE_HW_NR_LESEN'),
                         ('28dde504', 'FLASH_BLOCKLAENGE_LESEN')]:
            results.append(api.read_job(ecu, job))
    return {'transport': 'Installed EDIABAS / STD:OBD', 'reads': results,
            'programming_enabled': False, 'blockers': list(WRITE_BLOCKERS)}
