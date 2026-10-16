from __future__ import annotations

import json
from pathlib import Path
import struct
import tempfile
import unittest

from bmw_bin_editor.core import EditorError
from bmw_bin_editor.core import AxisDefinition, CalibrationDefinition
from bmw_bin_editor.a2l_import import ConversionResult
from bmw_bin_editor.xdf_edit import XDFWorkspace
from bmw_bin_editor.definition_library import (LibraryEntry, MemoryImage, discover_entries, parse_intel_hex,
                                             propose_matches, sha256, software_identifiers)
from generate_definition_library import (external_flash, verify_pair_identity,
                                        validate_json_structure, validate_source_coverage, _profile_naming)


def record(address, kind, value=b""):
    raw = bytes([len(value)]) + address.to_bytes(2, "big") + bytes([kind]) + value
    return b":" + (raw + bytes([-sum(raw) & 255])).hex().upper().encode() + b"\n"


class NamingTests(unittest.TestCase):
    def write_profile(self, path, profile):
        path.write_text(f"<XDFFORMAT><XDFHEADER><BMWBINEDITORPROFILE>{profile}</BMWBINEDITORPROFILE>"
                        "</XDFHEADER></XDFFORMAT>", encoding="utf-8")

    def test_reviewed_nomenclature_persists_from_previous_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            path = output / "EDC16C35 370888.xdf"
            self.write_profile(path, "a2l:reviewed")
            nomenclature = {"ecu_type": "EDC16C35", "software_label": "370888",
                            "evidence": ["Source ECU identification corroborated against the paired image."]}
            entry = {"key": "stable-key", "profile_id": "a2l:reviewed", "title": "EDC16C35 370888",
                     "xdf": path.name, "xdf_sha256": sha256(path.read_bytes()), "nomenclature": nomenclature}
            (output / "manifest.json").write_text(json.dumps({"entries": [entry]}), encoding="utf-8")
            title, selected, naming = _profile_naming(output, "stable-key", "a2l:reviewed",
                                                     "unrelated-export-name", ["1037123456"])
            self.assertEqual((title, selected, naming), (entry["title"], path, nomenclature))

    def test_different_profiles_with_same_software_never_overwrite_each_other(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            existing = output / "EDC16 370888.xdf"
            self.write_profile(existing, "a2l:first")
            claims = {}
            title, path, _ = _profile_naming(output, "second-key", "ols-json:second", "export",
                                            ["1037370888"], [], claims)
            self.assertEqual(title, "EDC16 370888")
            self.assertEqual(path.name, f"EDC16 370888 {sha256(b'second-key')[:8]}.xdf")
            self.assertNotEqual(path, existing)
            self.write_profile(path, "ols-json:second")
            self.assertEqual(_profile_naming(output, "second-key", "ols-json:second", "export",
                                             ["1037370888"], [], claims)[1], path)
            self.assertNotEqual(_profile_naming(output, "third-key", "a2l:third", "export",
                                               ["1037370888"], [], claims)[1], path)
            self.assertIn("a2l:first", existing.read_text(encoding="utf-8"))

    def test_previously_generated_file_with_user_edits_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            path = output / "EDC16C35 370888.xdf"
            self.write_profile(path, "a2l:reviewed")
            old = {"key": "stable-key", "profile_id": "a2l:reviewed", "xdf": path.name,
                   "xdf_sha256": sha256(path.read_bytes()),
                   "nomenclature": {"ecu_type": "EDC16C35", "software_label": "370888", "evidence": "Reviewed"}}
            path.write_text(path.read_text(encoding="utf-8").replace("<XDFHEADER>", "<XDFHEADER><author>User edit</author>"),
                            encoding="utf-8")
            selected = _profile_naming(output, "stable-key", "a2l:reviewed", "export", [], [old])[1]
            self.assertNotEqual(selected, path)
            self.assertIn("User edit", path.read_text(encoding="utf-8"))

    def test_manifest_filenames_cannot_escape_or_use_unsafe_windows_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            for filename in ("../escape.xdf", "..\\escape.xdf", "C:\\escape.xdf", "alternate:stream.xdf",
                             "EDC16 bad?.xdf", "NUL.xdf", "valid.xdf "):
                with self.subTest(filename=filename), self.assertRaises(EditorError):
                    _profile_naming(output, "key", "profile", "source", [],
                                    [{"key": "key", "xdf": filename,
                                      "nomenclature": {"ecu_type": "EDC16", "software_label": "123456"}}])
            _, path, naming = _profile_naming(output, "safe-key", "profile", "../untrusted:source", [], [])
            self.assertEqual(path.resolve().parent, output)
            self.assertEqual(naming["ecu_type"], "EDC16")


class ImageTests(unittest.TestCase):
    def test_sparse_hex_extended_address_and_dos_eof(self):
        raw = (record(0, 4, b"\0\0") + record(0x8000, 0, b"ABCD") +
               record(0, 4, b"\0\x40") + record(0, 0, b"CODE") + record(0, 1) + b"\x1a\r\n")
        image = parse_intel_hex(raw)
        self.assertEqual(image.read(0x8000, 4), b"ABCD")
        self.assertEqual(image.read(0x400000, 4), b"CODE")
        self.assertFalse(image.covered(0x8002, 4))
        with self.assertRaises(EditorError):
            image.read(0x8004, 1)
        data, base = external_flash(image)
        self.assertEqual(base, 0)
        self.assertEqual(len(data), 0x40000)
        self.assertEqual(data[0x8000:0x8004], b"ABCD")
        self.assertEqual(data[:10], b"\xff" * 10)

    def test_checksum_and_conflicting_overlaps_rejected(self):
        raw = record(0, 0, b"ABCD") + record(0, 1)
        with self.assertRaises(EditorError):
            parse_intel_hex(raw.replace(b"4142", b"4143"))
        with self.assertRaises(EditorError):
            parse_intel_hex(record(0, 0, b"ABCD") + record(1, 0, b"X") + record(0, 1))
        self.assertEqual(parse_intel_hex(record(0, 0, b"ABCD") + record(1, 0, b"BC") + record(0, 1)).data, b"ABCD")

    def test_missing_eof_or_data_after_eof_rejected(self):
        with self.assertRaises(EditorError):
            parse_intel_hex(record(0, 0, b"AB"))
        with self.assertRaises(EditorError):
            parse_intel_hex(record(0, 0, b"AB") + record(0, 1) + record(2, 0, b"C"))

    def test_segment_address_records(self):
        image = parse_intel_hex(record(0, 2, b"\x12\x34") + record(0x10, 0, b"ABC") + record(0, 1))
        self.assertEqual(image.start, 0x12350)
        self.assertEqual(image.data, b"ABC")

    def test_project_revision_pair_validation_and_conflicting_variant(self):
        data = b"\xff" * 10 + b"BMW EDC16C [P_390_WI.P.0.1]" + b"\xff" * 20
        image = MemoryImage(data, 0, [(0, len(data))], "Binary", sha256(data))
        report = {"project": "P390", "project_description": "BMW DDE6.2 6-Zylinder VP0.1",
                  "records": [{"dimension_fields": [1]} for _ in range(20)], "skipped": []}
        anchors = verify_pair_identity(report, image, data)
        self.assertEqual(bytes.fromhex(anchors[0]["bytes"]), b"P_390_WI.P.0.1")
        report["project_description"] = "BMW DDE6.2 6-Zylinder VR0.1"
        with self.assertRaises(EditorError):
            verify_pair_identity(report, image, data)

    def test_declared_epk_cannot_silently_bypass_validation(self):
        data = b"VERSION42" + b"\xff" * 30
        image = MemoryImage(data, 0, [(0, len(data))], "Binary", sha256(data))
        report = {"epk": "VERSION42", "epk_addresses": [0]}
        self.assertEqual(verify_pair_identity(report, image, data)[0]["offset"], 0)
        report["epk_addresses"] = []
        with self.assertRaises(EditorError):
            verify_pair_identity(report, image, data)
        report.update(epk="WRONG", epk_addresses=[0])
        with self.assertRaises(EditorError):
            verify_pair_identity(report, image, data)


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.data = bytearray((i * 17 + i // 31) & 255 for i in range(4096))
        self.data[20:30] = b"1037370888"
        self.axis = [{"offset": 1500 + 24*i, "length": 16,
                      "sha256": sha256(bytes(self.data[1500 + 24*i:1516 + 24*i]))} for i in range(12)]
        self.code = [{"offset": 128 + 96*i, "length": 64,
                      "sha256": sha256(bytes(self.data[128 + 96*i:192 + 96*i]))} for i in range(12)]
        self.meta = {"software_identifiers": ["1037370888"], "original_sha256": [sha256(bytes(self.data))],
                     "axis_fingerprints": self.axis, "code_fingerprints": self.code,
                     "map_count": 10, "layout": "test layout"}
        self.entry = LibraryEntry("test", "Test software", Path("test.xdf"), len(self.data), self.meta)

    def matches(self, data):
        return propose_matches(bytes(data), entries=[self.entry])

    def test_exact_original_and_tuned_calibration(self):
        self.assertEqual(self.matches(self.data)[0].score, 100)
        self.data[3500] ^= 1
        match = self.matches(self.data)[0]
        self.assertEqual(match.score, 95)
        self.assertIn("Calibration values may differ", " ".join(match.reasons))

    def test_boot_area_variation_does_not_break_supported_profile(self):
        self.data[0:20] = b"\xff" * 20
        self.assertTrue(self.matches(self.data))

    def test_id_conflict_and_removed_identity_rejected(self):
        self.data[20:30] = b"1037371268"
        self.assertFalse(self.matches(self.data))
        self.data[20:30] = b"0000000000"
        self.assertFalse(self.matches(self.data))

    def test_layout_wrong_size_or_excess_changed_axes_rejected(self):
        self.assertFalse(self.matches(self.data + b"\xff"))
        for sample in self.axis[:6]:
            self.data[sample["offset"]] ^= 1
        self.assertFalse(self.matches(self.data))

    def test_code_changes_and_insufficient_fingerprints_rejected(self):
        for sample in self.code[:5]:
            self.data[sample["offset"]] ^= 1
        self.assertFalse(self.matches(self.data))
        self.meta["original_sha256"] = []
        self.meta["code_fingerprints"] = self.code[:2]
        self.assertFalse(self.matches(self.data))

    def test_software_ids_adjacent_to_numeric_identifier(self):
        self.assertEqual(software_identifiers(b"103737088812345"), ["1037370888"])

    def test_project_identity_anchor_required_even_for_shared_code(self):
        self.meta["identity_anchors"] = [{"offset": 50, "bytes": bytes(self.data[50:60]).hex()}]
        self.data[3500] ^= 1
        self.assertTrue(self.matches(self.data))
        self.data[50] ^= 1
        self.assertFalse(self.matches(self.data))

    def test_retained_definition_requires_software_and_many_axes(self):
        values = struct.pack(">8H", *range(0, 800, 100))
        for i in range(16):
            self.data[1500+i*20:1516+i*20] = values
        self.meta.update(retained=True, software_identifiers=["1037370888"], original_sha256=[],
                         structural_axes=[{"offset": 1500+i*20, "count": 8, "bits": 16, "signed": False}
                                          for i in range(16)], byte_order="big")
        self.assertEqual(self.matches(self.data)[0].score, 86)
        self.data[20:30] = b"1037371268"
        self.assertFalse(self.matches(self.data))

    def test_discovery_rejects_changed_xdf_and_path_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "definitions"
            root = parent / "library"
            root.mkdir(parents=True)
            path = root / "test.xdf"
            path.write_text("<XDFFORMAT/>", encoding="utf-8")
            item = {"key": "test", "title": "Test", "xdf": path.name, "xdf_sha256": sha256(path.read_bytes()),
                    "byte_length": 4096, "usable": True}
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"format": 1, "entries": [item]}), encoding="utf-8")
            self.assertEqual(len(discover_entries(root)), 1)
            path.write_text("<XDFFORMAT version='changed'/>", encoding="utf-8")
            self.assertFalse(discover_entries(root))


