"""XDF authoring and read-only review of potential maps in a BIN."""
from __future__ import annotations

import copy
from dataclasses import replace
import json
from pathlib import Path

import numpy as np
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from PySide6.QtCore import Qt, Signal, QThread, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSpinBox, QSplitter,
    QTableWidget, QTableWidgetItem, QTabWidget, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from .core import AxisDefinition, CalibrationDefinition, EditorError, XDFDatabase, equation_affine, fixed_axis_values, format_number, safe_math
from .bin_analysis import analyze_maps, analysis_report, bin_metadata, candidate_definition
from .xdf_edit import XDFWorkspace


def fit_dialog(dialog, width, height):
    screen = dialog.screen()
    if screen:
        bounds = screen.availableGeometry()
        width, height = min(width, bounds.width() - 60), min(height, bounds.height() - 80)
    dialog.resize(width, height)


def load_bits(combo, value):
    combo.clear(); combo.addItems(["8", "16", "32"])
    if value not in (8, 16, 32):
        combo.addItem(f"{value} (unsupported)")
    combo.setCurrentIndex((8, 16, 32).index(value) if value in (8, 16, 32) else 3)


def load_number(spin, value, low, high):
    # Preserve unusual legacy metadata rather than clamping it during a name edit.
    spin.setRange(min(low, value), max(high, value)); spin.setValue(value)


def address(text: str) -> int:
    try:
        value = int(text.strip(), 0)
    except ValueError as exc:
        raise EditorError("Enter an address in decimal or hexadecimal, for example 0xE13AE.") from exc
    if value < 0:
        raise EditorError("Addresses cannot be negative.")
    return value


