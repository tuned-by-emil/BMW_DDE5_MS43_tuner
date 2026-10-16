"""Native ABI/policy tests use callbacks exclusively: no DLL or ECU access."""
import ctypes as ct
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ecu_programming import FakeApi, Function
from bmw_bin_editor.ecu_programming import NativeEdiabas, ProgrammingError
from bmw_bin_editor.dde5_auth import encode_response, prg_authentication_argument

AUTH = 'AUTHENTISIERUNG_START'
CHALLENGE = 'AUTHENTISIERUNG_ZUFALLSZAHL_LESEN'


class AuthenticationApi(FakeApi):
    def __init__(self, states=(1,), rows=None):
        super().__init__(states=states)
        self.data_jobs, self.text_names, self.binary_names = [], [], []
        self.rows = rows if rows is not None else [{}, {
            'JOB_STATUS': b'OKAY', 'ZUFALLSZAHL': b'\x00\x01\x02\x00\xff\x00\x06\x07',
            '_TEL_AUFTRAG': b'\x31\x07\x03\x00\x00\x00\x01',
            '_TEL_ANTWORT': b'\x71\x07\x00\x01\x02\x00\xff\x00\x06\x07'}]
        setattr(self, '__apiJobData', Function(self.data_job))
        setattr(self, '__apiResultSets', Function(lambda h, p: self.output(p, len(self.rows)-1)))
        setattr(self, '__apiResultNumber', Function(lambda h, p, s: self.output(p, len(self.rows[s]))))
        setattr(self, '__apiResultName', Function(lambda h, b, i, s: self.text(b, list(self.rows[s])[i-1].encode())))
        setattr(self, '__apiResultText', Function(self.result_text))
        setattr(self, '__apiResultBinary', Function(self.result_binary))

    def data_job(self, handle, ecu, job, buffer, length, result):
        self.data_jobs.append((handle, ecu, job, ct.string_at(buffer, length), length, result))

    def result_text(self, handle, buffer, name, rset, fmt):
        self.text_names.append(name.decode())
        return self.text(buffer, self.rows[rset][name.decode()])

    def result_binary(self, handle, buffer, size, name, rset):
        self.binary_names.append(name.decode())
        value = self.rows[rset][name.decode()]
        ct.memmove(buffer, value, len(value))
        size._obj.value = len(value)
        return 1


