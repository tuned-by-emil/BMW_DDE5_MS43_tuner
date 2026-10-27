"""Physical table coordinates use source symbols, not presentation order."""
import math
from pathlib import Path
import tempfile
import unittest

from bmw_bin_editor.core import AxisDefinition, CalibrationDefinition, XDFDatabase
from bmw_bin_editor.logdata import LiveTestOLog, MapLogProfile, SignalCatalog, TestOLog, profile_for_definition
from bmw_bin_editor.ms43_logging_definition import MS43Definition


ROOT = Path(__file__).resolve().parents[1]


class TableTracingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ms43 = XDFDatabase(ROOT / "definitions/ms43x/Siemens_MS43_MS43X001_512K.xdf")
        cls.dde5 = XDFDatabase(ROOT / "definitions/EDC16C31 361830.xdf")

    def ms_table(self, name):
        return next(d for d in self.ms43.tables if d.title == name)

    def test_linked_axes_preserve_the_real_xy_orientation(self):
        table = self.ms_table("ip_iga_ron98_pl__n__maf")
        profile = profile_for_definition(table)
        self.assertEqual(table.x_axis.source_name, "ldpm_maf_iga_2")
        self.assertEqual((profile.x_signal, profile.y_signal), ("MAFIGA", "N"))
        pedal = profile_for_definition(self.ms_table("ip_tps_sp_pvs__pvs__n"))
        self.assertEqual((pedal.x_signal, pedal.y_signal), ("N", "PVS"))

    def test_partial_xdf_has_the_same_coordinate_bindings(self):
        partial = XDFDatabase(ROOT / "definitions/ms43x/Siemens_MS43_MS43X001_64K.xdf")
        by_title = {d.title: d for d in partial.tables}
        for table in self.ms43.tables:
            first = profile_for_definition(table)
            second = profile_for_definition(by_title[table.title])
            self.assertEqual((first.x_signal, first.y_signal), (second.x_signal, second.y_signal), table.title)

    def test_vertical_curves_only_request_their_varying_axis(self):
        for name, signal in (("id_n_max_mt__gear", "GEAR"), ("ip_ti_add_dly__vb", "VB")):
            table = self.ms_table(name)
            self.assertEqual(table.cols, 1)
            profile = profile_for_definition(table)
            self.assertIsNone(profile.x_signal)
            self.assertEqual(profile.y_signal, signal)
            self.assertEqual(profile.required_position_signals, [signal])

    def test_ve_integer_gate_works_identically_live_and_recorded(self):
        profile = profile_for_definition(self.ms_table("ip_map_ve_2__map__n"))
        self.assertEqual((profile.x_signal, profile.y_signal), ("N", "MAP"))
        self.assertIn("ACTIVEVE", profile.required_position_signals)
        selections = (1., 2., 2.5, math.nan, 2.)
        live = LiveTestOLog()
        for i, selection in enumerate(selections):
            live.append_sample(i, {"N": 2000., "MAP": 80., "ACTIVEVE": selection})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ve.csv"
            path.write_text("time;N;MAP;ACTIVEVE\n" + "".join(
                f"{i * 1000};2000;80;{selection}\n" for i, selection in enumerate(selections)), encoding="utf-8")
            recorded = TestOLog(path, MS43Definition().signal_catalog)
            for log in (live, recorded):
                self.assertEqual([profile.sample_active(log, i) for i in range(5)], [False, True, False, False, True])

    def test_unlogged_start_temperature_and_peak_pedal_are_explicit(self):
        for quantity in ("tco_st", "pvs_max", "maf_sub_diag"):
            axis = AxisDefinition(100, 4, source_name="ldpm_" + quantity + "_1")
            table = CalibrationDefinition("ip_test__" + quantity, "", "Injection", 200, 4, 1,
                                          x_axis=AxisDefinition(None, 1), y_axis=axis, profile_id="MS43X001")
            profile = profile_for_definition(table)
            self.assertIsNone(profile.y_signal)
            self.assertTrue(any(quantity in note for note in profile.notes))

    def test_all_automatic_ms43_channels_exist_in_the_adx(self):
        catalog = MS43Definition().catalog
        supported = 0
        for table in self.ms43.tables:
            profile = profile_for_definition(table)
            self.assertTrue(all(s in catalog for s in profile.required_position_signals), table.title)
            if table.category != "Axis" and table.size > 1:
                supported += (table.cols <= 1 or bool(profile.x_signal)) and (table.rows <= 1 or bool(profile.y_signal))
        self.assertGreaterEqual(supported, 450)

    def test_dde5_typed_temperature_voltage_and_soot_curves(self):
        expected = {
            0xF7154: "STAT_KUEHLMITTELTEMPERATUR_WERT",
            0xF9132: "STAT_ANSAUGLUFTTEMPERATUR_WERT",
            0xFD06E: "STAT_UBATT_WERT",
            0xFAAB4: "STAT_RUSSMASSE_IM_PARTIKELFILTER_WERT",
        }
        for address, signal in expected.items():
            profile = profile_for_definition(next(d for d in self.dde5.tables if d.z_address == address))
            self.assertEqual(profile.x_signal, signal)
            self.assertIsNone(profile.y_signal)
        rate = CalibrationDefinition("Torque slew", "", "Torque", 0, 1, 3,
                                     x_axis=AxisDefinition(None, 3, units="Nm/s"))
        self.assertIsNone(profile_for_definition(rate).x_signal)

    def test_axis_units_convert_pressure_and_operating_time(self):
        live = LiveTestOLog(SignalCatalog.load_tsv(ROOT / "definitions/testo_signals.tsv"))
        live.append_sample(0, {"STAT_RAILDRUCK_SOLL_WERT": 1500000., "STAT_BETRIEBSSTUNDENZAEHLER_WERT": 7200.},
                           {"STAT_RAILDRUCK_SOLL_WERT": "hPa", "STAT_BETRIEBSSTUNDENZAEHLER_WERT": "s"})
        profile = MapLogProfile("Test", "STAT_RAILDRUCK_SOLL_WERT", "STAT_BETRIEBSSTUNDENZAEHLER_WERT", [], [])
        self.assertEqual(profile.axis_value(live, "x", 0, AxisDefinition(None, 2, units="bar")), 1500.)
        self.assertEqual(profile.axis_value(live, "y", 0, AxisDefinition(None, 2, units="h")), 2.)

    def test_sparse_axes_hold_only_recent_past_values_and_allow_backward_scrub(self):
        profile = MapLogProfile("Sparse", "STAT_A_WERT", "STAT_B_WERT", [], [])
        live = LiveTestOLog()
        live.append_sample(0., {"STAT_B_WERT": 5.})
        live.append_sample(.1, {"STAT_A_WERT": 10.})
        live.append_sample(.4, {"STAT_B_WERT": 20.})
        live.append_sample(1.2, {"STAT_B_WERT": 30.})
        axis = AxisDefinition(None, 2)
        # No future borrowing at the start, despite a later finite A reading.
        self.assertTrue(math.isnan(profile.axis_value(live, "x", 0, axis)))
        self.assertEqual(profile.axis_value(live, "x", 2, axis), 10.)
        self.assertEqual(profile.axis_value(live, "y", 1, axis), 5.)
        self.assertTrue(math.isnan(profile.axis_value(live, "x", 3, axis)))
        self.assertEqual(profile.axis_value(live, "x", 2, axis), 10.)
        live.append_sample(1.3, {"STAT_A_WERT": 40.})
        self.assertEqual(profile.axis_value(live, "x", 4, axis), 40.)
        self.assertTrue(math.isnan(profile.axis_value(live, "x", 3, axis)))


if __name__ == "__main__":
    unittest.main()
