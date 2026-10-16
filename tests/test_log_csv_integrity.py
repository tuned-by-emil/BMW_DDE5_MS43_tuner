"""Acquisition integrity: preserve partial groups and validate playback time."""
from pathlib import Path
import math
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bmw_bin_editor.logdata import LiveTestOLog, TestOLog


class CsvIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "capture.csv"

    def tearDown(self):
        self.temporary.cleanup()

    def parse(self, rows):
        self.path.write_text("time,time_unit,group,STAT_A_WERT,STAT_B_WERT\n" + rows, encoding="utf-8")
        return TestOLog(self.path)

    def test_complementary_equal_timestamps_preserve_both_groups(self):
        log = self.parse("0,s,1,5,\n0.0,s,2,,9\n1,s,1,6,\n")
        self.assertEqual(log.sample_count, 2)
        self.assertEqual(log.complementary_rows_merged, 1)
        self.assertEqual(log.duplicates_removed, 0)
        self.assertEqual(log.value("STAT_A_WERT", 0), 5)
        self.assertEqual(log.value("STAT_B_WERT", 0), 9)
        self.assertTrue(math.isnan(log.value("STAT_B_WERT", 1)))

    def test_duplicate_broadcast_still_counts_once(self):
        log = self.parse("0,s,1,5,9\n0.0,s,1,5.0,9\n1,s,1,6,10\n")
        self.assertEqual(log.sample_count, 2)
        self.assertEqual(log.duplicates_removed, 1)
        self.assertEqual(log.complementary_rows_merged, 0)

    def test_conflicting_equal_timestamp_readings_are_retained(self):
        log = self.parse("0,s,1,5,9\n0,s,1,6,9\n1,s,1,7,10\n")
        self.assertEqual(log.sample_count, 3)
        self.assertEqual(log.series("STAT_A_WERT").tolist(), [5, 6, 7])
        self.assertEqual(log.times.tolist(), [0, 0, 1])

    def test_blank_rows_between_request_groups_remain_missing(self):
        log = self.parse("0,s,1,5,\n1,s,2,,9\n2,s,1,6,\n")
        self.assertTrue(math.isnan(log.value("STAT_A_WERT", 1)))
        self.assertTrue(math.isnan(log.value("STAT_B_WERT", 2)))

    def test_invalid_timestamps_are_rejected_with_row_number(self):
        for invalid in ("", "invalid", "nan", "inf", "-inf"):
            with self.subTest(timestamp=invalid):
                with self.assertRaisesRegex(ValueError, "row 3.*invalid timestamp"):
                    self.parse(f"0,s,1,5,\n{invalid},s,2,,9\n")

    def test_decreasing_timestamps_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "row 4.*decreasing timestamp"):
            self.parse("0,s,1,5,\n2,s,2,,9\n1,s,1,6,\n")

    def test_cursor_uses_nearest_sample(self):
        csv_log = self.parse("0,s,1,5,\n2,s,1,6,\n4,s,1,7,\n")
        live_log = LiveTestOLog()
        for seconds in (0, 2, 4):
            live_log.append_sample(seconds, {"STAT_A_WERT": seconds})
        for log in (csv_log, live_log):
            for seconds, expected in ((-1, 0), (1, 0), (1.1, 1), (2.1, 1), (3, 1), (3.1, 2), (5, 2)):
                with self.subTest(log=type(log).__name__, seconds=seconds):
                    self.assertEqual(log.nearest_index(seconds), expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
