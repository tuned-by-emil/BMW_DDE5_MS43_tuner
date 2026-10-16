"""Retained 361830 command scaling, raw editing and catalog provenance."""

import unittest as _optional_unittest
from pathlib import Path as _OptionalPath
if not (_OptionalPath(__file__).resolve().parents[1] / 'definitions/EDC16C31 361830.xdf').is_file():
    raise _optional_unittest.SkipTest('Optional local calibration resource not installed: definitions/EDC16C31 361830.xdf')

from pathlib import Path
import struct
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bmw_bin_editor.core import CalibrationDocument, XDFDatabase, safe_math
from bmw_bin_editor.definition_library import discover_entries, sha256
from bmw_bin_editor.scaling_evidence import source_family


XDF = ROOT / "definitions" / "EDC16C31 361830.xdf"
BASE_ADDRESSES = (0xF7F44, 0xF8478, 0xF86BC, 0xF8900)
UPPER_BOUND = 0xFA3DC


class N75ScalingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.database = XDFDatabase(XDF)
        cls.definitions = {d.z_address: d for d in cls.database.tables}

    def test_five_command_definitions_use_signed_native_percent(self):
        for address in (*BASE_ADDRESSES, UPPER_BOUND):
            with self.subTest(address=hex(address)):
                definition = self.definitions[address]
                self.assertEqual(definition.z_units, "%")
                self.assertTrue(definition.z_signed)
                self.assertEqual((definition.rows, definition.cols, definition.z_element_bits), (16, 16, 16))
                self.assertEqual(safe_math(definition.z_equation, 4096), 50.0)
                self.assertEqual(safe_math(definition.z_equation, 8192), 100.0)
                self.assertEqual(safe_math(definition.z_equation, -4096), -50.0)
                self.assertAlmostEqual(safe_math(definition.z_equation, 6341), 77.40478515625)

    def test_reading_does_not_change_bin_and_percent_edits_encode_native_words(self):
        # Test only a temporary image, keeping the user's calibration untouched.
        original = bytearray(0x100000)
        for address in (*BASE_ADDRESSES, UPPER_BOUND):
            struct.pack_into(">h", original, address, 4096)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "synthetic.bin"
            path.write_bytes(original)
            document = CalibrationDocument(self.database, path)
            for address in (*BASE_ADDRESSES, UPPER_BOUND):
                definition = self.definitions[address]
                self.assertEqual(document.map_values(definition)[0][0], 50.0)
                self.assertEqual(bytes(document.data), bytes(original))
                self.assertEqual(path.read_bytes(), bytes(original))
                for percent, raw in ((75.0, 6144), (-25.0, -2048), (125.0, 10240)):
                    self.assertTrue(document.set_cells(definition, {(0, 0): percent}, "native command test"))
                    self.assertEqual(document.map_raw(definition)[0][0], raw)
                    self.assertEqual(document.map_values(definition)[0][0], percent)
                    expected = bytearray(original)
                    struct.pack_into(">h", expected, address, raw)
                    self.assertEqual(bytes(document.data), bytes(expected))
                    self.assertTrue(document.undo())
                    self.assertEqual(bytes(document.data), bytes(original))
                self.assertFalse(document.dirty)
            self.assertEqual(path.read_bytes(), bytes(original))

    def test_base_family_recognizes_four_maps_and_excludes_bound_contribution(self):
        for address in BASE_ADDRESSES:
            definition = self.definitions[address]
            self.assertEqual(source_family(definition.title, definition.description), "N75 / VNT duty")
        upper = self.definitions[UPPER_BOUND]
        self.assertIn("upper-command-bound contributor", upper.title)
        self.assertIsNone(source_family(upper.title, upper.description))
        # A base-looking legacy label or IdName must not override an explicit
        # command-bound role. Family evidence remains semantic, not address based.
        self.assertIsNone(source_family("N75 E base upper command bound", "IdName: PCR_rCtlBas_MAP"))

    def test_corrected_definition_is_accepted_by_hash_validated_library(self):
        retained = next(entry for entry in discover_entries(ROOT / "definitions" / "library")
                        if entry.key == "retained-361830")
        self.assertEqual(retained.path, XDF.resolve())
        self.assertEqual(retained.metadata["xdf_sha256"], sha256(XDF.read_bytes()))
        self.assertEqual(retained.metadata["map_count"], len(self.database.definitions))

    def test_contradicted_caller_claim_is_retired_without_reinterpreting_gains(self):
        header = self.database.root.findtext("./XDFHEADER/description")
        self.assertIn("ownership", header)
        self.assertIn("retired", header)
        shared = [d for d in self.database.definitions if "Shared controller (ownership unresolved)" in d.title]
        self.assertEqual(len(shared), 23)
        for definition in shared:
            self.assertIn("[UV]", definition.title)
            self.assertIn("historical 0x84A9C8 boost/N75 ownership claim is retired", definition.description)
            self.assertNotIn("This proves controller ownership", definition.description)
        # This separate multiplicative gain remains a dimensionless Q13 factor.
        gain = next(d for d in shared if d.z_address == 0xF7624)
        self.assertEqual(safe_math(gain.z_equation, 8192), 1.0)


if __name__ == "__main__":
    unittest.main()
