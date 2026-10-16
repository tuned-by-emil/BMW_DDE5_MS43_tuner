import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import tempfile
import unittest
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
from PySide6.QtWidgets import QApplication, QDialogButtonBox
from bmw_bin_editor.core import CalibrationDefinition, CalibrationDocument, EditorError
from bmw_bin_editor.ms43_drift import build_preview, apply_preview, RULES, EXPECTED
from bmw_bin_editor.ms43_drift_gui import DriftPreviewDialog

APP = QApplication.instance() or QApplication([])
ROOT = Path(__file__).resolve().parents[1]


class DriftTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'test.bin'

    def tearDown(self):
        self.tmp.cleanup()

    def document(self, partial=False):
        # Only synthetic bytes and the preset's six explicit layout contracts;
        # no supplied XDF or firmware is redistributed as a test fixture.
        definitions = []
        for symbol, (address, rows, cols, bits, signed, equation, order) in EXPECTED.items():
            definitions.append(CalibrationDefinition(symbol, 'Synthetic fixture', 'Synthetic',
                address - (0x70000 if partial else 0), rows, cols,
                z_element_bits=bits, z_signed=signed, z_equation=equation, storage_order=order))
        x = SimpleNamespace(title='MS43X001', default_lsb_first=True, definitions=definitions)
        self.path.write_bytes(bytes([17]) * (0x10000 if partial else 0x80000))
        return CalibrationDocument(x, self.path)

    def test_preview_is_read_only_and_apply_is_exactly_one_undo(self):
        doc = self.document()
        original = bytes(doc.data)
        p = build_preview(doc)
        self.assertEqual(bytes(doc.data), original)
        self.assertFalse(doc.undo_stack)
        self.assertEqual(len(p.cells), 205)
        self.assertEqual({c.symbol for c in p.cells}, set(RULES))
        self.assertTrue(all(c.after == b'\x80' for c in p.cells if 'iga_aj' in c.symbol))
        self.assertTrue(apply_preview(doc, p))
        expected = bytearray(original)
        for c in p.cells:
            expected[c.address:c.address + len(c.after)] = c.after
        self.assertEqual(doc.data, expected)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(len(doc.undo_stack), 1)
        self.assertFalse(build_preview(doc).changed)
        self.assertFalse(apply_preview(doc, build_preview(doc)))
        self.assertEqual(len(doc.undo_stack), 1)
        doc.undo()
        self.assertEqual(bytes(doc.data), original)
        self.assertFalse(doc.dirty)
        doc.redo()
        self.assertEqual(doc.data, expected)

    def test_full_partial_have_same_calibration_changes(self):
        full = build_preview(self.document())
        partial = build_preview(self.document(True))
        self.assertEqual([(c.address - 0x70000, c.after) for c in full.cells],
                         [(c.address, c.after) for c in partial.cells])

    def test_stale_bytes_definitions_and_tampered_preview_are_rejected(self):
        doc = self.document()
        p = build_preview(doc)
        doc.data[0] ^= 1
        with self.assertRaises(EditorError): apply_preview(doc, p)
        doc.data[0] ^= 1
        with self.assertRaises(EditorError): apply_preview(doc, replace(p, cells=p.cells[:-1]))
        d = next(d for d in doc.xdf.definitions if d.title in RULES)
        d.z_address += 1
        with self.assertRaises(EditorError): apply_preview(doc, p)
        self.assertFalse(doc.undo_stack)

    def test_missing_duplicate_unverified_and_wrong_ecu_rejected(self):
        for problem in ('missing', 'duplicate', 'unverified', 'other'):
            doc = self.document()
            d = next(d for d in doc.xdf.definitions if d.title in RULES)
            if problem == 'missing': doc.xdf.definitions.remove(d)
            elif problem == 'duplicate': doc.xdf.definitions.append(d)
            elif problem == 'unverified': d.description = 'UNVERIFIED semantic hypothesis test'
            else: doc.xdf.title = 'EDC16C31'
            with self.assertRaises(EditorError): build_preview(doc)

    def test_dialog_cancel_never_edits_and_noop_disables_apply(self):
        doc = self.document()
        p = build_preview(doc)
        dialog = DriftPreviewDialog(p)
        self.assertEqual(dialog.tree.topLevelItemCount(), 6)
        self.assertEqual(sum(dialog.tree.topLevelItem(i).childCount()-1 for i in range(6)), 205)
        dialog.reject()
        self.assertEqual(bytes(doc.data), doc.original)
        self.assertFalse(doc.undo_stack)
        apply_preview(doc, p)
        noop = DriftPreviewDialog(build_preview(doc))
        self.assertFalse(noop.findChild(QDialogButtonBox).button(QDialogButtonBox.Apply).isEnabled())
        dialog.close(); noop.close()
