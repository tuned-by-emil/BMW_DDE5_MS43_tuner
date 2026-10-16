"""A failed BIN save must preserve both the destination and document state."""
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bmw_bin_editor.core import CalibrationDocument


class AtomicSaveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)
        source = self.folder / "source.bin"
        source.write_bytes(b"original BIN")
        self.document = CalibrationDocument(SimpleNamespace(default_lsb_first=False), source)
        self.document.apply_bytes("Change", [(0, b"modified")])
        self.target = self.folder / "target.bin"
        self.target.write_bytes(b"previous revision")

    def tearDown(self):
        self.temporary.cleanup()

    def assert_failed_save_preserved_state(self):
        self.assertEqual(self.target.read_bytes(), b"previous revision")
        self.assertEqual(self.document.path, self.folder / "source.bin")
        self.assertEqual(self.document.saved, b"original BIN")
        self.assertTrue(self.document.dirty)
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ["source.bin", "target.bin"])

    def test_flush_failure_keeps_existing_revision(self):
        with patch("bmw_bin_editor.core.os.fsync", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.document.save_as(self.target)
        self.assert_failed_save_preserved_state()

    def test_replacement_failure_keeps_existing_revision(self):
        with patch("bmw_bin_editor.core.os.replace", side_effect=PermissionError("file in use")):
            with self.assertRaisesRegex(PermissionError, "file in use"):
                self.document.save_as(self.target)
        self.assert_failed_save_preserved_state()

    def test_success_updates_file_and_saved_state(self):
        self.assertEqual(self.document.save_as(self.target), self.target)
        self.assertEqual(self.target.read_bytes(), bytes(self.document.data))
        self.assertEqual(self.document.saved, self.target.read_bytes())
        self.assertEqual(self.document.path, self.target)
        self.assertFalse(self.document.dirty)
        self.document.undo()
        self.assertTrue(self.document.dirty)

    def test_in_place_save(self):
        source = self.document.path
        self.document.save_as(source)
        self.assertEqual(source.read_bytes(), bytes(self.document.data))
        self.assertFalse(self.document.dirty)


if __name__ == "__main__":
    unittest.main(verbosity=2)