class AuthenticationTransportTests(unittest.TestCase):
    def setUp(self):
        self.pin = patch.object(NativeEdiabas, 'authentication_prg', return_value={'path':'mock', 'sha256':'mock'})
        self.pin.start()
        self.addCleanup(self.pin.stop)

    def test_challenge_is_text_and_nonce_is_binary_without_nul_truncation(self):
        fake = AuthenticationApi()
        with NativeEdiabas(library=fake) as api:
            output = api.authentication_job('28DDE504', CHALLENGE, parameters='3;4294967295',
                                            binary_results=('_TEL_AUFTRAG', '_TEL_ANTWORT'))
        self.assertEqual(fake.jobs, [(42, b'28dde504', CHALLENGE.encode(), b'3;4294967295', b'')])
        self.assertEqual(fake.data_jobs, [])
        self.assertEqual(output['result_sets'][1]['ZUFALLSZAHL'], '00010200FF000607')
        self.assertEqual(fake.text_names, ['JOB_STATUS'])
        self.assertEqual(fake.ended, [42])

    def test_response_uses_void_jobdata_unsigned_buffer_exact_int_length(self):
        fake = AuthenticationApi(rows=[{}, {'JOB_STATUS':b'OKAY', '_TEL_ANTWORT':b'\x71\x08\x01'}])
        wrapper = prg_authentication_argument(encode_response(0xAB000000CD))
        with NativeEdiabas(library=fake) as api:
            api.authentication_job('28dde504', AUTH, binary=wrapper, binary_results=('_TEL_ANTWORT',))
        self.assertEqual(fake.data_jobs, [(42, b'28dde504', AUTH.encode(), wrapper, 90, b'')])
        self.assertEqual(fake.jobs, [])
        function = getattr(fake, '__apiJobData')
        self.assertIsNone(function.restype)
        self.assertEqual(function.argtypes, [ct.c_uint32, ct.c_char_p, ct.c_char_p,
                                              ct.POINTER(ct.c_ubyte), ct.c_int, ct.c_char_p])

    def test_only_two_decoded_ascii_identity_requests_admitted(self):
        for payload in (b'1A92', b'1A94'):
            fake = AuthenticationApi(rows=[{}, {'JOB_STATUS':b'OKAY','RESPONSE':b'\x5a\x92'+b'0281010565'}])
            with NativeEdiabas(library=fake) as api:
                api.authentication_job('28dde504', 'TEL_ROH', binary=payload)
            self.assertEqual(fake.data_jobs[0][3], payload)
        fake = AuthenticationApi()
        with NativeEdiabas(library=fake) as api:
            for payload in (b'\x1a\x92', b'1A 92', b'1a92', b'3102', b'1101', b'3492', b'1A92\0', b'1A9200'):
                with self.assertRaises(ProgrammingError):
                    api.authentication_job('28dde504', 'TEL_ROH', binary=payload)
        self.assertEqual(fake.jobs, [])
        self.assertEqual(fake.data_jobs, [])

    def test_all_flash_reset_outputs_and_wrong_ecu_rejected_before_dispatch(self):
        fake = AuthenticationApi()
        with NativeEdiabas(library=fake) as api:
            for job in ('FLASH_LOESCHEN', 'FLASH_SCHREIBEN_ADRESSE', 'FLASH_SCHREIBEN',
                        'FLASH_SCHREIBEN_ENDE', 'FLASH_SIGNATUR_PRUEFEN', 'STEUERGERAETE_RESET',
                        'AIF_SCHREIBEN', 'FS_LOESCHEN', 'DIAGNOSE_MODE', 'STEUERN_E_LUEFTER'):
                with self.assertRaises(ProgrammingError):
                    api.authentication_job('28dde504', job, binary=b'1A92')
            with self.assertRaises(ProgrammingError):
                api.authentication_job('other', CHALLENGE, parameters='3;1')
        self.assertEqual(fake.jobs, [])
        self.assertEqual(fake.data_jobs, [])

    def test_bad_challenge_parameters_cannot_reach_api(self):
        fake = AuthenticationApi()
        with NativeEdiabas(library=fake) as api:
            for parameters in ('', '2;1', '3;-1', '3;4294967296', '3;01', '3;1;4', '3;1\x00', '3;0x1234'):
                with self.assertRaises(ProgrammingError):
                    api.authentication_job('28dde504', CHALLENGE, parameters=parameters)
            with self.assertRaises(ProgrammingError):
                api.authentication_job('28dde504', CHALLENGE, parameters='3;1', binary=b'')
        self.assertEqual(fake.jobs, [])

    def test_every_wrapper_field_boundary_validated_before_dispatch(self):
        valid = prg_authentication_argument(encode_response(123))
        malformed = [valid[:-1], valid+b'\x00']
        for offset in (0, 1, 2, 3, 5, 8, 9, 12, 13, 14, 15, 20, 21, 24, 89):
            edited = bytearray(valid)
            edited[offset] ^= 1
            malformed.append(edited)
        for wait in (0, 61, 255):
            edited = bytearray(valid); edited[4] = wait; malformed.append(edited)
        fake = AuthenticationApi()
        with NativeEdiabas(library=fake) as api:
            for binary in malformed:
                with self.assertRaises(ProgrammingError):
                    api.authentication_job('28dde504', AUTH, binary=binary)
            with self.assertRaises(ProgrammingError):
                api.authentication_job('28dde504', AUTH, binary=valid, parameters='3;1')
        self.assertEqual(fake.data_jobs, [])

    def test_optional_binary_exports_are_lazy_and_fail_before_dispatch(self):
        with NativeEdiabas(library=FakeApi()) as api:
            api.read_job('28dde504', 'IDENT')
        for name, job, args in (
                ('ResultBinary', CHALLENGE, {'parameters':'3;1'}),
                ('JobData', AUTH, {'binary':prg_authentication_argument(encode_response(1))})):
            fake = AuthenticationApi()
            delattr(fake, '__api'+name)
            with NativeEdiabas(library=fake) as api:
                with self.assertRaisesRegex(ProgrammingError, 'lacks'):
                    api.authentication_job('28dde504', job, **args)
            self.assertEqual(fake.jobs, [])
            self.assertEqual(fake.data_jobs, [])

    def test_failed_pin_never_dispatches(self):
        fake = AuthenticationApi()
        with patch.object(NativeEdiabas, 'authentication_prg', side_effect=ProgrammingError('PRG differs')):
            with NativeEdiabas(library=fake) as api:
                with self.assertRaisesRegex(ProgrammingError, 'PRG differs'):
                    api.authentication_job('28dde504', CHALLENGE, parameters='3;1')
        self.assertEqual(fake.jobs, [])
        self.assertEqual(fake.ended, [42])

    def test_dispatch_record_distinguishes_preflight_failure_from_sent_timeout(self):
        fake = AuthenticationApi(states=[0])
        with NativeEdiabas(library=fake) as api:
            self.assertIsNone(api.last_authentication_dispatch)
            with patch('bmw_bin_editor.ecu_programming.time.monotonic', side_effect=[0, 2]):
                with self.assertRaisesRegex(ProgrammingError, 'timed out'):
                    api.authentication_job('28dde504', CHALLENGE, parameters='3;1', timeout=1)
            self.assertEqual(api.last_authentication_dispatch, ('28dde504', CHALLENGE))
            with self.assertRaises(ProgrammingError):
                api.authentication_job('28dde504', AUTH, binary=b'incorrect')
            self.assertIsNone(api.last_authentication_dispatch)

    def test_reused_native_object_starts_a_fresh_dispatch_record(self):
        fake = AuthenticationApi()
        api = NativeEdiabas(library=fake)
        with api:
            api.authentication_job('28dde504', CHALLENGE, parameters='3;1')
        self.assertEqual(api.last_authentication_dispatch, ('28dde504', CHALLENGE))
        with api:
            self.assertIsNone(api.last_authentication_dispatch)

    def test_directory_cleanup_error_still_releases_handle_and_shared_lock(self):
        fake = AuthenticationApi()
        class BadDirectory:
            def close(self):
                raise RuntimeError('directory cleanup failed')
        with self.assertRaisesRegex(RuntimeError, 'directory cleanup'):
            with NativeEdiabas(library=fake) as api:
                api._directory = BadDirectory()
        self.assertEqual(api.handle.value, 0)
        self.assertEqual(fake.ended, [42])
        with NativeEdiabas(library=FakeApi()):
            pass

    def test_cancel_before_dispatch_sends_no_break_or_job(self):
        fake = AuthenticationApi()
        with NativeEdiabas(library=fake) as api:
            with self.assertRaisesRegex(ProgrammingError, 'cancelled'):
                api.authentication_job('28dde504', CHALLENGE, parameters='3;1', cancelled=lambda:True)
        self.assertEqual(fake.jobs, [])
        self.assertEqual(fake.aborted, [])

    def test_cancel_inflight_breaks_once_and_does_not_retry(self):
        fake = AuthenticationApi(states=[0])
        calls = iter([False, False, True])
        with NativeEdiabas(library=fake) as api:
            with self.assertRaisesRegex(ProgrammingError, 'cancelled'):
                api.authentication_job('28dde504', CHALLENGE, parameters='3;1', cancelled=lambda:next(calls))
        self.assertEqual(fake.aborted, [42])
        self.assertEqual(len(fake.jobs), 1)
        self.assertEqual(fake.ended, [42])

    def test_timeout_breaks_once_and_does_not_retry(self):
        fake = AuthenticationApi(states=[0])
        with patch('bmw_bin_editor.ecu_programming.time.monotonic', side_effect=[0, 2]):
            with NativeEdiabas(library=fake) as api:
                with self.assertRaisesRegex(ProgrammingError, 'timed out'):
                    api.authentication_job('28dde504', CHALLENGE, parameters='3;1', timeout=1)
        self.assertEqual(fake.aborted, [42])
        self.assertEqual(len(fake.jobs), 1)
        self.assertEqual(fake.ended, [42])

    def test_api_error_and_negative_job_status_do_not_return_success(self):
        for fake, message in ((AuthenticationApi(states=[3]), 'No response'),
                              (AuthenticationApi(rows=[{}, {'JOB_STATUS':b'ERROR_ECU_REJECTED','ZUFALLSZAHL':b'12345678'}]), 'JOB_STATUS')):
            with NativeEdiabas(library=fake) as api:
                with self.assertRaisesRegex(ProgrammingError, message):
                    api.authentication_job('28dde504', CHALLENGE, parameters='3;1')
            self.assertEqual(len(fake.jobs), 1)
            self.assertEqual(fake.ended, [42])

    def test_binary_result_lengths_and_failures_are_bounded(self):
        for nonce in (b'', b'1234567', b'123456789', b'x'*65535):
            fake = AuthenticationApi(rows=[{}, {'JOB_STATUS':b'OKAY','ZUFALLSZAHL':nonce}])
            with NativeEdiabas(library=fake) as api:
                with self.assertRaises(ProgrammingError):
                    api.authentication_job('28dde504', CHALLENGE, parameters='3;1')
        fake = AuthenticationApi()
        setattr(fake, '__apiResultBinary', Function(lambda *args:0))
        with NativeEdiabas(library=fake) as api:
            with self.assertRaisesRegex(ProgrammingError, 'unavailable'):
                api.authentication_job('28dde504', CHALLENGE, parameters='3;1')
        self.assertEqual(fake.text_names, ['JOB_STATUS'])

    def test_required_binary_missing_and_bad_names_are_rejected(self):
        for rows, expected in (([{}, {'JOB_STATUS':b'OKAY'}], 'missing'),
                               ([{}, {'JOB_STATUS':b'OKAY','bad\x00name':b'1'}], 'Malformed')):
            fake = AuthenticationApi(rows=rows)
            with NativeEdiabas(library=fake) as api:
                with self.assertRaisesRegex(ProgrammingError, expected):
                    api.authentication_job('28dde504', CHALLENGE, parameters='3;1')

    def test_enumeration_limits_stop_bad_api_results(self):
        for export, limit in (('ResultSets',33), ('ResultNumber',129)):
            fake = AuthenticationApi()
            setattr(fake, '__api'+export, Function(lambda h,p,*args:self._output(p,limit)))
            with NativeEdiabas(library=fake) as api:
                with self.assertRaisesRegex(ProgrammingError, 'limit'):
                    api.authentication_job('28dde504', CHALLENGE, parameters='3;1')

    @staticmethod
    def _output(pointer, value):
        pointer._obj.value = value
        return 1

    def test_bad_timeout_result_names_and_closed_session_rejected(self):
        fake = AuthenticationApi()
        with NativeEdiabas(library=fake) as api:
            for timeout in (0, 61, float('nan'), float('inf'), True, '12'):
                with self.assertRaises(ProgrammingError):
                    api.authentication_job('28dde504', CHALLENGE, parameters='3;1', timeout=timeout)
            for names in ('ZUFALLSZAHL', ['RESPONSE_UNKNOWN'], [b'ZUFALLSZAHL']):
                with self.assertRaises(ProgrammingError):
                    api.authentication_job('28dde504', CHALLENGE, parameters='3;1', binary_results=names)
        with self.assertRaisesRegex(ProgrammingError, 'closed'):
            api.authentication_job('28dde504', CHALLENGE, parameters='3;1')
        self.assertEqual(fake.jobs, [])


