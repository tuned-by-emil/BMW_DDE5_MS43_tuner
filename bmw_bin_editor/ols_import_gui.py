"""Review native WinOLS recovery and choose every export destination explicitly."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

from PySide6.QtCore import QThread
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QVBoxLayout,
)

from .core import EditorError
from .ols_import import load_ols
from .ols_to_xdf import convert_ols


def output_root() -> Path:
    # User exports belong beside the portable application, outside _internal.
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]


def safe_filename(value: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .") or "WinOLS project"


def default_output_paths(project, version_index: int = 0) -> tuple[Path, Path]:
    title = safe_filename(project.title)
    version = safe_filename(project.versions[version_index].name)
    root = output_root()
    return (root / "definitions" / "library" / f"{title}.xdf",
            root / "mappacks" / "extracted" / f"{title} {version}.bin")


def review_text(project, conversion, version_index: int) -> str:
    report = conversion.report
    version = project.versions[version_index]
    metadata = project.metadata
    vehicle = " ".join(str(metadata.get(key, "")) for key in ("make", "model", "type", "year")).strip()
    lines = [project.title]
    if vehicle:
        lines.append(vehicle)
    if metadata.get("hw_number"):
        lines.append(f"Hardware: {metadata['hw_number']}")
    lines.extend([f"Selected BIN: {version.name} ({len(version.data):,} bytes)",
                  f"BIN SHA-256: {version.sha256}", "",
                  f"XDF coverage: {report.get('table_count', 0):,} tables/curves and {report.get('constant_count', 0):,} constants."])
    skipped = report.get("skipped", [])
    if isinstance(skipped, list) and skipped:
        lines.extend(["", f"Omitted definitions ({len(skipped):,}):"])
        for reason, count in report.get("skip_reasons", {}).items():
            lines.append(f"• {count:,}: {reason}")
        lines.extend(["", "Examples requiring review:"])
        for item in skipped[:18]:
            if isinstance(item, dict):
                lines.append(f"• {item.get('name', 'Unnamed')}: {item.get('reason', 'Unsupported definition')}")
        lines.append("The saved import report contains the complete list.")
    else:
        lines.append("No definitions omitted.")
    if report.get("column_storage_display_evidence"):
        lines.extend(["", "The smoke lambda table at 0xCF358 matches the supplied WinOLS display, with RPM across columns and air mass across rows."])
    lines.extend(["", "Table layout retains the source X/Y assignments using the supported format interpretation. Other tables have not each been verified against a WinOLS display.",
                  "", "Complete project settings retain all source definitions, including omissions. The .olsmeta file preserves these settings beside the exports.",
                  "", "Native recovery currently supports format 113 projects with exact 2 MiB BIN slots."])
    return "\n".join(lines)


def _atomic_bytes(path: Path, data: bytes) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", prefix=f".{path.name}.", suffix=".tmp",
                                         dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def recovery_output_paths(xdf_path: Path | None, bin_path: Path | None,
                          keep_settings: bool) -> list[Path]:
    main = xdf_path or bin_path
    if main is None:
        return []
    outputs = [path for path in (xdf_path, bin_path) if path is not None]
    if keep_settings:
        outputs.append(main.with_suffix(".olsmeta"))
    outputs.append(main.with_suffix(".import.json"))
    return outputs


def export_recovery(project, conversion, *, xdf_path: Path | None = None,
                    bin_path: Path | None = None, version_index: int = 0,
                    keep_settings: bool = True, protected_paths=()) -> list[Path]:
    """Write selected files only after all destinations have been collected."""
    xdf_path = Path(xdf_path) if xdf_path is not None else None
    bin_path = Path(bin_path) if bin_path is not None else None
    outputs = recovery_output_paths(xdf_path, bin_path, keep_settings)
    if not outputs:
        raise EditorError("Select an XDF or BIN to export.")
    if xdf_path is not None and xdf_path.suffix.lower() != ".xdf":
        raise EditorError("The definition filename must end in .xdf.")
    if bin_path is not None and bin_path.suffix.lower() != ".bin":
        raise EditorError("The firmware filename must end in .bin.")
    if not 0 <= version_index < len(project.versions):
        raise EditorError("Select a valid project version.")
    resolved = [path.resolve() for path in outputs]
    blocked = {Path(path).resolve() for path in protected_paths if path is not None}
    blocked.add(Path(project.source_path).resolve())
    if len(set(resolved)) != len(resolved) or any(path in blocked for path in resolved):
        raise EditorError("Choose separate output files that do not replace the source OLS or an open editor file.")
    version = project.versions[version_index]
    if hashlib.sha256(version.data).hexdigest() != version.sha256:
        raise EditorError("The recovered BIN failed its integrity check.")
    if hashlib.sha256(project.source_data).hexdigest() != project.source_sha256:
        raise EditorError("The recovered project failed its integrity check.")
    if (conversion.report.get("source_sha256") != project.source_sha256
            or conversion.report.get("version_index") != version_index
            or conversion.report.get("bin_sha256") != version.sha256):
        raise EditorError("The XDF conversion was validated against a different source or BIN version. Review the selected version again.")
    report = copy.deepcopy(conversion.report)
    report["xdf"] = xdf_path.name if xdf_path else None
    report["exported_bin"] = {"file": bin_path.name, "version_index": version_index,
                              "name": version.name, "byte_length": len(version.data),
                              "sha256": version.sha256} if bin_path else None
    settings_data = None
    main = xdf_path or bin_path
    if keep_settings:
        # Supported format 113 stores the verified complete BIN slots at EOF.
        prefix_length = len(project.source_data) - sum(len(item.data) for item in project.versions)
        if prefix_length <= 0 or project.source_data[prefix_length:] != b"".join(item.data for item in project.versions):
            raise EditorError("The project settings boundary could not be verified.")
        settings_data = project.source_data[:prefix_length]
        report["project_settings"] = {
            "file": main.with_suffix(".olsmeta").name,
            "byte_length": len(settings_data), "sha256": hashlib.sha256(settings_data).hexdigest(),
            "description": "Exact source project prefix, retaining all definitions and undecoded settings. Append all original BIN slots in order to reconstruct the source OLS.",
            "source_sha256": project.source_sha256,
            "bin_slots": [{"index": item.index, "name": item.name, "byte_length": len(item.data),
                           "sha256": item.sha256} for item in project.versions],
        }
    else:
        report.pop("project_settings", None)
    # Validate the report before creating any files.
    report_bytes = (json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    for path in outputs:
        path.parent.mkdir(parents=True, exist_ok=True)
    if xdf_path is not None:
        conversion.workspace.save_as(xdf_path)
    if bin_path is not None:
        _atomic_bytes(bin_path, version.data)
    if settings_data is not None:
        _atomic_bytes(main.with_suffix(".olsmeta"), settings_data)
    _atomic_bytes(main.with_suffix(".import.json"), report_bytes)
    return outputs


class _RecoveryWorker(QThread):
    def __init__(self, path, project=None, version_index=0, parent=None):
        super().__init__(parent)
        self.path = path
        self.project = project
        self.version_index = version_index
        self.conversion = None
        self.error = None

    def run(self):
        try:
            if self.project is None:
                self.project = load_ols(self.path)
            self.conversion = convert_ols(self.project, version_index=self.version_index)
        except Exception as exc:
            self.error = str(exc)


class OLSImportDialog(QDialog):
    def __init__(self, parent=None, *, protected_paths=()):
        super().__init__(parent)
        self.setWindowTitle("Import WinOLS project")
        self.resize(760, 550)
        self.project = None
        self.conversion = None
        self.exported_paths = []
        self.protected_paths = protected_paths
        self._worker = None
        layout = QVBoxLayout(self)
        source_row = QHBoxLayout()
        self.source = QLineEdit()
        self.source.setReadOnly(True)
        self.browse = QPushButton("Choose OLS…")
        self.browse.clicked.connect(self.choose_source)
        source_row.addWidget(self.source, 1)
        source_row.addWidget(self.browse)
        layout.addLayout(source_row)
        self.summary = QLabel("Choose a WinOLS project to recover its BIN and definitions. Supported project format: 113.")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        form = QFormLayout()
        self.version = QComboBox()
        self.version.currentIndexChanged.connect(self._version_changed)
        form.addRow("BIN version", self.version)
        layout.addLayout(form)
        self.review = QPlainTextEdit()
        self.review.setReadOnly(True)
        layout.addWidget(self.review, 1)
        self.save_xdf = QCheckBox("Generate XDF")
        self.save_bin = QCheckBox("Extract selected BIN")
        self.keep_settings = QCheckBox("Keep complete project settings (.olsmeta) beside the exports")
        for checkbox in (self.save_xdf, self.save_bin, self.keep_settings):
            checkbox.setChecked(True)
            layout.addWidget(checkbox)
        note = QLabel("Each export asks for a destination. Defaults: definitions/library for XDFs and mappacks/extracted for BINs. The import report lists any definitions omitted from the XDF.")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        layout.addWidget(self.progress)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.export_button = QPushButton("Export…")
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self.export_files)
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.reject)
        buttons.addWidget(self.export_button)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)

    def choose_source(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open WinOLS project", str(output_root() / "mappacks"),
                                             "WinOLS projects (*.ols);;All files (*.*)")
        if path:
            self.source.setText(path)
            self.project = self.conversion = None
            self.version.blockSignals(True)
            self.version.clear()
            self.version.blockSignals(False)
            self._start(path)

    def _start(self, path, project=None, version_index=0):
        self.export_button.setEnabled(False)
        self.browse.setEnabled(False)
        self.version.setEnabled(False)
        self.close_button.setEnabled(False)
        self.progress.show()
        self.summary.setText("Recovering and validating the project…")
        self._worker = _RecoveryWorker(path, project, version_index, self)
        self._worker.finished.connect(self._finished)
        self._worker.start()

    def _finished(self):
        worker = self._worker
        self.progress.hide()
        self.browse.setEnabled(True)
        self.version.setEnabled(True)
        self.close_button.setEnabled(True)
        if worker.error:
            self.conversion = None
            self.summary.setText("The project could not be imported.")
            self.review.setPlainText(worker.error)
        else:
            self.project, self.conversion = worker.project, worker.conversion
            self.version.blockSignals(True)
            self.version.clear()
            for item in self.project.versions:
                self.version.addItem(f"{item.name} — {len(item.data):,} bytes", item.index)
            self.version.setCurrentIndex(worker.version_index)
            self.version.blockSignals(False)
            count = len(self.conversion.workspace.database.definitions)
            skipped = len(self.project.maps) - count
            self.summary.setText(f"{self.project.title}: {len(self.project.maps):,} recovered definitions; {count:,} included in the XDF; {skipped:,} omitted for review.")
            self.review.setPlainText(review_text(self.project, self.conversion, worker.version_index))
            self.export_button.setEnabled(True)
        worker.deleteLater()
        self._worker = None

    def _version_changed(self, index):
        if index >= 0 and self.project is not None and self._worker is None:
            self._start(self.project.source_path, self.project, index)

    def export_files(self):
        if self.project is None or self.conversion is None or self._worker is not None:
            return
        if not self.save_xdf.isChecked() and not self.save_bin.isChecked():
            QMessageBox.information(self, "Import WinOLS project", "Select an XDF or BIN to export.")
            return
        index = self.version.currentIndex()
        default_xdf, default_bin = default_output_paths(self.project, index)
        xdf_path = bin_path = None
        if self.save_xdf.isChecked():
            path, _ = QFileDialog.getSaveFileName(self, "Save generated XDF", str(default_xdf), "TunerPro XDF (*.xdf)")
            if not path:
                return
            xdf_path = Path(path) if Path(path).suffix else Path(path + ".xdf")
        if self.save_bin.isChecked():
            path, _ = QFileDialog.getSaveFileName(self, "Save extracted BIN", str(default_bin), "BIN files (*.bin)")
            if not path:
                return
            bin_path = Path(path) if Path(path).suffix else Path(path + ".bin")
        extras = recovery_output_paths(xdf_path, bin_path, self.keep_settings.isChecked())
        existing = [path for path in extras if path not in (xdf_path, bin_path) and path.exists()]
        if existing and QMessageBox.question(self, "Replace export reports?",
                "Replace these related export files?\n" + "\n".join(str(path) for path in existing),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        try:
            self.exported_paths = export_recovery(self.project, self.conversion, xdf_path=xdf_path,
                bin_path=bin_path, version_index=index, keep_settings=self.keep_settings.isChecked(),
                protected_paths=self.protected_paths)
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", f"Could not finish the export:\n{exc}\n\nCheck the selected folders for any files already saved.")
            return
        self.summary.setText("Export complete. Open the generated XDF and BIN from the File menu.")
        self.review.setPlainText("Saved:\n" + "\n".join(str(path) for path in self.exported_paths))

    def reject(self):
        if self._worker is None:
            super().reject()

    def closeEvent(self, event):
        if self._worker is not None:
            event.ignore()
        else:
            super().closeEvent(event)
