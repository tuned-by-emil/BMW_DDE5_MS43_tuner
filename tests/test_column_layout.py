"""Non-square column storage must keep displayed X/Y axes and byte writes aligned."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dataclasses import replace
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bmw_bin_editor.core import AxisDefinition, CalibrationDefinition, CalibrationDocument, EditorError, XDFDatabase
from bmw_bin_editor.xdf_edit import XDFWorkspace


def definition(**changes):
    return replace(CalibrationDefinition(
        "Column table", "", "Test", 16, 2, 3,
        z_equation="X*2-1", decimal_places=0,
        x_axis=AxisDefinition(None, 3, units="rpm", labels=["1000", "2000", "3000"]),
        y_axis=AxisDefinition(None, 2, units="mg", labels=["200", "400"]),
        storage_order="column_major"), **changes)


class ColumnLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def document(self, *, endian=">", bits=16, signed=False):
        workspace = XDFWorkspace(bin_size=256)
        workspace.set_metadata("Layout test", "", "", endian == "<")
        source = workspace.add(definition(z_element_bits=bits, z_signed=signed))
        image = bytearray(256)
        code = {(8, False): "B", (8, True): "b", (16, False): "H", (16, True): "h", (32, False): "I"}[(bits, signed)]
        words = [1, 2, 10, 20, 50, 60]
        image[16:16 + bits // 8 * len(words)] = struct.pack(endian + code * len(words), *words)
        path = self.folder / "example.bin"
        path.write_bytes(image)
        return CalibrationDocument(workspace.database, path), source, workspace

    def test_non_square_read_write_overlay_original_cache_and_undo(self):
        for endian, bits, signed in ((">", 16, False), ("<", 16, True), (">", 8, True), ("<", 32, False)):
            with self.subTest(endian=endian, bits=bits, signed=signed):
                document, source, _ = self.document(endian=endian, bits=bits, signed=signed)
                self.assertEqual(document.map_raw(source), [[1, 10, 50], [2, 20, 60]])
                expected = [[1, 19, 99], [3, 39, 119]]
                self.assertEqual(document.map_values(source), expected)
                original = document.map_values(source, original=True)
                self.assertEqual(original, expected)
                self.assertIs(document.map_values(source, original=True), original)
                row_source = replace(source, storage_order="row_major")
                self.assertEqual(document.map_values(row_source, original=True), [[1, 3, 19], [39, 99, 119]])
                self.assertEqual(document.axis_values(source.x_axis), [1000, 2000, 3000])
                self.assertEqual(document.axis_values(source.y_axis), [200, 400])
                for row in range(2):
                    for col in range(3):
                        self.assertEqual(document.cell_value(source, row, col), expected[row][col])
                        self.assertEqual(document.cell_address(source, row, col), 16 + (col * 2 + row) * bits // 8)
                before = bytes(document.data)
                self.assertTrue(document.set_cells(source, {(0, 1): 85, (1, 2): 87}, "Column edit"))
                self.assertEqual(document.map_values(source), [[1, 85, 99], [3, 39, 87]])
                width = bits // 8
                changed = {i for i, (a, b) in enumerate(zip(before, document.data)) if a != b}
                allowed = set(range(16 + 2 * width, 16 + 3 * width)) | set(range(16 + 5 * width, 16 + 6 * width))
                self.assertTrue(changed <= allowed)
                self.assertEqual(document.map_values(source, original=True), expected)
                self.assertEqual(document.map_values_from_source(source, before), expected)
                self.assertEqual(document.map_values_from_source(source, document.data), [[1, 85, 99], [3, 39, 87]])
                self.assertEqual(document.undo(), "Column edit")
                self.assertEqual(bytes(document.data), before)
                self.assertEqual(document.redo(), "Column edit")
                self.assertEqual(document.cell_value(source, 0, 1), 85)

    def test_linked_edits_between_column_and_row_storage_keep_operating_points(self):
        document, source, workspace = self.document()
        target = workspace.add(replace(source, title="Row target", z_address=128, storage_order="row_major"))
        document.xdf = workspace.database
        document.data[128:140] = struct.pack(">6H", 1, 10, 50, 2, 20, 60)
        changed, warnings = document.set_cells_linked(source, {(1, 1): 101}, [target], "absolute", "Linked")
        self.assertTrue(changed)
        self.assertEqual(warnings, [])
        self.assertEqual(document.map_values(source)[1][1], 101)
        self.assertEqual(document.map_values(target)[1][1], 101)
        self.assertEqual(document.cell_address(source, 1, 1), 22)
        self.assertEqual(document.cell_address(target, 1, 1), 136)

    def test_xdf_roundtrip_and_storage_dimension_changes(self):
        _, source, workspace = self.document()
        embedded = workspace.database.root.find("./XDFTABLE/XDFAXIS[@id='z']/EMBEDDEDDATA")
        self.assertEqual(embedded.get("mmedrowstridebits"), "16")
        self.assertEqual(embedded.get("mmedcolstridebits"), "32")
        saved = workspace.save_as(self.folder / "column.xdf")
        loaded = XDFDatabase(saved.path).tables[0]
        self.assertEqual(loaded.storage_order, "column_major")
        self.assertEqual((loaded.rows, loaded.cols), (2, 3))
        self.assertEqual((loaded.x_axis.units, loaded.y_axis.units), ("rpm", "mg"))
        renamed = workspace.update(source.unique_id, replace(loaded, title="Renamed"))
        self.assertEqual(renamed.storage_order, "column_major")
        resized = workspace.update(renamed.unique_id, replace(renamed, rows=3, cols=2, z_element_bits=8,
            x_axis=AxisDefinition(None, 2), y_axis=AxisDefinition(None, 3)))
        embedded = workspace.database.root.find("./XDFTABLE/XDFAXIS[@id='z']/EMBEDDEDDATA")
        self.assertEqual((embedded.get("mmedrowstridebits"), embedded.get("mmedcolstridebits")), ("8", "24"))
        row = workspace.update(resized.unique_id, replace(resized, storage_order="row_major"))
        self.assertEqual(row.storage_order, "row_major")
        self.assertEqual((embedded.get("mmedrowstridebits"), embedded.get("mmedcolstridebits")), ("8", "24"))
        row_embedded = workspace.database.root.find("./XDFTABLE/XDFAXIS[@id='z']/EMBEDDEDDATA")
        self.assertEqual((row_embedded.get("mmedrowstridebits"), row_embedded.get("mmedcolstridebits")), ("16", "8"))

    def test_zero_absent_and_explicit_row_strides_retain_existing_layout(self):
        workspace = XDFWorkspace(bin_size=256)
        workspace.add(definition(storage_order="row_major"))
        for attributes in ({}, {"mmedrowstridebits": "0", "mmedcolstridebits": "0"},
                           {"mmedrowstridebits": "48", "mmedcolstridebits": "16"},
                           {"mmedrowstridebits": "48"}, {"mmedcolstridebits": "16"}):
            tree = ET.ElementTree(ET.fromstring(ET.tostring(workspace.database.root)))
            tree.getroot().find("./XDFTABLE/XDFAXIS[@id='z']/EMBEDDEDDATA").attrib.update(attributes)
            self.assertEqual(XDFDatabase.from_tree(tree).tables[0].storage_order, "row_major")

    def test_unsupported_stride_or_storage_rejected_without_writing(self):
        document, source, workspace = self.document()
        for strides in ((64, 16), (16, 48), (-16, 32), (16, 0), (0, 32), ("invalid", 32)):
            tree = ET.ElementTree(ET.fromstring(ET.tostring(workspace.database.root)))
            embedded = tree.getroot().find("./XDFTABLE/XDFAXIS[@id='z']/EMBEDDEDDATA")
            embedded.set("mmedrowstridebits", str(strides[0]))
            embedded.set("mmedcolstridebits", str(strides[1]))
            with self.assertRaisesRegex(EditorError, "storage strides"):
                XDFDatabase.from_tree(tree)
        invalid = replace(source, storage_order="interleaved")
        before = bytes(document.data)
        with self.assertRaisesRegex(EditorError, "storage order"):
            document.set_cells(invalid, {(0, 0): 50}, "Bad layout")
        self.assertEqual(bytes(document.data), before)
        with self.assertRaisesRegex(EditorError, "storage order"):
            workspace.update(source.unique_id, invalid)

    def test_definition_preview_and_form_preserve_horizontal_x_axis(self):
        from PySide6.QtWidgets import QApplication
        from bmw_bin_editor.xdf_gui import DefinitionForm, DefinitionPreview
        app = QApplication.instance() or QApplication([])
        document, source, _ = self.document()
        preview = DefinitionPreview(bytes(document.data))
        preview.set_definition(source, ">")
        self.assertIs(preview.definition, source)
        self.assertEqual(preview.table.rowCount(), 2)
        self.assertEqual(preview.table.columnCount(), 3)
        self.assertEqual(preview.table.horizontalHeaderItem(2).text(), "3000")
        self.assertEqual(preview.table.verticalHeaderItem(1).text(), "400")
        self.assertEqual([[int(preview.table.item(r, c).text()) for c in range(3)] for r in range(2)], [[1, 19, 99], [3, 39, 119]])
        form = DefinitionForm(["Test"])
        form.load(source)
        form.name.setText("Title changed")
        self.assertEqual(form.definition().storage_order, "column_major")
        preview.close()
        form.close()
        app.processEvents()


if __name__ == "__main__":
    unittest.main()
