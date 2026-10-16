"""Offline raw response vectors; these tests never open a port or load EDIABAS."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bmw_bin_editor.serial_measurements import SerialMeasurements, decode_measurements, validate_group

ROOT = Path(__file__).resolve().parents[1]
CATALOG = {row['arg']: row for row in json.loads((ROOT / 'definitions/live_parameters.json').read_text(encoding='utf-8'))}
BASE = ['INMOT', 'IMEIA', 'IPLAD', 'SPLAD', 'IALDS', 'IPRDR', 'ILMMG', 'IWHE1',
        'SMOMO', 'IVKMH', 'IWVE1', 'IMVE1', 'ITKUM', 'IMHE1']


def response(words):
    return b'\x6C\x10' + b''.join(word.to_bytes(2, 'big') for word in words)


class SerialMeasurementTests(unittest.TestCase):
    def setUp(self):
        self.decoder = SerialMeasurements(CATALOG)

    def test_all_catalog_channels_are_supported(self):
        self.assertEqual(len(self.decoder.supported_args), 210)
        self.assertEqual(set(self.decoder.supported_args), set(CATALOG))
        for arg in CATALOG:
            with self.subTest(arg=arg):
                self.assertEqual(self.decoder.validate_group([arg]), (arg,))
                self.assertEqual(len(self.decoder.decode_measurements(response([0x8000]), [arg])), 1)

    def test_fast_and_base_groups(self):
        groups = [BASE[:10], BASE[:2] + BASE[10:]]
        self.assertEqual(tuple(map(len, self.decoder.validate_groups(groups))), (10, 6))
        self.assertEqual(self.decoder.selection_payload(BASE[:2]), bytes.fromhex('2C10 002D 0055'))
        self.assertEqual(len(self.decoder.decode_measurements(response([0] * 10), BASE[:10])), 10)

    def test_exact_prg_constants_signed_physical_offsets_and_big_endian(self):
        # These expected literals are independently copied from original PRG
        # MesswerteTab. Do not replace them with idealized Bosch binary ratios.
        cases = [
            ('INMOT', 8000, 1000.016),
            ('IPLAD', 16000, 2000.032),
            ('IALDS', 7350, 73.5),
            ('IPRDR', 32768, 1000.013824),
            ('IMEIA', 0, -99.998993),
            ('IMEIA', 32768, 0.008942999999987267),
            ('ILMMG', 0, -1599.971484),
            ('ITKUM', 0, -50.138440),
            ('IWHE1', 0, -90.351389),
            ('SMOMO', 0, -2500.060084),
            ('IVKMH', 32768, 125.00992),
            ('IMHE1', 32768, 50.003968),
            ('AirCtl_stAirCtlBits10_mp', 65535, 65535.0),
        ]
        for arg, raw, expected in cases:
            with self.subTest(arg=arg, raw=raw):
                values = self.decoder.decode_measurements(response([raw]), [arg])
                self.assertAlmostEqual(values[CATALOG[arg]['result']], expected, places=8)
        values = self.decoder.decode_measurements(bytes.fromhex('6C10 0102'), ['INMOT'])
        self.assertAlmostEqual(values[CATALOG['INMOT']['result']], 32.250516)

    def test_unsigned_word_not_underlying_signed_ram_getter(self):
        values = self.decoder.decode_measurements(response([65535]), ['INMOT'])
        self.assertAlmostEqual(values[CATALOG['INMOT']['result']], 8192.00607)

    def test_strict_service_subfunction_and_response_length(self):
        invalid = [b'', bytes.fromhex('6C'), bytes.fromhex('6C11 0001'),
                   bytes.fromhex('6310 0001'), bytes.fromhex('7F2C31'),
                   bytes.fromhex('6C10'), bytes.fromhex('6C10 0001 0002'),
                   bytes.fromhex('6C10 000100'), '6C100001']
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.decoder.decode_measurements(payload, ['INMOT'])

    def test_invalid_groups_fail_before_any_transport_is_needed(self):
        invalid = [[], ['INMOT'] * 11, ['INMOT', 'INMOT'], ['not_a_channel'],
                   ['INMOT', 1], 'INMOT', None, ['IWSTO', 'RWSTR']]
        for group in invalid:
            with self.subTest(group=group), self.assertRaises(ValueError):
                self.decoder.validate_group(group)
        with self.assertRaises(ValueError):
            self.decoder.validate_groups([['IWSTO'], ['RWSTR']])
        with self.assertRaises(ValueError):
            self.decoder.validate_groups([])

    def test_changed_catalog_cannot_relabel_raw_values(self):
        for field, value in [('arg', 'IPLAD'), ('id', '0x002C'), ('id', '0x012D'),
                             ('id', 45), ('result', 'different'), ('unit', 'bar')]:
            changed = copy.deepcopy(CATALOG)
            changed['INMOT'][field] = value
            decoder = SerialMeasurements(changed)
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                decoder.validate_group(['INMOT'])
        changed = copy.deepcopy(CATALOG)
        decoder = SerialMeasurements(changed)
        changed['INMOT']['id'] = '0x002C'
        self.assertEqual(decoder.selection_payload(['INMOT']), bytes.fromhex('2C10 002D'))

    def test_definition_changes_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'definitions.json'
            original = (ROOT / 'definitions/direct_serial_parameters.json').read_bytes()
            path.write_bytes(original)
            self.assertEqual(len(SerialMeasurements(CATALOG, path).supported_args), 210)
            path.write_bytes(original.replace(b'0.125002', b'0.125000', 1))
            with self.assertRaises(ValueError):
                SerialMeasurements(CATALOG, path)
        with self.assertRaises(ValueError):
            SerialMeasurements(CATALOG, ROOT / 'missing_definition.json')

    def test_convenience_functions_and_binary_views(self):
        self.assertEqual(validate_group(['IALDS'], CATALOG), ('IALDS',))
        values = decode_measurements(memoryview(response([65535])), ['IALDS'], CATALOG)
        self.assertEqual(values[CATALOG['IALDS']['result']], 655.35)


if __name__ == '__main__':
    unittest.main()
