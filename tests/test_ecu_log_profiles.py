
from optional_resources import requires_resource
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication
from bmw_bin_editor import gui, live_logging
from bmw_bin_editor.settings import ECUSettings
from bmw_bin_editor.logdata import SIG_RPM, SIG_N75

APP = QApplication.instance() or QApplication([])


class ECUProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / 'settings.ini')
        self.base = QSettings(self.path, QSettings.IniFormat)
        self.base.setValue('settings/migrated', True)
        self.patcher = patch.object(gui, 'QSettings', lambda *a: QSettings(self.path, QSettings.IniFormat))
        self.patcher.start()
        self.widgets = []

    def tearDown(self):
        for widget in self.widgets:
            widget.close()
        self.patcher.stop()
        self.temp.cleanup()

    def panel(self):
        p = gui.LogViewerPanel()
        self.widgets.append(p)
        return p

    def test_legacy_copy_is_diesel_only_and_never_repeated(self):
        display = json.dumps({'readings': [SIG_RPM, SIG_N75], 'styles': {SIG_RPM: {'color': '#123456'}}})
        graphs = json.dumps({'graphs': [[SIG_N75]]})
        self.base.setValue('log/channel_display', display)
        self.base.setValue('log_graph_layouts/General', graphs)
        self.base.setValue('live126/profiles', '[{"name":"My diesel"}]')
        self.base.setValue('testo_mcp/job', 'MY_JOB')
        new = ECUSettings(self.base, 'MS43X001')
        self.assertFalse(new.contains('log/channel_display'))
        diesel = ECUSettings(self.base, '361830')
        for key in ['log/channel_display', 'log_graph_layouts/General', 'live126/profiles', 'testo_mcp/job']:
            self.assertEqual(diesel.value(key), self.base.value(key))
        diesel.setValue('log/channel_display', 'new diesel value')
        reopened = ECUSettings(self.base, '361830')
        self.assertEqual(reopened.value('log/channel_display'), 'new diesel value')
        self.assertEqual(self.base.value('log/channel_display'), display)
        diesel.remove('log_graph_layouts/General')
        self.assertFalse(ECUSettings(self.base, '361830').contains('log_graph_layouts/General'))
        self.assertEqual(self.base.value('log_graph_layouts/General'), graphs)

    def test_round_trip_and_restart_keep_independent_gauges_styles_graphs(self):
        self.base.setValue('log/channel_display', json.dumps({'readings': [SIG_N75], 'styles': {SIG_N75: {'color': '#123456'}}}))
        self.base.setValue('log_graph_layouts/General', json.dumps({'graphs': [[SIG_N75]]}))
        p = self.panel()
        p._update_profile()
        self.assertEqual(p.custom_graphs, [[SIG_N75]])
        p.set_ecu_profile('MS43X001')
        self.assertIsNone(p.reading_signals)
        self.assertEqual(p._reading_channels(), [])
        self.assertEqual(p.channel_styles, {})
        self.assertEqual(p.custom_graphs, [])
        p.channel_styles = {'MS43_RPM': {'color': '#abcdef'}}
        p._set_reading_channels(['MS43_RPM'])
        p.custom_graphs = [['MS43_RPM']]
        p._remember_graphs()
        p.set_ecu_profile('361830')
        self.assertEqual(p.reading_signals, [SIG_N75])
        self.assertEqual(p.channel_styles[SIG_N75]['color'], '#123456')
        self.assertEqual(p.custom_graphs, [[SIG_N75]])
        p.set_ecu_profile('MS43X001')
        self.assertEqual(p.custom_graphs, [['MS43_RPM']])
        self.assertEqual(p.reading_signals, ['MS43_RPM'])
        p.settings.sync()
        reopened = self.panel()
        reopened.set_ecu_profile('MS43X001')
        self.assertEqual(reopened.custom_graphs, [['MS43_RPM']])
        self.assertEqual(reopened.channel_styles['MS43_RPM']['color'], '#abcdef')
        self.assertEqual(reopened.reading_signals, ['MS43_RPM'])

    def test_same_table_type_in_two_ecus_is_separate(self):
        p = self.panel()
        p._update_profile()
        p.profile = SimpleNamespace(name='Boost target')
        p._load_saved_layout_for_type()
        p.custom_graphs = [[SIG_N75]]
        p._remember_graphs()
        p.set_ecu_profile('other-diesel')
        p.profile = SimpleNamespace(name='Boost target')
        p._load_saved_layout_for_type()
        self.assertEqual(p.custom_graphs, [])
        p.custom_graphs = [['OTHER_BOOST']]
        p._remember_graphs()
        p.set_ecu_profile('361830')
        p.profile = SimpleNamespace(name='Boost target')
        p._load_saved_layout_for_type()
        self.assertEqual(p.custom_graphs, [[SIG_N75]])

    def test_profile_without_tables_switches_and_clears_old_samples(self):
        editor = gui.MapEditor()
        self.widgets.append(editor)
        panel = editor.log_panel
        old_log = object()
        panel.log = old_log
        editor.log_data = old_log
        document = SimpleNamespace(xdf=SimpleNamespace(profile_id='MS43X001', title='MS43X001'))
        editor.set_document(document)
        self.assertEqual(panel.ecu_profile_id, 'MS43X001')
        self.assertIsNone(panel.log)
        self.assertIsNone(editor.log_data)
        self.assertEqual(panel.definition, None)
        # Another BIN with the same ECU retains the chosen profile.
        panel._set_reading_channels(['MS43_RPM'])
        editor.set_document(document)
        self.assertEqual(panel.reading_signals, ['MS43_RPM'])

    @requires_resource('definitions/ms43x/Siemens_MS43_MS43X001_Logging.adx')
    def test_ms43_adx_gauges_and_catalog_restore_separately_from_diesel(self):
        p = self.panel()
        diesel_catalog = p.catalog
        p._set_reading_channels([SIG_N75])
        p.set_ecu_profile('ms43-test', 'MS43X001')
        self.assertEqual(len(p.catalog.definitions), 131)
        self.assertEqual(p._signal_name('N'), 'Engine Speed')
        self.assertIn('N', p.reading_signals)
        self.assertNotIn(SIG_N75, p.reading_signals)
        p._set_reading_channels(['N', 'MAP'])
        p.set_ecu_profile('361830')
        self.assertEqual(p.reading_signals, [SIG_N75])
        self.assertEqual(set(p.catalog.by_result), set(diesel_catalog.by_result))
        p.set_ecu_profile('ms43-test', 'MS43X001')
        self.assertEqual(p.reading_signals, ['N', 'MAP'])

    def test_profile_switch_waits_for_logger_shutdown(self):
        editor = gui.MapEditor()
        self.widgets.append(editor)
        original = SimpleNamespace(xdf=SimpleNamespace(profile_id='361830', title='EDC16C31 361830'))
        editor.set_document(original)
        log = object()
        editor.log_data = log
        editor.log_panel.log = log
        other = SimpleNamespace(xdf=SimpleNamespace(profile_id='MS43X001', title='MS43X001'))
        with patch.object(editor.log_panel, 'stop_live', return_value=False):
            with self.assertRaises(gui.EditorError):
                editor.set_document(other)
        self.assertIs(editor.document, original)
        self.assertIs(editor.log_data, log)
        self.assertIs(editor.log_panel.log, log)
        self.assertEqual(editor.log_panel.ecu_profile_id, '361830')
        editor.log_panel.log = None

    def test_logging_dialog_uses_only_current_ecu_saved_selections(self):
        catalog = live_logging.load_parameters(gui.bundled_signal_catalog_path().parent / 'live_parameters.json')
        diesel = ECUSettings(self.base, '361830')
        petrol = ECUSettings(self.base, 'MS43X001')
        d = live_logging.LoggingDialog(catalog, settings=diesel)
        self.widgets.append(d)
        custom = dict(d.presets['Balanced engine (14)'], name='My diesel channels')
        d.saved[custom['name']] = custom
        d.persist()
        other = live_logging.LoggingDialog(catalog, settings=petrol)
        self.widgets.append(other)
        self.assertNotIn('My diesel channels', other.saved)
        again = live_logging.LoggingDialog(catalog, settings=diesel)
        self.widgets.append(again)
        self.assertIn('My diesel channels', again.saved)
        diesel.setValue('testo_mcp/job', 'DIESEL_JOB')
        petrol.setValue('testo_mcp/job', 'PETROL_JOB')
        dialog = gui.TestOLiveDialog(settings=petrol)
        self.widgets.append(dialog)
        self.assertEqual(dialog.job.currentText(), 'PETROL_JOB')
        self.assertEqual(diesel.value('testo_mcp/job'), 'DIESEL_JOB')


if __name__ == '__main__':
    unittest.main()