class ImportValidationTests(unittest.TestCase):
    def test_duplicate_display_names_have_independent_coverage_records(self):
        workspace = XDFWorkspace(bin_size=128)
        workspace.add(CalibrationDefinition("Same name", "", "Test", 16, 1, 1, kind="constant"))
        workspace.add(CalibrationDefinition("Same name", "", "Test", 80, 1, 1, kind="constant"))
        report = {"records": [{"name": "Same name", "spans": [{"offset": 16, "length": 2}]},
                              {"name": "Same name", "spans": [{"offset": 80, "length": 2}]}], "skipped": []}
        result = ConversionResult(workspace, report)
        image = MemoryImage(b"\0" * 128, 0, [(0, 64)], "Binary", "test")
        validate_source_coverage(result, image)
        self.assertEqual([d.z_address for d in result.definitions], [16])
        self.assertEqual(report["records"][0]["spans"][0]["offset"], 16)

    def json_fixture(self):
        data = bytearray(4096)
        workspace = XDFWorkspace(bin_size=len(data))
        records = []
        for index in range(8):
            start = 128 + index * 256
            # Flat maps are perfectly valid when exact exported axes and stored
            # counts corroborate them; shape-finder heuristics reject these.
            block = struct.pack(">HH", 8, 8) + struct.pack(">8H", *range(500, 2500, 250))
            block += struct.pack(">8H", *range(0, 800, 100)) + struct.pack(">64h", *([-77] * 64))
            data[start:start + len(block)] = block
            definition = CalibrationDefinition("Same name", "", "Test", start + 36, 8, 8, z_signed=True,
                                               x_axis=AxisDefinition(start + 20, 8), y_axis=AxisDefinition(start + 4, 8))
            workspace.add(definition)
            records.append({"name": "Same name", "z_offset": start + 36})
        return bytes(data), ConversionResult(workspace, {"records": records, "skipped": [], "byte_order": "big"})

    def test_explicit_headers_accept_flat_signed_tables_with_duplicate_names(self):
        data, result = self.json_fixture()
        validate_json_structure(result, data)
        self.assertEqual(result.report["validated_two_axis_headers"], 8)
        self.assertEqual(len(result.definitions), 8)
        self.assertEqual(len({d.z_address for d in result.definitions}), 8)

    def test_wrong_byte_order_or_offset_not_corroborated(self):
        data, result = self.json_fixture()
        result.report["byte_order"] = "little"
        with self.assertRaises(EditorError):
            validate_json_structure(result, data)

    def test_shared_axes_require_identical_local_bytes_and_preserve_addresses(self):
        data, result = self.json_fixture()
        definition = result.definitions[-1]
        definition.x_axis.address = result.definitions[0].x_axis.address
        definition.y_axis.address = result.definitions[0].y_axis.address
        original = (definition.x_axis.address, definition.y_axis.address)
        result.workspace.update(definition.unique_id, definition)
        validate_json_structure(result, data)
        self.assertEqual(result.report["validated_shared_axis_tables"], 1)
        self.assertEqual((result.definitions[-1].x_axis.address, result.definitions[-1].y_axis.address), original)
        self.assertIn("Editing an axis changes the referenced shared storage", result.definitions[-1].description)

    def test_conflicting_shared_axis_copy_cannot_be_imported(self):
        data, result = self.json_fixture()
        definition = result.definitions[-1]
        local_x = definition.x_axis.address
        definition.x_axis.address = result.definitions[0].x_axis.address
        result.workspace.update(definition.unique_id, definition)
        changed = bytearray(data)
        changed[local_x + 15] ^= 1
        with self.assertRaises(EditorError):
            validate_json_structure(result, bytes(changed))
            outside = Path(tmp) / "outside.xdf"
            outside.write_text("<XDFFORMAT/>", encoding="utf-8")
            item.update(xdf="../../outside.xdf", xdf_sha256=sha256(outside.read_bytes()))
            manifest.write_text(json.dumps({"format": 1, "entries": [item]}), encoding="utf-8")
            self.assertFalse(discover_entries(root))


if __name__ == "__main__":
    unittest.main()
