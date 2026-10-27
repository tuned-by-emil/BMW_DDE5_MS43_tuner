"""User consent, live display edits and acquisition stay independent."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication, QMessageBox, QDialogButtonBox

from bmw_bin_editor import gui
from bmw_bin_editor.core import AxisDefinition, CalibrationDefinition, fixed_axis_values
from bmw_bin_editor.live_logging import ANCHORS, FAST, load_parameters, make_groups, validate_profile
from bmw_bin_editor.live_channel_dialog import LiveChannelDialog
from bmw_bin_editor.logdata import (LiveTestOLog, MapLogProfile, SIG_RPM, SIG_IQ,
                                    SIG_BOOST, SIG_BOOST_SET, CALC_BOOST_ERROR, FUEL_RATE_SIGNAL)
from bmw_bin_editor.native_live_logging import NativeLiveLogger

APP = QApplication.instance() or QApplication([])
ROOT = Path(__file__).resolve().parents[1]
CATALOG = load_parameters(ROOT / 'definitions/live_parameters.json')


class PendingLogger:
    """Keep the applied profile unchanged until an ECU reply completes."""
    def __init__(self, parameters=FAST):
        self.catalog = deepcopy(CATALOG)
        self.profile = dict(version=1, ecu='D50M47A', name='GUI request',
                            parameters=list(parameters), interval_ms=0)
        self.groups = make_groups(self.profile['parameters'], self.catalog)
        self.capture = None
        self.requests = []

    def update_parameters(self, selected):
        requested = validate_profile(dict(self.profile, parameters=selected), self.catalog)
        self.requests.append(requested['parameters'])
        return requested


class LiveChannelRequestTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.settings = QSettings(str(Path(self.temporary.name) / 'prefs.ini'), QSettings.IniFormat)
        self.settings.setValue('settings/migrated', True)
        self.settings_patch = patch.object(gui, 'QSettings', lambda *_: self.settings)
        self.settings_patch.start()
        self.panel = gui.LogViewerPanel()
        self.panel.live_mode = True
        self.panel.log = LiveTestOLog(self.panel.catalog)
        self.logger = PendingLogger()
        self.panel.direct_logger = self.logger
        self.panel.reading_signals = [SIG_RPM]

    def tearDown(self):
        self.panel.live_mode = False
        self.panel.direct_logger = None
        self.panel._deferred_trace_definition = None
        self.panel.close()
        APP.processEvents()
        self.settings_patch.stop()
        self.temporary.cleanup()

    def test_accept_gauge_add_resolves_result_to_catalog_argument(self):
        temperature = CATALOG['ITKUM']['result']
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.Yes) as question:
            self.assertTrue(self.panel._set_reading_channels([SIG_RPM, temperature]))
        self.assertEqual(self.panel.reading_signals, [SIG_RPM, temperature])
        self.assertEqual(self.logger.requests, [FAST + ['ITKUM']])
        self.assertEqual(self.logger.profile['parameters'], FAST)
        self.assertIn('these gauges', question.call_args.args[2])

    def test_decline_add_leaves_gauges_and_logging_selection_unchanged(self):
        before = self.settings.value('log/channel_display')
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.No):
            self.assertFalse(self.panel._set_reading_channels([SIG_RPM, CATALOG['ITKUM']['result']]))
        self.assertEqual(self.panel.reading_signals, [SIG_RPM])
        self.assertEqual(self.logger.requests, [])
        self.assertEqual(self.settings.value('log/channel_display'), before)

    def test_remove_gauge_changes_display_without_removing_logged_parameter(self):
        self.panel.reading_signals = [SIG_RPM, SIG_BOOST]
        with patch.object(QMessageBox, 'question') as question:
            self.assertTrue(self.panel._set_reading_channels([SIG_RPM]))
        question.assert_not_called()
        self.assertEqual(self.panel.reading_signals, [SIG_RPM])
        self.assertIn('IPLAD', self.logger.profile['parameters'])
        self.assertEqual(self.logger.requests, [])

    def test_already_acquired_gauge_does_not_prompt(self):
        with patch.object(QMessageBox, 'question') as question:
            self.assertTrue(self.panel._set_reading_channels([SIG_RPM, SIG_BOOST]))
        question.assert_not_called()
        self.assertEqual(self.logger.requests, [])

    def test_eleventh_value_warns_about_another_ten_value_block_and_csv_segment(self):
        self.logger.capture = SimpleNamespace(path=Path(self.temporary.name) / 'run.csv')
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.No) as question:
            self.assertFalse(self.panel._ensure_live_signals([CATALOG['ITKUM']['result']], 'this gauge'))
        text = question.call_args.args[2]
        self.assertIn('1 → 2', text)
        self.assertIn('10 values per block', text)
        self.assertIn('significantly slows logging', text)
        self.assertIn('new segment', text)
        self.assertEqual(self.logger.requests, [])

    def test_adding_inside_existing_block_omits_extra_block_warning(self):
        self.panel.direct_logger = self.logger = PendingLogger(FAST[:-1])
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.Yes) as question:
            self.assertTrue(self.panel._ensure_live_signals([CATALOG[FAST[-1]]['result']], 'this gauge'))
        text = question.call_args.args[2]
        self.assertIn('1 → 1', text)
        self.assertNotIn('significantly slows logging', text)

    def test_table_prompt_requests_only_missing_position_parameters_and_once(self):
        definition = CalibrationDefinition('Trace fixture', '', '', 0, 2, 2)
        self.panel.definition = definition
        self.panel.profile = MapLogProfile('Table', SIG_RPM, CATALOG['IWVE1']['result'],
                                           ['unrelated context trace'], [])
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.Yes) as question, \
                patch.object(gui, 'profile_for_definition', return_value=self.panel.profile):
            self.panel._request_table_channels(definition)
            self.panel._request_table_channels(definition)
        self.assertEqual(question.call_count, 1)
        self.assertEqual(self.logger.requests, [FAST + ['IWVE1']])

    def test_stop_during_modal_confirmation_cancels_parameter_change(self):
        def stop_then_accept(*_):
            self.panel.live_mode = False
            self.panel.direct_logger = None
            return QMessageBox.Yes
        with patch.object(QMessageBox, 'question', side_effect=stop_then_accept):
            self.assertFalse(self.panel._set_reading_channels([SIG_RPM, CATALOG['ITKUM']['result']]))
        self.assertEqual(self.logger.requests, [])
        self.assertEqual(self.panel.reading_signals, [SIG_RPM])

    def test_pending_adds_are_unioned_without_waiting_for_reply(self):
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.Yes) as question:
            self.assertTrue(self.panel._ensure_live_signals([CATALOG['ITKUM']['result']], 'first gauge'))
            self.assertTrue(self.panel._ensure_live_signals([CATALOG['IWVE1']['result']], 'second gauge'))
            self.assertTrue(self.panel._ensure_live_signals([CATALOG['ITKUM']['result']], 'existing pending gauge'))
        self.assertEqual(question.call_count, 2)
        self.assertEqual(self.logger.requests, [FAST + ['ITKUM'], FAST + ['ITKUM', 'IWVE1']])
        self.assertEqual(self.logger.profile['parameters'], FAST)

    def test_nested_prompt_is_suppressed(self):
        nested = []
        def accept_outer(*_):
            nested.append(self.panel._ensure_live_signals([CATALOG['IWVE1']['result']], 'nested'))
            return QMessageBox.Yes
        with patch.object(QMessageBox, 'question', side_effect=accept_outer) as question:
            self.assertTrue(self.panel._ensure_live_signals([CATALOG['ITKUM']['result']], 'outer'))
        self.assertEqual(nested, [False])
        self.assertEqual(question.call_count, 1)
        self.assertEqual(self.logger.requests, [FAST + ['ITKUM']])

    def test_calculated_boost_gauge_regroups_split_dependencies_with_consent(self):
        pair = ['IPLAD', 'SPLAD']
        extras = [arg for arg in FAST[2:] if arg not in pair] + ['ITKUM']
        selected = ANCHORS + extras + pair
        self.panel.direct_logger = self.logger = PendingLogger(selected)
        self.assertFalse(any(set(pair) <= set(group) for group in self.logger.groups))
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.Yes) as question:
            self.assertTrue(self.panel._ensure_live_signals([CALC_BOOST_ERROR], 'calculated gauge'))
        question.assert_called_once()
        self.assertEqual(len(self.logger.requests), 1)
        requested = self.logger.requests[0]
        self.assertEqual(set(requested), set(selected))
        self.assertTrue(any(set(pair) <= set(group) for group in make_groups(requested, self.logger.catalog)))

    def test_calculated_boost_request_over_parameter_limit_shows_warning(self):
        extras = [arg for arg in CATALOG if arg not in ANCHORS + ['IPLAD', 'SPLAD']][:63]
        self.panel.direct_logger = self.logger = PendingLogger(ANCHORS + ['IPLAD'] + extras)
        with patch.object(QMessageBox, 'warning') as warning, patch.object(QMessageBox, 'question') as question:
            self.assertFalse(self.panel._ensure_live_signals([CALC_BOOST_ERROR], 'calculated gauge'))
        question.assert_not_called()
        warning.assert_called_once()
        self.assertIn('66 parameters', warning.call_args.args[2])
        self.assertEqual(self.logger.requests, [])

    def test_live_channel_dialog_locks_anchors_and_preserves_checks_while_searching(self):
        dialog = LiveChannelDialog(self.logger.catalog, self.logger.profile, self.panel)
        items = {dialog.channels.item(i).data(Qt.UserRole): dialog.channels.item(i)
                 for i in range(dialog.channels.count())}
        for anchor in ANCHORS:
            self.assertEqual(items[anchor].checkState(), Qt.Checked)
            self.assertFalse(items[anchor].flags() & Qt.ItemIsEnabled)
        items['ITKUM'].setCheckState(Qt.Checked)
        self.assertEqual(dialog.selected(), FAST + ['ITKUM'])
        self.assertIn('2 realtime block(s)', dialog.summary.text())
        self.assertIn('significantly slows logging', dialog.summary.text())
        self.assertTrue(dialog.buttons.button(QDialogButtonBox.Ok).isEnabled())
        dialog.search.setText('ITKUM')
        self.assertFalse(items['ITKUM'].isHidden())
        self.assertTrue(items['INMOT'].isHidden())
        self.assertEqual(dialog.selected(), FAST + ['ITKUM'])
        dialog.search.clear()
        self.assertTrue(all(not item.isHidden() for item in items.values()))
        self.assertEqual(dialog.selected(), FAST + ['ITKUM'])
        dialog.close()

    def test_native_logger_exposes_selected_channels_before_first_sample(self):
        logger = NativeLiveLogger(dict(profile=self.logger.profile, output=''), CATALOG)
        self.panel.direct_logger = logger
        signals = self.panel._available_gauge_signals()
        self.assertIn(SIG_RPM, signals)
        self.assertIn(SIG_IQ, signals)
        self.assertIn(SIG_BOOST, signals)
        self.assertIn(FUEL_RATE_SIGNAL, signals)
        self.assertNotIn(CATALOG['ITKUM']['result'], signals)
        self.assertEqual(self.panel.log.sample_count, 0)

    def test_fast_live_samples_are_all_retained_while_full_redraw_is_throttled(self):
        redraw = self.panel._redraw
        with patch.object(self.panel, '_redraw', wraps=redraw) as redraw_spy:
            for index in range(25):
                with patch.object(gui.time, 'monotonic', return_value=100 + index * .001):
                    self.panel._live_sample(index * .001, {SIG_RPM: 1000 + index}, 'D50M47A', 'test')
            self.assertEqual(redraw_spy.call_count, 1)
            with patch.object(gui.time, 'monotonic', return_value=100.2):
                self.panel._live_sample(.2, {SIG_RPM: 2000}, 'D50M47A', 'test')
            self.assertEqual(redraw_spy.call_count, 2)
        self.assertEqual(self.panel.log.sample_count, 26)
        np.testing.assert_array_equal(self.panel.log.series(SIG_RPM), list(range(1000, 1025)) + [2000])
        self.assertEqual(self.panel.cursor_index, 25)


class MapTraceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        settings = QSettings(str(Path(self.temporary.name) / 'map.ini'), QSettings.IniFormat)
        settings.setValue('settings/migrated', True)
        self.settings_patch = patch.object(gui, 'QSettings', lambda *_: settings)
        self.settings_patch.start()
        self.editor = gui.MapEditor()
        self.log = LiveTestOLog(self.editor.log_panel.catalog)
        self.editor.log_data = self.log
        self.editor.log_panel.log = self.log

    def tearDown(self):
        self.editor.close()
        APP.processEvents()
        self.settings_patch.stop()
        self.temporary.cleanup()

    def prepare(self, definition, values, profile):
        self.editor.definition = definition
        self.editor.document = SimpleNamespace(map_values=lambda _d: values, axis_values=fixed_axis_values)
        self.editor.log_panel.profile = profile

    def test_vertical_curve_uses_y_binding_and_converts_pressure_for_cursor_and_heatmap(self):
        definition = CalibrationDefinition('Vertical curve', '', '', 0, 3, 1,
            y_axis=AxisDefinition(None, 3, units='kPa', labels=['100', '150', '200']))
        self.prepare(definition, [[10], [20], [30]], MapLogProfile('Vertical', None, SIG_BOOST, [], []))
        for index, pressure in enumerate((1000, 1500, 2000)):
            self.log.append_sample(index, {SIG_BOOST: pressure})
        self.log.units[SIG_BOOST] = 'mbar'
        self.assertEqual(self.editor._log_position(1), (0., 150., 20., 1., 0.))
        self.editor._recompute_log_heatmap(0, 2)
        np.testing.assert_array_equal(self.editor.log_heatmap, [[1], [1], [1]])

    def test_heatmap_and_cursor_apply_same_active_table_gate_and_axis_units(self):
        definition = CalibrationDefinition('Gated table', '', '', 0, 2, 2,
            x_axis=AxisDefinition(None, 2, units='kPa', labels=['100', '200']),
            y_axis=AxisDefinition(None, 2, units='rpm', labels=['1000', '2000']))
        profile = MapLogProfile('VE table', SIG_BOOST, SIG_RPM, [], [], active_signal='ACTIVEVE', active_value=2)
        self.prepare(definition, [[10, 20], [30, 40]], profile)
        for index, state in enumerate((1, 2, 2.5, float('nan'))):
            self.log.append_sample(index, {SIG_BOOST: 2000, SIG_RPM: 2000, 'ACTIVEVE': state})
        self.log.units[SIG_BOOST] = 'mbar'
        self.assertIsNone(self.editor._log_position(0))
        self.assertEqual(self.editor._log_position(1), (200., 2000., 40., 1., 1.))
        self.assertIsNone(self.editor._log_position(2))
        self.assertIsNone(self.editor._log_position(3))
        self.editor._recompute_log_heatmap(0, 3)
        np.testing.assert_array_equal(self.editor.log_heatmap, [[0, 0], [0, 1]])


if __name__ == '__main__':
    unittest.main()
