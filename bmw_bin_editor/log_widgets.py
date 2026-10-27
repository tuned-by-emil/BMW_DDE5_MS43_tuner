"""Channel configuration and a compact instrument strip for log review."""
from __future__ import annotations

import copy
import math

import numpy as np
from PySide6.QtCore import Qt, QSize, Signal, QMimeData
from PySide6.QtGui import QColor, QDrag
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QColorDialog, QComboBox, QDialog,
    QDialogButtonBox, QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QMenu, QMessageBox, QPushButton, QLineEdit,
    QScrollArea, QSpinBox, QSplitter, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget, QHeaderView, QLayout,
)
from .logdata import FuelEstimateConfig

PALETTE = ("#0079bd", "#c83d38", "#23863f", "#a438bd", "#bd7700", "#007f83", "#5b58ba", "#a34570")


def channel_style(styles, signal, index=0):
    defaults = dict(color=PALETTE[index % len(PALETTE)], auto=True,
                    zero=0.0, full=100.0, lower=0.0, upper=100.0)
    stored = styles.get(signal, {})
    if isinstance(stored, dict):
        defaults.update(stored)
    try:
        for key in ("zero", "full", "lower", "upper"):
            defaults[key] = float(defaults[key])
        if (not all(math.isfinite(defaults[k]) for k in ("zero", "full", "lower", "upper"))
                or not 0 <= defaults["lower"] < defaults["upper"] <= 100
                or (not defaults["auto"] and defaults["full"] <= defaults["zero"])):
            raise ValueError("Invalid channel range")
    except (ValueError, TypeError):
        defaults.update(auto=True, zero=0.0, full=100.0, lower=0.0, upper=100.0)
    if not QColor(str(defaults["color"])).isValid():
        defaults["color"] = PALETTE[index % len(PALETTE)]
    return defaults


def scale_channel(values, style):
    """Map engineering values into the configured percent of a graph area."""
    finite = values[np.isfinite(values)]
    if style["auto"] and len(finite):
        low, high = float(finite.min()), float(finite.max())
        pad = (high - low) * 0.05 if high > low else max(abs(low) * 0.05, 1.0)
        low, high = low - pad, high + pad
    else:
        low, high = style["zero"], style["full"]
    return (style["lower"] + (values - low) / (high - low)
            * (style["upper"] - style["lower"])), (low, high)


def reading_text(value):
    if not math.isfinite(value):
        return "—"
    if abs(value) >= 100:
        return f"{value:.0f}"
    return f"{value:.1f}" if abs(value) >= 10 else f"{value:.2f}"


class FuelEstimateDialog(QDialog):
    """Owner-adjustable assumptions for calculated fuel readings."""

    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Fuel estimates")
        self.resize(450, 240)
        layout = QVBoxLayout(self)
        description = QLabel("Estimate fuel flow from ECU total injection quantity and RPM. "
                             "These readings use a four-stroke engine and are calculated, rather than independently measured.")
        description.setWordWrap(True)
        layout.addWidget(description)
        form = QFormLayout()
        self.cylinders = QSpinBox()
        self.cylinders.setRange(1, 16)
        self.cylinders.setValue(config.cylinders)
        form.addRow("Cylinders:", self.cylinders)
        self.density = QDoubleSpinBox()
        self.density.setRange(0.700, 1.000)
        self.density.setDecimals(3)
        self.density.setSingleStep(0.005)
        self.density.setSuffix(" kg/L")
        self.density.setValue(config.density_kg_l)
        self.density.setToolTip("Diesel density used to convert fuel mass into litres; the default is 0.835 kg/L.")
        form.addRow("Diesel density:", self.density)
        layout.addLayout(form)
        note = QLabel("Use total IQ in mg per cylinder per cycle; pilot quantity is not added again. "
                      f"L/100 km is hidden below {config.min_speed_kmh:g} km/h. "
                      "Changes apply to this log, live readings and future sessions.")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.minimum_speed_kmh = config.min_speed_kmh
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def config(self):
        return FuelEstimateConfig(cylinders=self.cylinders.value(), density_kg_l=self.density.value(),
                                  min_speed_kmh=self.minimum_speed_kmh)


