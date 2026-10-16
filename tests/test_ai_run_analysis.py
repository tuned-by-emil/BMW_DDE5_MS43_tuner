"""Log calculation regressions, including the recorded M47 transbrake sequence."""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path
import unittest

from bmw_bin_editor.ai_log_analysis import SIGNALS, calculate_log, calculate_run


def make_log(rows, names=('rpm', 'iq', 'maf', 'speed', 'coolant'), **metadata):
    return dict(columns=['time_s'] + [SIGNALS[name] for name in names], rows=rows, **metadata)


# Exact observed values from the real launch, including alternating request
# groups. No invented interpolation/MAF is inserted for request group 2.
LAUNCH_ROWS = [
    [430.843, {'1': 2029.907, '2': 42.478, '3': 681.271, '4': 0}],
    [431.078, {'1': 2237.911, '2': 50.617, '3': 765.110, '4': 0}],
    [431.359, {'1': 2447.914, '2': 52.186, '3': 848.998, '4': 0}],
    [431.640, {'1': 2668.918, '2': 61.037, '3': 956.275, '4': 0}],
    [431.875, {'1': 2922.922, '2': 69.140, '3': 1129.179, '4': 0}],
    [432.062, {'1': 3105.925, '2': 76.449}],
    [432.203, {'1': 3446.930, '2': 81.729, '3': 1288.508, '4': 5.780}],
    [432.468, {'1': 3830.936, '2': 78.079, '3': 1380.404, '4': 20.979}],
    [432.718, {'1': 4476.947, '2': 79.800, '3': 1231.085, '4': 35.609}],
    [433.000, {'1': 4378.945, '2': 74.588, '3': 1278.889, '4': 47.852}],
    [433.265, {'1': 4591.948, '2': 78.598, '3': 1233.966, '4': 63.890}],
    [433.515, {'1': 4696.950, '2': 76.489, '3': 1213.604, '4': 72.241}],
    [433.781, {'1': 4275.943, '2': 80.698, '3': 1303.303, '4': 80.703}],
    [434.078, {'1': 4360.945, '2': 82.428, '3': 1306.379, '4': 88.760}],
    [434.171, {'1': 4409.946, '2': 81.998}],
    [434.312, {'1': 4460.946, '2': 69.259, '3': 1284.895, '4': 94.601}],
    [434.578, {'1': 4558.948, '2': 77.359, '3': 1199.688, '4': 98.480}],
    [434.875, {'1': 4527.947, '2': 58.888, '3': 1244.904, '4': 100.804}],
    [435.109, {'1': 4256.943, '2': 68.618, '3': 1010.280, '4': 101.113}],
    [435.421, {'1': 3448.930, '2': 68.737, '3': 1055.301, '4': 102.204}],
    [435.703, {'1': 3133.925, '2': 69.747, '3': 1173.272, '4': 98.480}],
    [435.937, {'1': 2949.922, '2': 81.110, '3': 1341.683, '4': 92.804}],
    [436.234, {'1': 2836.920, '2': 88.459, '3': 1462.388, '4': 87.123}],
    [436.515, {'1': 2835.920, '2': 88.459, '3': 1477.769, '4': 84.552}],
    [436.765, {'1': 2767.919, '2': 88.581, '3': 1471.812, '4': 82.992}],
    [437.031, {'1': 2615.917, '2': 88.340, '3': 1460.777, '4': 80.371}],
    [437.296, {'1': 2327.912, '2': 87.391, '3': 1408.774, '4': 75.239}],
    [437.546, {'1': 1889.905, '2': 80.451, '3': 1039.968, '4': 67.651}],
    [438.093, {'1': 2686.918, '2': 1.746, '3': 875.366, '4': 57.820}],
    [438.593, {'1': 2727.919, '2': 29.946, '3': 660.079, '4': 56.912}],
    [546.906, {'1': 4412.946, '2': 83.029, '3': 1283.381, '4': 47.550}],
]