def read_values(data: bytes, offset: int, count: int, bits: int, signed: bool, equation: str, endian: str):
    if bits not in (8, 16, 32) or count <= 0 or offset < 0 or offset + count * (bits // 8) > len(data):
        raise EditorError("The definition reads outside the BIN or uses an unsupported integer size.")
    dtype = np.dtype(endian + ("i" if signed else "u") + str(bits // 8))
    raw = np.frombuffer(data, dtype=dtype, count=count, offset=offset).astype(float)
    try:
        scale, offset_value = equation_affine(equation)
        values = raw * scale + offset_value
    except EditorError:
        values = np.asarray([safe_math(equation, float(value)) for value in raw])
    if not np.isfinite(values).all():
        raise EditorError("The equation produces a non-finite value in this BIN.")
    return values


class DefinitionPreview(QWidget):
    """Preview never receives a mutable BIN buffer or an edit engine."""
    def __init__(self, data: bytes | None, parent=None):
        super().__init__(parent)
        self.data = data
        self.definition = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.note = QLabel("Open a BIN to preview definition values.")
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        self.tabs = QTabWidget()
        self.table = QTableWidget()
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.tabs.addTab(self.table, "2D values")
        self.figure = Figure(figsize=(5, 4), tight_layout=True)
        self.canvas = FigureCanvas(self.figure)
        self.tabs.addTab(self.canvas, "3D surface")
        self.hex = QPlainTextEdit()
        self.hex.setReadOnly(True)
        self.hex.setStyleSheet("font-family: Consolas, monospace;")
        self.tabs.addTab(self.hex, "Hex context")
        layout.addWidget(self.tabs, 1)

    def clear(self, message):
        self.definition = None
        self.note.setText(message)
        self.table.clear()
        self.table.setRowCount(0)
        self.table.setColumnCount(0)
        self.figure.clear()
        self.canvas.draw_idle()
        self.hex.clear()

    def set_definition(self, definition: CalibrationDefinition, endian: str):
        if self.data is None:
            self.clear("Open a BIN to preview definition values. XDF editing is available without a BIN.")
            return
        try:
            d = definition
            if d.rows <= 0 or d.cols <= 0 or d.size > 1_000_000:
                raise EditorError("Preview dimensions must be positive and at most 1,000,000 cells.")
            if d.storage_order not in ("row_major", "column_major"):
                raise EditorError(f"Unsupported table storage order: {d.storage_order!r}")
            values = read_values(self.data, d.z_address, d.size, d.z_element_bits, d.z_signed, d.z_equation, endian).reshape(
                d.rows, d.cols, order="F" if d.storage_order == "column_major" else "C")
            axes = []
            labels = []
            for axis, count in ((d.x_axis, d.cols), (d.y_axis, d.rows)):
                if axis is not None and axis.count != count:
                    raise EditorError("Axis lengths must match the table dimensions.")
                axis_values = (read_values(self.data, axis.address, count, axis.element_bits, axis.signed, axis.equation, endian)
                               if axis is not None and axis.address is not None else
                               np.asarray(fixed_axis_values(axis)) if axis is not None else np.arange(count, dtype=float))
                axes.append(axis_values)
                labels.append(axis.labels if axis and axis.labels else [format_number(v, axis.decimal_places if axis else 0) for v in axis_values])
            rows, cols = min(d.rows, 64), min(d.cols, 64)
            self.table.clear()
            self.table.setRowCount(rows)
            self.table.setColumnCount(cols)
            self.table.setHorizontalHeaderLabels(labels[0][:cols])
            self.table.setVerticalHeaderLabels(labels[1][:rows])
            for r in range(rows):
                for c in range(cols):
                    self.table.setItem(r, c, QTableWidgetItem(format_number(float(values[r, c]), d.decimal_places)))
            self.figure.clear()
            ax = self.figure.add_subplot(111, projection="3d")
            xx, yy = np.meshgrid(axes[0][:cols], axes[1][:rows])
            if rows > 1 and cols > 1:
                ax.plot_surface(xx, yy, values[:rows, :cols], cmap="viridis", linewidth=0.3, edgecolor="#29313b")
            else:
                ax.plot(xx.ravel(), yy.ravel(), values[:rows, :cols].ravel())
            ax.set_xlabel(d.x_axis.units if d.x_axis and d.x_axis.units else "X axis" if d.x_axis and d.x_axis.address is not None else "X index")
            ax.set_ylabel(d.y_axis.units if d.y_axis and d.y_axis.units else "Y axis" if d.y_axis and d.y_axis.address is not None else "Y index")
            ax.set_zlabel(d.z_units or "Value")
            self.canvas.draw_idle()
            begin = max(0, (d.z_address // 16) * 16 - 64)
            end = min(len(self.data), begin + 640)
            self.hex.setPlainText("\n".join(f"{i:08X}  " + " ".join(f"{b:02X}" for b in self.data[i:min(i+16, end)])
                                          for i in range(begin, end, 16)))
            self.definition = definition
            subset = f"; showing first {rows} × {cols} cells" if rows != d.rows or cols != d.cols else ""
            self.note.setText(f"Read-only BIN preview · 0x{d.z_address:X} · {d.rows} × {d.cols} · {d.z_element_bits}-bit "
                              f"{'signed' if d.z_signed else 'unsigned'} · {'big' if endian == '>' else 'little'} endian{subset}")
        except Exception as exc:
            self.clear(f"Preview unavailable: {exc}")


class AxisForm(QGroupBox):
    changed = Signal()

    def __init__(self, title, parent=None):
        super().__init__(title, parent)
        form = QFormLayout(self)
        self.source = QComboBox()
        self.source.addItems(["Index / labels", "BIN address"])
        self.address = QLineEdit("0x0")
        self.bits = QComboBox(); self.bits.addItems(["8", "16", "32"])
        self.signed = QCheckBox("Signed integer")
        self.equation = QLineEdit("X")
        self.units = QLineEdit()
        self.decimals = QSpinBox(); self.decimals.setRange(0, 8)
        self.labels = QPlainTextEdit()
        self.labels.setPlaceholderText("Optional labels, one per row; leave empty for indices")
        self.labels.setMaximumHeight(70)
        for label, widget in (("Source", self.source), ("Address", self.address), ("Integer size", self.bits),
                              ("", self.signed), ("Equation", self.equation), ("Units", self.units),
                              ("Decimals", self.decimals), ("Labels", self.labels)):
            form.addRow(label, widget)
        for widget in (self.address, self.equation, self.units):
            widget.textChanged.connect(self.changed)
        self.source.currentIndexChanged.connect(self._source_changed)
        self.bits.currentIndexChanged.connect(self.changed)
        self.signed.toggled.connect(self.changed)
        self.decimals.valueChanged.connect(self.changed)
        self.labels.textChanged.connect(self.changed)
        self._source_changed()
        self.original = None

    def _source_changed(self):
        embedded = self.source.currentIndex() == 1
        for widget in (self.address, self.bits, self.signed, self.equation):
            widget.setEnabled(embedded)
        self.labels.setEnabled(not embedded)
        self.changed.emit()

    def load(self, axis: AxisDefinition | None):
        self.original = axis
        a = axis or AxisDefinition(None, 1, decimal_places=0)
        self.source.setCurrentIndex(1 if a.address is not None else 0)
        self.address.setText(f"0x{a.address or 0:X}")
        load_bits(self.bits, a.element_bits)
        self.signed.setChecked(a.signed)
        self.equation.setText(a.equation)
        self.units.setText(a.units)
        load_number(self.decimals, a.decimal_places, 0, 8)
        self.labels.setPlainText("\n".join(a.labels))
        self._source_changed()
        self.loaded = self.values()

    def values(self):
        return dict(source=self.source.currentIndex(), address=self.address.text(), element_bits=self.bits.currentText(),
                    signed=self.signed.isChecked(), equation=self.equation.text(), units=self.units.text(),
                    decimal_places=self.decimals.value(), labels=self.labels.toPlainText())

    def definition(self, count, dimension_changed=False):
        values = self.values()
        if values == self.loaded and not dimension_changed:
            return copy.deepcopy(self.original)
        embedded = self.source.currentIndex() == 1
        original = self.original or AxisDefinition(None, count, decimal_places=0)
        def value(field, transform=lambda value: value):
            return getattr(original, field) if values[field] == self.loaded[field] else transform(values[field])
        labels = value("labels", lambda text: text.splitlines())
        if not embedded and self.original is None and values == self.loaded:
            return None
        return AxisDefinition(address(self.address.text()) if embedded else None, count,
                              value("element_bits", lambda text: int(text.split()[0])), value("signed"),
                              value("equation", lambda text: text.strip() or "X"), value("units", str.strip),
                              value("decimal_places"), list(labels))


class DefinitionForm(QWidget):
    changed = Signal()

    def __init__(self, categories, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit()
        self.category = QComboBox(); self.category.setEditable(True); self.category.addItems(sorted(set(categories)))
        self.address = QLineEdit()
        self.rows = QSpinBox(); self.rows.setRange(1, 4096)
        self.cols = QSpinBox(); self.cols.setRange(1, 4096)
        self.bits = QComboBox(); self.bits.addItems(["8", "16", "32"])
        self.signed = QCheckBox("Signed integer")
        self.equation = QLineEdit("X"); self.equation.setToolTip("Physical value = equation using raw X, for example X * 0.1 - 40")
        self.units = QLineEdit()
        self.decimals = QSpinBox(); self.decimals.setRange(0, 8)
        self.description = QPlainTextEdit(); self.description.setMinimumHeight(80); self.description.setMaximumHeight(130)
        for label, widget in (("Name", self.name), ("Category", self.category), ("Z address", self.address),
                              ("Rows / Y", self.rows), ("Columns / X", self.cols), ("Integer size", self.bits),
                              ("", self.signed), ("Z equation", self.equation), ("Z units", self.units),
                              ("Decimals", self.decimals), ("Description", self.description)):
            form.addRow(label, widget)
        layout.addLayout(form)
        self.x_axis = AxisForm("X axis · columns")
        self.y_axis = AxisForm("Y axis · rows")
        layout.addWidget(self.x_axis); layout.addWidget(self.y_axis); layout.addStretch()
        for widget in (self.name, self.address, self.equation, self.units):
            widget.textChanged.connect(self.changed)
        self.category.currentTextChanged.connect(self.changed)
        self.bits.currentIndexChanged.connect(self.changed)
        self.signed.toggled.connect(self.changed)
        self.rows.valueChanged.connect(self.changed); self.cols.valueChanged.connect(self.changed)
        self.decimals.valueChanged.connect(self.changed)
        self.description.textChanged.connect(self.changed)
        self.x_axis.changed.connect(self.changed); self.y_axis.changed.connect(self.changed)
        self.original = None

    def load(self, d: CalibrationDefinition):
        self.original = d
        self.name.setText(d.title); self.category.setCurrentText(d.category)
        self.address.setText(f"0x{d.z_address:X}")
        load_number(self.rows, d.rows, 1, 4096); load_number(self.cols, d.cols, 1, 4096)
        constant = d.kind == "constant"
        self.rows.setEnabled(not constant); self.cols.setEnabled(not constant)
        self.x_axis.setVisible(not constant); self.y_axis.setVisible(not constant)
        load_bits(self.bits, d.z_element_bits); self.signed.setChecked(d.z_signed)
        self.equation.setText(d.z_equation); self.units.setText(d.z_units); load_number(self.decimals, d.decimal_places, 0, 8)
        self.description.setPlainText(d.description)
        self.x_axis.load(d.x_axis); self.y_axis.load(d.y_axis)
        self.loaded = self.values()

    def values(self):
        return dict(title=self.name.text(), category=self.category.currentText(), description=self.description.toPlainText(),
                    z_address=self.address.text(), rows=self.rows.value(), cols=self.cols.value(),
                    z_element_bits=self.bits.currentText(), z_signed=self.signed.isChecked(),
                    z_equation=self.equation.text(), z_units=self.units.text(), decimal_places=self.decimals.value())

    def definition(self):
        if self.original is None:
            raise EditorError("Select or create a definition first.")
        d = self.original
        values = self.values()
        def value(field, transform=lambda value: value):
            return getattr(d, field) if values[field] == self.loaded[field] else transform(values[field])
        rows, cols = (value("rows"), value("cols"))
        category = value("category", lambda text: text.strip() or "Uncategorized")
        return replace(d, title=value("title", str.strip), category=category,
                       categories=d.categories if category == d.category else [category],
                       description=value("description"), z_address=value("z_address", address),
                       rows=rows, cols=cols, z_element_bits=value("z_element_bits", lambda text: int(text.split()[0])),
                       z_signed=value("z_signed"), z_equation=value("z_equation", lambda text: text.strip() or "X"),
                       z_units=value("z_units", str.strip), decimal_places=value("decimal_places"),
                       x_axis=None if d.kind == "constant" else self.x_axis.definition(cols, cols != d.cols),
                       y_axis=None if d.kind == "constant" else self.y_axis.definition(rows, rows != d.rows))


class XDFEditorDialog(QDialog):
    def __init__(self, database: XDFDatabase | None, data: bytes | None = None, parent=None, *, seeds=(), current_id=None):
        super().__init__(parent)
        self.setWindowTitle("XDF definition editor")
        fit_dialog(self, 1280, 850)
        self.workspace = XDFWorkspace(database, len(data) if data is not None else None)
        self.applied_database = None
        self._saved_database = None
        self.current_id = None
        self._loading = True
        self._draft_dirty = False
        self._header_dirty = False
        for definition in seeds:
            added = self.workspace.add(definition)
            current_id = added.unique_id
        layout = QVBoxLayout(self)
        intro = QLabel("Edit a working copy of the definitions. Save an XDF, then use it in the editor. BIN previews are read-only.")
        intro.setWordWrap(True); layout.addWidget(intro)
        split = QSplitter(Qt.Horizontal)
        left = QWidget(); ll = QVBoxLayout(left); ll.setContentsMargins(0, 0, 0, 0)
        self.search = QLineEdit(); self.search.setPlaceholderText("Find name, category or address…")
        ll.addWidget(self.search)
        self.listing = QTreeWidget(); self.listing.setHeaderLabels(["Definition", "Address", "Size"])
        self.listing.setRootIsDecorated(False)
        self.listing.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.listing.header().setSectionResizeMode(1, QHeaderView.Fixed); self.listing.setColumnWidth(1, 82)
        self.listing.header().setSectionResizeMode(2, QHeaderView.Fixed); self.listing.setColumnWidth(2, 72)
        ll.addWidget(self.listing, 1)
        buttons = QHBoxLayout()
        for title, kind in (("+ Table", "table"), ("+ Constant", "constant")):
            button = QPushButton(title); button.clicked.connect(lambda _=False, k=kind: self.new_definition(k)); buttons.addWidget(button)
        ll.addLayout(buttons)
        buttons = QHBoxLayout()
        duplicate = QPushButton("Duplicate"); duplicate.clicked.connect(self.duplicate_definition)
        remove = QPushButton("Remove"); remove.clicked.connect(self.remove_definition)
        buttons.addWidget(duplicate); buttons.addWidget(remove); ll.addLayout(buttons)
        suspect = QPushButton("Find suspected tables…"); suspect.setEnabled(data is not None)
        suspect.clicked.connect(self.find_maps); ll.addWidget(suspect)
        left.setMinimumWidth(230); split.addWidget(left)
        center = QWidget(); cl = QVBoxLayout(center); cl.setContentsMargins(0, 0, 0, 0)
        self.edit_tabs = QTabWidget()
        self.form = DefinitionForm(self.workspace.database.categories.values())
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(self.form)
        self.edit_tabs.addTab(scroll, "Definition")
        header = QWidget(); hl = QFormLayout(header)
        self.header_title = QLineEdit(self.workspace.database.title)
        self.header_author = QLineEdit(self.workspace.database.root.findtext("./XDFHEADER/author") or "")
        self.header_description = QPlainTextEdit(self.workspace.database.root.findtext("./XDFHEADER/description") or "")
        self.header_endian = QComboBox(); self.header_endian.addItems(["Big endian", "Little endian"])
        self.header_endian.setCurrentIndex(1 if self.workspace.database.default_lsb_first else 0)
        for label, widget in (("XDF title", self.header_title), ("Author", self.header_author),
                              ("Description", self.header_description), ("Integer byte order", self.header_endian)):
            hl.addRow(label, widget)
        note = QLabel("Byte order applies to all integer definitions in this XDF. Addresses are offsets in the BIN.")
        note.setWordWrap(True); hl.addRow(note)
        self.edit_tabs.addTab(header, "XDF properties")
        cl.addWidget(self.edit_tabs, 1)
        buttons = QHBoxLayout()
        self.apply_button = QPushButton("Apply definition"); self.apply_button.clicked.connect(self.commit_current)
        revert = QPushButton("Revert form"); revert.clicked.connect(self.revert_form)
        buttons.addWidget(self.apply_button); buttons.addWidget(revert); cl.addLayout(buttons)
        center.setMinimumWidth(315); split.addWidget(center)
        self.preview = DefinitionPreview(data); self.preview.setMinimumWidth(300); split.addWidget(self.preview)
        split.setSizes([330, 360, 580]); layout.addWidget(split, 1)
        self.status = QLabel(); self.status.setWordWrap(True); layout.addWidget(self.status)
        buttons = QHBoxLayout()
        self.save_button = QPushButton("Save XDF as…"); self.save_button.clicked.connect(self.save_xdf)
        self.use_button = QPushButton("Use saved XDF"); self.use_button.clicked.connect(self.use_saved)
        close = QPushButton("Close"); close.clicked.connect(self.reject)
        buttons.addWidget(self.save_button); buttons.addStretch(); buttons.addWidget(self.use_button); buttons.addWidget(close)
        layout.addLayout(buttons)
        self._preview_timer = QTimer(self); self._preview_timer.setSingleShot(True); self._preview_timer.setInterval(180)
        self._preview_timer.timeout.connect(self.update_preview)
        self.form.changed.connect(self.fields_changed)
        self.header_title.textChanged.connect(self.header_changed); self.header_author.textChanged.connect(self.header_changed)
        self.header_description.textChanged.connect(self.header_changed); self.header_endian.currentIndexChanged.connect(self.header_changed)
        self.search.textChanged.connect(self.filter_list)
        self.listing.currentItemChanged.connect(self.select_definition)
        self._loading = False
        self.refresh_list(current_id)
        self.update_status()

    def refresh_list(self, selected_id=None):
        self.listing.blockSignals(True); self.listing.clear()
        selected = None
        for d in sorted(self.workspace.database.definitions, key=lambda d: (d.category.casefold(), d.title.casefold(), d.z_address)):
            item = QTreeWidgetItem([d.title, f"0x{d.z_address:X}", f"{d.rows} × {d.cols}"])
            item.setData(0, Qt.UserRole, d.unique_id)
            item.setToolTip(0, f"{d.title}\n{d.category}\n{d.description}")
            self.listing.addTopLevelItem(item)
            if d.unique_id == selected_id:
                selected = item
        self.listing.blockSignals(False)
        self.filter_list()
        self.current_id = None
        self._draft_dirty = False
        self.listing.setCurrentItem(selected or self.listing.topLevelItem(0))
        if self.listing.currentItem() is None:
            self.form.setEnabled(False)
            self.preview.clear("Create a table or constant to begin.")

    def filter_list(self, *_):
        needle = self.search.text().casefold()
        for i in range(self.listing.topLevelItemCount()):
            item = self.listing.topLevelItem(i)
            item.setHidden(bool(needle and needle not in (item.text(0) + item.text(1) + item.toolTip(0)).casefold()))

    def select_definition(self, item, previous=None):
        if item is None or self._loading:
            return
        if self._draft_dirty and not self.commit_current(refresh=False):
            self.listing.blockSignals(True); self.listing.setCurrentItem(previous); self.listing.blockSignals(False)
            return
        self.current_id = item.data(0, Qt.UserRole)
        d = next(d for d in self.workspace.database.definitions if d.unique_id == self.current_id)
        self._loading = True; self.form.setEnabled(True); self.form.load(d); self._loading = False
        self._draft_dirty = False
        self.update_preview(); self.update_status()

    def fields_changed(self):
        if not self._loading:
            self._draft_dirty = True; self.update_status(); self._preview_timer.start()

    def header_changed(self, *_):
        if not self._loading:
            self._header_dirty = True; self.update_status(); self._preview_timer.start()

    def update_preview(self):
        if self.current_id:
            try:
                d = self.form.definition()
                self.preview.set_definition(d, "<" if self.header_endian.currentIndex() else ">")
            except Exception as exc:
                self.preview.clear(f"Preview unavailable: {exc}")

    def update_status(self, message=None, error=False):
        dirty = self.workspace.dirty or self._draft_dirty or self._header_dirty
        self.use_button.setEnabled(self._saved_database is not None and not dirty)
        self.apply_button.setEnabled(self.current_id is not None and self._draft_dirty)
        self.status.setStyleSheet("color:#b42318;" if error else "")
        self.status.setText(message or ("Unsaved definition changes · BIN bytes unchanged" if dirty else
                                       f"{len(self.workspace.database.definitions)} definitions · BIN bytes unchanged"))

    def commit_current(self, *_args, refresh=True):
        if not self.current_id or not self._draft_dirty:
            return True
        try:
            updated = self.workspace.update(self.current_id, self.form.definition())
        except Exception as exc:
            self.update_status(str(exc), error=True); return False
        current = self.current_id
        self._draft_dirty = False
        if refresh:
            self.refresh_list(current)
        else:
            for i in range(self.listing.topLevelItemCount()):
                item = self.listing.topLevelItem(i)
                if item.data(0, Qt.UserRole) == current:
                    item.setText(0, updated.title); item.setText(1, f"0x{updated.z_address:X}")
                    item.setText(2, f"{updated.rows} × {updated.cols}")
                    item.setToolTip(0, f"{updated.title}\n{updated.category}\n{updated.description}")
            self.filter_list()
        self.update_status(); return True

    def commit_header(self):
        if not self._header_dirty:
            return True
        try:
            self.workspace.set_metadata(self.header_title.text(), self.header_author.text(),
                                        self.header_description.toPlainText(), bool(self.header_endian.currentIndex()))
            self._header_dirty = False
            return True
        except Exception as exc:
            self.update_status(str(exc), error=True); return False

    def revert_form(self):
        if self.current_id:
            self._draft_dirty = False
            self.select_definition(self.listing.currentItem())

    def new_definition(self, kind):
        if not self.commit_current():
            return
        d = CalibrationDefinition("New constant" if kind == "constant" else "New table", "", "User definitions",
                                  0, 1, 1, kind=kind)
        try:
            added = self.workspace.add(d); self.refresh_list(added.unique_id); self.update_status()
        except Exception as exc:
            self.update_status(str(exc), error=True)

    def duplicate_definition(self):
        if self.current_id and self.commit_current():
            try:
                added = self.workspace.duplicate(self.current_id); self.refresh_list(added.unique_id); self.update_status()
            except Exception as exc:
                self.update_status(str(exc), error=True)

    def remove_definition(self):
        if self.current_id:
            self.workspace.remove(self.current_id)
            self._draft_dirty = False
            self.refresh_list(); self.update_status()

    def find_maps(self):
        if self.preview.data is None or not self.commit_current() or not self.commit_header():
            return
        dialog = PotentialMapsDialog(self.preview.data, self.workspace.database, self)
        if dialog.exec() == QDialog.Accepted:
            current = self.current_id
            for d in dialog.selected_definitions:
                added = self.workspace.add(d); current = added.unique_id
            self.refresh_list(current); self.update_status()

    def save_xdf(self, *_args, path=None):
        if not self.commit_current() or not self.commit_header():
            return False
        if path is None:
            source = self.workspace.database.path
            default = source.with_name(source.stem + "_edited.xdf")
            path, _ = QFileDialog.getSaveFileName(self, "Save XDF definitions", str(default), "TunerPro XDF (*.xdf)")
        if not path:
            return False
        try:
            self._saved_database = self.workspace.save_as(path)
            self.update_status(f"Saved XDF: {Path(path).name} · Ready to use · BIN bytes unchanged")
            return True
        except Exception as exc:
            self.update_status(f"Could not save XDF: {exc}", error=True); return False

    def use_saved(self):
        if self._saved_database is not None and not (self.workspace.dirty or self._draft_dirty or self._header_dirty):
            self.applied_database = self._saved_database
            super().accept()

    def reject(self):
        if self.workspace.dirty or self._draft_dirty or self._header_dirty:
            answer = QMessageBox.question(self, "Unsaved XDF changes", "Discard the unsaved definition changes?",
                                          QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return
        super().reject()


class MapScanThread(QThread):
    resultReady = Signal(object)
    failed = Signal(str)
    progress = Signal(int)

    def __init__(self, data, options, parent=None):
        super().__init__(parent)
        self.data = data; self.options = options

    def run(self):
        try:
            candidates = analyze_maps(self.data, **self.options, progress=self.progress.emit, cancelled=self.isInterruptionRequested)
            self.resultReady.emit(candidates)
        except Exception as exc:
            self.failed.emit(str(exc))


class PotentialMapsDialog(QDialog):
    def __init__(self, data: bytes, database: XDFDatabase | None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Find suspected tables")
        fit_dialog(self, 1200, 780)
        self.data = data; self.database = database
        self.source_metadata = bin_metadata(data)
        self.candidates = []; self.selected_definitions = []
        self._interpretation_choices = {}
        self.worker = None; self._closing = False; self._cancel_requested = False
        layout = QVBoxLayout(self)
        note = QLabel("Find table structures and compare scaling with the EDC16 definition library. "
                      "Family and scaling suggestions remain hypotheses for this BIN. Scores rank evidence, not probability.")
        note.setWordWrap(True); layout.addWidget(note)
        identity = sorted(self.source_metadata['identification_evidence'],
                          key=lambda item: (not item['text'].startswith('BOSCH '), -len(item['text'])))
        fingerprint = QLabel(f"BIN snapshot: {len(data):,} bytes · SHA-256 {self.source_metadata['sha256'][:16]}" +
                             (f" · {identity[0]['text']}" if identity else ""))
        fingerprint.setWordWrap(True); fingerprint.setToolTip(json.dumps(self.source_metadata, indent=2))
        layout.addWidget(fingerprint)
        controls = QHBoxLayout()
        self.start = QLineEdit("0x0"); self.end = QLineEdit(f"0x{len(data):X}")
        self.endian = QComboBox(); self.endian.addItems(["Big endian", "Little endian"])
        self.endian.setCurrentIndex(1 if database and database.default_lsb_first else 0)
        self.include_known = QCheckBox("Include defined regions")
        self.scan_button = QPushButton("Scan BIN"); self.scan_button.clicked.connect(self.start_scan)
        self.cancel_button = QPushButton("Cancel scan"); self.cancel_button.clicked.connect(self.cancel_scan); self.cancel_button.setEnabled(False)
        for label, widget in (("Start", self.start), ("End (exclusive)", self.end), ("Byte order", self.endian)):
            controls.addWidget(QLabel(label)); controls.addWidget(widget)
        controls.addWidget(self.include_known); controls.addWidget(self.scan_button); controls.addWidget(self.cancel_button)
        layout.addLayout(controls)
        self.progress = QProgressBar(); self.progress.setRange(0, 100); layout.addWidget(self.progress)
        split = QSplitter(Qt.Horizontal)
        left = QWidget(); ll = QVBoxLayout(left); ll.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.search = QLineEdit(); self.search.setPlaceholderText("Filter address or evidence…")
        self.min_score = QSpinBox(); self.min_score.setRange(0, 100); self.min_score.setValue(0)
        self.family_filter = QComboBox(); self.family_filter.addItems(["All families", "With family suggestion", "Unclassified", "Boost target", "N75 / VNT duty", "Main SOI", "Driver's wish"])
        row.addWidget(self.search, 1); row.addWidget(QLabel("Structure score")); row.addWidget(self.min_score)
        ll.addLayout(row)
        ll.addWidget(self.family_filter)
        self.results = QTreeWidget(); self.results.setHeaderLabels(["Z address", "Size", "Structure", "Family / rank", "Value scaling"])
        self.results.setRootIsDecorated(False); self.results.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.results.header().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.results.header().setStretchLastSection(True)
        ll.addWidget(self.results, 1)
        self.evidence = QPlainTextEdit(); self.evidence.setReadOnly(True); self.evidence.setMaximumHeight(170)
        ll.addWidget(self.evidence)
        interpretation = QHBoxLayout()
        self.interpretation = QComboBox()
        self.interpretation.setToolTip("Choose a family and scaling suggestion; alternatives show their equations and source evidence.")
        self.use_interpretation = QCheckBox("Use suggested scaling")
        self.use_interpretation.setToolTip("Preview and add this table with the selected scaling. Clear for raw integers; SOI keeps signed storage. Each table retains its own choice.")
        interpretation.addWidget(self.interpretation, 1); interpretation.addWidget(self.use_interpretation)
        ll.addLayout(interpretation)
        self.scaling_summary = QLabel("Select a table to inspect its scaling.")
        self.scaling_summary.setWordWrap(True)
        self.scaling_summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        ll.addWidget(self.scaling_summary)
        split.addWidget(left)
        self.preview = DefinitionPreview(data); split.addWidget(self.preview); split.setSizes([500, 700])
        layout.addWidget(split, 1)
        self.status = QLabel("Ready to scan."); self.status.setWordWrap(True); layout.addWidget(self.status)
        buttons = QHBoxLayout()
        self.report_button = QPushButton("Save findings…"); self.report_button.setEnabled(False)
        self.report_button.clicked.connect(self.save_findings)
        self.add_button = QPushButton("Add selected to XDF…"); self.add_button.setEnabled(False); self.add_button.clicked.connect(self.accept_candidates)
        close = QPushButton("Close"); close.clicked.connect(self.reject)
        buttons.addWidget(self.report_button); buttons.addStretch(); buttons.addWidget(self.add_button); buttons.addWidget(close); layout.addLayout(buttons)
        self.search.textChanged.connect(self.filter_results); self.min_score.valueChanged.connect(self.filter_results)
        self.family_filter.currentTextChanged.connect(self.filter_results)
        self.interpretation.currentIndexChanged.connect(self.update_candidate_preview)
        self.use_interpretation.toggled.connect(self.update_candidate_preview)
        self.results.currentItemChanged.connect(self.select_candidate)
        self.results.itemSelectionChanged.connect(self.update_add_button)
        self.results.itemDoubleClicked.connect(lambda *_: self.accept_candidates())
        QTimer.singleShot(0, self.start_scan)

    def start_scan(self):
        if self._closing or self.worker is not None:
            return
        try:
            begin, end = address(self.start.text()), address(self.end.text())
            if not (0 <= begin < end <= len(self.data)):
                raise EditorError("Scan range must lie inside the BIN, with start before end.")
        except EditorError as exc:
            self.status.setText(str(exc)); return
        self.candidates = []; self.results.clear(); self.update_add_button()
        self._interpretation_choices.clear()
        self.report_button.setEnabled(False)
        self.progress.setValue(0); self.status.setText("Searching for table structures…")
        self._cancel_requested = False
        self.scan_button.setEnabled(False); self.cancel_button.setEnabled(True)
        for widget in (self.start, self.end, self.endian, self.include_known):
            widget.setEnabled(False)
        self.scan_endian = "<" if self.endian.currentIndex() else ">"
        options = dict(endian=self.scan_endian, start=begin, end=end,
                       known=self.database.definitions if self.database else (), include_known=self.include_known.isChecked())
        worker = MapScanThread(self.data, options, self); self.worker = worker
        worker.resultReady.connect(self.show_results); worker.failed.connect(lambda message: self.status.setText("Scan failed: " + message))
        worker.progress.connect(self.progress.setValue); worker.finished.connect(self.scan_finished)
        worker.start()

    def cancel_scan(self):
        if self.worker is not None:
            self._cancel_requested = True
            self.worker.requestInterruption(); self.status.setText("Stopping scan…")

    def show_results(self, candidates):
        self.candidates = sorted(candidates, key=lambda c: (not bool(c.suggestions),
            -(c.suggestions[0].score if c.suggestions else 0), -c.score, c.address))
        for index, candidate in enumerate(self.candidates):
            family = candidate.suggestions[0] if candidate.suggestions else None
            item = QTreeWidgetItem([f"0x{candidate.address:X}", f"{candidate.rows} × {candidate.cols}",
                                   str(candidate.score), f"{family.family} · {family.score}" if family else "Unclassified",
                                   self.scaling_text(family) if family else "Raw integers"])
            item.setData(0, Qt.UserRole, index)
            item.setToolTip(0, candidate.layout + (" · defined overlap" if candidate.known else "") + '\n' + "\n".join(candidate.reasons))
            if family:
                item.setToolTip(4, self.suggestion_details(family))
            self.results.addTopLevelItem(item)
        self.filter_results()
        labelled = sum(bool(c.suggestions) for c in candidates)
        learned = sum(any(getattr(s, 'scaling_profile_count', 0) for s in c.suggestions) for c in candidates)
        self.status.setText(f"{len(candidates)} structures · {labelled} with family suggestions · {learned} with library scaling evidence · Up to 2,000 results")
        visible = next((self.results.topLevelItem(i) for i in range(self.results.topLevelItemCount())
                        if not self.results.topLevelItem(i).isHidden()), None)
        if visible:
            self.results.setCurrentItem(visible)
        else:
            self.preview.clear("No candidates match the current filters." if candidates else "No matching table structures found in this range.")

    def scan_finished(self):
        worker = self.worker
        interrupted = self._cancel_requested
        self.worker = None
        if worker:
            worker.deleteLater()
        self.scan_button.setEnabled(True); self.cancel_button.setEnabled(False)
        for widget in (self.start, self.end, self.endian, self.include_known):
            widget.setEnabled(True)
        if interrupted:
            self.status.setText(f"Scan cancelled · {len(self.candidates)} partial results")
        self.update_add_button()
        self.report_button.setEnabled(bool(self.candidates))
        if self._closing:
            super().reject()

    def filter_results(self, *_):
        needle = self.search.text().casefold()
        family_filter = self.family_filter.currentText()
        for i in range(self.results.topLevelItemCount()):
            item = self.results.topLevelItem(i); candidate = self.candidates[item.data(0, Qt.UserRole)]
            families = [s.family for s in candidate.suggestions]
            if candidate.suggestions:
                suggestion = candidate.suggestions[self.display_suggestion_index(candidate)]
                item.setText(3, f"{suggestion.family} · {suggestion.score}")
                item.setText(4, self.scaling_text(suggestion))
                item.setToolTip(4, self.suggestion_details(suggestion))
            family_match = (family_filter == "All families" or family_filter == "With family suggestion" and bool(families) or
                            family_filter == "Unclassified" and not families or family_filter in families)
            item.setHidden(candidate.score < self.min_score.value() or
                           not family_match or bool(needle and needle not in
                           (" ".join(item.text(j) for j in range(5)) + " ".join(candidate.reasons) + " ".join(families) +
                            " ".join(self.suggestion_details(s) for s in candidate.suggestions)).casefold()))
        current = self.results.currentItem()
        if current is None or current.isHidden():
            visible = next((self.results.topLevelItem(i) for i in range(self.results.topLevelItemCount())
                            if not self.results.topLevelItem(i).isHidden()), None)
            self.results.clearSelection()
            self.results.setCurrentItem(visible)
            if visible is None:
                self.select_candidate(None)
        else:
            self.select_candidate(current)
        self.update_add_button()

    def select_candidate(self, item, *_):
        if item:
            c = self.candidates[item.data(0, Qt.UserRole)]
            self.interpretation.blockSignals(True); self.interpretation.clear()
            for suggestion in c.suggestions:
                count = getattr(suggestion, 'scaling_profile_count', 0)
                origin = f"{count} software profiles" if count else "heuristic"
                load_units = suggestion.x_units if suggestion.y_units == 'rpm' else suggestion.y_units
                self.interpretation.addItem(f"{suggestion.family} · {self.scaling_text(suggestion)} · {load_units} · {'signed' if suggestion.z_signed else 'unsigned'} · {origin} · rank {suggestion.score}")
                self.interpretation.setItemData(self.interpretation.count() - 1, self.suggestion_details(suggestion), Qt.ToolTipRole)
            index, scaled = self.candidate_choice(c)
            self.interpretation.setCurrentIndex(min(index, max(0, len(c.suggestions) - 1)))
            self.interpretation.blockSignals(False)
            self.interpretation.setEnabled(bool(c.suggestions)); self.use_interpretation.setEnabled(bool(c.suggestions))
            self.use_interpretation.blockSignals(True)
            self.use_interpretation.setChecked(bool(c.suggestions) and scaled)
            self.use_interpretation.blockSignals(False)
            self.update_candidate_preview()
        else:
            self.interpretation.blockSignals(True); self.interpretation.clear(); self.interpretation.blockSignals(False)
            self.interpretation.setEnabled(False); self.use_interpretation.setEnabled(False)
            self.evidence.clear()
            self.scaling_summary.setText("Select a table to inspect its scaling.")
            self.scaling_summary.setToolTip('')
            self.preview.clear('No candidates match the current filters.')

    @staticmethod
    def candidate_key(candidate):
        return candidate.address, candidate.rows, candidate.cols, candidate.x_address, candidate.y_address

    @staticmethod
    def default_scaled(candidate, index=0):
        return bool(candidate.suggestions and getattr(candidate.suggestions[index], 'scaling_profile_count', 0))

    def display_suggestion_index(self, candidate):
        family = self.family_filter.currentText()
        return next((i for i, suggestion in enumerate(candidate.suggestions) if suggestion.family == family), 0)

    def candidate_choice(self, candidate):
        default_index = self.display_suggestion_index(candidate)
        choice = self._interpretation_choices.get(self.candidate_key(candidate))
        if choice is None:
            return default_index, self.default_scaled(candidate, default_index)
        index, scaled = choice
        family = self.family_filter.currentText()
        if any(s.family == family for s in candidate.suggestions) and candidate.suggestions[index].family != family:
            index = default_index
        return index, scaled

    @staticmethod
    def scaling_text(suggestion):
        return suggestion.z_equation + (' ' + suggestion.z_units if suggestion.z_units else '')

    @staticmethod
    def suggestion_details(suggestion):
        sources = getattr(suggestion, 'scaling_sources', [])
        return (f"Values: {suggestion.z_equation} [{suggestion.z_units or 'raw'}]\n"
                f"X axis: {suggestion.x_equation} [{suggestion.x_units}]\n"
                f"Y axis: {suggestion.y_equation} [{suggestion.y_units}]\n" +
                '\n'.join(sources) + '\n' + '\n'.join(suggestion.reasons))

    def update_candidate_preview(self, *_):
        item = self.results.currentItem()
        if item is None or item.isHidden():
            return
        c = self.candidates[item.data(0, Qt.UserRole)]
        index = max(0, self.interpretation.currentIndex())
        self._interpretation_choices[self.candidate_key(c)] = index, self.use_interpretation.isChecked()
        definition = candidate_definition(c, interpreted=self.use_interpretation.isChecked() and bool(c.suggestions),
                                          suggestion_index=index, source_sha256=self.source_metadata['sha256'])
        if c.suggestions:
            suggestion = c.suggestions[index]
            count = getattr(suggestion, 'scaling_profile_count', 0)
            origin = f"Supported by {count} reference software profiles; transfer to this BIN remains a hypothesis." if count else "Heuristic suggestion; no compatible library evidence."
            mode = "Scaled preview" if self.use_interpretation.isChecked() else "Raw preview (X)"
            self.scaling_summary.setText(f"Suggested values: {suggestion.z_equation} [{suggestion.z_units or 'raw'}] · {'signed' if suggestion.z_signed else 'unsigned'} {c.bits}-bit\n"
                f"X: {suggestion.x_equation} [{suggestion.x_units}] · Y: {suggestion.y_equation} [{suggestion.y_units}]\n{mode} · {origin}")
            self.scaling_summary.setToolTip(self.suggestion_details(suggestion))
        else:
            self.scaling_summary.setText("No supported scaling suggestion. Preview uses raw integers.")
        scale_evidence = self.suggestion_details(c.suggestions[index]) + '\n\n' if c.suggestions else ''
        self.evidence.setPlainText(scale_evidence + f"X axis: 0x{c.x_address:X}\nY axis: 0x{c.y_address:X}\n" + "\n".join(c.reasons))
        self.preview.set_definition(definition, self.scan_endian)

    def save_findings(self, *_args, path=None):
        if self.worker is not None:
            return False
        if path is None:
            path, _ = QFileDialog.getSaveFileName(self, "Save BIN analysis findings", "map-findings.json", "JSON report (*.json)")
        if not path:
            return False
        if Path(path).suffix.casefold() != '.json':
            self.status.setText("Save findings as a .json report; BIN bytes must remain separate.")
            return False
        try:
            report = analysis_report(self.data, self.candidates, endian=self.scan_endian)
            report['partial_scan'] = self._cancel_requested
            Path(path).write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
            self.status.setText(f"Saved findings: {Path(path).name}")
            return True
        except Exception as exc:
            self.status.setText(f"Could not save findings: {exc}"); return False

    def update_add_button(self):
        self.add_button.setEnabled(self.worker is None and any(not item.isHidden() for item in self.results.selectedItems()))

    def accept_candidates(self):
        selected = [self.candidates[item.data(0, Qt.UserRole)] for item in self.results.selectedItems() if not item.isHidden()]
        if not selected or self.worker is not None:
            return
        target_endian = "<" if self.database and self.database.default_lsb_first else ">"
        if self.scan_endian != target_endian:
            self.status.setText("The scan byte order differs from this XDF. Change XDF properties or scan using its byte order before adding.")
            return
        self.selected_definitions = []
        for c in selected:
            index, scaled = self.candidate_choice(c)
            self.selected_definitions.append(candidate_definition(c, interpreted=scaled and bool(c.suggestions),
                suggestion_index=index, source_sha256=self.source_metadata['sha256']))
        super().accept()

    def reject(self):
        self._closing = True
        if self.worker is not None:
            self.cancel_scan()
            return
        super().reject()