class GraphReadings(QWidget):
    """Fixed-height channel rows; overflow scrolls instead of overlapping traces."""

    def __init__(self, area, channels, label_for, parent=None, *, tooltip_for=None):
        super().__init__(parent)
        self.channels = tuple(channels)
        self.tooltip_for = tooltip_for or (lambda _signal: "")
        self.setStyleSheet("GraphReadings {background:#f3f5f7;}"
                           "QTreeWidget {background:#f3f5f7; border:0; font-size:12px;}"
                           "QTreeWidget::item {height:22px;}"
                           "QLabel {background:#f3f5f7; color:#657080; font-size:11px;}")
        layout = QVBoxLayout(self)
        layout.setSizeConstraint(QLayout.SetNoConstraint)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        heading = QLabel(f"Area {area}")
        heading.setFixedHeight(16)
        heading.setToolTip("Each trace uses its own scale. * marks a held value; hover for its age.")
        layout.addWidget(heading)
        self.listing = QTreeWidget()
        self.listing.setColumnCount(2)
        self.listing.setHeaderHidden(True)
        self.listing.setRootIsDecorated(False)
        self.listing.setIndentation(0)
        self.listing.setUniformRowHeights(True)
        self.listing.setSelectionMode(QAbstractItemView.NoSelection)
        self.listing.setFocusPolicy(Qt.NoFocus)
        self.listing.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.listing.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.listing.header().setStretchLastSection(False)
        self.listing.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.listing.header().setSectionResizeMode(1, QHeaderView.Fixed)
        self.listing.setColumnWidth(1, 128)
        self.rows = {}
        for signal, color in channels:
            item = QTreeWidgetItem([label_for(signal), "—"])
            item.setSizeHint(0, QSize(0, 22))
            item.setForeground(0, QColor(color))
            item.setForeground(1, QColor(color))
            item.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            item.setToolTip(0, f"{label_for(signal)}\n{signal}" + self._tooltip_suffix(signal))
            self.listing.addTopLevelItem(item)
            self.rows[signal] = item
        layout.addWidget(self.listing, 1)

    def update_value(self, signal, value, unit, age):
        item = self.rows[signal]
        text = reading_text(value) + (f" {unit}" if unit and math.isfinite(value) else "")
        item.setText(1, text + (" *" if age else ""))
        item.setToolTip(0, f"{item.text(0)}\n{signal}" + self._tooltip_suffix(signal))
        item.setToolTip(1, (f"{value:.3f} {unit}{age}" if math.isfinite(value) else "No usable data")
                       + self._tooltip_suffix(signal))

    def _tooltip_suffix(self, signal):
        explanation = self.tooltip_for(signal)
        return "\n" + explanation if explanation else ""


class ReadingCell(QWidget):
    def __init__(self, signal, parent=None):
        super().__init__(parent)
        self.signal = signal
        self._press = None
        self.setAttribute(Qt.WA_StyledBackground, True)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._press = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._press is not None and event.buttons() & Qt.LeftButton:
            if (event.position().toPoint() - self._press).manhattanLength() >= QApplication.startDragDistance():
                self._press = None
                drag = QDrag(self)
                mime = QMimeData()
                mime.setData("application/x-bmw-gauge", self.signal.encode("utf-8"))
                drag.setMimeData(mime)
                drag.setPixmap(self.grab())
                drag.exec(Qt.MoveAction)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._press = None
        super().mouseReleaseEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "remove_button"):
            self.remove_button.move(self.width() - self.remove_button.width() - 5, 3)


