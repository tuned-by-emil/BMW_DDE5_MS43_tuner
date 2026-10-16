"""Offline launch-preset/selector checks; never open COM or modify definitions."""

from optional_resources import requires_resource
import csv
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from bmw_bin_editor.ai_log_analysis import calculate_run, SIGNALS
from bmw_bin_editor.live_logging import load_parameters, make_groups, validate_profile, CsvCapture
from bmw_bin_editor.logdata import SignalCatalog
from bmw_bin_editor.serial_measurements import SerialMeasurements, DEFINITIONS_SHA256


class LaunchPresetTests(unittest.TestCase):
    def setUp(self):
        self.catalog = load_parameters(PROJECT / 'definitions/live_parameters.json')
        presets = json.loads((PROJECT / 'definitions/live_logging_presets.json').read_text(encoding='utf-8'))
        self.launch = next(item for item in presets if item['name'] == 'launch_traction')
        self.economy = next(item for item in presets if item['name'] == 'economy')

    def test_launch_is_exactly_one_ten_parameter_group_with_complete_evidence(self):
        validated = validate_profile(self.launch, self.catalog)
        groups = make_groups(validated['parameters'], self.catalog)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]), 10)
        self.assertEqual(set(groups[0]), {'INMOT', 'IMEIA', 'IPLAD', 'SPLAD', 'IALDS',
                                         'IPRDR', 'ILMMG', 'IVKMH', 'Ad_NumTyp_FreeApplic2_CSTR', 'ITKUM'})
        signals = {self.catalog[arg]['result'] for arg in groups[0]}
        self.assertTrue({SIGNALS[key] for key in ('rpm', 'iq', 'maf', 'speed', 'boost', 'target',
                                                'duty', 'rail', 'coolant', 'ecu_torque_selector')} <= signals)
        self.assertEqual(groups[0][:2], ['INMOT', 'IMEIA'])
        self.assertEqual(validated['interval_ms'], 0)

    def test_selector_display_alias_keeps_verified_identifier_and_wire_scaling(self):
        selector = self.catalog['Ad_NumTyp_FreeApplic2_CSTR']
        self.assertEqual(selector['id'], '0x00E0')
        self.assertEqual(selector['result'], 'STAT_Ad_NumTyp_FreeApplic2_WERT')
        self.assertEqual(selector['label'], 'ECU torque gear selector (CAN-derived)')
        signal_catalog = SignalCatalog.load_tsv(PROJECT / 'definitions/testo_signals.tsv')
        self.assertEqual(signal_catalog.label_for(selector['result']), selector['label'])
        pinned = PROJECT / 'definitions/direct_serial_parameters.json'
        self.assertEqual(hashlib.sha256(pinned.read_bytes()).hexdigest(), DEFINITIONS_SHA256)
        decoder = SerialMeasurements(self.catalog)
        for code in (0, 1, 2, 3, 7, 8, 255):
            with self.subTest(code=code):
                payload = b'\x6c\x10' + code.to_bytes(2, 'big')
                decoded = decoder.decode_measurements(payload, ['Ad_NumTyp_FreeApplic2_CSTR'])
                self.assertEqual(decoded[selector['result']], code)

    def test_launch_selection_payload_contains_e0_and_all_ten_raw_words_decode(self):
        decoder = SerialMeasurements(self.catalog)
        groups = make_groups(self.launch['parameters'], self.catalog)
        self.assertEqual(decoder.validate_groups(groups), (tuple(groups[0]),))
        selected = decoder.selection_payload(groups[0])
        expected_ids = [int(self.catalog[arg]['id'], 16) for arg in groups[0]]
        self.assertEqual(selected, b'\x2c\x10' + b''.join(value.to_bytes(2, 'big') for value in expected_ids))
        self.assertEqual(expected_ids[8], 0xE0)
        words = [16000, 50000, 20000, 24000, 6000, 32768, 50000, 20000, 2, 25000]
        values = decoder.decode_measurements(b'\x6c\x10' + b''.join(word.to_bytes(2, 'big') for word in words), groups[0])
        self.assertEqual(len(values), 10)
        self.assertEqual(values[SIGNALS['ecu_torque_selector']], 2)
        self.assertGreater(values[SIGNALS['rpm']], 0)
        self.assertGreater(values[SIGNALS['iq']], 0)

    def test_single_launch_group_capture_keeps_selector_paired_with_every_row(self):
        groups = make_groups(self.launch['parameters'], self.catalog)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'launch.csv'
            capture = CsvCapture(path, self.launch, groups, self.catalog)
            for index, code in enumerate((1, 2, 3)):
                values = {self.catalog[arg]['result']: 100.0 for arg in groups[0]}
                values[SIGNALS['ecu_torque_selector']] = code
                capture.append(index * .1, '2026-10-14T08:00:00+02:00', 1, str(index), values, {})
            capture.close()
            with path.open(encoding='utf-8-sig') as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual([row[SIGNALS['ecu_torque_selector']] for row in rows], ['1', '2', '3'])
            self.assertTrue(all(row[SIGNALS[key]] for row in rows for key in ('rpm', 'iq', 'maf', 'speed')))
            metadata = json.loads(path.with_suffix('.csv.profile.json').read_text(encoding='utf-8'))
            self.assertEqual(len(metadata['groups']), 1)
            self.assertIn('groups are not simultaneous', metadata['time_basis'])

    def test_adding_more_channels_does_not_fabricate_selector_in_other_groups(self):
        selected = self.launch['parameters'] + ['SMOMO', 'SPRDR']
        groups = make_groups(selected, self.catalog)
        self.assertEqual(list(map(len, groups)), [10, 4])
        self.assertIn('Ad_NumTyp_FreeApplic2_CSTR', groups[0])
        self.assertNotIn('Ad_NumTyp_FreeApplic2_CSTR', groups[1])
        self.assertTrue(all({'INMOT', 'IMEIA'} <= set(group) for group in groups))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sparse.csv'
            profile = dict(self.launch, parameters=selected)
            capture = CsvCapture(path, profile, groups, self.catalog)
            for index, group in enumerate(groups):
                values = {self.catalog[arg]['result']: 1.0 for arg in group}
                capture.append(index * .1, '2026-10-14T08:00:00+02:00', index + 1, str(index), values, {})
            capture.close()
            with path.open(encoding='utf-8-sig') as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(rows[0][SIGNALS['ecu_torque_selector']], '1.0')
            self.assertEqual(rows[1][SIGNALS['ecu_torque_selector']], '')
            self.assertEqual(rows[1][SIGNALS['maf']], '')
            self.assertEqual(rows[1][SIGNALS['speed']], '')

    def test_economy_profile_sparse_rail_and_coolant_do_not_become_same_row(self):
        groups = make_groups(self.economy['parameters'], self.catalog)
        self.assertEqual(list(map(len, groups)), [10, 6])
        self.assertTrue({'INMOT', 'IMEIA', 'ILMMG', 'IVKMH', 'IPRDR'} <= set(groups[0]))
        self.assertTrue({'INMOT', 'IMEIA', 'ITKUM', 'SPRDR'} <= set(groups[1]))
        self.assertNotIn('ITKUM', groups[0])
        self.assertNotIn('SPRDR', groups[0])
        self.assertNotIn('Ad_NumTyp_FreeApplic2_CSTR', set(sum(groups, [])))

    @requires_resource('local_resources/launch.csv')
    def test_actual_old_launch_stays_scoped_and_does_not_gain_new_selector(self):
        path = (Path(__file__).resolve().parents[1] / 'local_resources' / 'launch.csv')
        with path.open(encoding='utf-8-sig') as stream:
            source = list(csv.DictReader(stream))
        names = list(SIGNALS.values())
        log = {'columns': ['time_s'] + names,
               'rows': [[float(row['time']), {str(index + 1): float(row[name]) for index, name in enumerate(names)
                         if row.get(name)}] for row in source]}
        result = calculate_run(log, '8HP45, 3.07 rear end', launch_index=0)
        self.assertEqual(result['selected_run']['start_s'], 430.843)
        self.assertEqual(result['selected_run']['end_s'], 438.093)
        self.assertEqual(result['peaks']['power']['crank_bhp_by_assumed_efficiency']['40%'], 276.1)
        self.assertNotIn('ecu_torque_selector', result['peaks']['power'])
        self.assertNotIn('ecu_torque_selector', result['coherent_channel_statistics'])


if __name__ == '__main__':
    unittest.main()
