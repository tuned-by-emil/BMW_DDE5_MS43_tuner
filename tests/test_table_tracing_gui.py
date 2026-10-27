"""Cursor and cell counts share physical coordinates and activation gates."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from bmw_bin_editor import gui
from bmw_bin_editor.core import AxisDefinition, CalibrationDefinition, CalibrationDocument
from bmw_bin_editor.logdata import LiveTestOLog, SIG_RAIL_SET
from bmw_bin_editor.xdf_edit import XDFWorkspace


APP = QApplication.instance() or QApplication([])


class TableTracingGuiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        settings = self.folder / "settings.ini"
        self.settings_patch = patch.object(gui, "QSettings", lambda *args: QSettings(str(settings), QSettings.IniFormat))
        self.settings_patch.start()
        self.editor = gui.MapEditor()

    def tearDown(self):
        self.editor.close()
        APP.processEvents()
        self.settings_patch.stop()
        self.temp.cleanup()

    def load_synthetic(self, title, definition, words, samples, units=None):
        workspace = XDFWorkspace(bin_size=256)
        workspace.set_metadata(title, "", "", False)
        definition = workspace.add(definition)
        image = bytearray(256)
        image[32:32 + len(words) * 2] = struct.pack(">" + "H" * len(words), *words)
        binary = self.folder / "synthetic.bin"
        binary.write_bytes(image)
        document = CalibrationDocument(workspace.database, binary)
        self.editor.set_document(document)
        self.editor.set_definition(definition)
        log = LiveTestOLog(self.editor.log_panel.catalog)
        for seconds, values in samples:
            log.append_sample(seconds, values, units)
        self.editor.log_panel.log = log
        self.editor._log_loaded(log)
        return log

    def test_vertical_curve_positions_and_counts_without_an_x_channel(self):
        definition = CalibrationDefinition("id_n_max_mt__gear", "", "Engine Speed", 32, 3, 1,
            x_axis=AxisDefinition(None, 1, labels=["0"]),
            y_axis=AxisDefinition(None, 3, units="Gear", labels=["1", "2", "3"]))
        self.load_synthetic("MS43X001", definition, [100, 200, 300],
                            [(0., {"GEAR": 1.}), (.1, {"GEAR": 2.5})])
        self.assertIsNone(self.editor.log_panel.profile.x_signal)
        self.assertIsNotNone(self.editor._prepare_log_map())
        self.assertEqual(self.editor._log_position(1), (0., 2.5, 250., 1.5, 0.))
        self.editor._recompute_log_heatmap(0., .1)
        np.testing.assert_array_equal(self.editor.log_heatmap, [[1], [0], [1]])

    def test_heatmap_and_cursor_convert_logged_pressure_into_table_units(self):
        definition = CalibrationDefinition("Rail precontrol offset vs pressure", "", "Rail", 32, 1, 3,
            x_axis=AxisDefinition(None, 3, units="bar", labels=["500", "1000", "1500"]),
            y_axis=AxisDefinition(None, 1, labels=["0"]))
        self.load_synthetic("EDC16C31 361830", definition, [10, 20, 30],
                            [(0., {SIG_RAIL_SET: 500000.}), (.1, {SIG_RAIL_SET: 1000000.}),
                             (.2, {SIG_RAIL_SET: 1500000.})], {SIG_RAIL_SET: "hPa"})
        self.assertEqual(self.editor._log_position(1), (1000., None, 20., 0., 1.))
        self.editor._recompute_log_heatmap(0., .2)
        np.testing.assert_array_equal(self.editor.log_heatmap, [[1, 1, 1]])

    def test_ve_active_gate_applies_to_cursor_and_heatmap(self):
        definition = CalibrationDefinition("ip_map_ve_1__map__n", "", "Load Acquisition", 32, 2, 2,
            x_axis=AxisDefinition(None, 2, units="rpm", labels=["1000", "2000"]),
            y_axis=AxisDefinition(None, 2, units="kPa", labels=["50", "100"]))
        selections = (1., 2., 1.5, float("nan"), 1.)
        self.load_synthetic("MS43X001", definition, [10, 20, 30, 40],
                            [(i * .1, {"N": 1500., "MAP": 75., "ACTIVEVE": selection})
                             for i, selection in enumerate(selections)])
        self.assertEqual(self.editor._log_position(0), (1500., 75., 25., .5, .5))
        for index in (1, 2, 3):
            self.assertIsNone(self.editor._log_position(index))
        self.assertIsNotNone(self.editor._log_position(4))
        self.editor._recompute_log_heatmap(0., .4)
        np.testing.assert_array_equal(self.editor.log_heatmap, [[2, 0], [0, 0]])


if __name__ == "__main__":
    unittest.main()