class ReadingStrip(QScrollArea):
    layoutChanged = Signal(object)
    manageRequested = Signal()
    resetRequested = Signal()
    fuelEstimatesRequested = Signal()

    def __init__(self, label_for, on_gauge, parent=None, *, tooltip_for=None):
        super().__init__(parent)
        self.label_for = label_for
        self.on_gauge = on_gauge
        self.tooltip_for = tooltip_for or (lambda _signal: "")
        self.setWidgetResizable(True)
        self.setFixedHeight(88)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setStyleSheet("QScrollArea {background:#f3f5f8; border:0;}"
                           "QWidget#readingBody {background:#f3f5f8;}"
                           "QWidget#readingCell {background:#1b2636; border:1px solid #354257; border-radius:6px;}"
                           "QLabel {color:#f5f8ff; background:transparent; border:0;}")
        # Keep channel management reachable even when the cards overflow. The
        # actions live beside the viewport, outside its horizontal scroll body.
        self.setViewportMargins(132, 0, 0, 0)
        self.actions = QWidget(self)
        actions = QVBoxLayout(self.actions)
        actions.setContentsMargins(4, 2, 4, 2)
        actions.setSpacing(3)
        heading = QLabel("BOTTOM GAUGES")
        heading.setStyleSheet("color:#65728a; font-size:10px; font-weight:700;")
        actions.addWidget(heading)
        self.manage_button = QPushButton("+ Add gauge")
        self.manage_button.setMinimumHeight(30)
        self.manage_button.setCursor(Qt.PointingHandCursor)
        self.manage_button.setToolTip("Add, remove and reorder bottom gauges. Extra live channels require your confirmation.")
        self.manage_button.setStyleSheet("QPushButton {background:#245de7; color:white; border:1px solid #245de7; border-radius:5px; padding:4px; font-weight:600;}"
                                        "QPushButton:hover {background:#1c4fc9;}")
        self.manage_button.clicked.connect(self.manageRequested)
        actions.addWidget(self.manage_button)
        help_text = QLabel("Drag to reorder")
        help_text.setStyleSheet("color:#65728a; font-size:10px;")
        actions.addWidget(help_text)
        actions.addStretch(1)
        self.body = QWidget()
        self.body.setObjectName("readingBody")
        self.row = QHBoxLayout(self.body)
        self.row.setSizeConstraint(QLayout.SetMinimumSize)
        self.row.setContentsMargins(4, 2, 4, 2)
        self.row.setSpacing(8)
        self.setWidget(self.body)
        self.cells = {}
        self.schema = None
        self.setAcceptDrops(True)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(lambda pos: self._menu(None, self.viewport(), pos))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "actions"):
            self.actions.setGeometry(0, 0, 128, self.height())

    def set_signals(self, signals):
        schema = tuple(signals)
        if schema == self.schema:
            return
        self.schema = schema
        while self.row.count():
            item = self.row.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        self.cells = {}
        for signal in signals:
            cell = ReadingCell(signal)
            cell.setObjectName("readingCell")
            cell.setMinimumWidth(112)
            cell.setCursor(Qt.OpenHandCursor)
            column = QVBoxLayout(cell)
            column.setContentsMargins(12, 3, 10, 3)
            column.setSpacing(0)
            name = QLabel(self.label_for(signal))
            name.setContentsMargins(0, 0, 22, 0)
            name.setStyleSheet("font-size:11px; color:#b9c8de;")
            value = QLabel("—")
            value.setStyleSheet("font-size:22px; font-weight:600;")
            age = QLabel("")
            age.setStyleSheet("font-size:10px; color:#d5ad62;")
            column.addWidget(name)
            column.addWidget(value)
            column.addWidget(age)
            cell.remove_button = QPushButton("×", cell)
            cell.remove_button.setFixedSize(20, 18)
            cell.remove_button.setCursor(Qt.PointingHandCursor)
            cell.remove_button.setAccessibleName("Remove " + self.label_for(signal) + " gauge")
            cell.remove_button.setToolTip("Remove this gauge; its channel stays in the recording")
            cell.remove_button.setStyleSheet("QPushButton {background:transparent; color:#a6b6cc; border:0; font-size:17px; padding:0;}"
                                            "QPushButton:hover {background:#354257; color:white; border-radius:3px;}")
            cell.remove_button.clicked.connect(lambda _checked=False, s=signal: self.remove_signal(s))
            cell.remove_button.raise_()
            cell.setContextMenuPolicy(Qt.CustomContextMenu)
            cell.customContextMenuRequested.connect(lambda pos, s=signal, w=cell: self._menu(s, w, pos))
            self.row.addWidget(cell, 1)
            self.cells[signal] = (cell, value, age)
            self._fit_cell(cell)
        if not signals:
            empty = QLabel("Choose gauges to show readings")
            self.row.addWidget(empty)
        if not signals:
            self.row.addStretch(1)

    def _menu(self, signal, cell, position):
        menu = QMenu(self)
        gauge = left = right = remove = None
        if signal is not None:
            gauge = menu.addAction("Open floating gauge")
            left = menu.addAction("Move left")
            right = menu.addAction("Move right")
            index = list(self.schema).index(signal)
            left.setEnabled(index > 0)
            right.setEnabled(index < len(self.schema)-1)
            remove = menu.addAction("Remove from bottom strip")
            menu.addSeparator()
        manage = menu.addAction("Add / reorder gauges…")
        restore = menu.addAction("Restore default gauges")
        fuel_estimates = menu.addAction("Fuel estimates…")
        action = menu.exec(cell.mapToGlobal(position))
        if action is None:
            return
        if action == gauge:
            self.on_gauge(signal)
        elif action == left:
            self.move_signal(signal, index-1)
        elif action == right:
            self.move_signal(signal, index+1)
        elif action == remove:
            self.remove_signal(signal)
        elif action == manage:
            self.manageRequested.emit()
        elif action == restore:
            self.resetRequested.emit()
        elif action == fuel_estimates:
            self.fuelEstimatesRequested.emit()

    def move_signal(self, signal, index):
        signals = list(self.schema or ())
        if signal not in signals:
            return
        signals.remove(signal)
        signals.insert(max(0, min(index, len(signals))), signal)
        self.set_signals(signals)
        self.layoutChanged.emit(signals)

    def remove_signal(self, signal):
        signals = [s for s in self.schema or () if s != signal]
        self.set_signals(signals)
        self.layoutChanged.emit(signals)

    def _drag_signal(self, event):
        if event.mimeData().hasFormat("application/x-bmw-gauge"):
            signal = bytes(event.mimeData().data("application/x-bmw-gauge")).decode("utf-8", errors="replace")
            if signal in self.cells:
                return signal
        return None

    def dragEnterEvent(self, event):
        if self._drag_signal(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if self._drag_signal(event):
            x = event.position().x()
            bar = self.horizontalScrollBar()
            if x < 24:
                bar.setValue(bar.value()-20)
            elif x > self.viewport().width()-24:
                bar.setValue(bar.value()+20)
            event.acceptProposedAction()

    def dropEvent(self, event):
        signal = self._drag_signal(event)
        if not signal:
            event.ignore()
            return
        point = self.body.mapFrom(self.viewport(), event.position().toPoint())
        remaining = [s for s in self.schema if s != signal]
        index = len(remaining)
        for i, target in enumerate(remaining):
            if point.x() < self.cells[target][0].geometry().center().x():
                index = i
                break
        self.move_signal(signal, index)
        event.acceptProposedAction()

    def update_value(self, signal, value, unit, age):
        if signal in self.cells:
            cell, text, age_text = self.cells[signal]
            changed = []
            for label, display in ((text, reading_text(value) + (f" {unit}" if unit else "")),
                                   (age_text, age.strip().strip("()"))):
                if label.text() != display:
                    label.setText(display)
                    changed.append(label)
            if changed:
                self._fit_cell(cell, changed)
            explanation = self.tooltip_for(signal)
            cell.setToolTip((f"{signal}\n{value:.3f} {unit}{age}" if math.isfinite(value) else f"{signal}\nNo data")
                            + ("\n" + explanation if explanation else ""))

    def _fit_cell(self, cell, labels=None):
        # An explicit cell minimum overrides Qt's automatic layout minimum.
        # Include all labels and padding so long values cannot spill into the
        # next gauge; the body grows and the scroll area handles the overflow.
        column = cell.layout()
        margins = column.contentsMargins()
        content_width = 0
        if labels is None:
            labels = [column.itemAt(index).widget() for index in range(column.count())]
        for label in labels:
            label.ensurePolished()
            padding = label.contentsMargins()
            width = max(label.fontMetrics().horizontalAdvance(label.text()) + padding.left() + padding.right() + 2,
                        label.minimumWidth())
            if width > label.minimumWidth():
                label.setMinimumWidth(width)
            content_width = max(content_width, width)
        minimum = content_width + margins.left() + margins.right() + 2
        if minimum > cell.minimumWidth():
            cell.setMinimumWidth(minimum)
            # Most samples fit the existing cell. Only force an immediate layout
            # for a wider reading, keeping long units readable without redoing
            # the entire strip layout on every incoming channel value.
            column.activate()
            self.row.activate()


class GaugeSetupDialog(QDialog):
    def __init__(self, selected, available, label_for, parent=None, *, session_signals=None, available_only=True,
                 tooltip_for=None, request_live=False):
        super().__init__(parent)
        self.setWindowTitle("Bottom gauges")
        self.resize(680, 470)
        self.label_for = label_for
        self.tooltip_for = tooltip_for or (lambda _signal: "")
        self.session_signals = set(available if session_signals is None else session_signals)
        layout = QVBoxLayout(self)
        hint = QLabel("Add or remove channels, then drag the selected gauges into the order you want.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        columns = QHBoxLayout()
        left = QVBoxLayout()
        self.available_only = QCheckBox("Only channels in current log / live session")
        self.available_only.setChecked(available_only)
        self.available_only.setToolTip("Show CSV channels containing data, or channels selected for the active live session. Uncheck for the whole catalog.")
        left.addWidget(self.available_only)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Find a channel…")
        left.addWidget(self.search)
        self.available = QListWidget()
        for signal in dict.fromkeys(available):
            item = self._item(self.available, signal)
            if signal not in self.session_signals:
                item.setForeground(QColor("#657080"))
                item.setToolTip(item.toolTip() + ("\nNot currently logged. You will be asked to add it to live logging when you apply these gauges."
                                                if request_live else "\nNot available in this log. Recorded files cannot gain missing channels."))
        left.addWidget(self.available)
        self.count_label = QLabel()
        left.addWidget(self.count_label)
        self.add_button = QPushButton("Add →")
        self.add_button.clicked.connect(self.add_selected)
        left.addWidget(self.add_button)
        columns.addLayout(left, 1)
        right = QVBoxLayout()
        right.addWidget(QLabel("Bottom strip order"))
        self.chosen = QListWidget()
        self.chosen.setDragDropMode(QAbstractItemView.InternalMove)
        self.chosen.setDefaultDropAction(Qt.MoveAction)
        for signal in dict.fromkeys(selected):
            self._item(self.chosen, signal)
        right.addWidget(self.chosen)
        buttons = QHBoxLayout()
        for title, callback in (("↑", lambda: self.move(-1)), ("↓", lambda: self.move(1)), ("Remove", self.remove_selected)):
            button = QPushButton(title)
            button.clicked.connect(callback)
            buttons.addWidget(button)
        right.addLayout(buttons)
        columns.addLayout(right, 1)
        layout.addLayout(columns)
        note = QLabel("Extra live channels require your confirmation when you apply these gauges. Removing a gauge keeps its channel in the recording."
                      if request_live else "Choose which readings to display. Channels missing from a recorded file cannot be collected during playback.")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.search.textChanged.connect(self._filter)
        self.available_only.toggled.connect(lambda _checked: self._filter(self.search.text()))
        self.available.currentItemChanged.connect(lambda *_: self._update_add_button())
        self.available.itemDoubleClicked.connect(lambda _item: self.add_selected())
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._filter("")

    def _item(self, listing, signal):
        item = QListWidgetItem(self.label_for(signal), listing)
        item.setData(Qt.UserRole, signal)
        explanation = self.tooltip_for(signal)
        item.setToolTip(signal + ("\n" + explanation if explanation else ""))
        return item

    def _filter(self, text):
        needle = text.casefold()
        visible = 0
        for i in range(self.available.count()):
            item = self.available.item(i)
            show = (not self.available_only.isChecked() or item.data(Qt.UserRole) in self.session_signals)
            show = show and needle in (item.text()+" "+item.data(Qt.UserRole)).casefold()
            item.setHidden(not show)
            visible += int(show)
        self.count_label.setText(f"{visible} channel(s) shown" + (" — open a log or wait for live data" if not self.session_signals and self.available_only.isChecked() else ""))
        self._update_add_button()

    def _update_add_button(self):
        item = self.available.currentItem()
        self.add_button.setEnabled(bool(item and not item.isHidden() and item.data(Qt.UserRole) not in self.selected()))

    def selected(self):
        return [self.chosen.item(i).data(Qt.UserRole) for i in range(self.chosen.count())]

    def add_selected(self):
        item = self.available.currentItem()
        if item and not item.isHidden() and item.data(Qt.UserRole) not in self.selected():
            added = self._item(self.chosen, item.data(Qt.UserRole))
            self.chosen.setCurrentItem(added)
        self._update_add_button()

    def remove_selected(self):
        row = self.chosen.currentRow()
        if row >= 0:
            self.chosen.takeItem(row)
        self._update_add_button()

    def move(self, direction):
        row = self.chosen.currentRow()
        target = row + direction
        if row >= 0 and 0 <= target < self.chosen.count():
            item = self.chosen.takeItem(row)
            self.chosen.insertItem(target, item)
            self.chosen.setCurrentRow(target)


class ChannelSetupDialog(QDialog):
    def __init__(self, graphs, styles, readings, available, label_for, parent=None, *,
                 gauge_available=None, session_signals=None, available_only=True, tooltip_for=None,
                 request_live=False):
        super().__init__(parent)
        self.setWindowTitle("Log channel setup")
        self.resize(780, 540)
        self.graphs = copy.deepcopy(graphs)
        self.styles = copy.deepcopy(styles)
        self.readings = list(readings)
        self.available = list(available)
        self.gauge_available = list(available if gauge_available is None else gauge_available)
        self.session_signals = list(available if session_signals is None else session_signals)
        self.gauge_available_only = available_only
        self.request_live = request_live
        self.label_for = label_for
        self.tooltip_for = tooltip_for or (lambda _signal: "")
        self.current = None
        self.setStyleSheet("QDialog {background:#20242a;} QLabel,QCheckBox {color:#edf0f4;}"
                           "QTreeWidget {background:#181b20; color:#edf0f4; border:1px solid #4d5663;}"
                           "QTreeWidget::item:selected {background:#3d536b;}"
                           "QPushButton {padding:5px 10px;}")
        layout = QVBoxLayout(self)
        split = QSplitter()
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.addWidget(QLabel("Channels by graph area"))
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.currentItemChanged.connect(self._select)
        ll.addWidget(self.tree)
        row = QHBoxLayout()
        for title, callback in (("↑", lambda: self._move(-1)), ("↓", lambda: self._move(1)),
                                ("Add area", self._add_area)):
            button = QPushButton(title)
            button.clicked.connect(callback)
            row.addWidget(button)
        ll.addLayout(row)
        row = QHBoxLayout()
        for title, callback in (("Remove selected", self._remove), ("Add/remove…", self._channels)):
            button = QPushButton(title)
            button.clicked.connect(callback)
            row.addWidget(button)
        ll.addLayout(row)
        split.addWidget(left)
        right = QWidget()
        form = QFormLayout(right)
        form.addRow(QLabel("Selected channel settings"))
        self.color = QPushButton("Select color…")
        self.color.clicked.connect(self._color)
        form.addRow("Trace color", self.color)
        self.auto = QCheckBox("Autorange")
        self.auto.toggled.connect(self._auto_changed)
        form.addRow(self.auto)
        self.zero = self._number(-1e9, 1e9)
        self.full = self._number(-1e9, 1e9)
        self.lower = self._number(0, 100)
        self.upper = self._number(0, 100)
        form.addRow("Zero value", self.zero)
        form.addRow("Full scale value", self.full)
        form.addRow("Lower % of graph area", self.lower)
        form.addRow("Higher % of graph area", self.upper)
        self.area = QComboBox()
        self.area.activated.connect(self._change_area)
        form.addRow("Graph area", self.area)
        self.show_reading = QCheckBox("Show in bottom readings")
        form.addRow(self.show_reading)
        self.reading_button = QPushButton("Choose bottom readings…")
        self.reading_button.clicked.connect(self._choose_readings)
        form.addRow(self.reading_button)
        hint = QLabel("Each trace has its own scale. Readings always show the actual value and unit.")
        hint.setWordWrap(True)
        form.addRow(hint)
        split.addWidget(right)
        split.setSizes([350, 400])
        layout.addWidget(split)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._rebuild()

    def _number(self, low, high):
        box = QDoubleSpinBox()
        box.setRange(low, high)
        box.setDecimals(2)
        return box

    def _auto_changed(self, checked):
        self.zero.setEnabled(not checked)
        self.full.setEnabled(not checked)

    def _commit(self):
        if self.current is None:
            return True
        gi, signal = self.current
        if self.lower.value() >= self.upper.value() or (not self.auto.isChecked() and self.zero.value() >= self.full.value()):
            QMessageBox.warning(self, "Channel range", "Higher % must exceed lower %, and full scale must exceed zero.")
            return False
        self.styles[signal] = dict(color=self.color_value, auto=self.auto.isChecked(),
                                   zero=self.zero.value(), full=self.full.value(),
                                   lower=self.lower.value(), upper=self.upper.value())
        if self.show_reading.isChecked() and signal not in self.readings:
            self.readings.append(signal)
        elif not self.show_reading.isChecked() and signal in self.readings:
            self.readings.remove(signal)
        target = self.area.currentIndex()
        if target >= 0 and target != gi and signal in self.graphs[gi]:
            self.graphs[gi].remove(signal)
            if signal not in self.graphs[target]:
                self.graphs[target].append(signal)
            self.current = (target, signal)
        return True

    def _rebuild(self, select=None):
        self.current = None
        self.tree.blockSignals(True)
        self.tree.clear()
        self.area.clear()
        chosen = None
        for gi, signals in enumerate(self.graphs):
            self.area.addItem(f"Area {gi+1}")
            root = QTreeWidgetItem([f"Area {gi+1}"])
            root.setData(0, Qt.UserRole, (gi, None))
            self.tree.addTopLevelItem(root)
            for signal in signals:
                item = QTreeWidgetItem([self.label_for(signal)])
                item.setData(0, Qt.UserRole, (gi, signal))
                explanation = self.tooltip_for(signal)
                item.setToolTip(0, signal + ("\n" + explanation if explanation else ""))
                root.addChild(item)
                if select == (gi, signal):
                    chosen = item
            root.setExpanded(True)
        self.tree.blockSignals(False)
        self.tree.setCurrentItem(chosen or self.tree.topLevelItem(0))

    def _select(self, item, previous):
        if not self._commit():
            self.tree.blockSignals(True)
            self.tree.setCurrentItem(previous)
            self.tree.blockSignals(False)
            return
        self.current = None
        gi, signal = item.data(0, Qt.UserRole) if item else (0, None)
        for widget in (self.color, self.auto, self.lower, self.upper, self.area, self.show_reading):
            widget.setEnabled(signal is not None)
        if signal is None:
            self.zero.setEnabled(False)
            self.full.setEnabled(False)
            return
        style = channel_style(self.styles, signal, self.available.index(signal) if signal in self.available else 0)
        self.color_value = style["color"]
        self.color.setStyleSheet(f"background:{self.color_value}; color:white; border:1px solid #718096;")
        self.auto.setChecked(style["auto"])
        self._auto_changed(style["auto"])
        for key in ("zero", "full", "lower", "upper"):
            getattr(self, key).setValue(style[key])
        self.area.setCurrentIndex(gi)
        self.show_reading.setChecked(signal in self.readings)
        self.current = (gi, signal)

    def _color(self):
        color = QColorDialog.getColor(QColor(self.color_value), self, "Trace color")
        if color.isValid():
            self.color_value = color.name()
            self.color.setStyleSheet(f"background:{self.color_value}; color:white; border:1px solid #718096;")

    def _change_area(self):
        if self._commit():
            self._rebuild(self.current)

    def _selected_area(self):
        item = self.tree.currentItem()
        return item.data(0, Qt.UserRole)[0] if item else 0

    def _move(self, direction):
        if not self._commit() or self.current is None:
            return
        gi, signal = self.current
        index = self.graphs[gi].index(signal)
        target = index + direction
        if 0 <= target < len(self.graphs[gi]):
            self.graphs[gi][index], self.graphs[gi][target] = self.graphs[gi][target], signal
        self._rebuild((gi, signal))

    def _add_area(self):
        if self._commit() and len(self.graphs) < 8:
            self.graphs.append([])
            self._rebuild()
            self.tree.setCurrentItem(self.tree.topLevelItem(len(self.graphs)-1))

    def _remove(self):
        if not self._commit():
            return
        if self.current:
            gi, signal = self.current
            self.graphs[gi].remove(signal)
        elif len(self.graphs) > 1:
            self.graphs.pop(self._selected_area())
        self._rebuild()

    def _pick(self, title, selected):
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.resize(420, 500)
        layout = QVBoxLayout(dialog)
        search = QLabel("Select the channels to display")
        layout.addWidget(search)
        listing = QListWidget()
        listing.setSelectionMode(QAbstractItemView.NoSelection)
        # Keep absent saved signals so opening a different log does not erase them.
        for signal in dict.fromkeys(self.available + list(selected)):
            item = QListWidgetItem(self.label_for(signal), listing)
            item.setData(Qt.UserRole, signal)
            explanation = self.tooltip_for(signal)
            item.setToolTip(signal + ("\n" + explanation if explanation else ""))
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if signal in selected else Qt.Unchecked)
        layout.addWidget(listing)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.Accepted:
            return None
        checked = [listing.item(i).data(Qt.UserRole) for i in range(listing.count())
                   if listing.item(i).checkState() == Qt.Checked]
        return [s for s in selected if s in checked] + [s for s in checked if s not in selected]

    def _channels(self):
        if not self._commit():
            return
        gi = self.current[0] if self.current else self._selected_area()
        selected = self._pick(f"Channels in Area {gi+1}", self.graphs[gi])
        if selected is not None:
            self.graphs[gi] = selected
            self._rebuild()

    def _choose_readings(self):
        if not self._commit():
            return
        dialog = GaugeSetupDialog(self.readings, self.gauge_available, self.label_for, self,
                                  session_signals=self.session_signals,
                                  available_only=self.gauge_available_only, tooltip_for=self.tooltip_for,
                                  request_live=self.request_live)
        if dialog.exec() == QDialog.Accepted:
            self.readings = dialog.selected()
            self.gauge_available_only = dialog.available_only.isChecked()
            current = self.current
            self._rebuild(current)

    def accept(self):
        if self._commit():
            super().accept()
