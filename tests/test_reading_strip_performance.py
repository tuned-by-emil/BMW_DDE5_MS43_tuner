"""Keep live gauge refreshes cheap without clipping values or stale-read ages."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import unittest
from unittest.mock import patch

from PySide6.QtCore import QEventLoop, QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from bmw_bin_editor.log_widgets import ReadingStrip, GaugeSetupDialog


APP = QApplication.instance() or QApplication([])


class ReadingStripPerformanceTests(unittest.TestCase):
    def test_pinned_add_and_direct_remove_stay_usable_during_updates(self):
        strip = ReadingStrip(lambda signal: signal, lambda *_args: None)
        signals = [f'Channel {index}' for index in range(12)]
        strip.set_signals(signals)
        strip.resize(520, 88)
        strip.show()
        APP.processEvents()
        additions, layouts, frames = [], [], []
        strip.manageRequested.connect(lambda: additions.append(True))
        strip.layoutChanged.connect(layouts.append)
        loop, ticker, deadline = QEventLoop(), QTimer(), QTimer()
        deadline.setSingleShot(True)
        deadline.timeout.connect(loop.quit)

        def sample():
            frames.append(len(frames))
            for signal in tuple(strip.cells):
                strip.update_value(signal, 1000 + len(frames), 'rpm', '')
            if len(frames) == 3:
                before = strip.manage_button.mapTo(strip, strip.manage_button.rect().center())
                strip.horizontalScrollBar().setValue(strip.horizontalScrollBar().maximum())
                after = strip.manage_button.mapTo(strip, strip.manage_button.rect().center())
                self.assertEqual(before, after)
                self.assertLess(strip.actions.geometry().right(), strip.viewport().geometry().left())
                QTest.mouseClick(strip.manage_button, Qt.LeftButton)
                QTest.mouseClick(strip.cells[signals[-1]][0].remove_button, Qt.LeftButton)
            if len(frames) >= 10:
                loop.quit()

        ticker.timeout.connect(sample)
        try:
            ticker.start(1)
            deadline.start(2000)
            loop.exec()
            self.assertEqual(len(frames), 10)
            self.assertEqual(additions, [True])
            self.assertEqual(layouts, [signals[:-1]])
            self.assertEqual(list(strip.schema), signals[:-1])
            self.assertEqual(strip.cells[signals[0]][1].text(), '1010 rpm')
        finally:
            ticker.stop()
            deadline.stop()
            strip.close()

    def test_samples_that_fit_do_not_force_strip_layout(self):
        strip = ReadingStrip(lambda signal: signal, lambda *_args: None)
        strip.set_signals(['RPM', 'Boost'])
        strip.show()
        try:
            strip.update_value('RPM', 9999, 'rpm', '')
            strip.update_value('Boost', 9999, 'hPa', '')
            APP.processEvents()
            widths = [cell.minimumWidth() for cell, _value, _age in strip.cells.values()]
            with patch.object(strip.row, 'activate', wraps=strip.row.activate) as activate:
                for index in range(100):
                    strip.update_value('RPM', 1000 + index, 'rpm', '')
                    strip.update_value('Boost', 1000 + index, 'hPa', '')
                activate.assert_not_called()
            self.assertEqual(widths, [cell.minimumWidth() for cell, _value, _age in strip.cells.values()])
            self.assertEqual(strip.cells['RPM'][1].text(), '1099 rpm')
        finally:
            strip.close()

    def test_longer_age_and_units_still_grow_cells(self):
        strip = ReadingStrip(lambda signal: signal, lambda *_args: None)
        strip.set_signals(['Fuel'])
        strip.show()
        try:
            strip.update_value('Fuel', 8.1, 'L/h', '')
            APP.processEvents()
            cell, value, age = strip.cells['Fuel']
            with patch.object(strip.row, 'activate', wraps=strip.row.activate) as activate:
                strip.update_value('Fuel', 888888.0, 'L/100 km', ' (12345.6s old)')
                self.assertGreater(activate.call_count, 0)
            APP.processEvents()
            self.assertGreaterEqual(value.width(), value.fontMetrics().horizontalAdvance(value.text()))
            self.assertGreaterEqual(age.width(), age.fontMetrics().horizontalAdvance(age.text()))
            widest = cell.minimumWidth()
            strip.update_value('Fuel', 1.0, 'L/h', '')
            self.assertEqual(cell.minimumWidth(), widest)
        finally:
            strip.close()

    def test_channel_picker_explains_live_request_and_recorded_limit(self):
        for request_live, expected in ((True, 'asked to add it to live logging'),
                                       (False, 'Recorded files cannot gain missing channels')):
            dialog = GaugeSetupDialog([], ['RPM'], str, session_signals=[],
                                      available_only=False, request_live=request_live)
            try:
                self.assertIn(expected, dialog.available.item(0).toolTip())
            finally:
                dialog.close()


if __name__ == '__main__':
    unittest.main()
