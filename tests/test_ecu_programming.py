import ctypes as ct
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bmw_bin_editor.ecu_programming import NativeEdiabas, ProgrammingError, prepare_change_report, pe_machine


class Function:
    def __init__(self, callback):
        self.callback = callback

    def __call__(self, *args):
        return self.callback(*args)


class FakeApi:
    def __init__(self, states=(1,), status=b'OKAY'):
        self.states = list(states)
        self.status = status
        self.jobs, self.ended, self.aborted = [], [], []
        functions = {
            'InitExt': self.init,
            'End': lambda h: self.ended.append(h),
            'Break': lambda h: self.aborted.append(h),
            'Job': lambda *args: self.jobs.append(args),
            'State': self.state,
            'ErrorCode': lambda _: 19,
            'ErrorText': lambda h, buf, n: setattr(buf, 'value', b'No response'),
            'ResultSets': lambda h, p: self.output(p, 1),
            'ResultNumber': lambda h, p, s: self.output(p, 0 if s == 0 else 1),
            'ResultName': lambda h, buf, i, s: self.text(buf, b'JOB_STATUS'),
            'ResultText': lambda h, buf, name, s, fmt: self.text(buf, self.status),
        }
        for name, fn in functions.items():
            setattr(self, '__api'+name, Function(fn))

    def init(self, pointer, *args):
        assert args[0] == b'STD:OBD'
        pointer._obj.value = 42
        return 1

    def state(self, _):
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]

    def output(self, pointer, value):
        pointer._obj.value = value
        return 1

    def text(self, buffer, value):
        buffer.value = value
        return 1


class ProgrammingTests(unittest.TestCase):
    def test_native_handle_and_job_result(self):
        fake = FakeApi()
        with NativeEdiabas(library=fake) as api:
            result = api.read_job('28DDE504', 'IDENT')
        self.assertEqual(fake.jobs, [(42, b'28dde504', b'IDENT', b'', b'')])
        self.assertEqual(result['result_sets'][1]['JOB_STATUS'], 'OKAY')
        self.assertEqual(fake.ended, [42])

    def test_all_programming_and_raw_jobs_rejected_before_dispatch(self):
        fake = FakeApi()
        with NativeEdiabas(library=fake) as api:
            for job in ['FLASH_LOESCHEN', 'FLASH_SCHREIBEN', 'STEUERGERAETE_RESET',
                        'AUTHENTISIERUNG_START', 'TEL_ROH', 'STEUERN_E_LUEFTER']:
                with self.assertRaises(ProgrammingError):
                    api.read_job('28dde504', job)
        self.assertEqual(fake.jobs, [])

    def test_api_error_closes_session(self):
        fake = FakeApi(states=[3])
        with self.assertRaisesRegex(ProgrammingError, 'No response'):
            with NativeEdiabas(library=fake) as api:
                api.read_job('28dde504', 'IDENT')
        self.assertEqual(fake.ended, [42])

    def test_timeout_aborts_once_no_retry(self):
        fake = FakeApi(states=[0])
        with patch('bmw_bin_editor.ecu_programming.time.monotonic', side_effect=[0, 2]):
            with self.assertRaisesRegex(ProgrammingError, 'timed out'):
                with NativeEdiabas(library=fake) as api:
                    api.read_job('28dde504', 'IDENT', timeout=1)
        self.assertEqual(fake.aborted, [42])
        self.assertEqual(len(fake.jobs), 1)
        self.assertEqual(fake.ended, [42])

    def test_job_status_error_and_missing_status_rejected(self):
        for status in [b'ERROR', b'']:
            with NativeEdiabas(library=FakeApi(status=status)) as api:
                with self.assertRaisesRegex(ProgrammingError, 'JOB_STATUS'):
                    api.read_job('28dde504', 'IDENT')

    def test_connection_is_exclusive_and_released(self):
        with NativeEdiabas(library=FakeApi()):
            with self.assertRaisesRegex(ProgrammingError, 'Another editor'):
                with NativeEdiabas(library=FakeApi()):
                    pass
        with NativeEdiabas(library=FakeApi()):
            pass

    def test_missing_export_releases_lock(self):
        fake = FakeApi()
        delattr(fake, '__apiResultText')
        with self.assertRaisesRegex(ProgrammingError, 'lacks'):
            with NativeEdiabas(library=fake):
                pass
        with NativeEdiabas(library=FakeApi()):
            pass

    def test_changed_offsets_keep_spaces_separate_and_never_flash_ready(self):
        original = bytes(459264)
        edited = bytearray(original)
        edited[0x26ed9] = 12
        report = prepare_change_report(original, edited, 'MPC')
        self.assertEqual(report['changed_ranges'], [dict(file_offset=0x26ed9,
                         end_exclusive=0x26eda, runtime_address=0x26ed9, bytes=1)])
        self.assertFalse(report['flash_ready'])
        self.assertFalse(report['baseline_matches_connected_ECU'])
        self.assertEqual(report['outside_factory_MPC_application'], [])
        edited[10:12] = b'AB'
        report = prepare_change_report(original, edited, 'MPC')
        self.assertEqual(report['outside_factory_MPC_application'][0]['bytes'], 2)
        original = bytes(1048576)
        edited = bytearray(original)
        edited[0x38e03] = 12
        report = prepare_change_report(original, edited, 'FLS')
        self.assertEqual(report['changed_ranges'][0]['runtime_address'], 0x838e03)

    def test_incorrect_image_and_size_change_rejected(self):
        for original, edited, space in [(b'a', b'b', 'MPC'), (b'a', b'ab', 'FLS'),
                                         (bytes(1048576), bytes(1048576), 'AUTO')]:
            with self.assertRaises(ProgrammingError):
                prepare_change_report(original, edited, space)

    def test_malformed_dll_is_not_loaded(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'api.dll'
            path.write_bytes(b'not a DLL')
            with self.assertRaises(ProgrammingError):
                pe_machine(path)


if __name__ == '__main__':
    unittest.main()
