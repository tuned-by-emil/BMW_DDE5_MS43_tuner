"""IQ-derived fuel estimates: numeric correctness and sparse/live validity."""
from pathlib import Path
import csv
import hashlib
import math
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bmw_bin_editor.logdata import (
    FUEL_CONSUMPTION_SIGNAL,
    FUEL_RATE_SIGNAL,
    FuelEstimateConfig,
    LiveTestOLog,
    SIG_IQ,
    SIG_RPM,
    SIG_SPEED,
    SignalCatalog,
    TestOLog,
)


class FuelEstimateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "capture.csv"

    def tearDown(self):
        self.temporary.cleanup()

    def parse(self, samples, *, config=None, extra_columns=()):
        columns = ["time", "time_unit", SIG_RPM, SIG_IQ, SIG_SPEED, *extra_columns]
        with self.path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            for seconds, sample in samples:
                writer.writerow(dict(time=seconds, time_unit="s", **sample))
        return TestOLog(self.path, fuel_estimate_config=config)

    def test_previous_373_second_event_matches_independent_fuel_math(self):
        samples = [
            (372.750, {SIG_RPM: 2300.912, SIG_IQ: 22.365, SIG_SPEED: 129.115}),
            (373.172, {SIG_RPM: 2306.912, SIG_IQ: 31.405, SIG_SPEED: 129.042}),
            (374.953, {SIG_RPM: 2312.912, SIG_IQ: 30.456, SIG_SPEED: 129.855}),
        ]
        log = self.parse(samples)
        # g/s = IQ * RPM / 30000 for four cylinders, each firing once per 2 revs.
        reference = [iq * rpm / 30000 * 3.6 / 0.835 for rpm, iq in ((2300.912, 22.365), (2306.912, 31.405), (2312.912, 30.456))]
        np.testing.assert_allclose(log.series(FUEL_RATE_SIGNAL), reference, rtol=1e-12)
        self.assertAlmostEqual(log.value(FUEL_RATE_SIGNAL, 0), 7.395434282155688, places=8)
        self.assertAlmostEqual(log.value(FUEL_RATE_SIGNAL, 1), 10.411770734371258, places=8)
        self.assertAlmostEqual(log.value(FUEL_CONSUMPTION_SIGNAL, 0), 5.72778, places=4)
        self.assertAlmostEqual(log.value(FUEL_CONSUMPTION_SIGNAL, 1), 8.06851, places=4)
        self.assertEqual(log.units[FUEL_RATE_SIGNAL], "L/h")
        self.assertEqual(log.units[FUEL_CONSUMPTION_SIGNAL], "L/100 km")
        self.assertIn("Estimated", log.info[FUEL_RATE_SIGNAL])

    def test_calculated_catalog_labels_and_units_are_available_without_tsv(self):
        catalog = SignalCatalog()
        self.assertEqual(catalog.label_for(FUEL_RATE_SIGNAL), "Estimated fuel flow")
        self.assertEqual(catalog.label_for(FUEL_CONSUMPTION_SIGNAL), "Estimated fuel consumption")
        self.assertEqual(catalog.unit_for(FUEL_RATE_SIGNAL), "L/h")
        self.assertEqual(catalog.unit_for(FUEL_CONSUMPTION_SIGNAL), "L/100 km")

    def test_cycle_quantity_does_not_add_pilot_or_main_injection_channels(self):
        pilot = "STAT_MENGE_VOREINSPRITZUNG1_WERT"
        main = "STAT_MENGE_HAUPTEINSPRITZUNG1_WERT"
        log = self.parse([(0, {SIG_RPM: 2000, SIG_IQ: 25, SIG_SPEED: 100, pilot: 5, main: 20})], extra_columns=(pilot, main))
        self.assertAlmostEqual(log.value(FUEL_RATE_SIGNAL, 0), 25 * 2000 / 30000 * 3.6 / 0.835)
        self.assertEqual(log.value(pilot, 0), 5)
        self.assertEqual(log.value(main, 0), 20)

    def test_configuration_recalculates_without_changing_sources_or_csv(self):
        log = self.parse([(0, {SIG_RPM: 2000, SIG_IQ: 25, SIG_SPEED: 100})])
        source_hash = hashlib.sha256(self.path.read_bytes()).hexdigest()
        source_values = {name: log.series(name).copy() for name in (SIG_RPM, SIG_IQ, SIG_SPEED)}
        initial_rate = log.value(FUEL_RATE_SIGNAL, 0)
        config = FuelEstimateConfig(cylinders=6, density_kg_l=0.840, min_speed_kmh=110)
        log.set_fuel_estimate_config(config)
        self.assertEqual(log.fuel_estimate_config, config)
        self.assertAlmostEqual(log.value(FUEL_RATE_SIGNAL, 0), initial_rate * 1.5 * 0.835 / 0.840)
        self.assertTrue(math.isnan(log.value(FUEL_CONSUMPTION_SIGNAL, 0)))
        self.assertEqual(log.columns.count(FUEL_RATE_SIGNAL), 1)
        self.assertEqual(log.columns.count(FUEL_CONSUMPTION_SIGNAL), 1)
        for name, values in source_values.items():
            np.testing.assert_array_equal(log.series(name), values)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), source_hash)
        six_cylinder = TestOLog(self.path, fuel_estimate_config=FuelEstimateConfig(cylinders=6))
        self.assertAlmostEqual(six_cylinder.value(FUEL_RATE_SIGNAL, 0), initial_rate * 1.5)

    def test_alternating_groups_preserve_missing_speed_and_show_coherent_age(self):
        log = self.parse([
            (0, {SIG_RPM: 2000, SIG_IQ: 25, SIG_SPEED: 100}),
            (0.15, {SIG_RPM: 2010, SIG_IQ: 26}),
            (0.35, {SIG_RPM: 2020, SIG_IQ: 27, SIG_SPEED: 101}),
            (0.50, {SIG_RPM: 2030, SIG_IQ: 28}),
        ])
        self.assertTrue(math.isnan(log.value(SIG_SPEED, 1)))
        self.assertTrue(math.isnan(log.value(FUEL_CONSUMPTION_SIGNAL, 1)))
        value, age = log.fuel_estimate_display(FUEL_CONSUMPTION_SIGNAL, 1)
        self.assertAlmostEqual(value, log.value(FUEL_CONSUMPTION_SIGNAL, 0))
        self.assertAlmostEqual(age, 0.15)
        current_rate, rate_age = log.fuel_estimate_display(FUEL_RATE_SIGNAL, 1)
        self.assertAlmostEqual(current_rate, 26 * 2010 / 30000 * 3.6 / 0.835)
        self.assertEqual(rate_age, 0)
        self.assertEqual(log.fuel_estimate_display(FUEL_CONSUMPTION_SIGNAL, 2), (log.value(FUEL_CONSUMPTION_SIGNAL, 2), 0))

    def test_stopped_and_low_speed_clear_consumption_but_keep_fuel_flow(self):
        samples = [(i, {SIG_RPM: 800, SIG_IQ: 5, SIG_SPEED: speed}) for i, speed in enumerate((30, 0, 4.999, 5, -1, math.nan, math.inf))]
        log = self.parse(samples)
        for index in (1, 2, 4, 5, 6):
            with self.subTest(index=index):
                self.assertTrue(math.isfinite(log.value(FUEL_RATE_SIGNAL, index)))
                self.assertTrue(math.isnan(log.value(FUEL_CONSUMPTION_SIGNAL, index)))
                self.assertTrue(math.isnan(log.fuel_estimate_display(FUEL_CONSUMPTION_SIGNAL, index)[0]))
        self.assertTrue(math.isfinite(log.value(FUEL_CONSUMPTION_SIGNAL, 3)))

    def test_missing_or_invalid_fuel_input_never_holds_previous_estimate(self):
        samples = [
            (0, {SIG_RPM: 2000, SIG_IQ: 25, SIG_SPEED: 100}),
            (0.1, {SIG_RPM: 2000, SIG_SPEED: 100}),
            (0.2, {SIG_IQ: 25, SIG_SPEED: 100}),
            (0.3, {SIG_RPM: -1, SIG_IQ: 25, SIG_SPEED: 100}),
            (0.4, {SIG_RPM: 2000, SIG_IQ: -1, SIG_SPEED: 100}),
            (0.5, {SIG_RPM: math.inf, SIG_IQ: 25, SIG_SPEED: 100}),
            (0.6, {SIG_RPM: 2000, SIG_IQ: "bad", SIG_SPEED: 100}),
            (0.7, {SIG_RPM: 2000, SIG_IQ: 25}),
        ]
        log = self.parse(samples)
        for index in range(1, 7):
            for signal in (FUEL_RATE_SIGNAL, FUEL_CONSUMPTION_SIGNAL):
                with self.subTest(index=index, signal=signal):
                    self.assertTrue(math.isnan(log.value(signal, index)))
                    self.assertTrue(math.isnan(log.fuel_estimate_display(signal, index)[0]))
        # A valid fuel row after an invalid row cannot revive an old speed estimate.
        self.assertTrue(math.isnan(log.fuel_estimate_display(FUEL_CONSUMPTION_SIGNAL, 7)[0]))

    def test_explicit_bad_speed_and_stale_speed_do_not_hold(self):
        for speed in ("bad", "nan", "inf", "-inf", "0"):
            with self.subTest(speed=speed):
                log = self.parse([
                    (0, {SIG_RPM: 2000, SIG_IQ: 25, SIG_SPEED: 100}),
                    (0.15, {SIG_RPM: 2000, SIG_IQ: 25, SIG_SPEED: speed}),
                    (0.30, {SIG_RPM: 2000, SIG_IQ: 25}),
                ])
                self.assertTrue(math.isnan(log.fuel_estimate_display(FUEL_CONSUMPTION_SIGNAL, 1)[0]))
                self.assertTrue(math.isnan(log.fuel_estimate_display(FUEL_CONSUMPTION_SIGNAL, 2)[0]))
        log = self.parse([
            (0, {SIG_RPM: 2000, SIG_IQ: 25, SIG_SPEED: 100}),
            (1.01, {SIG_RPM: 2000, SIG_IQ: 25}),
        ])
        self.assertTrue(math.isnan(log.fuel_estimate_display(FUEL_CONSUMPTION_SIGNAL, 1)[0]))

    def test_zero_quantity_and_engine_stopped_are_zero_rate(self):
        log = self.parse([
            (0, {SIG_RPM: 2000, SIG_IQ: 0, SIG_SPEED: 100}),
            (1, {SIG_RPM: 0, SIG_IQ: 25, SIG_SPEED: 10}),
        ])
        np.testing.assert_array_equal(log.series(FUEL_RATE_SIGNAL), [0, 0])
        np.testing.assert_array_equal(log.series(FUEL_CONSUMPTION_SIGNAL), [0, 0])

    def test_missing_dependency_does_not_create_available_estimates(self):
        log = self.parse([(0, {SIG_RPM: 2000, SIG_SPEED: 100})])
        self.assertFalse(log.has(FUEL_RATE_SIGNAL))
        self.assertFalse(log.has(FUEL_CONSUMPTION_SIGNAL))
        log = self.parse([(0, {SIG_RPM: 2000, SIG_IQ: 25})])
        self.assertTrue(log.has(FUEL_RATE_SIGNAL))
        self.assertFalse(log.has(FUEL_CONSUMPTION_SIGNAL))

    def test_live_and_csv_calculation_match_including_reconfiguration(self):
        samples = [
            (0, {SIG_RPM: 2000, SIG_IQ: 25, SIG_SPEED: 100}),
            (0.15, {SIG_RPM: 2010, SIG_IQ: 26}),
            (0.35, {SIG_RPM: 2020, SIG_IQ: 27, SIG_SPEED: 0}),
            (0.50, {SIG_RPM: 2030, SIG_IQ: 28}),
            (0.70, {SIG_RPM: 2040, SIG_IQ: 29, SIG_SPEED: math.nan}),
            (0.85, {SIG_RPM: 2050, SIG_IQ: 30}),
            (1.05, {SIG_RPM: 2060, SIG_IQ: 31, SIG_SPEED: 102}),
            (1.20, {SIG_RPM: 2070, SIG_IQ: 32}),
        ]
        recorded = self.parse(samples)
        live = LiveTestOLog()
        for seconds, sample in samples:
            live.append_sample(seconds, sample)
        for log in (recorded, live):
            self.assertTrue(math.isnan(log.fuel_estimate_display(FUEL_CONSUMPTION_SIGNAL, 3)[0]))
            self.assertTrue(math.isnan(log.fuel_estimate_display(FUEL_CONSUMPTION_SIGNAL, 5)[0]))
        for signal in (SIG_RPM, SIG_IQ, SIG_SPEED, FUEL_RATE_SIGNAL, FUEL_CONSUMPTION_SIGNAL):
            np.testing.assert_allclose(live.series(signal), recorded.series(signal), equal_nan=True)
        config = FuelEstimateConfig(cylinders=6, density_kg_l=0.84)
        recorded.set_fuel_estimate_config(config)
        live.set_fuel_estimate_config(config)
        for signal in (FUEL_RATE_SIGNAL, FUEL_CONSUMPTION_SIGNAL):
            np.testing.assert_allclose(live.series(signal), recorded.series(signal), equal_nan=True)
        live.append_sample(1.40, {SIG_RPM: 2080, SIG_IQ: 33, SIG_SPEED: 103})
        expected = 33 * 2080 * 6 / 120000 * 3.6 / 0.84
        self.assertAlmostEqual(live.value(FUEL_RATE_SIGNAL, 8), expected)
        self.assertAlmostEqual(live.value(FUEL_CONSUMPTION_SIGNAL, 8), expected * 100 / 103)

    def test_live_can_gain_dependencies_mid_session_and_keep_aligned_arrays(self):
        live = LiveTestOLog()
        live.append_sample(0, {SIG_RPM: 2000})
        self.assertNotIn(FUEL_RATE_SIGNAL, live.values)
        live.append_sample(0.2, {SIG_RPM: 2000, SIG_IQ: 25})
        self.assertTrue(math.isnan(live.value(FUEL_RATE_SIGNAL, 0)))
        self.assertTrue(math.isfinite(live.value(FUEL_RATE_SIGNAL, 1)))
        live.append_sample(0.4, {SIG_RPM: 2000, SIG_IQ: 25, SIG_SPEED: 100})
        self.assertTrue(math.isnan(live.value(FUEL_CONSUMPTION_SIGNAL, 0)))
        self.assertTrue(math.isnan(live.value(FUEL_CONSUMPTION_SIGNAL, 1)))
        self.assertTrue(math.isfinite(live.value(FUEL_CONSUMPTION_SIGNAL, 2)))
        for values in live.values.values():
            self.assertEqual(len(values), live.sample_count)

    def test_live_display_policy_remains_aligned_after_buffer_rollover(self):
        live = LiveTestOLog()
        live.append_sample(0, {SIG_RPM: 2000, SIG_IQ: 25, SIG_SPEED: 100})
        # Prepopulate an established session, then exercise the real rollover.
        live.times = np.arange(20000, dtype=float) * 0.1
        live.values = {name: np.full(20000, values[0], dtype=float) for name, values in live.values.items()}
        live._fuel_speed_observed = np.full(20000, True, dtype=bool)
        live.append_sample(2000, {SIG_RPM: 2000, SIG_IQ: 25})
        self.assertEqual(live.sample_count, 20000)
        self.assertEqual(len(live._fuel_speed_observed), live.sample_count)
        self.assertTrue(math.isnan(live.value(FUEL_CONSUMPTION_SIGNAL, 19999)))
        value, age = live.fuel_estimate_display(FUEL_CONSUMPTION_SIGNAL, 19999)
        self.assertTrue(math.isfinite(value))
        self.assertAlmostEqual(age, 0.1)

    def test_invalid_configuration_is_rejected(self):
        for kwargs in (
            dict(cylinders=0), dict(cylinders=6.5), dict(cylinders=True), dict(cylinders=17),
            dict(density_kg_l=0), dict(density_kg_l=-1), dict(density_kg_l=math.nan), dict(density_kg_l=math.inf),
            dict(min_speed_kmh=0), dict(min_speed_kmh=-1), dict(min_speed_kmh=math.nan),
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    FuelEstimateConfig(**kwargs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
