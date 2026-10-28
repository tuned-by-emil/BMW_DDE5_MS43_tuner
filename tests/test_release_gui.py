
from optional_resources import requires_resource
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import sys
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QSettings, QThread, Qt, QPointF, QMimeData
from PySide6.QtGui import QCloseEvent, QDropEvent
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QPushButton
from bmw_bin_editor import gui, APP_NAME, APP_AUTHOR, __version__
from bmw_bin_editor.settings import editor_settings
from bmw_bin_editor.log_widgets import GaugeSetupDialog, ChannelSetupDialog
from bmw_bin_editor.logdata import (LiveTestOLog, TestOLog, SIG_RPM, SIG_N75, SIG_BOOST,
                                   SIG_BOOST_SET, SIG_MAF, SIG_IQ,
                                   CALC_BOOST_ERROR, CALC_EST_LAMBDA, FUEL_RATE_SIGNAL)
from test_log_channel_gui import demo_log

APP = QApplication.instance() or QApplication([])


class ReleaseGuiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old = gui.QSettings
        gui.QSettings = lambda org, name: QSettings(str(Path(self.temp.name)/(name+'.ini')), QSettings.IniFormat)
        self.panel = gui.LogViewerPanel()
        self.panel.log = demo_log(self.panel.catalog)
        self.panel._populate_signal_combo()
        self.panel._update_profile()
        self.panel._refresh_graph_combo()
        self.panel._redraw()

    def tearDown(self):
        self.panel.close()
        gui.QSettings = self.old
        self.temp.cleanup()

    def test_move_remove_restore_and_drag_gauges_persist(self):
        self.panel._set_reading_channels([SIG_RPM, SIG_N75, SIG_BOOST])
        self.panel.reading_strip.move_signal(SIG_BOOST, 0)
        self.assertEqual(self.panel.reading_signals, [SIG_BOOST, SIG_RPM, SIG_N75])
        removed_cell = self.panel.reading_strip.cells[SIG_RPM][0]
        self.panel.reading_strip.remove_signal(SIG_RPM)
        self.assertTrue(removed_cell.isHidden())
        self.assertNotIn(SIG_RPM, self.panel.reading_strip.cells)
        saved = json.loads(self.panel.settings.value('log/channel_display'))
        self.assertEqual(saved['readings'], [SIG_BOOST, SIG_N75])
        self.panel.resize(1100, 400)
        self.panel.show()
        APP.processEvents()
        mime = QMimeData()
        mime.setData('application/x-bmw-gauge', SIG_BOOST.encode())
        event = QDropEvent(QPointF(1000, 30), Qt.MoveAction, mime, Qt.LeftButton, Qt.NoModifier)
        self.panel.reading_strip.dropEvent(event)
        self.assertEqual(self.panel.reading_signals, [SIG_N75, SIG_BOOST])
        for signal in list(self.panel.reading_signals):
            self.panel.reading_strip.remove_signal(signal)
        self.assertEqual(self.panel.reading_signals, [])
        self.assertTrue(self.panel.reading_strip.manage_button.isVisible())
        self.assertEqual(self.panel.reading_strip.manage_button.text(), '+ Add gauge')
        self.panel.restore_default_gauges()
        self.assertIn(SIG_RPM, self.panel.reading_strip.cells)
        self.assertIsNone(json.loads(self.panel.settings.value('log/channel_display'))['readings'])

    def test_gauge_dialog_reordering_search_and_no_duplicate_add(self):
        dlg = GaugeSetupDialog([SIG_RPM, SIG_N75], [SIG_RPM, SIG_N75, SIG_BOOST], gui.short_signal_name)
        dlg.available.setCurrentRow(2)
        dlg.add_selected()
        dlg.add_selected()
        self.assertEqual(dlg.selected().count(SIG_BOOST), 1)
        dlg.move(-1)
        self.assertEqual(dlg.selected(), [SIG_RPM, SIG_BOOST, SIG_N75])
        dlg.search.setText('RPM')
        self.assertFalse(dlg.available.item(0).isHidden())
        self.assertTrue(dlg.available.item(2).isHidden())
        dlg.reject()
        self.assertIsNone(self.panel.reading_signals)

    def test_gauge_available_filter_preserves_selected_and_combines_with_search(self):
        dlg = GaugeSetupDialog([SIG_BOOST], [SIG_RPM, SIG_N75, SIG_BOOST], gui.short_signal_name,
                               session_signals=[SIG_RPM, SIG_N75])
        self.assertTrue(dlg.available_only.isChecked())
        self.assertTrue(dlg.available.item(2).isHidden())
        self.assertEqual(dlg.selected(), [SIG_BOOST])
        self.assertIn('2 channel(s)', dlg.count_label.text())
        dlg.available.setCurrentRow(2)
        self.assertFalse(dlg.add_button.isEnabled())
        dlg.chosen.setCurrentRow(0)
        dlg.remove_selected()
        dlg.add_selected()
        self.assertEqual(dlg.selected(), [])
        dlg.available_only.setChecked(False)
        self.assertFalse(dlg.available.item(2).isHidden())
        self.assertTrue(dlg.add_button.isEnabled())
        dlg.add_selected()
        self.assertEqual(dlg.selected(), [SIG_BOOST])
        dlg.search.setText('RPM')
        self.assertFalse(dlg.available.item(0).isHidden())
        self.assertTrue(dlg.available.item(1).isHidden())
        self.assertTrue(dlg.available.item(2).isHidden())
        self.assertIn('1 channel(s)', dlg.count_label.text())
        dlg.close()

    def test_recorded_availability_excludes_empty_channels_and_unusable_calculations(self):
        csv = Path(self.temp.name)/'availability.csv'
        csv.write_text(f'time,time_unit,{SIG_RPM},{SIG_N75},{SIG_MAF},{SIG_IQ}\n'
                       '0,s,1000,,600,\n0.1,s,1200,,,30\n', encoding='utf-8')
        self.panel.log = TestOLog(csv, self.panel.catalog)
        available = self.panel._available_gauge_signals()
        self.assertEqual(set(available), {SIG_RPM, SIG_MAF, SIG_IQ, FUEL_RATE_SIGNAL})
        self.assertNotIn(SIG_N75, available)
        self.assertIn(CALC_EST_LAMBDA, self.panel.log.values)
        self.assertNotIn(CALC_EST_LAMBDA, available)
        self.assertTrue(self.panel.log.series(SIG_MAF)[1] != self.panel.log.series(SIG_MAF)[1])

    def test_live_availability_before_samples_and_gauges_do_not_change_requests(self):
        self.panel.log = LiveTestOLog(self.panel.catalog)
        parameters = ['RPM', 'MAF', 'IQ', 'MAP', 'TARGET']
        profile = {'parameters': parameters.copy()}
        groups = [['RPM', 'MAF', 'IQ'], ['MAP', 'TARGET']]
        logger = SimpleNamespace(profile=profile, groups=groups,
                                 catalog={arg: {'result': result} for arg, result in zip(
                                     parameters, [SIG_RPM, SIG_MAF, SIG_IQ, SIG_BOOST, SIG_BOOST_SET])})
        self.panel.direct_logger = logger
        try:
            available = self.panel._available_gauge_signals()
            self.assertEqual(set(available), {SIG_RPM, SIG_MAF, SIG_IQ, SIG_BOOST, SIG_BOOST_SET,
                                              CALC_EST_LAMBDA, CALC_BOOST_ERROR, FUEL_RATE_SIGNAL})
            self.assertNotIn(SIG_N75, available)
            self.panel._set_reading_channels([SIG_RPM, SIG_N75])
            self.assertEqual(profile['parameters'], parameters)
            self.assertEqual(groups, [['RPM', 'MAF', 'IQ'], ['MAP', 'TARGET']])
            self.assertEqual(self.panel.log.sample_count, 0)
            self.assertIn('—', self.panel.reading_strip.cells[SIG_N75][1].text())
            logger.groups = [[arg] for arg in parameters]
            self.assertNotIn(CALC_EST_LAMBDA, self.panel._available_gauge_signals())
            self.assertNotIn(CALC_BOOST_ERROR, self.panel._available_gauge_signals())
        finally:
            self.panel.direct_logger = None
        self.assertEqual(self.panel._available_gauge_signals(), [])

    def test_filter_preference_applies_only_when_gauge_setup_is_accepted(self):
        def accepted(dialog):
            self.assertTrue(dialog.available_only.isChecked())
            dialog.available_only.setChecked(False)
            return QDialog.Accepted
        with patch.object(GaugeSetupDialog, 'exec', accepted):
            self.panel.configure_gauges()
        self.assertFalse(self.panel.settings.value('log/gauges_available_only', True, type=bool))
        def rejected(dialog):
            self.assertFalse(dialog.available_only.isChecked())
            dialog.available_only.setChecked(True)
            return QDialog.Rejected
        with patch.object(GaugeSetupDialog, 'exec', rejected):
            self.panel.configure_gauges()
        self.assertFalse(self.panel.settings.value('log/gauges_available_only', True, type=bool))

    def test_channel_setup_uses_same_gauge_filter_and_keeps_cancelled_choices(self):
        dlg = ChannelSetupDialog([[SIG_RPM]], {}, [SIG_RPM], [SIG_RPM], gui.short_signal_name,
                                 gauge_available=[SIG_RPM, SIG_N75], session_signals=[SIG_RPM])
        def accepted(picker):
            self.assertTrue(picker.available_only.isChecked())
            self.assertTrue(picker.available.item(1).isHidden())
            picker.available_only.setChecked(False)
            picker.available.setCurrentRow(1)
            picker.add_selected()
            return QDialog.Accepted
        with patch.object(GaugeSetupDialog, 'exec', accepted):
            dlg._choose_readings()
        self.assertEqual(dlg.readings, [SIG_RPM, SIG_N75])
        self.assertFalse(dlg.gauge_available_only)
        def rejected(picker):
            picker.chosen.setCurrentRow(0)
            picker.remove_selected()
            picker.available_only.setChecked(True)
            return QDialog.Rejected
        with patch.object(GaugeSetupDialog, 'exec', rejected):
            dlg._choose_readings()
        self.assertEqual(dlg.readings, [SIG_RPM, SIG_N75])
        self.assertFalse(dlg.gauge_available_only)
        dlg.close()

    def test_release_identity_and_visible_shortcuts(self):
        self.assertRegex(__version__, r'^\d+\.\d+\.\d+$')
        self.assertIn(__version__, APP_NAME)
        dialog = gui.AboutDialog()
        labels = '\n'.join(label.text() for label in dialog.findChildren(QLabel))
        self.assertIn(APP_NAME, labels)
        self.assertIn('by '+APP_AUTHOR, labels)
        dialog.close()
        window = gui.MainWindow()
        window.showMaximized()
        APP.processEvents()
        self.assertTrue(window.isMaximized())
        self.assertEqual(window.act_save.shortcut().toString(), 'Ctrl+Shift+S')
        window.act_fullscreen.setChecked(True)
        self.assertTrue(window.isFullScreen())
        window.act_fullscreen.setChecked(False)
        self.assertTrue(window.isMaximized())
        window.close()

    @requires_resource('definitions/EDC16C31 361830.xdf')
    def test_startup_is_empty_and_retained_definition_is_available_explicitly(self):
        bundled = gui.bundled_definition_path()
        original = bundled.read_bytes()
        window = gui.MainWindow()
        try:
            self.assertIsNone(window.xdf)
            self.assertIsNone(window.document)
            self.assertIsNone(window.current_xdf_path)
            self.assertEqual(window.windowTitle(), APP_NAME)
            self.assertEqual(window.tree.topLevelItemCount(), 0)
            self.assertEqual(window.favorite_tree.topLevelItemCount(), 0)
            self.assertEqual(window.link_groups, [])
            self.assertEqual(window.step_overrides, {})
            self.assertFalse(window.settings.contains('linked_map_groups_initialized'))
            self.assertFalse(window.settings.contains('edit_steps_initialized'))
            for action in (window.act_save, window.act_quick_save, window.act_reload,
                           window.act_undo, window.act_redo):
                self.assertFalse(action.isEnabled())
            self.assertTrue(window.act_bin.isEnabled())
            self.assertTrue(window.act_xdf.isEnabled())
            self.assertTrue(window.act_new_xdf.isEnabled())
            window.act_bundled.trigger()
            self.assertEqual(window.xdf.profile_id, '361830')
            self.assertEqual(window.current_xdf_path.resolve(), bundled.resolve())
            self.assertEqual(len(window.xdf.tables), 207)
            self.assertGreater(window.tree.topLevelItemCount(), 0)
            self.assertIsNone(window.document)
            self.assertEqual(bundled.read_bytes(), original)
        finally:
            window.close()

    def test_empty_startup_preserves_saved_map_preferences_without_selecting_an_xdf(self):
        settings = editor_settings(gui.QSettings)
        values = {
            'linked_map_groups': json.dumps([{'name': 'My group', 'mode': 'absolute',
                'members': ['table:0F7F44', 'table:0F8478']}]),
            'edit_steps': json.dumps({'defaults': {'normal': 2, 'shift': 20, 'ctrl': .2},
                'overrides': {'table:0F7F44': {'normal': .5, 'shift': 5, 'ctrl': .1}}}),
            'favorite_maps': json.dumps(['table:0F7F44']),
        }
        for key, value in values.items():
            settings.setValue(key, value)
        window = gui.MainWindow()
        try:
            self.assertIsNone(window.xdf)
            self.assertEqual(window.link_groups, json.loads(values['linked_map_groups']))
            self.assertEqual(window.step_overrides, json.loads(values['edit_steps'])['overrides'])
            self.assertEqual(window.favorites, {'table:0F7F44'})
            self.assertEqual(window.tree.topLevelItemCount(), 0)
            self.assertEqual(window.favorite_tree.topLevelItemCount(), 0)
            for key, value in values.items():
                self.assertEqual(settings.value(key), value)
        finally:
            window.close()

    def test_empty_editor_definition_and_information_actions_are_graceful(self):
        from bmw_bin_editor.xdf_gui import XDFEditorDialog
        window = gui.MainWindow()
        dialogs = []
        try:
            window.editor.btn_info.setChecked(True)
            self.assertEqual(window.editor.info.title.text(), 'No map selected')
            self.assertEqual(window.editor.info.meta.text(), '')
            with patch.object(gui.QMessageBox, 'information') as information:
                window.find_suspected_tables()
                window.suggest_matching_xdf()
                window.show_calibration_review()
                window.configure_link_groups()
                self.assertEqual(information.call_count, 4)
            def open_empty_dialog(dialog):
                dialogs.append(dialog)
                self.assertEqual(dialog.workspace.database.definitions, [])
                self.assertNotIn('361830', dialog.workspace.database.title)
                self.assertIsNone(dialog.preview.data)
                return QDialog.Rejected
            with patch.object(XDFEditorDialog, 'exec', open_empty_dialog):
                window.act_new_xdf.trigger()
                window.act_edit_xdf.trigger()
            self.assertEqual(len(dialogs), 2)
            self.assertIsNone(window.xdf)
            self.assertIsNone(window.document)
        finally:
            window.close()

    def test_drag_range_updates_existing_graph_and_finalizes_outside(self):
        ax = self.panel.axes[0]
        events = []
        self.panel.rangeChanged.connect(lambda a,b: events.append((a,b)))
        self.panel._mouse_press(SimpleNamespace(inaxes=ax, button=1, xdata=10, x=100))
        self.panel._mouse_motion(SimpleNamespace(inaxes=ax, xdata=20, x=200))
        self.assertIs(self.panel.axes[0], ax)
        self.assertEqual(self.panel.range_seconds, (10,20))
        self.panel._mouse_release(SimpleNamespace(inaxes=None, xdata=None, x=210))
        self.assertEqual(events, [(10,20)])

    def test_graph_limit_and_gauge_order_survive_table_layout_changes(self):
        self.panel._set_reading_channels([SIG_N75])
        self.panel.custom_graphs = [[SIG_RPM] for _ in range(8)]
        self.panel._refresh_graph_combo()
        self.assertFalse(self.panel.new_graph_button.isEnabled())
        self.panel.add_new_graph()
        self.assertEqual(len(self.panel.custom_graphs), 8)
        self.panel._layout_type_key = lambda: 'Another table type'
        self.panel._load_saved_layout_for_type()
        self.assertEqual(self.panel.custom_graphs, [])
        self.assertEqual(self.panel.reading_signals, [SIG_N75])

    def test_migration_keeps_accepted_preview_preferences_once(self):
        factory = lambda org, name: QSettings(str(Path(self.temp.name)/('migration-'+name+'.ini')), QSettings.IniFormat)
        preview = factory('EmilTools', 'BMWBinEditorV1265ReviewPreview')
        preview.setValue('favorites', 'favorite-list')
        preview.setValue('log/channel_display', '{"readings":["RPM"]}')
        migrated = editor_settings(factory, channel='stable')
        self.assertEqual(migrated.value('favorites'), 'favorite-list')
        migrated.setValue('favorites', 'edited-list')
        preview.setValue('favorites', 'obsolete-list')
        self.assertEqual(editor_settings(factory, channel='stable').value('favorites'), 'edited-list')

    def test_close_waits_for_a_pending_request_without_destroying_thread(self):
        class PendingThread(QThread):
            def __init__(self, parent):
                super().__init__(parent)
                self.ready = threading.Event()
                self.release = threading.Event()
            def run(self):
                self.ready.set()
                self.release.wait(10)
        window = gui.MainWindow()
        panel = window.editor.log_panel
        thread = PendingThread(panel)
        panel.live_worker = SimpleNamespace(stop=lambda: None)
        panel.live_thread = thread
        panel.live_mode = True
        thread.finished.connect(lambda: panel._live_finished(thread))
        thread.start()
        self.assertTrue(thread.ready.wait(1))
        try:
            event = QCloseEvent()
            window.closeEvent(event)
            self.assertFalse(event.isAccepted())
            self.assertIs(panel.live_thread, thread)
            self.assertTrue(thread.isRunning())
            self.assertFalse(panel.live_button.isEnabled())
        finally:
            thread.release.set()
            self.assertTrue(thread.wait(2000))
            APP.processEvents()
            window.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
