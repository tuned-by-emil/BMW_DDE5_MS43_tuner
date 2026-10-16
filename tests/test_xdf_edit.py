"""Definition authoring must preserve XML and remain separate from BIN edits."""

import unittest as _optional_unittest
from pathlib import Path as _OptionalPath
if not (_OptionalPath(__file__).resolve().parents[1] / 'definitions/EDC16C31_361830_v1_22_R005_REVIEW.xdf').is_file():
    raise _optional_unittest.SkipTest('Optional local calibration resource not installed: definitions/EDC16C31_361830_v1_22_R005_REVIEW.xdf')

import copy
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bmw_bin_editor.core import AxisDefinition, CalibrationDefinition, CalibrationDocument, EditorError, XDFDatabase
from bmw_bin_editor.xdf_edit import XDFWorkspace

ROOT = Path(__file__).resolve().parents[1]
BUNDLED = ROOT / "definitions" / "EDC16C31_361830_v1_22_R005_REVIEW.xdf"


def xml(database):
    return ET.tostring(database.root, encoding="utf-8")


def table(**changes):
    definition = CalibrationDefinition("Example", "Notes", "Fuel", 32, 2, 3,
        z_element_bits=16, z_equation="X * 0.1", z_units="mg", decimal_places=1,
        x_axis=AxisDefinition(64, 3, units="rpm", decimal_places=0),
        y_axis=AxisDefinition(None, 2, labels=["Low", "High"], decimal_places=0))
    return replace(definition, **changes)


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_empty_header_and_transactional_metadata(self):
        workspace = XDFWorkspace(bin_size=256)
        self.assertFalse(workspace.dirty)
        self.assertFalse(workspace.database.default_lsb_first)
        self.assertEqual(workspace.database.root.findtext("./XDFHEADER/author"), "Emil Larsson")
        workspace.set_metadata("My ECU", "Emil Larsson", "Custom definitions", True)
        self.assertEqual(workspace.database.title, "My ECU")
        self.assertTrue(workspace.database.default_lsb_first)
        self.assertTrue(workspace.dirty)
        before = xml(workspace.database)
        with self.assertRaises(EditorError):
            workspace.set_metadata("", "Author", "Bad", False)
        self.assertEqual(xml(workspace.database), before)

    def test_real_bundled_round_trip_preserves_every_parsed_node(self):
        source = XDFDatabase(BUNDLED)
        original = xml(source)
        workspace = XDFWorkspace(source, bin_size=0x100000)
        self.assertFalse(workspace.dirty)
        self.assertIsNot(workspace.database.tree, source.tree)
        target = self.folder / "review.xdf"
        saved = workspace.save_as(target)
        self.assertEqual(saved.path, target)
        self.assertIs(saved, workspace.database)
        self.assertEqual(xml(saved), original)
        self.assertEqual(xml(source), original)
        self.assertFalse(workspace.dirty)
        self.assertEqual(len(saved.definitions), 249)

    def test_file_load_and_save_preserve_comments_and_processing_instructions(self):
        path = self.folder / 'annotated.xdf'
        path.write_text('<XDFFORMAT version="1.80"><!--Author note--><?review approved?>'
                        '<XDFHEADER><deftitle>Annotated</deftitle></XDFHEADER></XDFFORMAT>', encoding='utf-8')
        workspace = XDFWorkspace(XDFDatabase(path))
        saved = workspace.save_as(self.folder / 'roundtrip.xdf')
        serialized = xml(saved)
        self.assertIn(b'<!--Author note-->', serialized)
        self.assertIn(b'<?review approved?>', serialized)

    def test_xdf_save_rejects_bin_destination_without_overwriting(self):
        target = self.folder / 'calibration.bin'
        target.write_bytes(b'Original BIN bytes')
        workspace = XDFWorkspace()
        with self.assertRaises(EditorError):
            workspace.save_as(target)
        self.assertEqual(target.read_bytes(), b'Original BIN bytes')

    def test_add_edit_constant_duplicate_and_delete(self):
        workspace = XDFWorkspace(bin_size=256)
        added = workspace.add(table(unique_id="0xFFFF"))
        self.assertNotEqual(added.unique_id, "0xFFFF")
        self.assertEqual(added.category, "Fuel")
        changed = replace(added, title="Updated", description="New notes", category="Boost",
                          z_address=100, rows=3, cols=2, z_element_bits=8, z_signed=True,
                          z_equation="(X-10)*2", z_units="hPa", decimal_places=2,
                          x_axis=AxisDefinition(None, 2, labels=["Idle", "Full"]),
                          y_axis=AxisDefinition(128, 3, element_bits=8, signed=True, equation="X*10", units="rpm"))
        updated = workspace.update(added.unique_id, changed)
        self.assertEqual(updated.unique_id, added.unique_id)
        for field in ("title", "description", "category", "z_address", "rows", "cols", "z_element_bits",
                      "z_signed", "z_equation", "z_units", "decimal_places", "x_axis", "y_axis"):
            self.assertEqual(getattr(updated, field), getattr(changed, field), field)
        constant = workspace.add(CalibrationDefinition("Limit", "", "Constants", 200, 1, 1,
            z_element_bits=32, z_signed=True, z_equation="X/100", kind="constant"))
        self.assertEqual(constant.kind, "constant")
        constant = workspace.update(constant.unique_id, replace(constant, z_address=204, z_signed=False, z_units="Nm"))
        self.assertEqual((constant.z_address, constant.z_signed, constant.z_units), (204, False, "Nm"))
        duplicate = workspace.duplicate(updated.unique_id)
        self.assertNotEqual(duplicate.unique_id, updated.unique_id)
        self.assertEqual(duplicate.title, "Updated (copy)")
        workspace.remove(duplicate.unique_id)
        self.assertEqual(len(workspace.database.definitions), 2)
        saved = workspace.save_as(self.folder / "custom.xdf")
        self.assertEqual(len(saved.tables), 1)
        self.assertEqual(len(saved.constants), 1)
        self.assertFalse(workspace.dirty)

    def test_definition_edits_do_not_mutate_source_xdf_or_bin(self):
        source = XDFDatabase(BUNDLED)
        path = self.folder / "synthetic.bin"
        path.write_bytes(bytes(0x100000))
        document = CalibrationDocument(source, path)
        document.apply_bytes("BIN edit", [(0, b"Q")])
        original_xml, original_data, original_undo = xml(source), bytes(document.data), list(document.undo_stack)
        workspace = XDFWorkspace(source, len(document.data))
        before = workspace.database.tables[0]
        workspace.update(before.unique_id, replace(before, title="Renamed"))
        workspace.save_as(self.folder / "definition.xdf")
        self.assertEqual(xml(source), original_xml)
        self.assertEqual(bytes(document.data), original_data)
        self.assertEqual(document.undo_stack, original_undo)
        self.assertTrue(document.dirty)
        self.assertEqual(path.read_bytes(), bytes(0x100000))

    def test_unknown_xml_and_other_category_memberships_survive_title_edit(self):
        source = XDFDatabase(BUNDLED)
        item = source.root.find("XDFTABLE")
        item.set("vendor-attribute", "keep")
        vendor = ET.SubElement(item, "VENDOR_DATA", revision="42")
        vendor.text = "custom payload"
        ET.SubElement(item, "CATEGORYMEM", index="1", category="4", custom="keep")
        math_node = item.find("./XDFAXIS[@id='z']/MATH")
        math_node.set("vendor-expression", "keep")
        source = XDFDatabase.from_tree(source.tree, source.path)
        workspace = XDFWorkspace(source, 0x100000)
        before = workspace.database.tables[0]
        before_item = copy.deepcopy(workspace.database.root.find("XDFTABLE"))
        workspace.update(before.unique_id, replace(before, title="New title", description="New description"))
        after_item = workspace.database.root.find("XDFTABLE")
        before_item.find("title").text = "New title"
        before_item.find("description").text = "New description"
        self.assertEqual(ET.tostring(after_item), ET.tostring(before_item))

    def test_category_change_preserves_parent_membership_and_metadata(self):
        workspace = XDFWorkspace(XDFDatabase(BUNDLED), 0x100000)
        before = next(d for d in workspace.database.tables if len(d.categories) > 1)
        item = next(node for node in workspace.database.root if node.get("uniqueid") == before.unique_id)
        parent = item.findall("CATEGORYMEM")[0]
        parent.set("vendor", "keep")
        original_parent = dict(parent.attrib)
        updated = workspace.update(before.unique_id, replace(before, category="New category"))
        item = next(node for node in workspace.database.root if node.get("uniqueid") == before.unique_id)
        self.assertEqual(item.findall("CATEGORYMEM")[0].attrib, original_parent)
        self.assertEqual(updated.category, "New category")
        self.assertEqual(updated.categories[0], before.categories[0])
        # Switching to an existing parent also produces that requested primary.
        updated = workspace.update(before.unique_id, replace(updated, category=before.categories[0]))
        self.assertEqual(updated.category, before.categories[0])

    def test_unrelated_unsupported_fields_can_be_kept_untouched(self):
        source = XDFDatabase(BUNDLED)
        item = source.root.find("XDFTABLE")
        embedded = item.find("./XDFAXIS[@id='z']/EMBEDDEDDATA")
        embedded.set("mmedelementsizebits", "24")
        item.find("./XDFAXIS[@id='z']/MATH").set("equation", "LOOKUP(X)")
        source = XDFDatabase.from_tree(source.tree, source.path)
        workspace = XDFWorkspace(source, 0x100000)
        before = workspace.database.tables[0]
        updated = workspace.update(before.unique_id, replace(before, title="Keep unusual storage"))
        self.assertEqual((updated.z_element_bits, updated.z_equation), (24, "LOOKUP(X)"))
        workspace.save_as(self.folder / "preserved.xdf")

    def test_missing_and_duplicate_ids_are_normalized_only_on_clone(self):
        source = XDFDatabase(BUNDLED)
        items = source.root.findall("XDFTABLE")
        items[0].set("uniqueid", "0x5")
        items[1].set("uniqueid", "5")
        items[2].attrib.pop("uniqueid")
        original = xml(source)
        workspace = XDFWorkspace(source)
        ids = [d.unique_id for d in workspace.database.definitions]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(ids))
        self.assertEqual(ids[0], "0x5")
        self.assertNotEqual(ids[1], "5")
        self.assertEqual(xml(source), original)
        self.assertTrue(workspace.dirty)
        stable = list(ids)
        workspace.set_metadata(workspace.database.title, "Emil", "New header", False)
        self.assertEqual([d.unique_id for d in workspace.database.definitions], stable)

    def test_invalid_storage_or_axis_changes_roll_back(self):
        workspace = XDFWorkspace(bin_size=256)
        added = workspace.add(table())
        snapshot = xml(workspace.database)
        bad_definitions = [replace(added, z_address=-1), replace(added, z_address=250),
            replace(added, z_element_bits=24), replace(added, rows=0), replace(added, cols=-1),
            replace(added, x_axis=replace(added.x_axis, address=252)),
            replace(added, x_axis=replace(added.x_axis, count=2)),
            replace(added, z_equation="__import__('os')"), replace(added, z_equation="X/0"),
            replace(added, decimal_places=9), replace(added, kind="constant")]
        for definition in bad_definitions:
            with self.subTest(definition=definition):
                database = workspace.database
                with self.assertRaises(EditorError):
                    workspace.update(added.unique_id, definition)
                self.assertIs(workspace.database, database)
                self.assertEqual(xml(workspace.database), snapshot)
        with self.assertRaises(EditorError):
            workspace.add(table(z_address=256))
        self.assertEqual(xml(workspace.database), snapshot)

    def test_static_labels_counts_and_uniqueness(self):
        workspace = XDFWorkspace(bin_size=256)
        for labels in (["one"], ["one", "one"], ["one", " one "], ["", "two"]):
            with self.subTest(labels=labels), self.assertRaises(EditorError):
                workspace.add(table(y_axis=AxisDefinition(None, 2, labels=labels)))
        added = workspace.add(table(x_axis=AxisDefinition(None, 3, labels=["800", "2000", "4000"])))
        self.assertIsNone(added.x_axis.address)
        self.assertEqual(added.x_axis.labels, ["800", "2000", "4000"])
        self.assertEqual(added.y_axis.labels, ["Low", "High"])
        workspace.update(added.unique_id, replace(added, x_axis=None, y_axis=None))
        saved = workspace.save_as(self.folder / "index.xdf")
        self.assertIsNone(saved.tables[0].x_axis)
        self.assertIsNone(saved.tables[0].y_axis)

    def test_supported_non_affine_equations_remain_read_only(self):
        workspace = XDFWorkspace()
        added = workspace.add(table(z_equation="X^2"))
        self.assertEqual(added.z_equation, "X^2")

    def test_update_compares_against_xml_after_caller_edits_returned_dataclass(self):
        workspace = XDFWorkspace(bin_size=256)
        added = workspace.add(table())
        added.title = "Edited directly"
        added.y_axis.labels = ["Small", "Large"]
        updated = workspace.update(added.unique_id, added)
        self.assertEqual(updated.title, "Edited directly")
        self.assertEqual(updated.y_axis.labels, ["Small", "Large"])
        saved = workspace.save_as(self.folder / "in-place.xdf")
        self.assertEqual(saved.tables[0].title, "Edited directly")

    def test_category_membership_ties_follow_existing_document_order(self):
        source = XDFDatabase(BUNDLED)
        first = source.root.find("XDFTABLE")
        ET.SubElement(first, "CATEGORYMEM", index="0", category="4", vendor="keep")
        source = XDFDatabase.from_tree(source.tree, source.path)
        workspace = XDFWorkspace(source, 0x100000)
        before = workspace.database.tables[0]
        self.assertEqual(before.category, "Fuel")
        updated = workspace.update(before.unique_id, replace(before, category="Boost"))
        self.assertEqual(updated.category, "Boost")
        first = workspace.database.root.find("XDFTABLE")
        self.assertEqual(first.findall("CATEGORYMEM")[-1].get("vendor"), "keep")

    def test_duplicate_preserves_unmodeled_xml(self):
        workspace = XDFWorkspace(XDFDatabase(BUNDLED))
        source = workspace.database.root.find("XDFTABLE")
        ET.SubElement(source, "CUSTOM", key="preserve").text = "Payload"
        duplicate = workspace.duplicate(source.get("uniqueid"))
        cloned = next(node for node in workspace.database.root if node.get("uniqueid") == duplicate.unique_id)
        expected = copy.deepcopy(source)
        expected.set("uniqueid", duplicate.unique_id)
        expected.find("title").text = duplicate.title
        self.assertEqual(ET.tostring(cloned), ET.tostring(expected))

    def test_save_failures_preserve_existing_file_and_workspace(self):
        workspace = XDFWorkspace()
        workspace.add(table())
        target = self.folder / "existing.xdf"
        old = b"previous file"
        target.write_bytes(old)
        snapshot, database = xml(workspace.database), workspace.database
        for patch_name, failure in (("os.fsync", OSError("disk full")),
                                    ("os.replace", PermissionError("in use")),
                                    ("ET.parse", ET.ParseError("invalid XML"))):
            with self.subTest(patch=patch_name), patch("bmw_bin_editor.xdf_edit." + patch_name, side_effect=failure):
                with self.assertRaises((OSError, EditorError)):
                    workspace.save_as(target)
            self.assertEqual(target.read_bytes(), old)
            self.assertIs(workspace.database, database)
            self.assertEqual(xml(workspace.database), snapshot)
            self.assertTrue(workspace.dirty)
            self.assertEqual(list(self.folder.iterdir()), [target])

    def test_saved_workspace_and_input_objects_are_independent(self):
        workspace = XDFWorkspace()
        definition = table()
        added = workspace.add(definition)
        definition.title = "Caller change"
        definition.y_axis.labels[0] = "Caller label change"
        self.assertEqual(added.title, "Example")
        self.assertEqual(added.y_axis.labels[0], "Low")
        saved = workspace.save_as(self.folder / "saved.xdf")
        workspace.update(added.unique_id, replace(added, title="Changed again"))
        self.assertTrue(workspace.dirty)
        self.assertEqual(saved.tables[0].title, "Example")
        self.assertEqual(XDFDatabase(saved.path).tables[0].title, "Example")


if __name__ == "__main__":
    unittest.main(verbosity=2)
