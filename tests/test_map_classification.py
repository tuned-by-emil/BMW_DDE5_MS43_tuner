"""Semantic suggestions require multiple independent, explicitly tentative cues."""
import hashlib
from pathlib import Path
import struct
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bmw_bin_editor.core import safe_math
from bmw_bin_editor.map_classification import FamilySuggestion, classify_candidate
from bmw_bin_editor.map_finder import MapCandidate


RPM = np.array([500, 750, 1000, 1500, 2000, 2500, 3500, 5000])
LOAD = np.array([0, 500, 1000, 2000, 3500, 5000, 6500, 7500])
PEDAL = np.array([0, 819, 1065, 1638, 2458, 4096, 5734, 8192])


def blob(x, y, grid, *, endian=">", header=128):
    grid = np.asarray(grid)
    rows, cols = grid.shape
    words = list(map(int, y)) + list(map(int, x)) + [int(round(value)) & 0xFFFF for value in grid.ravel()]
    data = b"\xff" * header + struct.pack(endian + "H" * len(words), *words) + b"\xff" * 32
    candidate = MapCandidate(header + (rows + cols) * 2, rows, cols, header + rows * 2, header,
                             score=92, reasons=["Synthetic structural evidence"])
    return data, candidate


def family_blob(family, *, transpose=False, endian=">", quarter_rpm=False):
    r, l = np.meshgrid(np.linspace(0, 1, 8), np.linspace(0, 1, 8), indexing="ij")
    x, y = LOAD, RPM * (4 if quarter_rpm else 1)
    if family == "Boost target":
        grid = 1000 + 1800 * l * (.75 + .25 * np.sin(r * np.pi))
    elif family == "N75 / VNT duty":
        grid = (np.round((95 - 40 * r - 20 * l) * 2) / 2) * 8192 / 100
    elif family == "Main SOI":
        grid = (np.round((-2 + 22 * r + 4 * l) * 10) / 10) * 128 / 3
    elif family == "Driver's wish":
        x = PEDAL
        grid = 5400 * (PEDAL / 8192)[None, :] ** 1.3 * (1 - .2 * r)
    else:
        raise AssertionError(family)
    return blob(y, x, grid.T, endian=endian) if transpose else blob(x, y, grid, endian=endian)


