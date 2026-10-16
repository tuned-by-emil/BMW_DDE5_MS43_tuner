"""Native import verifies source identity, serialized coverage, and exact slots."""
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bmw_bin_editor.core import EditorError
from bmw_bin_editor import ols_import


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'mappacks' / 'BMW_30D_EDC16_218PS__E60.ols'


class OLSImportValidationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_invalid_truncated_and_unsupported_headers_do_not_start_helper(self):
        for data, message in ((b'', 'header'), (b'WinOLS File', 'header'),
                              (ols_import._HEADER + struct.pack('<II', 439, 24), 'format 439')):
            path = self.folder / 'invalid.ols'
            path.write_bytes(data)
            with self.subTest(data=data), patch.object(ols_import, '_run_parser') as helper:
                with self.assertRaisesRegex(EditorError, message):
                    ols_import.load_ols(path)
                helper.assert_not_called()

    def test_invalid_metadata_strings_do_not_start_helper(self):
        path = self.folder / 'invalid.ols'
        path.write_bytes(ols_import._HEADER + struct.pack('<II', 113, 8193))
        with patch.object(ols_import, '_run_parser') as helper:
            with self.assertRaisesRegex(EditorError, 'string'):
                ols_import.load_ols(path)
            helper.assert_not_called()

    def test_packaged_helper_resolves_inside_meipass(self):
        with patch.object(sys, '_MEIPASS', str(self.folder), create=True):
            self.assertEqual(ols_import._helper_path(), self.folder / 'assets' / 'ols' / 'ols_dump.exe')

    def test_missing_helper_has_actionable_failure(self):
        with patch.object(ols_import, '_helper_path', return_value=self.folder / 'missing.exe'):
            with self.assertRaisesRegex(EditorError, 'bundled OLS parser is missing'):
                ols_import._run_parser(b'input')

    def test_timeout_is_bounded_and_native_process_is_hidden(self):
        fake_helper = self.folder / 'helper.exe'
        fake_helper.write_bytes(b'placeholder')
        with patch.object(ols_import, '_helper_path', return_value=fake_helper), \
                patch.object(subprocess, 'run', side_effect=subprocess.TimeoutExpired('helper', 45)) as run:
            with self.assertRaisesRegex(EditorError, '45-second limit'):
                ols_import._run_parser(b'input')
        self.assertEqual(run.call_args.kwargs['timeout'], 45)
        self.assertFalse(run.call_args.kwargs['check'])
        if os.name == 'nt':
            self.assertEqual(run.call_args.kwargs['creationflags'], subprocess.CREATE_NO_WINDOW)


@unittest.skipUnless(os.name == 'nt' and SOURCE.is_file() and ols_import._helper_path().is_file(),
                     'Optional local OLS integration fixture and Windows helper are required')
class OLSImportPackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project = ols_import.load_ols(SOURCE)
        cls.source = SOURCE.read_bytes()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def native_output(self):
        return {'header': {'format_version': 113, 'file_size': len(self.source), 'valid': True},
                'metadata': {}, 'maps': deepcopy(self.project.maps), 'warnings': []}

    def test_exact_slots_complete_definitions_and_correct_identity(self):
        project = self.project
        self.assertEqual(project.title, 'EDC16C31 361888')
        self.assertEqual(project.metadata['hw_number'], '0281011120')
        self.assertEqual(len(project.maps), 9531)
        self.assertEqual(len(project.folders), 444)
        self.assertTrue(any(record['name'] == 'BNSCLRAPP' for record in project.maps))
        self.assertEqual([version.name for version in project.versions], ['Original', 'Version 1'])
        self.assertEqual([len(version.data) for version in project.versions], [2097152, 2097152])
        self.assertEqual(project.versions[0].data, project.versions[1].data)
        self.assertEqual(project.versions[0].sha256,
                         '26d24996e5f420d99f835acd6112ad3f068e25f95eb5910a68e59030b37df05e')
        self.assertEqual(project.source_sha256, hashlib.sha256(self.source).hexdigest())
        prefix = project.source_data[:project.metadata['settings_prefix_length']]
        self.assertEqual(prefix + b''.join(version.data for version in project.versions), self.source)
        self.assertEqual(SOURCE.read_bytes(), self.source)

    def test_screenshot_axis_and_cell_evidence_survives_parser(self):
        record = next(item for item in self.project.maps if item['raw_address'] == 0xCF358)
        self.assertEqual(record['x_axis']['pts_values'][:8], [500, 700, 800, 1000, 1250, 1500, 1750, 2000])
        self.assertEqual(record['y_axis']['pts_values'], list(range(2500, 10001, 500)))
        self.assertAlmostEqual(record['y_axis']['scale'], 0.1)
        raw = struct.unpack_from('>256h', self.project.versions[0].data, 0xCF358)
        first_row = [raw[column * 16] * record['scale'] + record['offset'] for column in range(8)]
        for actual, expected in zip(first_row, [0.8, 1, 1.06, 1.24, 1.24, 1.23, 1.22, 1.22]):
            self.assertAlmostEqual(actual, expected)

    def test_truncated_spoofed_or_appended_slot_directory_is_rejected(self):
        directory = self.project.metadata['directory_evidence']
        spoofed = bytearray(self.source)
        start = directory['version_data_directory_offset']
        spoofed[start:start + 4] = b'FAKE'
        wrong_stride = bytearray(self.source)
        struct.pack_into('<I', wrong_stride, directory['version_count_minus_one_offset'] + 8, 1048576)
        for index, data in enumerate((self.source[:-1], self.source + b'\0', bytes(spoofed), bytes(wrong_stride))):
            path = self.folder / f'invalid{index}.ols'
            path.write_bytes(data)
            with self.subTest(index=index), patch.object(ols_import, '_run_parser') as helper:
                with self.assertRaisesRegex(EditorError, 'version directory'):
                    ols_import.load_ols(path)
                helper.assert_not_called()

    def test_changed_source_is_rejected_before_conversion(self):
        path = self.folder / 'changing.ols'
        path.write_bytes(self.source)
        def change_source(snapshot):
            self.assertEqual(snapshot, self.source)
            path.write_bytes(self.source[:-1])
            return {'header': {}}
        with patch.object(ols_import, '_run_parser', side_effect=change_source):
            with self.assertRaisesRegex(EditorError, 'changed during import'):
                ols_import.load_ols(path)

    def test_native_record_spoof_and_partial_coverage_are_rejected(self):
        path = self.folder / 'project.ols'
        path.write_bytes(self.source)
        output = self.native_output()
        output['maps'][0]['source_record_hex'] = '00'
        with patch.object(ols_import, '_run_parser', return_value=output):
            with self.assertRaisesRegex(EditorError, 'record span'):
                ols_import.load_ols(path)
        output['maps'] = output['maps'][1:]
        with patch.object(ols_import, '_run_parser', return_value=output):
            with self.assertRaisesRegex(EditorError, 'record count'):
                ols_import.load_ols(path)


if __name__ == '__main__':
    unittest.main()