class RunAnalysisTests(unittest.TestCase):
    def test_launch_peak_is_distinct_and_every_sparse_request_group_counts(self):
        result = calculate_run(make_log(LAUNCH_ROWS), '8HP45 and 3.07 rear end', launch_index=0)
        peak = result['peaks']['power']
        self.assertEqual(peak['time_s'], 434.171)
        self.assertEqual(peak['crank_bhp_by_assumed_efficiency']['40%'], 276.1)
        self.assertEqual(peak['crank_nm_by_assumed_efficiency']['40%'], 445.8)
        self.assertEqual(result['whole_log_peak_power']['time_s'], 546.906)
        self.assertEqual(result['whole_log_peak_power']['crank_bhp_by_assumed_efficiency']['40%'], 279.7)
        self.assertNotIn('calculated_lambda', peak)
        self.assertNotIn('air_kg_h', peak)
        self.assertNotIn('speed', peak)
        paired = result['peaks']['power_with_coherent_airflow']
        self.assertEqual(paired['time_s'], 433.265)
        self.assertEqual(paired['calculated_lambda'], 1.083)
        self.assertIn('not measured', result['assumptions']['power_is'])

    def test_standstill_and_before_first_observed_motion_are_not_conflated(self):
        event = calculate_run(make_log(LAUNCH_ROWS), launch_index=0)['selected_launch']
        self.assertEqual(event['loaded_standstill_peak']['time_s'], 431.875)
        self.assertEqual(event['loaded_standstill_peak']['crank_bhp_by_assumed_efficiency']['40%'], 154.3)
        self.assertEqual(event['pre_first_motion_peak']['time_s'], 432.062)
        self.assertEqual(event['pre_first_motion_peak']['crank_bhp_by_assumed_efficiency']['40%'], 181.3)
        self.assertNotIn('speed', event['pre_first_motion_peak'])
        self.assertEqual(event['first_motion']['time_s'], 432.203)
        self.assertEqual(event['first_motion']['crank_bhp_by_assumed_efficiency']['40%'], 215.1)

    def test_shifts_are_candidates_without_invented_gear_or_hookup(self):
        result = calculate_run(make_log(LAUNCH_ROWS), '8HP45 and 3.07 rear end', launch_index=0)
        windows = [(e['start_s'], e['end_s']) for e in result['shift_candidates']]
        self.assertEqual(windows, [(433.515, 433.781), (434.875, 435.421)])
        self.assertNotIn('candidate_gear', result['shift_candidates'][0]['before'])
        traction = result['traction_evidence']
        self.assertIsNone(traction['confirmed_hookup_time_s'])
        self.assertIsNone(traction['confirmed_gear'])
        self.assertGreaterEqual(len(traction['loaded_indicated_speed_falls']), 1)
        self.assertEqual(traction['loaded_indicated_speed_falls'][0]['before']['speed'], 102.204)

    def test_exact_window_and_launch_intersection_use_original_rows(self):
        result = calculate_run(make_log(LAUNCH_ROWS), start_s=435, end_s=436)
        self.assertEqual(result['computed_from_rows'], 4)
        self.assertEqual(result['peaks']['power']['time_s'], 435.109)
        self.assertEqual(result['selected_run']['start_s'], 435.109)
        self.assertEqual(result['selected_run']['end_s'], 435.937)
        selected = calculate_run(make_log(LAUNCH_ROWS), start_s=435, end_s=436, launch_index=0)
        self.assertEqual(selected['computed_from_rows'], 4)
        self.assertEqual(selected['selected_launch']['peak_power']['time_s'], 434.171)
        self.assertNotEqual(selected['peaks']['power']['time_s'], selected['selected_launch']['peak_power']['time_s'])

    def test_invalid_samples_do_not_produce_negative_power_or_lambda(self):
        rows = [[0, 2000, -20, 500, 80, 85], [1, -2000, 20, 500, 80, 85],
                [2, 2000, 20, -500, 80, 85], [3, 2000, 20, 500, -80, 85],
                [4, 2000, 20, 500, 80, 85], [5, 2000, 20, math.inf, 80, 85],
                [math.nan, 2000, 20, 500, 80, 85], [6, True, 20, 500, 80, 85]]
        result = calculate_log(make_log(rows))
        peak = result['peaks']['power']
        self.assertEqual(peak['time_s'], 2)
        self.assertNotIn('calculated_lambda', peak)
        invalid = result['anomalies']['excluded_channel_values']
        self.assertEqual(invalid['iq:negative'], 1)
        self.assertEqual(invalid['rpm:negative'], 1)
        self.assertEqual(invalid['maf:negative'], 1)
        self.assertEqual(invalid['maf:nonfinite_or_nonnumeric'], 1)
        self.assertEqual(result['anomalies']['invalid_timestamp_rows'], 1)
        json.dumps(result, allow_nan=False)

    def test_sparse_air_and_fuel_rows_are_never_paired(self):
        log = make_log([[0, {'1': 2000, '2': 30}], [0.1, {'3': 600}],
                        [0.2, {'1': 2000, '2': 20, '3': 500}]])
        result = calculate_log(log)
        self.assertNotIn('calculated_lambda', result['peaks']['power'])
        self.assertEqual(result['peaks']['power_with_coherent_airflow']['time_s'], .2)
        self.assertEqual(result['matched_rpm_iq_maf_observations'], 1)

    def test_cruise_integrates_fuel_distance_with_declared_density(self):
        log = make_log([[i * .5, 2000, 20, 500, 80, 85] for i in range(25)])
        economy = calculate_log(log)['economy']
        expected = (20 * 2000 * 4 * 30 / 1e6 / .835) * 100 / 80
        self.assertEqual(len(economy['warm_stable_cruise']), 1)
        self.assertAlmostEqual(economy['warm_stable_cruise'][0]['estimated_l_100km'], expected, places=3)
        self.assertEqual(economy['warm_stable_cruise'][0]['covered_duration_s'], 12)
        self.assertEqual(economy['overall_moving']['indicated_distance_km'], .26667)

    def test_economy_does_not_integrate_across_a_stop_or_invalid_observation(self):
        for middle_speed in (0, -1):
            economy = calculate_log(make_log([[0, 2000, 20, 500, 80, 85],
                                              [.25, 2000, 20, 500, middle_speed, 85],
                                              [.5, 2000, 20, 500, 80, 85]]))['economy']
            self.assertEqual(economy['overall_moving']['covered_duration_s'], 0)
            self.assertIsNone(economy['overall_moving']['estimated_l_100km'])

    def test_stationary_fuel_is_separate_from_moving_consumption(self):
        log = make_log([[0, 2000, 20, 500, 0, 85], [.5, 2000, 20, 500, 0, 85],
                        [1, 2000, 20, 500, 80, 85], [1.5, 2000, 20, 500, 80, 85]])
        economy = calculate_log(log)['economy']
        stationary = economy['stationary_observed']
        expected_fuel = (20 * 2000 * 4 * 30 / 1e6 / .835) * .5 / 3600
        self.assertEqual(stationary['covered_duration_s'], .5)
        self.assertAlmostEqual(stationary['estimated_fuel_l'], expected_fuel, places=5)
        self.assertIsNone(stationary['estimated_l_100km'])
        self.assertEqual(economy['overall_moving']['covered_duration_s'], .5)
        self.assertIn('Do not label', economy['consumption_coverage'])

    def test_warm_classification_uses_at_most_one_second_old_coolant(self):
        rows = [[0, {'5': 85}], [.1, {'1': 2000, '2': 20, '4': 80}],
                [.6, {'1': 2000, '2': 20, '4': 80}],
                [1.1, {'1': 2000, '2': 20, '4': 80}]]
        result = calculate_log(make_log(rows))
        self.assertEqual(result['economy']['warm_moving']['covered_duration_s'], .5)
        self.assertEqual(result['economy']['overall_moving']['covered_duration_s'], 1)
        self.assertNotIn('coolant', result['peaks']['power'])

    def test_gear_number_needs_declared_transmission_and_tyre_size(self):
        row = [[0, 2000, 20, 500, 80, 85]]
        unknown = calculate_log(make_log(row), '3.07 rear end, 225/45 R17')
        self.assertNotIn('candidate_gear', unknown['peaks']['power'])
        self.assertFalse(unknown['assumptions']['rpm_speed_gear_numbering_supported'])
        missing_tyre=calculate_log(make_log(row),'8HP45 and 3.07 rear end')
        self.assertFalse(missing_tyre['assumptions']['rpm_speed_gear_numbering_supported'])
        known = calculate_log(make_log(row), '8HP45 and 3.07 rear end, 225/45 R17')
        self.assertEqual(known['peaks']['power']['candidate_gear'], 6)
        self.assertTrue(known['assumptions']['rpm_speed_gear_numbering_supported'])
        self.assertIn('Converter slip', known['assumptions']['gear_caveat'])

    def test_current_final_drive_fact_wins_in_either_word_order(self):
        log = make_log([[0, 2000, 20, 500, 80, 85]])
        for setup in ('8HP45, 3.91 rear end, 235/40R18\nDiff is 3.07 rear end, tyres 225/45R17',
                      '8HP45, differential is 3.91, 235/40R18\n3.07 rear end, tyres 225/45R17',
                      '8HP45, 3,91 rear-end ratio, 235/40R18 Diff is 3.07 rear end, tyres 225/45R17'):
            assumptions = calculate_log(log, setup)['assumptions']
            self.assertEqual(assumptions['final_drive'], 3.91)
            expected_tyre = math.pi * (18 * .0254 + 2 * 235 * 40 / 100000)
            self.assertEqual(assumptions['nominal_tyre_circumference_m'], expected_tyre)

    def test_setup_parser_does_not_join_unrelated_numbers_across_sentences(self):
        log = make_log([[0, 2000, 20, 500, 80, 85]])
        for setup in ('8HP45, rear end unknown. Estimated lambda 3.07',
                      '8HP45, rear end\n3.07 is a power figure',
                      '8HP45, diff torque question at lambda 3.07',
                      '8HP45, 43.91 rear end'):
            self.assertIsNone(calculate_log(log, setup)['assumptions']['final_drive'])

    def test_run_input_validation_and_empty_scope_are_explicit(self):
        for arguments in ({'start_s': math.nan}, {'end_s': True}, {'start_s': 2, 'end_s': 1},
                          {'launch_index': -1}, {'launch_index': True}, {'launch_index': 20}):
            with self.assertRaises(ValueError):
                calculate_run(make_log(LAUNCH_ROWS), **arguments)
        empty = calculate_run(make_log(LAUNCH_ROWS), start_s=1000, end_s=1100)
        self.assertEqual(empty['computed_from_rows'], 0)
        self.assertNotIn('power', empty['peaks'])
        self.assertIsNone(empty['selected_run']['start_s'])

    def test_second_launch_keeps_original_index_in_scoped_candidates(self):
        rows = [[0, 2000, 40, 600, 0, 85], [.5, 2300, 45, 700, 0, 85],
                [1, 2500, 50, 800, 10, 85], [1.5, 3000, 60, 900, 20, 85],
                [2, 2500, 0, 600, 30, 85], [2.5, 2400, 0, 600, 30, 85],
                [10, 2000, 40, 600, 0, 85], [10.5, 2300, 45, 700, 0, 85],
                [11, 2500, 50, 800, 10, 85], [11.5, 3000, 60, 900, 20, 85],
                [12, 2500, 0, 600, 30, 85], [12.5, 2400, 0, 600, 30, 85]]
        result = calculate_run(make_log(rows), launch_index=1)
        self.assertEqual(result['detected_launch_count'], 2)
        self.assertEqual(result['selected_run']['launch_index'], 1)
        self.assertEqual(result['selected_launch']['launch_index'], 1)
        self.assertEqual(result['launch_candidates'][0]['launch_index'], 1)
        self.assertEqual(result['peaks']['power']['time_s'], 11.5)

    def test_bad_assumptions_fall_back_without_poisoning_math(self):
        result = calculate_log(make_log([[0, 2000, 20, 500, 80, 85]],
                                        fuel_estimate_assumptions={'cylinders': -4, 'density_kg_l': 0}))
        self.assertEqual(result['assumptions']['cylinders'], 4)
        self.assertEqual(result['assumptions']['diesel_density_kg_l'], .835)
        self.assertEqual(len(result['assumptions']['warnings']), 2)
        json.dumps(result, allow_nan=False)

    def test_observed_ecu_selector_is_retained_but_not_mislabeled_actual_gear(self):
        log = make_log([[1, 4000, 80, 80, 1], [1.3, 3100, 75, 81, 2]],
                       names=('rpm', 'iq', 'speed', 'ecu_torque_selector'))
        result = calculate_run(log)
        self.assertEqual(result['peaks']['power']['ecu_torque_selector'], 1)
        self.assertEqual(result['shift_candidates'][0]['before']['ecu_torque_selector'], 1)
        self.assertEqual(result['shift_candidates'][0]['after']['ecu_torque_selector'], 2)
        self.assertEqual(result['coherent_channel_statistics']['ecu_torque_selector']['observed_codes'], [1, 2])
        self.assertIsNone(result['traction_evidence']['confirmed_gear'])
        self.assertIn('not universal actual', result['assumptions']['ecu_torque_selector_caveat'])

    def test_selector_codes_must_be_nonnegative_integer_diagnostic_bytes(self):
        log = make_log([[0, 2000, 20, -1], [1, 2000, 20, 1.5], [2, 2000, 20, 256]],
                       names=('rpm', 'iq', 'ecu_torque_selector'))
        result = calculate_log(log)
        self.assertNotIn('ecu_torque_selector', result['peaks']['power'])
        self.assertEqual(sum(result['anomalies']['excluded_channel_values'].values()), 3)