class AuthenticationConfigurationTests(unittest.TestCase):
    def setup_installation(self, folder, contents=None):
        root = Path(folder)
        bindir, ecudir = root/'bin', root/'ecu'
        bindir.mkdir(); ecudir.mkdir()
        (bindir/'EDIABAS.INI').write_text(contents if contents is not None else
            f'[Configuration]\nInterface=STD:OBD\nSimulation=0\nEcuPath={ecudir}\n', encoding='cp1252')
        return bindir, ecudir

    def test_configuration_resolves_absolute_ecu_directory_and_hash_pin(self):
        with patch.dict(os.environ, {}, clear=True), tempfile.TemporaryDirectory() as folder:
            bindir, ecudir = self.setup_installation(folder)
            api = NativeEdiabas(bindir, library=FakeApi())
            self.assertEqual(api.ecu_path(), ecudir.resolve())
            (ecudir/'28dde504.prg').write_bytes(b'incorrect PRG')
            with self.assertRaisesRegex(ProgrammingError, 'differs'):
                api.authentication_prg()
            (ecudir/'28dde504.prg').unlink()
            with self.assertRaisesRegex(ProgrammingError, 'cannot be read'):
                api.authentication_prg()

    def test_alternate_simulated_ambiguous_or_missing_configuration_fails_closed(self):
        for configuration in ('[Configuration]\nInterface=REMOTE\nSimulation=0\nEcuPath=C:\\EDIABAS\\ECU',
                              '[Configuration]\nInterface=STD:OBD\nSimulation=1\nEcuPath=C:\\EDIABAS\\ECU',
                              '[Configuration]\nInterface=STD:OBD\nSimulation=0\nEcuPath=../ecu',
                              '[Configuration]\nInterface=STD:OBD\nSimulation=0\nEcuPath=%ECU%',
                              '[Configuration]\nInterface=STD:OBD\nInterface=REMOTE\nSimulation=0',
                              '[Other]\nEcuPath=.', ''):
            with patch.dict(os.environ, {}, clear=True), tempfile.TemporaryDirectory() as folder:
                bindir, _ = self.setup_installation(folder, configuration)
                with self.assertRaises(ProgrammingError):
                    NativeEdiabas(bindir, library=FakeApi()).ecu_path()

    def test_environment_override_is_refused(self):
        for name in ('ECU','EDIABAS_CONFIG_DIR','EDIABASINIPATH','EDIABAS_INI'):
            with patch.dict(os.environ, {name:'other'}, clear=True):
                with self.assertRaisesRegex(ProgrammingError, 'override'):
                    NativeEdiabas(library=FakeApi()).ecu_path()

    def test_matching_config_directory_environment_is_accepted(self):
        with tempfile.TemporaryDirectory() as folder:
            bindir, ecudir = self.setup_installation(folder)
            with patch.dict(os.environ, {'EDIABAS_CONFIG_DIR':str(bindir)}, clear=True):
                self.assertEqual(NativeEdiabas(bindir, library=FakeApi()).ecu_path(), ecudir.resolve())


if __name__ == '__main__':
    unittest.main()
