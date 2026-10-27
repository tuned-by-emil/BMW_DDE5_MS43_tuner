import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication
from bmw_bin_editor.core import CalibrationDefinition, EditorError, XDFDatabase
from bmw_bin_editor.ols_import import OLSProject, OLSVersion
from bmw_bin_editor import ols_import_gui as gui
from bmw_bin_editor.xdf_edit import XDFWorkspace

APP = QApplication.instance() or QApplication([])


class OLSExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        images = [b"\x00\x02\x00\x03", b"\x00\x04\x00\x05"]
        self.source_data = b"exact project settings" + b"".join(images)
        self.source = self.root / "source.ols"
        self.source.write_bytes(self.source_data)
        self.project = OLSProject(self.source, hashlib.sha256(self.source_data).hexdigest(), 113,
            "EDC16C31 361888", {}, [{}], [],
            [OLSVersion(index, "Original" if index == 0 else "Version 1", data,
                        hashlib.sha256(data).hexdigest()) for index, data in enumerate(images)], [], self.source_data)
        workspace = XDFWorkspace(bin_size=4)
        workspace.add(CalibrationDefinition("Limit", "From project", "Limits", 0, 1, 1, kind="constant"))
        self.conversion = SimpleNamespace(workspace=workspace, report={"converted": 1, "skipped": [],
            "source_sha256": self.project.source_sha256, "version_index": 0,
            "bin_sha256": self.project.versions[0].sha256})
        self.dialogs = []

    def tearDown(self):
        for dialog in self.dialogs:
            dialog.reject()
        APP.processEvents()
        self.temp.cleanup()

    def dialog(self):
        dialog = gui.OLSImportDialog()
        self.dialogs.append(dialog)
        worker = SimpleNamespace(error=None, project=self.project, conversion=self.conversion,
                                 version_index=0, deleteLater=lambda: None)
        dialog._worker = worker
        dialog._finished()
        return dialog

    def test_exports_exact_selected_bin_and_lossless_settings_and_matching_report(self):
        xdf, binary = self.root / "custom" / "definition.xdf", self.root / "bins" / "version.bin"
        self.conversion.report.update(version_index=1, bin_sha256=self.project.versions[1].sha256)
        before_report = deepcopy(self.conversion.report)
        files = gui.export_recovery(self.project, self.conversion, xdf_path=xdf, bin_path=binary,
                                    version_index=1)
        self.assertEqual(binary.read_bytes(), self.project.versions[1].data)
        self.assertEqual(XDFDatabase(xdf).definitions[0].z_address, 0)
        prefix = xdf.with_suffix(".olsmeta").read_bytes()
        self.assertEqual(prefix + b"".join(item.data for item in self.project.versions), self.source_data)
        report = json.loads(xdf.with_suffix(".import.json").read_text(encoding="utf-8"))
        self.assertEqual(report["exported_bin"]["sha256"], self.project.versions[1].sha256)
        self.assertEqual(report["xdf"], xdf.name)
        self.assertEqual(len(files), 4)
        self.assertEqual(self.source.read_bytes(), self.source_data)
        self.assertEqual(self.conversion.report, before_report)

    def test_output_root_defaults_source_and_portable_never_internal(self):
        with patch.object(gui, "output_root", return_value=self.root):
            xdf, binary = gui.default_output_paths(self.project)
        self.assertEqual(xdf.resolve(), (self.root / "definitions" / "library" / "EDC16C31 361888.xdf").resolve())
        self.assertEqual(binary.resolve(), (self.root / "mappacks" / "extracted" / "EDC16C31 361888 Original.bin").resolve())
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", str(self.root / "Editor.exe")):
            self.assertEqual(gui.output_root().resolve(), self.root.resolve())

    def test_cancel_bin_prompt_creates_no_xdf_or_directories(self):
        dialog = self.dialog()
        xdf = self.root / "not-created" / "definition.xdf"
        with patch.object(gui.QFileDialog, "getSaveFileName", side_effect=[(str(xdf), ""), ("", "")]) as choose:
            dialog.export_files()
        self.assertEqual(choose.call_count, 2)
        self.assertFalse(xdf.parent.exists())
        self.assertEqual(dialog.exported_paths, [])

    def test_mismatched_conversion_source_and_version_cannot_export(self):
        xdf = self.root / "new" / "definition.xdf"
        with self.assertRaisesRegex(EditorError, "different source or BIN"):
            gui.export_recovery(self.project, self.conversion, xdf_path=xdf, version_index=1)
        self.conversion.report["source_sha256"] = "another project"
        with self.assertRaisesRegex(EditorError, "different source or BIN"):
            gui.export_recovery(self.project, self.conversion, xdf_path=xdf)
        self.assertFalse(xdf.parent.exists())

    def test_each_export_prompts_for_both_destinations_and_keeps_chosen_folders(self):
        dialog = self.dialog()
        xdfs = [self.root / "definitions-a" / "one.xdf", self.root / "definitions-b" / "two.xdf"]
        bins = [self.root / "bins-a" / "one.bin", self.root / "bins-b" / "two.bin"]
        answers = [(str(path), "") for pair in zip(xdfs, bins) for path in pair]
        with patch.object(gui.QFileDialog, "getSaveFileName", side_effect=answers) as choose:
            dialog.export_files()
            dialog.export_files()
        self.assertEqual(choose.call_count, 4)
        self.assertTrue(all(path.is_file() for path in xdfs + bins))
        self.assertTrue(all(path.read_bytes() == self.project.versions[0].data for path in bins))
        self.assertEqual(choose.call_args_list[0].args[1], "Save generated XDF")
        self.assertEqual(choose.call_args_list[1].args[1], "Save extracted BIN")

    def test_bin_only_export_has_no_xdf_and_optional_settings(self):
        dialog = self.dialog()
        dialog.save_xdf.setChecked(False)
        dialog.keep_settings.setChecked(False)
        binary = self.root / "only.bin"
        with patch.object(gui.QFileDialog, "getSaveFileName", return_value=(str(binary), "")) as choose:
            dialog.export_files()
        self.assertEqual(choose.call_count, 1)
        self.assertEqual([path.resolve() for path in dialog.exported_paths],
                         [binary.resolve(), binary.with_suffix(".import.json").resolve()])
        self.assertFalse(binary.with_suffix(".olsmeta").exists())
        self.assertFalse(binary.with_suffix(".xdf").exists())

    def test_protected_paths_and_integrity_fail_before_any_outputs(self):
        protected = self.root / "active.bin"
        protected.write_bytes(b"active edits")
        xdf = self.root / "new" / "definition.xdf"
        with self.assertRaisesRegex(EditorError, "open editor file"):
            gui.export_recovery(self.project, self.conversion, xdf_path=xdf, bin_path=protected,
                                protected_paths=[protected])
        self.assertEqual(protected.read_bytes(), b"active edits")
        self.assertFalse(xdf.parent.exists())
        self.project.versions[0].sha256 = "incorrect"
        with self.assertRaisesRegex(EditorError, "integrity"):
            gui.export_recovery(self.project, self.conversion, xdf_path=xdf)
        self.assertFalse(xdf.parent.exists())

    def test_worker_errors_and_active_worker_block_export_and_close(self):
        worker = gui._RecoveryWorker(self.source)
        with patch.object(gui, "load_ols", side_effect=EditorError("Unsupported format")):
            worker.run()
        self.assertEqual(worker.error, "Unsupported format")
        dialog = self.dialog()
        dialog._worker = SimpleNamespace()
        with patch.object(gui.QFileDialog, "getSaveFileName") as choose:
            dialog.export_files()
            dialog.reject()
            choose.assert_not_called()
        dialog._worker = None


if __name__ == "__main__":
    unittest.main()
