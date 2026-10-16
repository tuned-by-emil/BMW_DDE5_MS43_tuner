import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import sys
import unittest
from pathlib import Path

import numpy as np
from PySide6.QtCore import QRect
from PySide6.QtWidgets import QApplication

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bmw_bin_editor.eoi_gui import EOIDialog
from bmw_bin_editor.logdata import LiveTestOLog, SignalCatalog, SignalDefinition, SIG_RPM, SIG_SOI


APP = QApplication.instance() or QApplication([])
DURATION = 'STAT_ANSTEUERDAUER_HAUPTEINSPRITZUNG1_WERT'


class EOILayoutTests(unittest.TestCase):
    def setUp(self):
        catalog = SignalCatalog([
            SignalDefinition('', '', signal, '', unit, signal, label, '')
            for signal, unit, label in [(SIG_RPM, 'rpm', 'RPM'),
                                        (SIG_SOI, 'deg', 'Start angle'),
                                        (DURATION, 'us', 'Injection duration')]
        ])
        log = LiveTestOLog(catalog)
        for index, rpm in enumerate((2000, 3000)):
            log.append_sample(float(index), {SIG_RPM: rpm, SIG_SOI: 10, DURATION: 1000})
        self.dialog = EOIDialog(log, None)

    def tearDown(self):
        self.dialog.close()

    def test_initial_size_fits_screen_and_small_window_keeps_controls_reachable(self):
        available = self.dialog.screen().availableGeometry()
        self.assertLessEqual(self.dialog.width(), available.width())
        self.assertLessEqual(self.dialog.height(), available.height())
        self.dialog.resize(800, 500)
        self.dialog.show()
        APP.processEvents()
        self.assertLessEqual(self.dialog.height(), 500)
        scroll = self.dialog.settings_scroll
        self.assertGreater(scroll.verticalScrollBar().maximum(), 0)
        scroll.ensureWidgetVisible(self.dialog.units)
        APP.processEvents()
        bounds = QRect(self.dialog.units.mapTo(scroll.viewport(), self.dialog.units.rect().topLeft()),
                       self.dialog.units.size())
        self.assertTrue(scroll.viewport().rect().contains(bounds))
        self.assertGreaterEqual(self.dialog.canvas.height(), 120)
        for button in (self.dialog.calculate_button, self.dialog.export, self.dialog.close_button):
            bounds = QRect(button.mapTo(self.dialog, button.rect().topLeft()), button.size())
            self.assertTrue(self.dialog.rect().contains(bounds))

    def test_calculation_and_invalidation_work_after_scrolling(self):
        self.dialog.resize(800, 500)
        self.dialog.show()
        APP.processEvents()
        self.dialog.settings_scroll.ensureWidgetVisible(self.dialog.sign)
        self.dialog.sign.setCurrentIndex(self.dialog.sign.findData('BTDC'))
        self.dialog.units.setCurrentIndex(self.dialog.units.findData('us'))
        self.dialog.calculate_button.click()
        self.assertIsNotNone(self.dialog.result)
        np.testing.assert_allclose(self.dialog.result[4], [2., 8.])
        self.assertTrue(self.dialog.export.isEnabled())
        self.assertEqual(self.dialog.result[5]['start_convention'], 'BTDC')
        self.dialog.units.setCurrentIndex(self.dialog.units.findData('ms'))
        self.assertIsNone(self.dialog.result)
        self.assertFalse(self.dialog.export.isEnabled())
        self.dialog.close_button.click()
        self.assertFalse(self.dialog.isVisible())


if __name__ == '__main__':
    unittest.main(verbosity=2)
