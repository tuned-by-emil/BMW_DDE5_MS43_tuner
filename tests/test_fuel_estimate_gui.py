import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import hashlib
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication, QDialog
from bmw_bin_editor import gui
from bmw_bin_editor.log_widgets import FuelEstimateDialog, GaugeSetupDialog
from bmw_bin_editor.logdata import (FuelEstimateConfig, LiveTestOLog, SIG_RPM, SIG_IQ, SIG_SPEED,
                                   FUEL_RATE_SIGNAL, FUEL_CONSUMPTION_SIGNAL)

APP = QApplication.instance() or QApplication([])


class FuelEstimateGuiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_settings = gui.QSettings
        gui.QSettings = lambda *_args: QSettings(str(Path(self.temp.name) / 'settings.ini'), QSettings.IniFormat)
        self.panel = gui.LogViewerPanel()
        self.csv = Path(self.temp.name) / 'fuel.csv'
        self.csv.write_text(f'time,time_unit,{SIG_RPM},{SIG_IQ},{SIG_SPEED}\n'
                            '0,s,1000,20,100\n0.1,s,1000,20,\n0.2,s,1000,20,0\n'
                            '0.3,s,1000,20,\n0.4,s,,20,100\n', encoding='utf-8')
        self.source_hash = hashlib.sha256(self.csv.read_bytes()).hexdigest()
        self.assertTrue(self.panel.open_log(self.csv))

    def tearDown(self):
        self.panel.close()
        gui.QSettings = self.old_settings
        self.temp.cleanup()

    def test_estimates_are_default_selectable_readings_with_assumptions(self):
        channels = self.panel._reading_channels()
        self.assertIn(FUEL_RATE_SIGNAL, channels)
        self.assertIn(FUEL_CONSUMPTION_SIGNAL, channels)
        self.assertEqual(self.panel.reading_strip.cells[FUEL_RATE_SIGNAL][1].text(), '2.87 L/h')
        self.assertEqual(self.panel.reading_strip.cells[FUEL_CONSUMPTION_SIGNAL][1].text(), '2.87 L/100 km')
        for signal in (FUEL_RATE_SIGNAL, FUEL_CONSUMPTION_SIGNAL):
            idx = self.panel.signal_combo.findData(signal)
            self.assertGreaterEqual(idx, 0)
            tooltip = self.panel.signal_combo.itemData(idx, Qt.ToolTipRole)
            self.assertIn('4 cylinders', tooltip)
            self.assertIn('0.835 kg/L', tooltip)
            self.assertIn('pilot quantity is not added', tooltip)
            self.assertIn('Estimated', self.panel.catalog.label_for(signal))
        self.panel.custom_graphs = [[FUEL_RATE_SIGNAL, FUEL_CONSUMPTION_SIGNAL]]
        self.panel._redraw()
        self.assertEqual(self.panel.graph_readings[0].rows[FUEL_RATE_SIGNAL].text(0), 'Est. fuel flow')
        self.assertIn('four-stroke', self.panel.graph_readings[0].rows[FUEL_RATE_SIGNAL].toolTip(1))

    def test_stopped_speed_clears_prior_estimate_across_gauges_and_graphs(self):
        self.panel.custom_graphs = [[FUEL_CONSUMPTION_SIGNAL]]
        self.panel._redraw()
        gauge = self.panel.add_floating_gauge(FUEL_CONSUMPTION_SIGNAL)
        self.panel.set_cursor(1)
        self.assertEqual(self.panel.reading_strip.cells[FUEL_CONSUMPTION_SIGNAL][2].text(), '')
        self.assertAlmostEqual(self.panel._display_log_value(FUEL_CONSUMPTION_SIGNAL, 1)[0], 2.874251497)
        self.panel.set_cursor(2)
        self.assertTrue(math.isnan(self.panel._display_log_value(FUEL_CONSUMPTION_SIGNAL, 2)[0]))
        self.assertIn('—', gauge.value_label.text())
        self.assertIn('—', self.panel.graph_readings[0].rows[FUEL_CONSUMPTION_SIGNAL].text(1))
        self.panel.set_cursor(3)
        self.assertTrue(math.isnan(self.panel._display_log_value(FUEL_CONSUMPTION_SIGNAL, 3)[0]))
        self.panel.set_cursor(4)
        self.assertTrue(math.isnan(self.panel._display_log_value(FUEL_RATE_SIGNAL, 4)[0]))
        self.panel.set_cursor(0)
        self.assertAlmostEqual(self.panel._display_log_value(FUEL_CONSUMPTION_SIGNAL, 0)[0], 2.874251497)
        gauge.close()

    def test_accepted_assumptions_recalculate_persist_and_apply_to_future_logs(self):
        self.panel._set_reading_channels([SIG_RPM, FUEL_RATE_SIGNAL])
        def accepted(dialog):
            self.assertEqual(dialog.cylinders.value(), 4)
            self.assertEqual(dialog.density.value(), .835)
            dialog.cylinders.setValue(6)
            dialog.density.setValue(.900)
            return QDialog.Accepted
        with patch.object(FuelEstimateDialog, 'exec', accepted):
            self.panel.configure_fuel_estimates()
        self.assertAlmostEqual(self.panel.log.value(FUEL_RATE_SIGNAL, 0), 4.)
        self.assertEqual(self.panel.reading_strip.cells[FUEL_RATE_SIGNAL][1].text(), '4.00 L/h')
        self.assertEqual(self.panel.reading_signals, [SIG_RPM, FUEL_RATE_SIGNAL])
        self.assertIn('6 cylinders', self.panel.reading_strip.cells[FUEL_RATE_SIGNAL][0].toolTip())
        stored = json.loads(self.panel.settings.value('log/fuel_estimate_config'))
        self.assertEqual(stored['cylinders'], 6)
        other = gui.LogViewerPanel()
        try:
            self.assertTrue(other.open_log(self.csv))
            self.assertAlmostEqual(other.log.value(FUEL_RATE_SIGNAL, 0), 4.)
            self.assertEqual(other.reading_signals, [SIG_RPM, FUEL_RATE_SIGNAL])
        finally:
            other.close()
        self.assertEqual(hashlib.sha256(self.csv.read_bytes()).hexdigest(), self.source_hash)

    def test_cancel_keeps_assumptions_and_invalid_saved_preferences_use_defaults(self):
        before = self.panel.log.value(FUEL_RATE_SIGNAL, 0)
        def rejected(dialog):
            dialog.cylinders.setValue(8)
            return QDialog.Rejected
        with patch.object(FuelEstimateDialog, 'exec', rejected):
            self.panel.configure_fuel_estimates()
        self.assertEqual(self.panel.log.value(FUEL_RATE_SIGNAL, 0), before)
        self.assertFalse(self.panel.settings.contains('log/fuel_estimate_config'))
        self.panel.settings.setValue('log/fuel_estimate_config', '{"cylinders": 0, "density_kg_l": 0}')
        other = gui.LogViewerPanel()
        try:
            self.assertEqual(other.fuel_estimate_config, FuelEstimateConfig())
        finally:
            other.close()

    def test_live_request_availability_and_display_selection_do_not_add_requests(self):
        self.panel.log = LiveTestOLog(self.panel.catalog)
        parameters = ['RPM', 'IQ', 'SPEED']
        logger = SimpleNamespace(profile={'parameters': parameters.copy()}, groups=[parameters.copy()],
                                 catalog={arg: {'result': signal} for arg, signal in zip(
                                     parameters, [SIG_RPM, SIG_IQ, SIG_SPEED])})
        self.panel.direct_logger = logger
        try:
            available = self.panel._available_gauge_signals()
            self.assertIn(FUEL_RATE_SIGNAL, available)
            self.assertIn(FUEL_CONSUMPTION_SIGNAL, available)
            self.panel._set_reading_channels([FUEL_RATE_SIGNAL, FUEL_CONSUMPTION_SIGNAL])
            self.assertEqual(logger.profile['parameters'], parameters)
            self.assertEqual(logger.groups, [parameters])
            logger.groups = [['RPM', 'IQ'], ['SPEED']]
            self.assertIn(FUEL_RATE_SIGNAL, self.panel._available_gauge_signals())
            self.assertNotIn(FUEL_CONSUMPTION_SIGNAL, self.panel._available_gauge_signals())
            self.panel.set_fuel_estimate_config(FuelEstimateConfig(cylinders=8, density_kg_l=.800))
            self.panel._live_sample(0., {SIG_RPM: 1500., SIG_IQ: 20., SIG_SPEED: 100.}, 'test', 'test')
            self.assertAlmostEqual(self.panel.log.value(FUEL_RATE_SIGNAL, 0), 9.)
        finally:
            self.panel.direct_logger = None

    def test_custom_gauge_choices_are_preserved_and_low_speed_channel_is_not_available(self):
        self.panel._set_reading_channels([SIG_RPM])
        self.assertNotIn(FUEL_RATE_SIGNAL, self.panel._reading_channels())
        self.panel.restore_default_gauges()
        self.assertIn(FUEL_RATE_SIGNAL, self.panel._reading_channels())
        self.panel.log = LiveTestOLog(self.panel.catalog)
        self.panel.log.append_sample(0., {SIG_RPM: 1000., SIG_IQ: 20., SIG_SPEED: 0.})
        available = self.panel._available_gauge_signals()
        self.assertIn(FUEL_RATE_SIGNAL, available)
        self.assertNotIn(FUEL_CONSUMPTION_SIGNAL, available)
        dlg = GaugeSetupDialog([], available + [FUEL_CONSUMPTION_SIGNAL], gui.short_signal_name,
                               session_signals=available)
        try:
            item = next(dlg.available.item(i) for i in range(dlg.available.count())
                        if dlg.available.item(i).data(Qt.UserRole) == FUEL_CONSUMPTION_SIGNAL)
            self.assertTrue(item.isHidden())
            dlg.available_only.setChecked(False)
            self.assertFalse(item.isHidden())
        finally:
            dlg.close()

    def test_live_graph_picker_reveals_consumption_when_driving_starts(self):
        self.panel.log = LiveTestOLog(self.panel.catalog)
        self.panel._live_sample(0., {SIG_RPM: 1000., SIG_IQ: 20., SIG_SPEED: 0.}, 'test', 'test')
        self.assertGreaterEqual(self.panel.signal_combo.findData(FUEL_RATE_SIGNAL), 0)
        self.assertEqual(self.panel.signal_combo.findData(FUEL_CONSUMPTION_SIGNAL), -1)
        self.panel._live_sample(.1, {SIG_RPM: 1000., SIG_IQ: 20., SIG_SPEED: 10.}, 'test', 'test')
        self.assertGreaterEqual(self.panel.signal_combo.findData(FUEL_CONSUMPTION_SIGNAL), 0)

    def test_long_fuel_units_fit_cells_and_overflow_scrolls(self):
        from PySide6.QtGui import QFont, QFontDatabase
        from bmw_bin_editor.log_widgets import ReadingStrip
        font_path = Path(os.environ.get('WINDIR', '')) / 'Fonts' / 'arial.ttf'
        if font_path.is_file():
            QFontDatabase.addApplicationFont(str(font_path))
        strip = ReadingStrip(gui.short_signal_name, lambda *_args: None)
        strip.setFont(QFont('Arial', 9))
        strip.resize(400, 88)
        signals = [SIG_RPM, SIG_IQ, FUEL_RATE_SIGNAL, FUEL_CONSUMPTION_SIGNAL]
        strip.set_signals(signals)
        strip.show()
        try:
            for signal, value, unit in [(SIG_RPM, 2307, 'rpm'), (SIG_IQ, 31.4, 'mg/cyc'),
                                        (FUEL_RATE_SIGNAL, 10.41, 'L/h'),
                                        (FUEL_CONSUMPTION_SIGNAL, 8.07, 'L/100 km')]:
                strip.update_value(signal, value, unit, '')
            for _ in range(3):
                APP.processEvents()
            for signal in signals:
                cell, value, _age = strip.cells[signal]
                self.assertGreaterEqual(value.width(), value.fontMetrics().horizontalAdvance(value.text()))
                self.assertGreaterEqual(cell.width(), value.width() + 20)
                self.assertTrue(cell.testAttribute(Qt.WA_StyledBackground))
            self.assertGreater(strip.horizontalScrollBar().maximum(), 0)
            cell = strip.cells[FUEL_CONSUMPTION_SIGNAL][0]
            widest = cell.minimumWidth()
            strip.update_value(FUEL_CONSUMPTION_SIGNAL, 1, 'L/100 km', '')
            self.assertGreaterEqual(cell.minimumWidth(), widest)
        finally:
            strip.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