class ActualRecordingRegression(unittest.TestCase):
    @unittest.skipUnless((Path(__file__).resolve().parents[1] / 'local_resources' / 'launch.csv').exists(),
                         'User recording is not available on this computer')
    def test_every_actual_row_launch_power_shifts_and_economy(self):
        path = (Path(__file__).resolve().parents[1] / 'local_resources' / 'launch.csv')
        names = list(SIGNALS.values())
        with path.open(encoding='utf-8-sig') as stream:
            source = list(csv.DictReader(stream))
        log = {'columns': ['time_s'] + names,
               'rows': [[float(row['time']), {str(i + 1): float(row[name]) for i, name in enumerate(names)
                         if row.get(name)}] for row in source]}
        full = calculate_log(log, '8HP45 and 3.07 rear end')
        launch = calculate_run(log, '8HP45 and 3.07 rear end', launch_index=0)
        self.assertEqual(full['computed_from_rows'], 4747)
        self.assertEqual(full['peaks']['power']['time_s'], 546.906)
        self.assertEqual(launch['peaks']['power']['time_s'], 434.171)
        self.assertEqual(launch['peaks']['power']['crank_bhp_by_assumed_efficiency']['40%'], 276.1)
        self.assertEqual(launch['selected_run']['start_s'], 430.843)
        self.assertEqual(launch['selected_run']['end_s'], 438.093)
        self.assertEqual(len(launch['shift_candidates']), 2)
        self.assertEqual(full['economy']['stable_cruise_segments_found'], 0)
        self.assertEqual(full['economy']['warm_moving']['estimated_l_100km'], 6.17)
        self.assertEqual(full['anomalies']['excluded_channel_values'], {})


if __name__ == '__main__':
    unittest.main()
