
import unittest as _optional_unittest
from pathlib import Path as _OptionalPath
if not (_OptionalPath(__file__).resolve().parents[1] / 'definitions/EDC16C31 361830.xdf').is_file():
    raise _optional_unittest.SkipTest('Optional local calibration resource not installed: definitions/EDC16C31 361830.xdf')

import os, sys, tempfile, unittest
from pathlib import Path
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
from PySide6.QtWidgets import QApplication
from bmw_bin_editor.core import XDFDatabase, CalibrationDocument
from bmw_bin_editor.log_delta import plan_delta, LogDeltaDialog

APP = QApplication.instance() or QApplication([])

class Tests(unittest.TestCase):
    def test_preview_apply_and_undo(self):
        root = Path(__file__).resolve().parents[1]
        xdf = XDFDatabase(root/'definitions/EDC16C31 361830.xdf')
        d = next(d for d in xdf.tables if d.z_address == 0xF7F44)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td)/'test.bin'; path.write_bytes(bytes(0x100000))
            doc = CalibrationDocument(xdf, path)
            cells = [(0, c) for c in range(5)]
            doc.set_cells(d, {cell: 50 for cell in cells}, 'setup')
            before = bytes(doc.data)
            delta = np.full((d.rows,d.cols), np.nan); hits = np.zeros_like(delta)
            delta[0,:5] = [10, -8, np.nan, 200, 5]; hits[0,:5] = [10,10,0,10,1]
            stats = dict(log_diff=delta, hits=hits)
            updates, _ = plan_delta(doc,d,stats,cells,50,2)
            self.assertEqual(set(updates), {(0,0),(0,1)})
            self.assertAlmostEqual(updates[0,0],55,places=1)
            self.assertAlmostEqual(updates[0,1],46,places=1)
            dlg = LogDeltaDialog(doc,d,stats,cells)
            dlg.percent.setValue(50); dlg.minimum.setValue(2)
            self.assertEqual(dlg.updates, updates)
            self.assertEqual(bytes(doc.data), before)
            doc.set_cells(d, dlg.updates, 'Apply log delta')
            changed = bytes(doc.data)
            allowed = {doc.cell_address(d,r,c)+i for r,c in updates for i in range(d.z_width)}
            self.assertTrue(all(i in allowed for i,(a,b) in enumerate(zip(before,changed)) if a != b))
            doc.undo(); self.assertEqual(bytes(doc.data),before)
            doc.redo(); self.assertEqual(bytes(doc.data),changed)
            self.assertEqual(path.read_bytes(),bytes(0x100000))
            dlg.percent.setValue(0); self.assertFalse(dlg.apply.isEnabled())
            dlg.close()

            from bmw_bin_editor import gui
            panel = gui.MapEditor()
            panel.set_document(doc); panel.set_definition(d)
            panel.engine.selection.set_rect(0,0,0,4)
            panel.log_stats = dict(stats, samples=hits, log_avg=delta+50,
                                   std=hits*0, min=delta+50, max=delta+50)
            panel._recompute_log_stats = lambda *a: None
            panel.table_data_combo.setCurrentIndex(panel.table_data_combo.findData('log_diff'))
            old_exec = gui.LogDeltaDialog.exec
            def accept_preview(dialog):
                dialog.percent.setValue(50); dialog.minimum.setValue(2)
                return gui.QDialog.Accepted
            gui.LogDeltaDialog.exec = accept_preview
            before_action = bytes(doc.data)
            try:
                panel.apply_log_delta_dialog()
                self.assertNotEqual(bytes(doc.data), before_action)
                self.assertEqual(panel.table_display_mode, 'calibration')
                doc.undo(); self.assertEqual(bytes(doc.data), before_action)
            finally:
                gui.LogDeltaDialog.exec = old_exec
                panel.close()

if __name__ == '__main__': unittest.main()
