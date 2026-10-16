"""Concise free local results use deterministic values and preserve run scope."""
import importlib.util
from pathlib import Path
import sys
import unittest

HERE = Path(__file__).parent
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bmw_bin_editor.ai_log_analysis import calculate_log, calculate_run, SIGNALS

from bmw_bin_editor import local_results as formatter
fixture_spec = importlib.util.spec_from_file_location('fixture', HERE / 'test_ai_run_analysis.py')
fixture = importlib.util.module_from_spec(fixture_spec)
fixture_spec.loader.exec_module(fixture)


class LocalResultsTests(unittest.TestCase):
    def test_integer_formatting_keeps_significant_trailing_zeros(self):
        self.assertEqual(formatter._number(1000, 0), '1000')
        self.assertEqual(formatter._number(20, 0), '20')
        self.assertEqual(formatter._number(20, 1), '20')
        self.assertEqual(formatter._number(0, 0), '0')

    def test_whole_log_defaults_to_launch_not_elsewhere_peak(self):
        result = calculate_log(fixture.make_log(fixture.LAUNCH_ROWS))
        text = formatter.format_local_results(result)
        self.assertIn('Launch 1 peak: 276.1 bhp at 434.171 s', text)
        self.assertNotIn('279.7 bhp', text)
        self.assertIn('40% assumed brake efficiency', text)
        self.assertIn('not measured power', text)
        self.assertIn('433.515–433.781 s', text)
        self.assertIn('434.875–435.421 s', text)
        self.assertIn('Actual gear and hookup are unconfirmed', text)
        self.assertIn('no simultaneous MAF at the power peak', text)
        self.assertIn('Separate paired check at 433.265 s', text)
        self.assertEqual(len(text.splitlines()), 4)
        self.assertLess(len(text.split()), 150)

    def test_explicit_launch_window_does_not_revert_to_complete_launch_peak(self):
        result = calculate_run(fixture.make_log(fixture.LAUNCH_ROWS), start_s=435, end_s=436, launch_index=0)
        text = formatter.format_local_results(result)
        self.assertIn('within selected window', text)
        self.assertIn('435.109 s', text)
        self.assertNotIn('276.1 bhp', text)

    def test_caller_can_choose_captured_whole_scope_peak(self):
        result = calculate_log(fixture.make_log(fixture.LAUNCH_ROWS))
        text = formatter.format_local_results(result, prefer_launch=False)
        self.assertIn('Captured-scope peak: 279.7 bhp at 546.906 s', text)
        self.assertIn('calculated λ 1.066 at that peak', text)

    def test_no_samples_has_clear_short_result(self):
        text = formatter.format_local_results(calculate_log({'columns': [], 'rows': []}))
        self.assertIn('Power: unavailable', text)
        self.assertIn('no economy-patch conclusion', text)
        self.assertEqual(len(text.splitlines()), 4)

    def test_warm_cruise_uses_local_integrated_value_and_excludes_stops(self):
        log = fixture.make_log([[i * .5, 2000, 20, 500, 80, 85] for i in range(25)])
        text = formatter.format_local_results(calculate_log(log))
        self.assertIn('Warm cruise: 7.19 estimated L/100 km at 80 km/h over 12 s', text)
        self.assertIn('stops excluded', text)
        self.assertIn('IQ-derived', text)

    def test_observed_selector_uses_only_confirmed_table_bindings(self):
        log = fixture.make_log([[1, 4000, 80, 80, 1], [1.3, 3100, 75, 81, 2],
                                [1.6, 3000, 75, 82, 7]],
                               names=('rpm', 'iq', 'speed', 'ecu_torque_selector'))
        result = calculate_run(log)
        bindings = {'1': {'table_address': 0xE945A, 'source': 'Confirmed actual-gear CAN'},
                    '2': {'table_address': 0xE94C0, 'source': 'Confirmed actual-gear CAN'},
                    '3': {'table_address': 0xE9526, 'source': 'Confirmed actual-gear CAN'}}
        text = formatter.format_local_results(result, confirmed_gear_bindings=bindings)
        self.assertIn('Confirmed selector bindings observed gears 1, 2.', text)
        self.assertIn('Unmapped selector codes 7.', text)
        self.assertNotIn('observed gears 1, 2, 3', text)
        self.assertNotIn('Actual gear and hookup are unconfirmed', text)
        self.assertIn('Exact hookup is unconfirmed', text)
        self.assertEqual(len(text.splitlines()), 4)

    def test_selector_without_confirmation_is_not_physical_gear(self):
        log = fixture.make_log([[1, 4000, 80, 80, 1]],
                               names=('rpm', 'iq', 'speed', 'ecu_torque_selector'))
        text = formatter.format_local_results(calculate_run(log), confirmed_gear_bindings={
            '1': {'table_address': 0xE945A, 'source': ''}})
        self.assertIn('Observed selector codes 1; physical-gear bindings unconfirmed', text)
        self.assertNotIn('Confirmed selector bindings observed', text)

    def test_launch_formatter_does_not_take_selector_from_elsewhere_scope(self):
        log = fixture.make_log([[0, 2000, 40, 0, 1], [.5, 2300, 45, 0, 1],
                                [1, 2500, 50, 10, 2], [1.5, 3000, 60, 20, 2],
                                [2, 2500, 0, 30, 2], [2.5, 2400, 0, 30, 2],
                                [100, 3500, 80, 100, 3]],
                               names=('rpm', 'iq', 'speed', 'ecu_torque_selector'))
        bindings = {str(gear): {'table_address': address, 'source': 'Confirmed actual-gear CAN'}
                    for gear, address in ((1, 0xE945A), (2, 0xE94C0), (3, 0xE9526))}
        text = formatter.format_local_results(calculate_log(log), confirmed_gear_bindings=bindings)
        self.assertIn('observed gears 1, 2.', text)
        self.assertNotIn('observed gears 1, 2, 3', text)

    def test_ratio_candidates_are_labeled_and_remain_window_scoped(self):
        log = fixture.make_log([[1, 2000, 20, 500, 80, 85], [100, 3000, 30, 700, 50, 85]])
        text = formatter.format_local_results(calculate_run(log, '8HP45, 3.07 rear end, 225/45R17', start_s=0, end_s=2))
        self.assertIn('RPM/speed ratio candidates 6 (converter slip may affect them)', text)
        self.assertNotIn('candidates 2, 6', text)
        self.assertIn('Exact hookup is unconfirmed', text)


if __name__ == '__main__':
    unittest.main()