class ClassificationTests(unittest.TestCase):
    def test_positive_patterns_for_all_four_families(self):
        for family in ("Boost target", "N75 / VNT duty", "Main SOI", "Driver's wish"):
            with self.subTest(family=family):
                data, candidate = family_blob(family)
                suggestions = classify_candidate(data, candidate)
                self.assertTrue(suggestions)
                self.assertEqual(suggestions[0].family, family)
                self.assertTrue(all(isinstance(s, FamilySuggestion) and 0 <= s.score <= 100 for s in suggestions))

    def test_transposed_storage_preserves_axes_and_dimensions(self):
        for family in ("Boost target", "N75 / VNT duty", "Main SOI", "Driver's wish"):
            with self.subTest(family=family):
                data, candidate = family_blob(family, transpose=True)
                suggestion = classify_candidate(data, candidate)[0]
                self.assertEqual(suggestion.family, family)
                self.assertEqual(suggestion.x_units, "rpm")
                self.assertEqual((suggestion.rows, suggestion.cols), (candidate.rows, candidate.cols))
                self.assertEqual((suggestion.x_address, suggestion.y_address), (candidate.x_address, candidate.y_address))

    def test_rectangular_transposed_table_preserves_stored_cell_order(self):
        rpm = RPM[[0, 1, 2, 4, 6, 7]]
        r, l = np.meshgrid(np.linspace(0, 1, len(rpm)), np.linspace(0, 1, len(LOAD)), indexing="ij")
        grid = np.rint(1000 + 1800*l*(.75+.25*np.sin(r*np.pi)))
        for x, y, stored in ((LOAD, rpm, grid), (rpm, LOAD, grid.T)):
            data, candidate = blob(x, y, stored)
            suggestion = classify_candidate(data, candidate)[0]
            definition = suggestion.to_definition(candidate)
            self.assertEqual(suggestion.family, "Boost target")
            self.assertEqual((definition.rows, definition.cols), stored.shape)
            raw = np.frombuffer(data, dtype=">u2", count=definition.size, offset=definition.z_address)
            physical = np.array([safe_math(definition.z_equation, v) for v in raw]).reshape(definition.rows, definition.cols)
            np.testing.assert_array_equal(physical, stored)
            self.assertEqual((definition.x_axis.address, definition.x_axis.count), (candidate.x_address, len(x)))
            self.assertEqual((definition.y_axis.address, definition.y_axis.count), (candidate.y_address, len(y)))

    def test_actual_and_quarter_rpm_encodings_are_distinguished(self):
        for quarter in (False, True):
            data, candidate = family_blob("Boost target", quarter_rpm=quarter)
            suggestion = classify_candidate(data, candidate)[0]
            self.assertEqual(suggestion.y_equation, "X * 0.25" if quarter else "X")
            self.assertTrue(any("quarter-RPM" in reason for reason in suggestion.reasons) if quarter else True)
            self.assertEqual(safe_math(suggestion.y_equation, 20000 if quarter else 5000), 5000)

    def test_quantized_duty_and_signed_timing_are_numeric_without_overflow(self):
        data, candidate = family_blob("N75 / VNT duty")
        duty = classify_candidate(data, candidate)[0]
        self.assertEqual(duty.z_equation, "X * 100 / 8192")
        data, candidate = family_blob("Main SOI")
        before = hashlib.sha256(data).hexdigest()
        timing = classify_candidate(data, candidate)[0]
        self.assertTrue(timing.z_signed)
        self.assertEqual(timing.z_equation, "X * 3 / 128")
        definition = timing.to_definition(candidate)
        self.assertTrue(definition.z_signed)
        self.assertEqual(hashlib.sha256(data).hexdigest(), before)
        unsigned = np.frombuffer(data, dtype=">u2", count=candidate.rows*candidate.cols, offset=candidate.address)
        signed = np.where(unsigned.astype(float) >= 32768, unsigned.astype(float) - 65536, unsigned.astype(float))
        scaled = [safe_math(timing.z_equation, raw) for raw in signed]
        self.assertAlmostEqual(min(scaled), -2, delta=.03)
        self.assertGreater(max(scaled), 20)
        self.assertLess(max(scaled), 30)

    def test_decimal_and_direct_encodings_are_also_supported(self):
        r, l = np.meshgrid(np.linspace(0, 1, 8), np.linspace(0, 1, 8), indexing="ij")
        for multiplier, expected in ((1, "X"), (100, "X * 0.01")):
            data, candidate = blob(LOAD, RPM, (90 - 35*r - 15*l) * multiplier)
            suggestion = classify_candidate(data, candidate)[0]
            self.assertEqual(suggestion.family, "N75 / VNT duty")
            self.assertEqual(suggestion.z_equation, expected)
        data, candidate = blob(LOAD, RPM, (1000 + 1500*l*(1-.15*r))*10)
        suggestion = classify_candidate(data, candidate)[0]
        self.assertEqual(suggestion.family, "Boost target")
        self.assertEqual(suggestion.z_equation, "X * 0.1")

    def test_random_surfaces_and_small_or_wrong_axes_abstain(self):
        rng = np.random.default_rng(211)
        patterns = [blob(LOAD, RPM, rng.integers(700, 3000, size=(8, 8))),
                    blob(LOAD, RPM, rng.integers(0, 65536, size=(8, 8))),
                    blob(LOAD, np.arange(2331, 3131, 100), 1000 + np.arange(64).reshape(8, 8)*20),
                    blob(LOAD, np.array([500, 750, 1000, 2500, 2000, 3500, 4000, 5000]), np.arange(64).reshape(8, 8)+1000),
                    blob(np.array([0, 5000, 25000, 30000, 40000, 45000, 50000, 60000]), RPM, np.arange(64).reshape(8, 8)+1000),
                    blob([0, 100, 200], [500, 2000, 5000], np.arange(9).reshape(3, 3)+1000),
                    blob(LOAD, RPM, np.full((8, 8), 1000)),
                    blob(LOAD, RPM, np.full((8, 8), 65535))]
        for data, candidate in patterns:
            with self.subTest(candidate=candidate):
                self.assertEqual(classify_candidate(data, candidate), [])

    def test_two_equally_plausible_rpm_axes_abstain(self):
        data, candidate = blob(RPM, RPM, 1000 + np.arange(64).reshape(8, 8)*20)
        self.assertEqual(classify_candidate(data, candidate), [])

    def test_comparable_alternative_families_are_retained(self):
        r, l = np.meshgrid(np.linspace(0, 1, 8), np.linspace(0, 1, 8), indexing="ij")
        data, candidate = blob(LOAD, RPM, 1000 + 600*r + 700*l)
        suggestions = classify_candidate(data, candidate)
        families = [suggestion.family for suggestion in suggestions]
        self.assertIn("Boost target", families)
        self.assertIn("Main SOI", families)
        self.assertEqual([s.score for s in suggestions], sorted((s.score for s in suggestions), reverse=True))

    def test_torque_request_magnitude_alone_does_not_become_duty(self):
        r, l = np.meshgrid(np.linspace(0, 1, 8), np.linspace(0, 1, 8), indexing="ij")
        data, candidate = blob(LOAD, RPM, 500 + 6000*l + 700*r)
        self.assertNotIn("N75 / VNT duty", [s.family for s in classify_candidate(data, candidate)])

    def test_increasing_requests_declining_with_rpm_do_not_become_duty(self):
        r, l = np.meshgrid(np.linspace(0, 1, 8), np.linspace(0, 1, 8), indexing="ij")
        # The raw percentage-compatible magnitude and decreasing RPM trend
        # are shared by torque/quantity request maps; neither establishes duty.
        for x, grid in ((LOAD, 8000*l*(1-.4*r)),
                        (np.array([819, 1638, 2458, 3277, 4096, 5734, 6554, 8192]),
                         2000 + 8000*l*(1-.3*r))):
            data, candidate = blob(x, RPM, grid)
            self.assertNotIn("N75 / VNT duty", [s.family for s in classify_candidate(data, candidate)])

    def test_driver_request_has_no_false_actuator_alternative(self):
        data, candidate = family_blob("Driver's wish")
        families = [s.family for s in classify_candidate(data, candidate)]
        self.assertIn("Driver's wish", families)
        self.assertNotIn("N75 / VNT duty", families)

    def test_rpm_only_schedule_does_not_become_injection_timing(self):
        rpm_profile = np.linspace(2500, 3200, 8)[:, None]
        for grid in (np.repeat(rpm_profile, 8, axis=1),
                     rpm_profile + np.arange(8)[None, :]):
            data, candidate = blob(LOAD, RPM, grid)
            self.assertNotIn("Main SOI", [s.family for s in classify_candidate(data, candidate)])

    def test_deliberate_interpretation_is_separate_from_raw_definition(self):
        data, candidate = family_blob("Driver's wish")
        raw = candidate.to_definition()
        suggestion = classify_candidate(data, candidate)[0]
        interpreted = suggestion.to_definition(candidate)
        self.assertIn("Likely Driver's wish (unverified)", interpreted.title)
        self.assertIn("UNVERIFIED", interpreted.description)
        self.assertIn("not a probability", interpreted.description)
        self.assertIn("empirical hypotheses", interpreted.description)
        self.assertEqual(interpreted.x_axis.equation, "X * 100 / 8192")
        self.assertEqual(interpreted.z_units, "Nm")
        self.assertEqual(raw.z_equation, "X")
        self.assertEqual(raw.x_axis.equation, "X")
        self.assertEqual(raw.z_units, "")
        self.assertFalse(raw.z_signed)
        self.assertEqual(candidate.to_definition(), raw)

    def test_little_endian_and_descending_axes(self):
        data, candidate = family_blob("Main SOI", endian="<")
        self.assertEqual(classify_candidate(data, candidate, endian="<")[0].family, "Main SOI")
        self.assertEqual(classify_candidate(data, candidate, endian=">"), [])
        r, l = np.meshgrid(np.linspace(0, 1, 8), np.linspace(0, 1, 8), indexing="ij")
        grid = (95 - 40*r - 20*l)*8192/100
        data, candidate = blob(LOAD[::-1], RPM[::-1], grid[::-1, ::-1])
        self.assertEqual(classify_candidate(data, candidate)[0].family, "N75 / VNT duty")

    def test_partial_idle_rpm_axis_still_distinguishes_zero_based_load(self):
        rpm = np.array([250, 500, 750, 1000, 1500, 2500, 3500, 4800])
        r, l = np.meshgrid(np.linspace(0, 1, 8), np.linspace(0, 1, 8), indexing="ij")
        data, candidate = blob(LOAD, rpm, (95-45*r-10*l)*8192/100)
        suggestion = classify_candidate(data, candidate)[0]
        self.assertEqual(suggestion.family, "N75 / VNT duty")
        self.assertEqual(suggestion.y_units, "rpm")

    def test_positive_only_timing_retains_signed_storage_for_future_negative_edits(self):
        r, l = np.meshgrid(np.linspace(0, 1, 8), np.linspace(0, 1, 8), indexing='ij')
        grid = np.round((2+20*r+4*l)*10)/10 * 128/3
        for endian in ('>', '<'):
            data, candidate = blob(LOAD, RPM, grid, endian=endian)
            suggestion = next(s for s in classify_candidate(data, candidate, endian=endian) if s.family == 'Main SOI')
            self.assertTrue(suggestion.z_signed)
            self.assertTrue(suggestion.to_definition(candidate).z_signed)
            self.assertIn('only nonnegative values', ' '.join(suggestion.reasons))

    def test_input_buffer_and_candidate_are_unchanged_and_ranges_are_safe(self):
        data, candidate = family_blob("Main SOI")
        mutable = bytearray(data)
        before = bytes(mutable)
        old_fields = dict(vars(candidate))
        classify_candidate(mutable, candidate)
        self.assertEqual(bytes(mutable), before)
        self.assertEqual(vars(candidate), old_fields)
        self.assertEqual(classify_candidate(data[:candidate.address+10], candidate), [])
        candidate.x_address = -1
        self.assertEqual(classify_candidate(data, candidate), [])
        with self.assertRaises(ValueError):
            classify_candidate(data, candidate, endian="big")
        with self.assertRaises(TypeError):
            classify_candidate("not bytes", candidate)


if __name__ == "__main__":
    unittest.main(verbosity=2)
