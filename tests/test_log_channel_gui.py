import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication
from bmw_bin_editor import gui
from bmw_bin_editor.log_widgets import ChannelSetupDialog, channel_style, scale_channel
from bmw_bin_editor.logdata import LiveTestOLog, SIG_RPM, SIG_N75, SIG_BOOST, SIG_BOOST_SET, SIG_IQ, SIG_RAIL, SIG_MAF

APP = QApplication.instance() or QApplication([])


def demo_log(catalog):
    log = LiveTestOLog(catalog)
    for t in range(120):
        wave = math.sin(t / 10)
        log.append_sample(float(t), {SIG_RPM: 2200 + wave*1200, SIG_N75: 65-wave*12,
                          SIG_BOOST: 1800+wave*700, SIG_BOOST_SET: 1900+wave*700,
                          SIG_IQ: 35+wave*20, SIG_RAIL: 800+wave*400, SIG_MAF: 650+wave*250})
    return log


class ChannelTests(unittest.TestCase):
    def test_age_warning_starts_at_half_a_second(self):
        self.panel.log = LiveTestOLog(self.panel.catalog)
        self.panel.log.append_sample(0., {SIG_RPM: 1200., SIG_IQ: 20.})
        self.panel.log.append_sample(.49, {SIG_IQ: 20.})
        self.panel.log.append_sample(.5, {SIG_IQ: 20.})
        gauge = self.panel.add_floating_gauge(SIG_RPM)
        self.panel.set_cursor(1)
        self.assertEqual(self.panel._display_log_value(SIG_RPM, 1), (1200., ''))
        self.assertNotIn('old', gauge.detail_label.text())
        self.panel.set_cursor(2)
        self.assertEqual(self.panel._display_log_value(SIG_RPM, 2), (1200., ' (0.5s old)'))
        self.assertIn('0.5s old', gauge.detail_label.text())
        self.assertTrue(math.isnan(self.panel.log.value(SIG_RPM, 1)))
        gauge.close()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_settings = gui.QSettings
        gui.QSettings = lambda *args: QSettings(str(Path(self.temp.name)/'settings.ini'), QSettings.IniFormat)
        self.panel = gui.LogViewerPanel()
        self.panel.log = demo_log(self.panel.catalog)
        self.panel._populate_signal_combo()
        self.panel._update_profile()
        self.panel._refresh_graph_combo()
        self.panel._redraw()

    def tearDown(self):
        self.panel.close()
        gui.QSettings = self.old_settings
        self.temp.cleanup()

    def test_independent_scales_cursor_and_missing_channel(self):
        self.panel.custom_graphs = [[SIG_RPM, SIG_N75, 'missing']]
        self.panel._redraw()
        lines = self.panel.axes[0].lines
        self.assertGreater(np.ptp(lines[0].get_ydata()), 80)
        self.assertGreater(np.ptp(lines[1].get_ydata()), 80)
        self.assertEqual(self.panel.axes[0].get_ylim(), (0, 100))
        self.panel.reading_signals = [SIG_RPM, SIG_N75, 'missing']
        self.panel.set_cursor(30)
        self.assertEqual(self.panel.graph_readings[0].rows[SIG_RPM].text(0), 'RPM')
        self.assertIn('—', self.panel.graph_readings[0].rows['missing'].text(1))
        self.assertIn('rpm', self.panel.reading_strip.cells[SIG_RPM][1].text())
        self.assertEqual(self.panel.reading_strip.cells['missing'][1].text(), '—')
        self.panel.log.append_sample(121, {SIG_N75: 50})
        self.panel.set_cursor(self.panel.log.sample_count-1)
        self.assertIn('2.0s old', self.panel.reading_strip.cells[SIG_RPM][2].text())

    def test_alternating_csv_groups_hold_only_previous_received_values(self):
        csv = Path(self.temp.name)/'alternating.csv'
        csv.write_text(f'time,time_unit,group,{SIG_RPM},{SIG_N75}\n'
                       '0,s,1,1000,\n0.1,s,2,,0\n0.2,s,1,1200,\n0.3,s,2,,55\n', encoding='utf-8')
        self.assertTrue(self.panel.open_log(csv))
        self.panel.custom_graphs = [[SIG_RPM, SIG_N75]]
        self.panel._redraw()
        self.panel.set_cursor(0)
        self.assertEqual(self.panel.reading_strip.cells[SIG_N75][1].text().split()[0], '—')
        self.panel.set_cursor(1)
        self.assertEqual(self.panel._display_log_value(SIG_RPM, 1)[0], 1000)
        self.assertEqual(self.panel.reading_strip.cells[SIG_RPM][2].text(), '')
        self.assertIn('1000', self.panel.graph_readings[0].rows[SIG_RPM].text(1))
        self.assertEqual(self.panel._display_log_value(SIG_N75, 2)[0], 0)
        self.panel.set_cursor(3)
        self.assertEqual(self.panel._display_log_value(SIG_RPM, 3)[0], 1200)
        self.assertEqual(self.panel.time_label.text(), 'Time 0.300 s')
        # Scrubbing backwards must never borrow a future sample or fill source arrays.
        self.panel.set_cursor(0)
        self.assertTrue(math.isnan(self.panel._display_log_value(SIG_N75, 0)[0]))
        self.assertTrue(math.isnan(self.panel.log.value(SIG_RPM, 1)))
        self.assertTrue(math.isnan(self.panel.log.value(SIG_N75, 2)))
        gauge = self.panel.add_floating_gauge(SIG_RPM)
        self.panel.set_cursor(1)
        self.assertEqual(gauge.value_label.text(), '1000.00')
        self.assertNotIn('old', gauge.detail_label.text())
        gauge.close()

    def test_crowded_graph_lists_scroll_without_overlapping(self):
        self.panel.resize(1100, 300)
        self.panel.compact_button.setChecked(True)
        self.panel.readings_button.setChecked(True)
        self.panel.custom_graphs = [self.panel.log.available_signals(), [SIG_RPM, SIG_N75]]
        self.panel.show()
        APP.processEvents()
        self.panel._redraw()
        self.panel.canvas.draw()
        APP.processEvents()
        first, second = self.panel.graph_readings
        self.assertLessEqual(first.geometry().bottom(), second.geometry().top())
        self.assertGreater(first.listing.verticalScrollBar().maximum(), 0)
        rects = [first.listing.visualItemRect(first.listing.topLevelItem(i))
                 for i in range(first.listing.topLevelItemCount())]
        self.assertTrue(all(a.bottom() < b.top() for a, b in zip(rects, rects[1:])))
        first.listing.verticalScrollBar().setValue(first.listing.verticalScrollBar().maximum())
        self.panel._redraw()
        self.panel.canvas.draw()
        self.assertGreater(self.panel.graph_readings[0].listing.verticalScrollBar().value(), 0)
        self.assertFalse(hasattr(self.panel, 'readout'))

    def test_manual_ranges_and_invalid_saved_settings(self):
        style = channel_style({SIG_N75: dict(auto=False, zero=0, full=100, lower=25, upper=75)}, SIG_N75)
        scaled, bounds = scale_channel(np.array([0., 50., 100., np.nan]), style)
        np.testing.assert_allclose(scaled[:3], [25, 50, 75])
        self.assertTrue(math.isnan(scaled[3]))
        self.assertEqual(bounds, (0, 100))
        bad = channel_style({SIG_N75: dict(auto=False, zero=100, full=0, color='bad')}, SIG_N75)
        self.assertTrue(bad['auto'])

    def test_dialog_order_area_cancel_and_persistence(self):
        original = [[SIG_RPM, SIG_N75], [SIG_BOOST]]
        dialog = ChannelSetupDialog(original, {}, [SIG_RPM], self.panel.log.available_signals(), gui.short_signal_name)
        dialog.tree.setCurrentItem(dialog.tree.topLevelItem(0).child(1))
        dialog._move(-1)
        self.assertEqual(dialog.graphs[0], [SIG_N75, SIG_RPM])
        dialog.auto.setChecked(False)
        dialog.zero.setValue(0)
        dialog.full.setValue(100)
        dialog.lower.setValue(20)
        dialog.upper.setValue(80)
        dialog.color_value = '#ff0000'
        dialog.show_reading.setChecked(True)
        dialog.area.setCurrentIndex(1)
        dialog._change_area()
        dialog.accept()
        self.assertEqual(dialog.graphs, [[SIG_RPM], [SIG_BOOST, SIG_N75]])
        self.assertEqual(original, [[SIG_RPM, SIG_N75], [SIG_BOOST]])
        self.panel.custom_graphs = dialog.graphs
        self.panel.channel_styles = dialog.styles
        self.panel.reading_signals = dialog.readings
        self.panel.save_layout_for_type()
        saved = json.loads(self.panel.settings.value(self.panel._layout_settings_key()))
        self.assertEqual(saved['styles'][SIG_N75]['lower'], 20)
        other = gui.LogViewerPanel()
        csv = Path(self.temp.name)/'reopen.csv'
        csv.write_text(f'time;{SIG_RPM};{SIG_N75}\n0;1000;50\n1;1200;55\n', encoding='utf-8')
        self.assertTrue(other.open_log(csv))
        self.assertEqual(other.custom_graphs, dialog.graphs)
        self.assertEqual(other.reading_signals, [SIG_RPM, SIG_N75])
        self.assertEqual(other.channel_styles[SIG_N75]['color'], '#ff0000')
        other.close()
        dialog.close()

    def test_strip_spans_main_window(self):
        window = gui.MainWindow()
        window.resize(1500, 900)
        window.show()
        APP.processEvents()
        strip = window.editor.log_panel.reading_strip
        self.assertGreater(strip.width(), window.width()-20)
        window.editor.log_panel.readings_button.setChecked(True)
        self.assertTrue(strip.isHidden())
        window.editor.log_panel.readings_button.setChecked(False)
        window.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
