
import unittest as _optional_unittest
from pathlib import Path as _OptionalPath
if not (_OptionalPath(__file__).resolve().parents[1] / 'definitions/ms43x/Siemens_MS43_MS43X001_512K.xdf').is_file():
    raise _optional_unittest.SkipTest('Optional local calibration resource not installed: definitions/ms43x/Siemens_MS43_MS43X001_512K.xdf')

import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication
from bmw_bin_editor import gui
from bmw_bin_editor.core import XDFDatabase, CalibrationDocument
from bmw_bin_editor.ms43_navigation import readable_name, drift_shortcuts

APP=QApplication.instance() or QApplication([])
ROOT=Path(__file__).resolve().parents[1]

class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.folder=Path(self.tmp.name)
        self.settings=QSettings(str(self.folder/'prefs.ini'), QSettings.IniFormat)
        self.settings.setValue('settings/migrated',True)
        self.patch=patch.object(gui,'QSettings',lambda *a:self.settings)
        self.patch.start()
        self.window=gui.MainWindow()
        x=XDFDatabase(ROOT/'definitions/ms43x/Siemens_MS43_MS43X001_512K.xdf')
        source=self.folder/'full.bin';source.write_bytes(bytes(0x80000))
        self.doc=CalibrationDocument(x,source)
        self.window.xdf=x;self.window.document=self.doc
        selector=next(d for d in x.definitions if d.title=='ip_nr_ip_ve__vo')
        for i,angle in enumerate([3,9,15,24,36,48,60,67.875]):
            self.doc.set_axis_value(selector.y_axis,i,angle,'test points')
        self.doc.set_cells(selector,{(i,0):i+1 for i in range(8)},'test selector')
        self.window.editor.set_document(self.doc)
        self.window.populate_tree()

    def tearDown(self):
        self.doc.dirty=False
        self.window.close()
        self.patch.stop()
        self.tmp.cleanup()

    def group(self,*names):
        current=self.window.tree.invisibleRootItem()
        for name in names:
            current=next(current.child(i) for i in range(current.childCount()) if current.child(i).text(0)==name)
        return current

    def test_fuel_ve_names_and_displacement_shortcut(self):
        fuel=self.group('Fuel')
        ve=[fuel.child(i) for i in range(fuel.childCount()) if fuel.child(i).text(0).startswith('VE Table ')]
        self.assertEqual(len(ve),8)
        self.assertEqual(ve[0].text(0),'VE Table 1 — 3° overlap point')
        engine=self.group('Drift setup','Engine setup')
        self.assertEqual(engine.child(0).text(0),'Engine displacement (cc)')
        self.window.open_tree_item(engine.child(0),0)
        self.assertEqual(self.window.editor.definition.title,'c_eng_disp')
        self.assertEqual(self.window.editor.definition.z_units,'cc')
        self.assertEqual(self.window.editor.info.title.text(),'<b>Engine displacement (cc)</b>')

    def test_nested_search_finds_original_symbol_and_shortcut_uses_same_map(self):
        self.window.filter_tree('ip_tps_sp_pvs__pvs__n')
        drift=self.group('Drift setup','Pedal response')
        self.assertFalse(drift.isHidden())
        self.assertFalse(drift.child(0).isHidden())
        self.window.open_tree_item(drift.child(0),0)
        self.assertEqual(self.window.editor.definition.title,'ip_tps_sp_pvs__pvs__n')
        self.assertEqual(drift.child(0).text(0),'Pedal-to-throttle response map')
        self.window.filter_tree('anti-jerk')
        anti=self.group('Drift setup','Anti-jerk damping')
        self.assertTrue(all(not anti.child(i).isHidden() for i in range(anti.childCount())))

    def test_selector_change_updates_labels_and_safety_diagnostics_not_shortcut(self):
        selector=next(d for d in self.doc.xdf.definitions if d.title=='ip_nr_ip_ve__vo')
        self.doc.set_cells(selector,{(0,0):8},'remap')
        self.window._document_changed()
        labels={number:item.text(0) for item,d,number in self.window._ms43_ve_items}
        self.assertIn('no exact selector point',labels[1])
        self.assertIn('3°, 67.875°',labels[8])
        diagnostic=next(d for d in self.doc.xdf.definitions if d.title=='c_pvs_av_bls_not_plaus')
        self.assertIsNone(drift_shortcuts(diagnostic))

    def test_help_reaches_shortcuts_favorites_and_information_panel(self):
        engine=self.group('Drift setup','Engine setup').child(0)
        self.assertIn('Total engine capacity in cc',engine.toolTip(0))
        self.assertIn('<table width="480">',engine.toolTip(0))
        self.window.open_tree_item(engine,0)
        self.assertIn('0.977 cc',self.window.editor.info.desc.text())
        self.window.favorites={gui.definition_key(self.window.editor.definition)}
        self.window.populate_favorites()
        self.assertIn('Total engine capacity in cc',self.window.favorite_tree.topLevelItem(0).toolTip(0))
        self.assertIn('100% is VE = 1.0',self.window._ms43_ve_items[0][0].toolTip(0))

if __name__=='__main__': unittest.main()
