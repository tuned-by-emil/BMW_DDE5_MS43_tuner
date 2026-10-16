"""Scales are learned from validated independent profiles, never addresses."""
from dataclasses import replace
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bmw_bin_editor.core import AxisDefinition, CalibrationDefinition, safe_math
from bmw_bin_editor.definition_library import LibraryEntry, sha256
from bmw_bin_editor.map_classification import classify_candidate
from bmw_bin_editor.map_finder import MapCandidate
from bmw_bin_editor.scaling_evidence import (extract_scaling_profiles, load_scaling_profiles,
                                            source_family)
from bmw_bin_editor.xdf_edit import XDFWorkspace


RPM = [500, 750, 1000, 1500, 2000, 2500, 3500, 5000]
LOAD = [0, 500, 1000, 2000, 3500, 5000, 6500, 7500]


def table(title="InjCrv_phiMI1Bas1_MAP", *, equation="X * 3 / 128", units="deg CrS",
          load_units="mg/cyc", load_equation="X * 0.01", signed=True, address=1024):
    return CalibrationDefinition(title, "Real source declaration", "source", address, 8, 8,
        z_signed=signed, z_equation=equation, z_units=units,
        x_axis=AxisDefinition(512, 8, equation=load_equation, units=load_units),
        y_axis=AxisDefinition(528, 8, equation="X", units="rpm"))


def write_entry(directory, name, definitions, *, software="1037999991"):
    workspace = XDFWorkspace(bin_size=8192)
    for definition in definitions:
        workspace.add(definition)
    path = directory / (name + ".xdf")
    workspace.save_as(path)
    record = {"key": name, "title": name, "xdf": path.name, "byte_length": 8192,
              "software_identifiers": [software], "usable": True,
              "xdf_sha256": sha256(path.read_bytes())}
    return LibraryEntry(name, name, path, 8192, record)


def candidate_blob(grid, *, transpose=False, endian=">", address=2304):
    grid = np.asarray(grid)
    x, y = LOAD, RPM
    if transpose:
        grid, x, y = grid.T, RPM, LOAD
    words = list(y) + list(x) + [int(round(v)) & 65535 for v in grid.ravel()]
    data = b"\xff" * address + struct.pack(endian + "H" * len(words), *words)
    return data, MapCandidate(address + 32, 8, 8, address + 16, address,
                              score=92, reasons=["Structural test"])


class ScaleEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def profile(self, definition=None, *, software="1037999991", name="sample"):
        return extract_scaling_profiles([write_entry(self.directory, name, [definition or table()], software=software)])

    def test_only_specific_base_map_names_supply_family_evidence(self):
        cases = {"InjCrv_phiMI1Bas1_MAP": "Main SOI", "PCR_pBDesBasRgn1_MAP": "Boost target",
                 "PCR_rCtlBas_MAP": "N75 / VNT duty", "AccPed_trqEngHigh_MAP": "Driver's wish",
                 "InjCrv_phiPiI1Bas_MAP": None, "FMTC_phiStrtInjOpt_MAP": None,
                 "AirCtl_rEGRCtlBas_MAP": None, "PCR_rBPCtlBasCorr_MAP": None,
                 "Rail_dvolPreCtlBas_MAP": None, "0x1000 [UV] Main SOI base": None}
        for title, expected in cases.items():
            self.assertEqual(source_family(title), expected)
        self.assertEqual(source_family("Injection timing - Main Injection Base Map 1",
                                       "IdName: InjCrv_Bas1."), "Main SOI")

    def test_many_tables_and_duplicate_pack_are_one_independent_source(self):
        entries = [write_entry(self.directory, "one", [table(address=1024 + i*128) for i in range(10)]),
                   write_entry(self.directory, "copy", [table(address=4096)]),
                   write_entry(self.directory, "other", [table(address=2048)], software="1037999992")]
        profiles = extract_scaling_profiles(entries)
        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0].source_count, 2)

    def test_mass_and_volume_units_stay_separate_with_rounded_angle_scale(self):
        entries = [write_entry(self.directory, "mass", [table()]),
                   write_entry(self.directory, "volume", [table(equation="X * 0.023436", load_units="mm3/Stk")],
                               software="1037999992")]
        profiles = extract_scaling_profiles(entries)
        self.assertEqual(len(profiles), 2)
        self.assertEqual({p.load.units for p in profiles}, {"mg/cycle", "mm³/stroke"})
        self.assertTrue(all(p.source_count == 1 for p in profiles))

    def test_near_angle_scales_merge_only_with_same_units_and_choose_corroborated_formula(self):
        entries = [write_entry(self.directory, "rounded", [table(equation="X * 0.023436")]),
                   write_entry(self.directory, "exact", [table()], software="1037999992"),
                   write_entry(self.directory, "exact2", [table()], software="1037999993")]
        profiles = extract_scaling_profiles(entries)
        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0].z_equation, "X * 3 / 128")
        self.assertEqual(profiles[0].source_count, 3)
        self.assertTrue(any("0.023436" in scale for scale in profiles[0].source_scales))

    def test_unsigned_soi_missing_units_and_mixed_axes_are_rejected(self):
        definitions = [table(signed=False), table(load_units=""), table(units="us"),
                       table(load_units="rpm"), table(load_units="%")]
        entry = write_entry(self.directory, "unsupported", definitions)
        self.assertEqual(extract_scaling_profiles([entry]), ())

    def test_unknown_software_and_shifted_addresses_receive_signed_z_and_quantity_scaling(self):
        profiles = self.profile()
        r, load = np.meshgrid(np.linspace(0, 1, 8), np.linspace(0, 1, 8), indexing="ij")
        for address in (128, 4096):
            for transpose in (False, True):
                data, candidate = candidate_blob((-2 + 22*r + 4*load) * 128/3,
                                                 address=address, transpose=transpose)
                original = bytes(data)
                suggestions = classify_candidate(data, candidate, scaling_profiles=profiles)
                selected = next(s for s in suggestions if s.scaling_profile_count)
                self.assertEqual(selected.family, "Main SOI")
                self.assertTrue(selected.z_signed)
                self.assertEqual(selected.z_equation, "X * 3 / 128")
                self.assertIn("mg/cycle", (selected.x_units, selected.y_units))
                self.assertIn("X * 0.01", (selected.x_equation, selected.y_equation))
                self.assertEqual(selected.to_definition(candidate).z_address, candidate.address)
                self.assertEqual((selected.x_address, selected.y_address), (candidate.x_address, candidate.y_address))
                self.assertTrue(any("never addresses" in reason for reason in selected.reasons))
                self.assertTrue(any("sample" in source for source in selected.scaling_sources))
                raw = np.frombuffer(data, dtype=">i2", offset=candidate.address, count=64)
                self.assertAlmostEqual(min(safe_math(selected.z_equation, float(v)) for v in raw), -2, delta=.02)
                self.assertEqual(data, original)

    def test_new_library_coefficient_is_detected_without_a_hardcoded_encoding(self):
        profiles = self.profile(table(equation="X * 0.001"))
        r, load = np.meshgrid(np.linspace(0, 1, 8), np.linspace(0, 1, 8), indexing="ij")
        data, candidate = candidate_blob((-2 + 22*r + 4*load)*1000)
        suggestions = classify_candidate(data, candidate, scaling_profiles=profiles)
        self.assertTrue(suggestions)
        self.assertEqual(suggestions[0].z_equation, "X * 0.001")
        self.assertEqual(suggestions[0].scaling_profile_count, 1)
        self.assertNotIn("Main SOI", [s.family for s in classify_candidate(data, candidate, scaling_profiles=[])])

    def test_new_affine_offset_can_establish_an_interpretation(self):
        profiles = self.profile(table(equation="X * 0.001 - 20"))
        r, load = np.meshgrid(np.linspace(0, 1, 8), np.linspace(0, 1, 8), indexing="ij")
        data, candidate = candidate_blob((18 + 10*r + 2*load)*1000)
        suggestions = classify_candidate(data, candidate, scaling_profiles=profiles)
        selected = next(s for s in suggestions if s.scaling_profile_count)
        self.assertEqual(selected.z_equation, "X * 0.001 - 20")
        self.assertEqual(selected.family, "Main SOI")

    def test_competing_physical_scales_and_units_remain_reviewable(self):
        mass = write_entry(self.directory, "mass", [table()])
        volume = write_entry(self.directory, "volume", [table(load_units="mm3/Stk")], software="1037999992")
        profiles = extract_scaling_profiles([mass, volume])
        r, load = np.meshgrid(np.linspace(0, 1, 8), np.linspace(0, 1, 8), indexing="ij")
        data, candidate = candidate_blob((-2 + 22*r + 4*load)*128/3)
        suggestions = [s for s in classify_candidate(data, candidate, scaling_profiles=profiles) if s.family == "Main SOI"]
        self.assertIn("X * 0.01", {s.z_equation for s in suggestions})
        self.assertIn("mg/cycle", {s.x_units for s in suggestions})
        self.assertIn("mm³/stroke", {s.x_units for s in suggestions})
        identities = [s.to_definition(candidate).unique_id for s in suggestions]
        self.assertEqual(len(identities), len(set(identities)))

    def test_wrong_endianness_and_pedal_quantity_roles_do_not_transfer(self):
        profiles = self.profile()
        r, load = np.meshgrid(np.linspace(0, 1, 8), np.linspace(0, 1, 8), indexing="ij")
        data, candidate = candidate_blob((-2 + 22*r + 4*load)*128/3, endian="<")
        self.assertEqual(classify_candidate(data, candidate, endian=">", scaling_profiles=profiles), [])
        self.assertTrue(any(s.scaling_profile_count for s in classify_candidate(data, candidate, endian="<", scaling_profiles=profiles)))
        wrong_role = self.profile(table(load_units="%", load_equation="X * 100 / 8192"), name="pedal")
        self.assertEqual(wrong_role, ())

    def test_catalog_hash_validation_and_cache_invalidation_after_edit(self):
        root = self.directory / "library"
        root.mkdir()
        entry = write_entry(root, "scale", [table()])
        (root / "manifest.json").write_text(json.dumps({"format": 1, "entries": [entry.metadata]}), encoding="utf-8")
        profiles = load_scaling_profiles(root)
        self.assertEqual(len(profiles), 1)
        with patch("bmw_bin_editor.scaling_evidence.extract_scaling_profiles", side_effect=AssertionError("cache missed")):
            self.assertIs(load_scaling_profiles(root), profiles)
        entry.path.write_bytes(entry.path.read_bytes().replace(b"X * 3 / 128", b"X * 4 / 128"))
        self.assertEqual(load_scaling_profiles(root), ())
        self.assertEqual(load_scaling_profiles(self.directory / "missing"), ())

    def test_no_corpus_keeps_explicit_raw_axis_fallback(self):
        r, load = np.meshgrid(np.linspace(0, 1, 8), np.linspace(0, 1, 8), indexing="ij")
        data, candidate = candidate_blob((-2 + 22*r + 4*load)*128/3)
        suggestions = classify_candidate(data, candidate, scaling_profiles=[])
        selected = next(s for s in suggestions if s.family == "Main SOI")
        self.assertEqual(selected.scaling_profile_count, 0)
        self.assertEqual(selected.x_units, "load (raw)")
        self.assertEqual(selected.x_equation, "X")

    def test_missing_catalog_member_does_not_hide_other_valid_sources(self):
        root = self.directory / "library"
        root.mkdir()
        entry = write_entry(root, "valid", [table()])
        missing = dict(entry.metadata, key="missing", xdf="missing.xdf")
        manifest = root / "manifest.json"
        manifest.write_text(json.dumps({"format": 1, "entries": [entry.metadata, missing]}), encoding="utf-8")
        self.assertEqual(len(load_scaling_profiles(root)), 1)
        manifest.write_text("[]", encoding="utf-8")
        self.assertEqual(load_scaling_profiles(root), ())


if __name__ == "__main__":
    unittest.main(verbosity=2)
