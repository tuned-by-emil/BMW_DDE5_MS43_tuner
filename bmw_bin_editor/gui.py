from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QStandardPaths, QEvent, QItemSelectionModel, QObject, QSettings, QSize, Qt, Signal, QTimer, QThread

import numpy as np
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas, NavigationToolbar2QT as NavigationToolbar
from matplotlib.figure import Figure
from matplotlib import colormaps
from mpl_toolkits.mplot3d import proj3d  # noqa: F401 - ensures 3D projection registration

from PySide6.QtGui import QAction, QColor, QFont, QKeyEvent, QKeySequence, QShortcut, QPainter, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QMenu,
    QPushButton,
    QPlainTextEdit,
    QSpinBox,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QSplitterHandle,
    QStackedWidget,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .core import (
    CalibrationDefinition,
    CalibrationDocument,
    EditEngine,
    EditorError,
    XDFDatabase,
    format_number,
    estimate_lambda_from_map_target,
)
from .floating_gauge import FloatingGauge
from .log_widgets import ChannelSetupDialog, FuelEstimateDialog, GaugeSetupDialog, GraphReadings, ReadingStrip, channel_style, scale_channel
from .ecu_programming_gui import EcuProgrammingDialog
from .log_delta import LogDeltaDialog
from .logging_power import LoggingPowerGuard
from .live_logging import LiveLogger, LoggingDialog, load_parameters
from .native_live_logging import NativeLiveLogger
from .serial_live_logging import SerialLiveLogger
from .testo_mcp import TestOMcpClient, TestOMcpError, flatten_numeric_fields

from .logdata import (
    SignalCatalog,
    TestOLog,
    LiveTestOLog,
    profile_for_definition,
    SIG_N75,
    SIG_RPM, SIG_IQ, SIG_TORQUE, SIG_SOI, SIG_BOOST_SET, SIG_BOOST, SIG_MAF, SIG_RAIL, SIG_SPEED,
    CALC_EST_LAMBDA, CALC_BOOST_ERROR,
    FUEL_RATE_SIGNAL, FUEL_CONSUMPTION_SIGNAL, FuelEstimateConfig,
)


from . import APP_NAME, APP_AUTHOR, __version__
from .settings import editor_settings, ECUSettings
from .ms43 import is_ms43x, database_for_size, export_plan, export_binary
from .dde5_labels import table_name as dde5_table_name
from .ms43_navigation import is_ms43_definition, readable_name, category_path, drift_shortcuts, ve_selection_labels
from .ms43_help import setting_tooltip, help_text as ms43_help_text, GROUP_HELP, tooltip_html
from .bin_analysis import bin_metadata
from .definition_library import discover_entries, propose_matches
from .definition_library_gui import DefinitionProposalDialog


class ReviewSplitterHandle(QSplitterHandle):
    """Visible grab handle for resizing the map and log review panes."""

    def __init__(self, orientation, parent):
        super().__init__(orientation, parent)
        self.setCursor(Qt.SplitVCursor if orientation == Qt.Vertical else Qt.SplitHCursor)
        self.setMouseTracking(True)
        self.setToolTip("Drag to resize the table and log panes. The position is saved automatically.")

    def enterEvent(self, event):
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#e8ebef"))
        if self.orientation() == Qt.Vertical:
            grip = QColor("#758292" if self.underMouse() else "#a0a9b4")
            center = self.rect().center()
            painter.setPen(Qt.NoPen)
            painter.setBrush(grip)
            painter.drawRoundedRect(center.x() - 29, center.y() - 2, 58, 4, 2, 2)
            painter.drawRoundedRect(center.x() - 29, center.y() + 5, 58, 2, 1, 1)
        else:
            painter.setPen(QColor("#c6ccd3"))
            painter.drawLine(self.rect().center().x(), 4, self.rect().center().x(), self.height() - 4)


class ReviewSplitter(QSplitter):
    def createHandle(self):
        return ReviewSplitterHandle(self.orientation(), self)


def bundled_definition_path() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    return base / "definitions" / "EDC16C31 361830.xdf"


def bundled_signal_catalog_path() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    return base / "definitions" / "testo_signals.tsv"


def definition_key(definition: CalibrationDefinition) -> str:
    """Keep 361830 preferences; isolate other software's maps at reused offsets."""
    prefix = definition.profile_id + ':' if definition.profile_id and definition.profile_id != '361830' else ''
    return f"{prefix}{definition.kind}:{definition.z_address:06X}"

def short_map_name(definition: CalibrationDefinition) -> str:
    """Compact navigator title; full audit/XDF title remains in tooltips/info."""
    if is_ms43_definition(definition):
        return readable_name(definition)
    english = dde5_table_name(definition)
    if english:
        return english
    title = re.sub(r"^0x[0-9A-Fa-f]+\s*", "", definition.title or "")
    title = re.sub(r"\[[A-Z]{2}\]\s*", "", title)
    title = re.sub(r"^(BOOST CTRL|BOOST|FUEL/AIR LIMITER|FUEL|AIR|RAIL)\s*[-—:]\s*", "", title, flags=re.I)
    title = re.sub(r"\s+", " ", title).strip(" -—:")
    if len(title) > 42:
        title = title[:39].rstrip() + "…"
    return title or definition.category or 'Table'


def short_signal_name(signal: str) -> str:
    """Compact tuner-friendly visible labels. Raw TestO names stay in tooltips/details."""
    names = {
        SIG_RPM: "RPM", SIG_IQ: "IQ", SIG_TORQUE: "TQ", SIG_SOI: "SOI",
        SIG_BOOST: "MAP", SIG_BOOST_SET: "MAP tgt", SIG_N75: "N75",
        SIG_RAIL: "Rail", SIG_MAF: "MAF", SIG_SPEED: "Speed",
        CALC_EST_LAMBDA: "Est. λ", CALC_BOOST_ERROR: "ΔMAP",
        FUEL_RATE_SIGNAL: "Est. fuel flow", FUEL_CONSUMPTION_SIGNAL: "Est. consumption",
    }
    for index in (1, 2, 3):
        names[f"STAT_MENGE_VOREINSPRITZUNG{index}_WERT"] = f"Pilot {index} quantity"
        names[f"STAT_WINKELKOMPONENTE_BEGINN_VOREINSPRITZUNG{index}_WERT"] = f"Pilot {index} angle component"
    names["STAT_ANSTEUERDAUER_NACHEINSPRITZUNG1_WERT"] = "Post 1 duration"
    names["STAT_BEZUGSWINKEL_BEGINN_NACHEINSPRITZUNG1_WERT"] = "Post 1 reference angle"
    return names.get(signal, signal.removeprefix("STAT_").removesuffix("_WERT"))


class CalibrationTable(QTableWidget):
    selectionFromMouse = Signal(int, int, bool)
    axisEditRequested = Signal(str, int)

    CELL_MODES = {
        "fit": (None, None),
        "compact": (44, 22),
        "normal": (72, 26),
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectItems)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        # ResizeToContents repeatedly scans every cell during tuning and is noticeably
        # expensive. Interactive/fixed sections make repeated W/Q edits much snappier.
        self.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.horizontalHeader().setMinimumSectionSize(24)
        self.verticalHeader().setSectionResizeMode(QHeaderView.Fixed)
        self.verticalHeader().setMinimumSectionSize(16)
        self._cell_mode = "fit"
        self._base_font = self.font()
        self.setStyleSheet("QTableWidget::item { padding: 1px; }")
        self.horizontalHeader().sectionDoubleClicked.connect(lambda i: self.axisEditRequested.emit("x", i))
        self.verticalHeader().sectionDoubleClicked.connect(lambda i: self.axisEditRequested.emit("y", i))
        # Mouse selection is handled explicitly instead of letting QTableWidget's
        # native ExtendedSelection compete with the editor's rectangular selection
        # model. This makes click, Shift+click and click-drag deterministic.
        self._dragging = False

    @property
    def cell_mode(self) -> str:
        return self._cell_mode

    def set_cell_mode(self, mode: str) -> None:
        if mode not in self.CELL_MODES:
            mode = "fit"
        self._cell_mode = mode
        self.apply_cell_size()

    def apply_cell_size(self) -> None:
        cols = self.columnCount()
        rows = self.rowCount()
        if cols <= 0 or rows <= 0:
            return

        if self._cell_mode == "fit":
            # Fit the complete map into the data viewport where practical. Caps keep
            # small curves/tables from turning into giant cells, while minimums keep
            # values usable on very large maps. The viewport already excludes headers.
            available_w = max(1, self.viewport().width() - 2)
            available_h = max(1, self.viewport().height() - 2)
            cell_w = max(30, min(72, available_w // max(1, cols)))
            cell_h = max(17, min(26, available_h // max(1, rows)))
        else:
            cell_w, cell_h = self.CELL_MODES[self._cell_mode]

        # At the narrowest Fit size a slightly smaller font keeps four-digit ECU
        # values readable instead of eliding them.
        font = QFont(self._base_font)
        font.setPointSizeF(max(7.5, min(self._base_font.pointSizeF(), 8.5 if cell_w < 40 else self._base_font.pointSizeF())))
        self.setFont(font)

        self.horizontalHeader().setDefaultSectionSize(int(cell_w))
        self.verticalHeader().setDefaultSectionSize(int(cell_h))
        for c in range(cols):
            self.horizontalHeader().resizeSection(c, int(cell_w))
        for r in range(rows):
            self.verticalHeader().resizeSection(r, int(cell_h))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._cell_mode == "fit":
            # Delay until Qt has completed the viewport geometry update.
            QTimer.singleShot(0, self.apply_cell_size)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        # Main editor owns MTune hotkeys; retain Ctrl+C/V and native focus handling.
        if event.key() in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down, Qt.Key_Home, Qt.Key_End):
            event.ignore()
            return
        super().keyPressEvent(event)

    def _index_from_mouse(self, event):
        try:
            point = event.position().toPoint()
        except AttributeError:
            point = event.pos()
        return self.indexAt(point)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            index = self._index_from_mouse(event)
            if index.isValid():
                self._dragging = True
                self.selectionFromMouse.emit(
                    index.row(), index.column(), bool(event.modifiers() & Qt.ShiftModifier)
                )
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._dragging and (event.buttons() & Qt.LeftButton):
            index = self._index_from_mouse(event)
            if index.isValid():
                # Drag always extends from the anchor established on mouse press.
                self.selectionFromMouse.emit(index.row(), index.column(), True)
                event.accept()
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self._dragging:
            self._dragging = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            index = self._index_from_mouse(event)
            if index.isValid():
                self.selectionFromMouse.emit(index.row(), index.column(), False)
                parent = self.parent()
                while parent is not None and not isinstance(parent, MapEditor):
                    parent = parent.parent()
                if isinstance(parent, MapEditor):
                    parent.set_value_dialog()
                event.accept()
                return
        super().mouseDoubleClickEvent(event)


class SurfaceWidget(QWidget):
    cellPicked = Signal(int, int, bool)
    focusReceived = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.figure = Figure(figsize=(6, 5))
        # tight_layout can emit repeated warnings when the 3D axes becomes small
        # inside a splitter. Fixed margins are more predictable for this embedded UI.
        self.figure.subplots_adjust(left=0.06, right=0.98, bottom=0.08, top=0.98)
        self.canvas = FigureCanvas(self.figure)
        self.ax = None
        self.scatter = None
        self.selection_artist = None
        self.log_artist = None
        self.log_trail_artist = None
        self.log_point = None
        self.log_trail = []
        self._background = None
        self._last_render_key: str | None = None
        self.definition: CalibrationDefinition | None = None
        self.x_values: list[float] = []
        self.y_values: list[float] = []
        self.z_values: list[list[float]] = []
        self.base_values: list[list[float]] = []
        self.selection = None
        self.show_original = True
        self._redraw_timer = QTimer(self)
        self._redraw_timer.setSingleShot(True)
        self._redraw_timer.timeout.connect(self.redraw)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.nav_toolbar = NavigationToolbar(self.canvas, self, coordinates=False)
        self.nav_toolbar.setIconSize(QSize(16, 16))
        self.nav_toolbar.setMaximumHeight(28)
        self.nav_toolbar.setToolTip("3D navigation: rotate with mouse; toolbar provides pan/zoom/home/save")
        layout.addWidget(self.nav_toolbar)
        layout.addWidget(self.canvas)
        self.canvas.mpl_connect("pick_event", self._pick)
        self.canvas.mpl_connect("draw_event", self._on_draw)
        self.canvas.mpl_connect("button_press_event", lambda e: self.canvas.setFocus(Qt.MouseFocusReason))
        self.setFocusPolicy(Qt.StrongFocus)
        self.canvas.setFocusPolicy(Qt.StrongFocus)
        self.setFocusProxy(self.canvas)

    def focusInEvent(self, event):
        self.focusReceived.emit()
        super().focusInEvent(event)

    def _pick(self, event):
        if event.artist is not self.scatter or len(getattr(event, "ind", [])) == 0 or self.definition is None:
            return
        idx = int(event.ind[0])
        row = idx // self.definition.cols
        col = idx % self.definition.cols
        key = (event.mouseevent.key or "").lower() if event.mouseevent else ""
        self.cellPicked.emit(row, col, "shift" in key)

    def update_surface(self, definition, x_values, y_values, z_values, base_values, selection, defer=False):
        self.definition = definition
        self.x_values = x_values
        self.y_values = y_values
        self.z_values = z_values
        self.base_values = base_values
        self.selection = selection
        if defer:
            # During key-repeat, keep the 2D table responsive and redraw the much
            # heavier matplotlib surface once the user pauses briefly.
            self._redraw_timer.start(45)
        else:
            self._redraw_timer.stop()
            self.redraw()

    def _on_draw(self, _event=None):
        pass

    def _blit_dynamic(self):
        # 3D artists need a full projection pass after moving or changing camera.
        self.canvas.draw_idle()

    def update_selection(self, selection):
        """Fast path: move only the highlight without rebuilding/redrawing the surface."""
        self.selection = selection
        if self.ax is None or self.definition is None:
            return
        self._draw_selection()
        self._blit_dynamic()

    def _draw_selection(self):
        if self.selection_artist is not None:
            try:
                self.selection_artist.remove()
            except (ValueError, AttributeError):
                pass
            self.selection_artist = None
        d = self.definition
        s = self.selection
        if d is None or s is None or not self.z_values:
            return
        z = np.asarray(self.z_values, dtype=float)
        if d.rows <= 1:
            x = np.asarray(self.x_values if self.x_values else list(range(d.cols)), dtype=float)
            inds = np.arange(s.c0, s.c1 + 1, dtype=int)
            self.selection_artist = self.ax.scatter(x[inds], z[0][inds], s=30, marker="s", color="darkorange", animated=False)
        else:
            x = np.asarray(self.x_values if self.x_values else list(range(d.cols)), dtype=float)
            y = np.asarray(self.y_values if self.y_values else list(range(d.rows)), dtype=float)
            X, Y = np.meshgrid(x, y)
            rr, cc = np.mgrid[s.r0:s.r1 + 1, s.c0:s.c1 + 1]
            self.selection_artist = self.ax.scatter(
                X[rr, cc], Y[rr, cc], z[rr, cc], s=24, marker="s", color="darkorange", zorder=10, depthshade=False, animated=False
            )

    def set_log_overlay(self, point=None, trail=None):
        self.log_point = point
        self.log_trail = list(trail or [])
        if self.ax is None or self.definition is None:
            return
        self._draw_log_overlay()
        self._blit_dynamic()

    def _draw_log_overlay(self):
        for attr in ("log_artist", "log_trail_artist"):
            artist = getattr(self, attr)
            if artist is not None:
                try:
                    artist.remove()
                except (ValueError, AttributeError):
                    pass
                setattr(self, attr, None)
        if self.ax is None or self.definition is None or self.log_point is None:
            return
        d = self.definition
        if d.rows <= 1:
            # A map switch can briefly leave a 3-value (x,y,z) cursor from the previous 2-axis map.
            # Use first/last values rather than unpacking exactly two, avoiding the V1.15 curve/scalar crash.
            if len(self.log_point) < 2:
                return
            x, z = self.log_point[0], self.log_point[-1]
            if self.log_trail:
                tx = [p[0] for p in self.log_trail if len(p) >= 2]
                tz = [p[-1] for p in self.log_trail if len(p) >= 2]
                self.log_trail_artist, = self.ax.plot(tx, tz, linewidth=2.0, alpha=0.55, animated=False)
            self.log_artist = self.ax.scatter([x], [z], s=95, marker="o", animated=False)
        else:
            if len(self.log_point) < 3:
                return
            x, y, z = self.log_point[:3]
            if self.log_trail:
                valid_trail = [p for p in self.log_trail if len(p) >= 3]
                tx = [p[0] for p in valid_trail]
                ty = [p[1] for p in valid_trail]
                tz = [p[2] for p in valid_trail]
                self.log_trail_artist, = self.ax.plot(tx, ty, tz, linewidth=2.0, alpha=0.55, animated=False)
            self.log_artist = self.ax.scatter([x], [y], [z], s=60, marker="o", color="limegreen", zorder=11, depthshade=False, animated=False)

    def redraw(self):
        # Preserve camera while editing the SAME map, but do not carry an odd
        # camera angle from one calibration into the next one.  V1.14 did that
        # and could leave the next 3D plot looking almost edge-on.
        current_key = definition_key(self.definition) if self.definition is not None else None
        old_view = None
        if current_key == self._last_render_key and self.ax is not None and hasattr(self.ax, "elev"):
            try:
                old_view = (float(self.ax.elev), float(self.ax.azim))
            except Exception:
                old_view = None
        self._background = None
        self.figure.clear()
        self.selection_artist = None
        self.log_artist = None
        self.log_trail_artist = None
        d = self.definition
        if d is None:
            self.ax = None
            self.scatter = None
            self.canvas.draw_idle()
            return
        z = np.asarray(self.z_values, dtype=float)
        base = np.asarray(self.base_values, dtype=float)

        if d.rows <= 1:
            self.ax = self.figure.add_subplot(111)
            x = np.asarray(self.x_values if self.x_values else list(range(d.cols)), dtype=float)
            self.ax.plot(x, z[0], marker="o")
            # Separate low-alpha scatter gives stable point indices for mouse picking.
            self.scatter = self.ax.scatter(x, z[0], s=30, alpha=0.30, picker=8)
            if self.show_original and np.any(np.abs(z - base) > 1e-12):
                self.ax.plot(x, base[0], linestyle="--", linewidth=1.0)
            self.ax.set_xlabel(d.x_axis.units if d.x_axis and d.x_axis.units else "X")
            self.ax.set_ylabel(d.z_units or "Value")
            self.ax.grid(True, alpha=0.25)
        else:
            self.ax = self.figure.add_subplot(111, projection="3d", computed_zorder=False)
            x = np.asarray(self.x_values if self.x_values else list(range(d.cols)), dtype=float)
            y = np.asarray(self.y_values if self.y_values else list(range(d.rows)), dtype=float)
            X, Y = np.meshgrid(x, y)
            self.ax.plot_surface(X, Y, z, cmap="viridis", alpha=0.88, linewidth=0.35, edgecolor="k")
            if self.show_original and np.any(np.abs(z - base) > 1e-12):
                self.ax.plot_wireframe(X, Y, base, rstride=1, cstride=1, linewidth=0.55, alpha=0.5)
            self.scatter = self.ax.scatter(X.ravel(), Y.ravel(), z.ravel(), s=12, alpha=0.20, picker=8)
            self.ax.set_xlabel(d.x_axis.units if d.x_axis and d.x_axis.units else "X", labelpad=4)
            self.ax.set_ylabel(d.y_axis.units if d.y_axis and d.y_axis.units else "Y", labelpad=4)
            self.ax.set_zlabel(d.z_units or "Value", labelpad=4)
            # Use the pane much more effectively.  A deliberately wide XY box and
            # a slightly larger 3D zoom avoids the tiny cube floating in white space.
            try:
                self.ax.set_box_aspect((1.35, 1.0, 0.72), zoom=1.18)
            except TypeError:
                self.ax.set_box_aspect((1.35, 1.0, 0.72))
            self.ax.tick_params(axis="both", which="major", labelsize=8, pad=1)
            if old_view is not None:
                try:
                    self.ax.view_init(elev=old_view[0], azim=old_view[1])
                except Exception:
                    pass
            else:
                # Predictable tuner-style starting view for every newly opened map.
                self.ax.view_init(elev=27, azim=-135)
            # Matplotlib's default 3D axes leave generous margins in a Qt splitter.
            # Pull the plot out toward the pane edges while keeping room for labels.
            self.ax.set_position([0.035, 0.04, 0.93, 0.86])
        self._draw_selection()
        self._draw_log_overlay()
        self.ax.set_title(short_map_name(d), fontsize=10, pad=6)
        self._last_render_key = current_key
        self.canvas.draw_idle()


class InfoPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.title = QLabel("No map selected")
        self.title.setWordWrap(True)
        self.meta = QLabel("")
        self.meta.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.desc = QLabel("")
        self.desc.setWordWrap(True)
        self.desc.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout = QVBoxLayout(self)
        layout.addWidget(self.title)
        layout.addWidget(self.meta)
        layout.addWidget(self.desc)
        layout.addStretch(1)

    def set_definition(self, d: CalibrationDefinition | None):
        if d is None:
            self.title.setText("No map selected")
            self.meta.clear()
            self.desc.clear()
            self.desc.setToolTip("")
            self.title.setToolTip("")
            return
        self.title.setText(f"<b>{short_map_name(d)}</b>")
        self.title.setToolTip(tooltip_html(setting_tooltip(d)) if is_ms43_definition(d) else d.title)
        self.meta.setText(
            f"Z: 0x{d.z_address:06X}    Size: {d.rows} x {d.cols}\n"
            f"Type: {'signed' if d.z_signed else 'unsigned'} {d.z_element_bits}-bit    "
            f"Units: {d.z_units or '—'}    Math: {d.z_equation.strip()}\n"
            f"Category: {d.category}"
        )
        if is_ms43_definition(d):
            self.desc.setText(ms43_help_text(d))
            self.desc.setToolTip(tooltip_html(setting_tooltip(d)))
        else:
            self.desc.setText(d.description or "No description in XDF.")
            self.desc.setToolTip("")



class TestOLiveWorker(QObject):
    sampleReady = Signal(float, object)
    error = Signal(str)
    stopped = Signal()

    def __init__(self, endpoint: str, token: str, ecu: str, job: str, params: dict, interval_ms: int):
        super().__init__()
        self.endpoint = endpoint; self.token = token; self.ecu = ecu; self.job = job
        self.params = params; self.interval_ms = max(80, int(interval_ms)); self.running = True
        from threading import Event
        self._stop_requested = Event()

    def stop(self):
        self.running = False
        self._stop_requested.set()

    def run(self):
        import time
        client = TestOMcpClient(self.endpoint, self.token, timeout=max(2.0, self.interval_ms / 1000.0 * 4))
        t0 = time.monotonic()
        try:
            client.initialize()
            while self.running:
                started = time.monotonic()
                result = client.run_job(self.ecu, self.job, self.params)
                fields = flatten_numeric_fields(result)
                if fields:
                    self.sampleReady.emit(time.monotonic() - t0, fields)
                elapsed = time.monotonic() - started
                delay = max(0.0, self.interval_ms / 1000.0 - elapsed)
                if delay:
                    self._stop_requested.wait(delay)
        except Exception as exc:
            if self.running:
                self.error.emit(str(exc))
        self.stopped.emit()


class TestOLiveDialog(QDialog):
    def __init__(self, parent=None, settings=None):
        super().__init__(parent)
        self.settings = settings if settings is not None else ECUSettings(editor_settings(QSettings))
        self.setWindowTitle("TestO 2.0 live connection")
        self.resize(700, 520)
        st = self.settings
        self.endpoint = QLineEdit(st.value("testo_mcp/endpoint", "http://127.0.0.1:13721/mcp"))
        self.token = QLineEdit(st.value("testo_mcp/token", "")); self.token.setEchoMode(QLineEdit.Password)
        self.ecu = QComboBox(); self.ecu.setEditable(True); self.ecu.setCurrentText(st.value("testo_mcp/ecu", "DDE5"))
        self.job = QComboBox(); self.job.setEditable(True); self.job.setCurrentText(st.value("testo_mcp/job", "STATUS_LESEN"))
        self.interval = QSpinBox(); self.interval.setRange(80, 5000); self.interval.setValue(int(st.value("testo_mcp/interval", 150))); self.interval.setSuffix(" ms")
        self.params = QPlainTextEdit(st.value("testo_mcp/params", "{}")); self.params.setMaximumHeight(90)
        self.info = QPlainTextEdit(); self.info.setReadOnly(True); self.info.setPlaceholderText("Use Probe / ECUs / Jobs / Describe before Start if needed.")
        probe=QPushButton("Probe MCP"); probe.clicked.connect(self.probe)
        ecus=QPushButton("Load ECUs"); ecus.clicked.connect(self.load_ecus)
        jobs=QPushButton("Load Jobs"); jobs.clicked.connect(self.load_jobs)
        desc=QPushButton("Describe Job"); desc.clicked.connect(self.describe)
        start=QPushButton("Start LIVE"); start.clicked.connect(self.accept)
        cancel=QPushButton("Cancel"); cancel.clicked.connect(self.reject)
        form=QFormLayout(); form.addRow("MCP endpoint", self.endpoint); form.addRow("Bearer token", self.token); form.addRow("ECU (.prg name)", self.ecu); form.addRow("Job", self.job); form.addRow("Poll interval", self.interval); form.addRow("Job params (JSON)", self.params)
        buttons=QHBoxLayout(); [buttons.addWidget(x) for x in (probe,ecus,jobs,desc)]; buttons.addStretch(1); buttons.addWidget(start); buttons.addWidget(cancel)
        lay=QVBoxLayout(self); lay.addLayout(form); lay.addLayout(buttons); lay.addWidget(self.info,1)

    def client(self): return TestOMcpClient(self.endpoint.text().strip(), self.token.text().strip())
    def probe(self):
        try:
            c=self.client(); ok=c.health(); tools=c.list_tools(); self.info.setPlainText("Health: %s\n\nTools:\n%s" % ("OK" if ok else "FAILED", json.dumps(tools, indent=2)[:12000]))
        except Exception as e: self.info.setPlainText("ERROR: "+str(e))
    def load_ecus(self):
        try:
            vals=self.client().list_ecus(); cur=self.ecu.currentText(); self.ecu.clear(); self.ecu.addItems(vals); i=self.ecu.findText(cur); self.ecu.setCurrentIndex(i if i>=0 else 0); self.info.setPlainText(f"Found {len(vals)} ECU files.")
        except Exception as e: self.info.setPlainText("ERROR: "+str(e))
    def load_jobs(self):
        try:
            vals=self.client().list_jobs(self.ecu.currentText().strip()); cur=self.job.currentText(); self.job.clear(); self.job.addItems(vals); i=self.job.findText(cur); self.job.setCurrentIndex(i if i>=0 else 0); self.info.setPlainText(f"Found {len(vals)} jobs for {self.ecu.currentText()}.")
        except Exception as e: self.info.setPlainText("ERROR: "+str(e))
    def describe(self):
        try: self.info.setPlainText(json.dumps(self.client().describe_job(self.ecu.currentText().strip(), self.job.currentText().strip()), indent=2, ensure_ascii=False)[:18000])
        except Exception as e: self.info.setPlainText("ERROR: "+str(e))
    def config(self):
        try: params=json.loads(self.params.toPlainText().strip() or "{}")
        except Exception as e: raise ValueError(f"Job params are not valid JSON: {e}")
        if not isinstance(params,dict): raise ValueError("Job params JSON must be an object/dictionary.")
        st=self.settings; st.setValue("testo_mcp/endpoint",self.endpoint.text().strip()); st.setValue("testo_mcp/token",self.token.text().strip()); st.setValue("testo_mcp/ecu",self.ecu.currentText().strip()); st.setValue("testo_mcp/job",self.job.currentText().strip()); st.setValue("testo_mcp/interval",self.interval.value()); st.setValue("testo_mcp/params",self.params.toPlainText().strip())
        return self.endpoint.text().strip(), self.token.text().strip(), self.ecu.currentText().strip(), self.job.currentText().strip(), params, self.interval.value()

class LogViewerPanel(QWidget):
    """Recorded TestO log viewer synchronized with the currently open calibration.

    V1.10 separates plotted context signals from the X/Y signals that position the
    cursor on the calibration table. Multiple stacked graphs can therefore show a
    large amount of context without changing which map cells are considered used.
    """

    cursorChanged = Signal(int)
    rangeChanged = Signal(float, float)
    logLoaded = Signal(object)
    statusChanged = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.catalog = SignalCatalog.load_tsv(bundled_signal_catalog_path())
        self.log: TestOLog | LiveTestOLog | None = None
        self.live_thread: QThread | None = None
        self.live_worker: TestOLiveWorker | None = None
        self.live_mode = False
        self.direct_logger = None
        journal = Path(QStandardPaths.writableLocation(QStandardPaths.GenericDataLocation)) / 'BMW_Bin_Editor' / 'logging-power.json'
        self.power_guard = LoggingPowerGuard(journal)
        recovery_notice = self.power_guard.recover()
        self.definition: CalibrationDefinition | None = None
        self.profile = None
        self.cursor_index = 0
        self.range_seconds: tuple[float, float] | None = None
        self._press_xdata: float | None = None
        self._press_pixel: float | None = None
        self.map_value: float | None = None
        self.map_value_unit: str = ""
        self.custom_graphs: list[list[str]] = []
        self.view_xlim: tuple[float, float] | None = None
        # Keep preview layout preferences separate from the installed V1.26.4 release.
        self._base_settings = editor_settings(QSettings)
        self.ecu_profile_id = "361830"
        self.ecu_profile_label = "EDC16C31 361830"
        self.settings = ECUSettings(self._base_settings, self.ecu_profile_id)
        try:
            fuel_settings = json.loads(str(self.settings.value("log/fuel_estimate_config", "{}")))
            self.fuel_estimate_config = FuelEstimateConfig(**fuel_settings)
        except (ValueError, TypeError):
            self.fuel_estimate_config = FuelEstimateConfig()
        self._loaded_layout_type: str | None = None
        try:
            display = json.loads(str(self.settings.value("log/channel_display", "{}")))
        except (ValueError, TypeError):
            display = {}
        self.channel_styles = display.get("styles", {}) if isinstance(display, dict) else {}
        if not isinstance(self.channel_styles, dict):
            self.channel_styles = {}
        self.reading_signals = display.get("readings") if isinstance(display, dict) else None
        if not isinstance(self.reading_signals, list):
            self.reading_signals = None
        self.axis_override_x: str | None = None
        self.axis_override_y: str | None = None

        self.figure = Figure(figsize=(8, 4.2))
        # Manual margins avoid matplotlib tight_layout warnings on stacked, short axes.
        self.figure.subplots_adjust(left=0.065, right=0.985, bottom=0.10, top=0.975, hspace=0.12)
        self.canvas = FigureCanvas(self.figure)
        self.canvas.setFocusPolicy(Qt.StrongFocus)
        self.axes: list = []
        self.cursor_lines: list = []
        self.range_patches: list = []
        self.graph_readings: list[GraphReadings] = []
        self._display_lookup_log = None
        self._display_lookup = {}

        self.file_label = QLabel("No TestO log loaded")
        self.file_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.profile_label = QLabel("")
        self.profile_label.setWordWrap(True)
        self.missing_label = QLabel("")
        self.missing_label.setWordWrap(True)
        self.missing_label.setStyleSheet("color: #8a4f00;")
        self.time_label = QLabel("Time —")
        self.time_label.setFixedWidth(145)
        self.time_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.gauges = {}
        self.reading_strip = ReadingStrip(self._signal_name, self.add_floating_gauge, self,
                                          tooltip_for=self.fuel_estimate_tooltip)
        self.reading_strip.set_signals([])
        self.reading_strip.layoutChanged.connect(self._set_reading_channels)
        self.reading_strip.manageRequested.connect(self.configure_gauges)
        self.reading_strip.resetRequested.connect(self.restore_default_gauges)
        self.reading_strip.fuelEstimatesRequested.connect(self.configure_fuel_estimates)
        self.file_label.setWordWrap(False)
        self.file_label.setFixedHeight(22)
        self.file_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.power_status = QLabel(recovery_notice)
        self.power_status.setTextFormat(Qt.PlainText)
        self.power_status.setWordWrap(True)
        self.power_status.setVisible(bool(recovery_notice))
        self._power_timer = QTimer(self)
        self._power_timer.setInterval(2000)
        self._power_timer.timeout.connect(self._refresh_logging_power)
        self.live_status = QLabel("")
        self.live_status.setFixedHeight(22)
        self.live_status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.live_status.setTextFormat(Qt.PlainText)
        self.live_status.hide()
        self._pending_status = ""
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(500)
        self._status_timer.timeout.connect(self._flush_live_status)
        self._status_timer.start()
        self.profile_label.setWordWrap(True)

        self.load_button = QPushButton("Open Log…")
        self.load_button.clicked.connect(self.open_dialog)
        self.live_button = QPushButton("Live logging…")
        self.live_button.setToolTip("Live logging with cable or TestO, adjustable parameters, profiles and CSV capture")
        self.live_button.clicked.connect(self.open_live_dialog)
        self.play_button = QPushButton("▶ Play")
        self.play_button.setEnabled(False)
        self.play_button.clicked.connect(self.toggle_play)
        self.clear_range_button = QPushButton("Clear")
        self.clear_range_button.setEnabled(False)
        self.clear_range_button.clicked.connect(self.clear_range)
        self.fit_button = QPushButton("Fit time")
        self.fit_button.clicked.connect(self.fit_time)
        self.zoom_range_button = QPushButton("Zoom sel")
        self.zoom_range_button.setEnabled(False)
        self.zoom_range_button.clicked.connect(self.zoom_to_range)
        self.channel_setup_button = QPushButton("Channels…")
        self.channel_setup_button.setToolTip("Assign channels to graph areas, choose colors and scales, and configure bottom readings")
        self.channel_setup_button.clicked.connect(self.open_channel_setup)
        self.fuel_estimates_button = QPushButton("Fuel estimates…")
        self.fuel_estimates_button.setToolTip("Set cylinder count and diesel density for estimated L/h and L/100 km")
        self.fuel_estimates_button.clicked.connect(self.configure_fuel_estimates)

        self.graph_combo = QComboBox()
        self.graph_combo.setMinimumWidth(120)
        self.graph_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.graph_combo.setMinimumContentsLength(16)
        self.graph_combo.currentIndexChanged.connect(lambda _i: self._update_graph_buttons())
        self.signal_combo = QComboBox()
        self.signal_combo.setEditable(True)
        self.signal_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        # Human-readable signal names are the primary UI. Keep the raw TestO name
        # visible after them, but give the popup enough room that the English label
        # is never reduced to fragments such as "Boo...ERT".
        self.signal_combo.setFixedWidth(320)
        self.signal_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.signal_combo.setMinimumContentsLength(24)
        self.signal_combo.completer().setFilterMode(Qt.MatchContains)
        self.signal_combo.completer().setCaseSensitivity(Qt.CaseInsensitive)
        self.signal_combo.view().setFixedWidth(320)

        self.signal_combo.setToolTip("Choose a logged signal by its English name. The raw TestO signal name is shown after it for reference.")
        self.add_trace_button = QPushButton("+ Trace")
        self.add_trace_button.clicked.connect(self.add_trace_to_graph)
        self.new_graph_button = QPushButton("+ Graph")
        self.new_graph_button.clicked.connect(self.add_new_graph)
        self.remove_trace_button = QPushButton("− Trace")
        self.remove_trace_button.clicked.connect(self.remove_trace_from_graph)
        self.remove_graph_button = QPushButton("− Graph")
        self.remove_graph_button.clicked.connect(self.remove_graph)
        self.auto_graph_button = QPushButton("Auto")
        self.auto_graph_button.setToolTip("Return to the automatic current-table trace layout")
        self.auto_graph_button.clicked.connect(self.reset_auto_graphs)
        self.save_layout_button = QPushButton("Save")
        self.save_layout_button.setToolTip("Save the current graph/trace layout for this table type (N75, Boost, Rail, SOI, etc.)")
        self.save_layout_button.clicked.connect(self.save_layout_for_type)
        self.clear_layout_button = QPushButton("Forget")
        self.clear_layout_button.setToolTip("Forget the saved graph layout for this table type and return to Auto")
        self.clear_layout_button.clicked.connect(self.clear_saved_layout_for_type)

        row = QHBoxLayout()
        row.addWidget(self.load_button)
        row.addWidget(self.live_button)
        row.addWidget(self.play_button)
        row.addWidget(self.clear_range_button)
        row.addWidget(self.fit_button)
        row.addWidget(self.zoom_range_button)
        row.addWidget(self.channel_setup_button)
        row.addWidget(self.time_label)
        row.addStretch(1)
        self.compact_button = QPushButton("Hide setup")
        self.compact_button.setCheckable(True)
        self.compact_button.setToolTip("Collapse graph setup to make room for the log plot")
        row.addWidget(self.compact_button)
        self.readings_button = QPushButton("Hide readings")
        self.readings_button.setCheckable(True)
        self.readings_button.setToolTip("Show or hide the bottom instrument strip")
        row.addWidget(self.readings_button)
        graph_row = QHBoxLayout()
        graph_row.addWidget(QLabel("Graph:"))
        graph_row.addWidget(self.graph_combo)
        graph_row.addWidget(QLabel("Signal:"))
        graph_row.addWidget(self.signal_combo)
        graph_row.addStretch(1)
        graph_row.addWidget(self.add_trace_button)
        graph_row.addWidget(self.new_graph_button)
        edit_row = QHBoxLayout()
        edit_row.addWidget(self.remove_trace_button)
        edit_row.addWidget(self.remove_graph_button)
        edit_row.addWidget(self.auto_graph_button)
        edit_row.addWidget(self.save_layout_button)
        edit_row.addWidget(self.clear_layout_button)
        edit_row.addWidget(self.fuel_estimates_button)

        edit_row.addStretch(1)
        meta = QHBoxLayout()
        meta.addWidget(self.file_label, 1)
        meta.addWidget(self.profile_label, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.addLayout(row)
        self.setup_panel = QWidget()
        setup_layout = QVBoxLayout(self.setup_panel)
        setup_layout.setContentsMargins(0, 0, 0, 0)
        setup_layout.addLayout(graph_row)
        setup_layout.addLayout(edit_row)
        setup_layout.addLayout(meta)
        layout.addWidget(self.setup_panel)
        layout.addWidget(self.live_status); layout.addWidget(self.power_status)
        layout.addWidget(self.missing_label)
        layout.addWidget(self.canvas, 1)
        layout.addWidget(self.reading_strip)
        self.setMinimumHeight(220)
        self.compact_button.toggled.connect(self._set_compact_controls)
        self.readings_button.toggled.connect(self._set_readings_hidden)
        self.compact_button.setChecked(self.settings.value("log/compact_controls", True, type=bool))
        self.readings_button.setChecked(self.settings.value("log/hide_readings", False, type=bool))

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._play_step)
        self._plot_resize_timer = QTimer(self)
        self._plot_resize_timer.setSingleShot(True)
        self._plot_resize_timer.setInterval(80)
        self._plot_resize_timer.timeout.connect(self._redraw)
        self.canvas.mpl_connect("button_press_event", self._mouse_press)
        self.canvas.mpl_connect("button_release_event", self._mouse_release)
        self.canvas.mpl_connect("motion_notify_event", self._mouse_motion)
        self.canvas.mpl_connect("scroll_event", self._mouse_scroll)
        self.canvas.mpl_connect("key_press_event", self._key_press)
        self.canvas.mpl_connect("draw_event", lambda _event: self._position_graph_readings())
        self._refresh_graph_combo()

    def _signal_name(self, signal):
        if self.ecu_profile_id == "361830":
            return short_signal_name(signal)
        return self.catalog.label_for(signal)

    def set_ecu_profile(self, profile_id, label=None):
        """Switch independently of the selected table, including BINs without maps."""
        profile_id = str(profile_id or "unassigned")
        if profile_id == self.ecu_profile_id:
            return
        if not self.stop_live():
            raise EditorError("Wait for live logging to stop before changing ECU profiles.")
        self._remember_graphs()
        self.settings.setValue("log/channel_display", json.dumps({
            "styles": self.channel_styles, "readings": self.reading_signals}))
        self.settings.sync()
        for gauge in list(self.gauges.values()):
            gauge.close()
        self.ecu_profile_id = profile_id
        self.ecu_profile_label = label or profile_id
        self.settings = ECUSettings(self._base_settings, profile_id)
        self.catalog = SignalCatalog.load_tsv(bundled_signal_catalog_path())
        adx_styles = {}
        if label and label.strip() == "MS43X001":
            from .adx import load_adx_catalog, bundled_ms43_adx_path
            if bundled_ms43_adx_path().is_file():
                self.catalog, adx_styles = load_adx_catalog(bundled_ms43_adx_path(), include_states=True)
            else:
                self.catalog = SignalCatalog([])
        try:
            display = json.loads(str(self.settings.value("log/channel_display", "{}")))
        except (ValueError, TypeError):
            display = {}
        self.channel_styles = display.get("styles", {}) if isinstance(display, dict) else {}
        if not isinstance(self.channel_styles, dict):
            self.channel_styles = {}
        self.reading_signals = display.get("readings") if isinstance(display, dict) else None
        if not isinstance(self.reading_signals, list):
            self.reading_signals = None
        try:
            config = json.loads(str(self.settings.value("log/fuel_estimate_config", "{}")))
            self.fuel_estimate_config = FuelEstimateConfig(**config)
        except (ValueError, TypeError):
            self.fuel_estimate_config = FuelEstimateConfig()
        if not self.settings.contains("log/channel_display") and adx_styles:
            self.channel_styles = adx_styles
            self.reading_signals = [name for name in ("N", "MAP", "MAF", "TPS", "TCO", "TIA", "LAM")
                                    if name in self.catalog.by_result]
        self.axis_override_x = self.axis_override_y = None
        self.definition = self.profile = None
        self._loaded_layout_type = None
        self.custom_graphs = []
        # A previous ECU's samples must not appear against the new calibration.
        self.log = None
        self.cursor_index = 0
        self.map_value = None
        self.range_seconds = self.view_xlim = None
        self.timer.stop()
        self.play_button.setText("▶ Play")
        self.play_button.setEnabled(False)
        self.clear_range_button.setEnabled(False)
        self.zoom_range_button.setEnabled(False)
        self.file_label.setText("No log loaded — " + self.ecu_profile_label)
        self.compact_button.setChecked(self.settings.value("log/compact_controls", True, type=bool))
        self.readings_button.setChecked(self.settings.value("log/hide_readings", False, type=bool))
        self._populate_signal_combo()
        self._update_profile()
        self._refresh_graph_combo()
        self._redraw()
        self.live_button.setToolTip("Live logging profile: " + self.ecu_profile_label)
        self.statusChanged.emit("Log / gauge / graph profile: " + self.ecu_profile_label)

    def _remember_graphs(self):
        # Persist the actual custom state, including Auto, without freezing an
        # automatically generated layout into a manual selection.
        key = self._layout_settings_key()
        if key and self._loaded_layout_type is not None:
            self.settings.setValue(key, json.dumps({"graphs": self.custom_graphs,
                "styles": self.channel_styles, "readings": self.reading_signals}))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "_plot_resize_timer"):
            self._plot_resize_timer.start()

    def open_live_dialog(self):
        if self.direct_logger is not None or self.live_thread is not None:
            self.stop_live()
            return
        if self.ecu_profile_label.strip() == "MS43X001":
            from .adx import bundled_ms43_adx_path
            if not bundled_ms43_adx_path().is_file():
                QMessageBox.information(self, APP_NAME,
                    'Install your MS43X logging ADX at:\n' + str(bundled_ms43_adx_path()) +
                    '\n\nSee docs/LOCAL_RESOURCES.md for the optional local resource setup.')
                return
            from .ms43_logging_gui import MS43LoggingDialog
            dlg = MS43LoggingDialog(self, settings=self.settings,
                cable_defaults=ECUSettings(self._base_settings, "361830"))
            if dlg.exec() == QDialog.Accepted:
                try:
                    self.start_direct_live(dlg.config(), dlg.catalog)
                except (ValueError, OSError, RuntimeError) as error:
                    QMessageBox.warning(self, APP_NAME, "Could not start MS43X logging:\n" + str(error))
            return
        catalog = load_parameters(bundled_signal_catalog_path().parent / "live_parameters.json")
        if self.ecu_profile_id != "361830":
            QMessageBox.information(self, APP_NAME,
                "Direct live logging currently supports EDC16C31 361830 (DDE5). "
                "This ECU's saved display and graph profile is separate.")
            return
        dlg = LoggingDialog(catalog, self, settings=self.settings)
        if dlg.exec() == QDialog.Accepted:
            try:
                self.start_direct_live(dlg.config(), catalog)
            except (ValueError, OSError, RuntimeError) as error:
                QMessageBox.warning(self, APP_NAME, "Could not start live logging:\n" + str(error))

    def start_direct_live(self, config, catalog):
        transport = config.get("transport", "ediabas")
        from .ms43_live_logging import MS43LiveLogger
        if config.get("transport") == "ms43x" and self.ecu_profile_label.strip() != "MS43X001":
            raise ValueError("Load the matching MS43X001 BIN/XDF before using its live logger.")
        backends = {"ediabas": NativeLiveLogger, "testo": LiveLogger, "serial": SerialLiveLogger,
                    "ms43x": MS43LiveLogger}
        if transport not in backends:
            raise ValueError("Unsupported live logging transport: " + str(transport))
        if not self.stop_live():
            return
        # Construct before replacing the reviewed log or changing the live UI.
        # Invalid serial definitions/profiles must fail without a false LIVE state.
        logger = backends[transport](config, catalog, self)
        labels = {"ediabas": "EDIABAS LIVE", "testo": "TestO LIVE", "serial": "SERIAL LIVE", "ms43x": "MS43X LIVE"}
        self.log = LiveTestOLog(self.catalog, label=labels[transport], fuel_estimate_config=self.fuel_estimate_config)
        self.live_mode = True
        self.cursor_index = 0; self.range_seconds = None; self.view_xlim = None
        self.custom_graphs = []; self._loaded_layout_type = None
        self.timer.stop(); self.play_button.setText("▶ Play"); self.play_button.setEnabled(False)
        self.clear_range_button.setEnabled(False); self.zoom_range_button.setEnabled(False)
        self.live_button.setText("■ Stop logging")
        destination = config.get("output") or "Live preview (no file)"
        self.file_label.setText(Path(destination).name if config.get("output") else destination)
        self.file_label.setToolTip(destination)
        statuses = {"ediabas": "Connecting to EDIABAS…", "testo": "Connecting to TestO…",
                    "serial": "Connecting to serial cable…", "ms43x": "Connecting to MS43X DS2…"}
        self._set_live_status(statuses[transport])
        self._flush_live_status()
        self._populate_signal_combo(); self._update_profile(); self._refresh_graph_combo(); self._redraw()
        self.direct_logger = logger
        logger.sampleReady.connect(lambda t, d: self._live_sample(t, d, "MS43X001" if transport == "ms43x" else "D50M47A", config["profile"]["name"]) if self.direct_logger is logger and self.live_mode else None)
        logger.status.connect(self._set_live_status)
        logger.error.connect(self._live_error)
        logger.stopped.connect(self._direct_finished)
        self.logLoaded.emit(self.log)
        self._start_logging_power(config)
        try:
            logger.start()
        except Exception:
            self._stop_logging_power()
            self.direct_logger = None; self.live_mode = False
            self.live_button.setText("Live logging…")
            logger.deleteLater()
            raise

    def _direct_finished(self):
        self._stop_logging_power()
        logger = self.direct_logger
        self.direct_logger = None
        self.live_button.setEnabled(True)
        self.live_mode = False
        self.live_button.setText("Live logging…")
        self.play_button.setEnabled(bool(self.log and self.log.sample_count))
        self.view_xlim = None
        self._redraw()
        if logger and not self._pending_status.startswith("LIVE ERROR"):
            self._set_live_status(f"Stopped — {logger.count:,} replies" + (" — saved to CSV" if logger.capture else " — preview only"))
            self._flush_live_status()
        if logger:
            logger.deleteLater()

    def start_live(self, endpoint: str, token: str, ecu: str, job: str, params: dict, interval_ms: int):
        if not self.stop_live():
            return
        self.log = LiveTestOLog(self.catalog, label="TestO LIVE", fuel_estimate_config=self.fuel_estimate_config)
        self.live_mode = True
        self.cursor_index = 0; self.range_seconds = None; self.view_xlim = None; self.custom_graphs = []; self._loaded_layout_type = None
        self.timer.stop(); self.play_button.setText("▶ Play"); self.play_button.setEnabled(False)
        self.clear_range_button.setEnabled(False); self.zoom_range_button.setEnabled(False)
        self.file_label.setText(f"LIVE — {ecu} / {job} — waiting for first sample…")
        self.live_button.setText("■ Stop LIVE")
        self._populate_signal_combo(); self._update_profile(); self._refresh_graph_combo(); self._redraw()
        thread = QThread(self); worker = TestOLiveWorker(endpoint, token, ecu, job, params, interval_ms); worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.sampleReady.connect(lambda t, d: self._live_sample(t, d, ecu, job) if self.live_thread is thread and self.live_mode else None)
        worker.error.connect(lambda message: self._live_error(message) if self.live_thread is thread and self.live_mode else None)
        worker.stopped.connect(thread.quit); worker.stopped.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(lambda: self._live_finished(thread))
        self.live_thread = thread; self.live_worker = worker
        self._start_logging_power(dict(keep_awake=self.settings.value('live126/keep_awake', True, type=bool),
                                       lid_closed=self.settings.value('live126/lid_closed', True, type=bool)))
        try: thread.start()
        except Exception:
            self._stop_logging_power()
            self.live_thread = None; self.live_worker = None; self.live_mode = False
            raise
        self.statusChanged.emit(f"TestO LIVE started: {ecu} / {job} @ {interval_ms} ms")

    def stop_live(self):
        if self.direct_logger is not None:
            if self.direct_logger.stop() is False:
                self.live_mode = False
                self.live_button.setText("Stopping logging…")
                self.live_button.setEnabled(False)
                self._set_live_status("Waiting for the diagnostic connection to close…")
                self._flush_live_status()
                return False
        if self.live_worker is not None:
            self.live_worker.stop()
        if self.live_thread is not None:
            self.live_thread.quit()
            if not self.live_thread.wait(1200):
                self.live_mode = False
                self.live_button.setText("Stopping logging…")
                self.live_button.setEnabled(False)
                self._set_live_status("Waiting for the current TestO request to finish…")
                self._flush_live_status()
                return False
        self._stop_logging_power()
        self.live_thread = None; self.live_worker = None
        if self.live_mode:
            self.live_mode = False; self.live_button.setText("Live logging…")
        return True

    def _live_finished(self, thread=None):
        if thread is not None and self.live_thread is not thread:
            return
        self._stop_logging_power()
        self.live_thread = None; self.live_worker = None; self.live_button.setText("Live logging…")
        self.live_mode = False
        self.live_button.setEnabled(True)
        self.play_button.setEnabled(bool(self.log and self.log.sample_count))

    def _live_error(self, message: str):
        self._stop_logging_power()
        self.statusChanged.emit("Live logging error: " + message)
        self._set_live_status("LIVE ERROR — " + message)
        self._flush_live_status()
        QMessageBox.warning(self, APP_NAME, "Live logging stopped:\n" + message)

    def _start_logging_power(self, config):
        text = self.power_guard.start(keep_awake=config.get('keep_awake', False), lid_closed=config.get('lid_closed', False))
        self.power_status.setText(text); self.power_status.show()
        self._power_timer.start()

    def _refresh_logging_power(self):
        self.power_status.setText(self.power_guard.refresh())

    def _stop_logging_power(self):
        self._power_timer.stop()
        text = self.power_guard.stop()
        self.power_status.setText(text); self.power_status.setVisible(bool(text))

    def _canonical_live_fields(self, fields: dict[str, float]) -> dict[str, float]:
        # TestO/EDIABAS may return the exact RESULTNAME, lower/upper variants, or
        # records keyed by ARG. Prefer exact canonical RESULTNAME names used by CSV logs.
        out: dict[str, float] = {}
        upper = {str(k).upper(): v for k, v in fields.items()}
        arg_to_result = {d.arg.upper(): d.result_name for d in self.catalog.definitions if d.arg}
        for key, value in fields.items():
            ku = str(key).upper()
            if ku in self.catalog.by_result:
                out[ku] = value
            elif ku in arg_to_result:
                out[arg_to_result[ku]] = value
            elif ku.endswith("_WERT") and ku.startswith("STAT_"):
                out[ku] = value
        # Some JSON wrappers preserve canonical result names only as a case-insensitive key.
        for result_name in self.catalog.by_result:
            if result_name.upper() in upper:
                out[result_name] = upper[result_name.upper()]
        return out

    def _live_sample(self, seconds: float, fields: object, ecu: str, job: str):
        if not isinstance(self.log, LiveTestOLog):
            return
        raw = fields if isinstance(fields, dict) else {}
        sample = self._canonical_live_fields(raw)
        # If TestO returned numeric fields we do not yet know, keep them too: they can
        # still be manually plotted and help identify the job result schema at the car.
        if not sample:
            sample = {str(k): float(v) for k, v in raw.items() if isinstance(v, (int, float))}
        if not sample:
            return
        known_before = set(self.log.values)
        fuel_available_before = tuple(self.log.has(signal) for signal in (FUEL_RATE_SIGNAL, FUEL_CONSUMPTION_SIGNAL))
        self.log.append_sample(seconds, sample)
        self.cursor_index = self.log.sample_count - 1
        fuel_available_after = tuple(self.log.has(signal) for signal in (FUEL_RATE_SIGNAL, FUEL_CONSUMPTION_SIGNAL))
        if set(self.log.values) != known_before or fuel_available_after != fuel_available_before:
            self._populate_signal_combo(); self._update_profile(); self._load_saved_layout_for_type(); self._refresh_graph_combo(); self._redraw()
        else:
            # Replot periodically because matplotlib Line2D arrays need the newly appended data.
            # At diagnostic rates this is still light enough, while avoiding redraw on every
            # individual low-level result if TestO returns quickly.
            if self.log.sample_count <= 3 or self.log.sample_count % 3 == 0:
                self._redraw()
            else:
                self._update_readout()
        if self.direct_logger is None:
            self._set_live_status(f"LIVE — {self.log.sample_count:,} samples / {self.log.duration:.1f} s")
        self.cursorChanged.emit(self.cursor_index)

    def _set_compact_controls(self, compact):
        self.setup_panel.setVisible(not compact)
        self.compact_button.setText("Show setup" if compact else "Hide setup")
        self.settings.setValue("log/compact_controls", compact)

    def _set_readings_hidden(self, hidden):
        self.reading_strip.setVisible(not hidden)
        self.readings_button.setText("Show readings" if hidden else "Hide readings")
        self.settings.setValue("log/hide_readings", hidden)

    def _reading_channels(self):
        if self.reading_signals is not None:
            return [s for s in self.reading_signals if isinstance(s, str)]
        preferred = [SIG_RPM, SIG_BOOST, SIG_BOOST_SET, SIG_IQ, FUEL_RATE_SIGNAL, FUEL_CONSUMPTION_SIGNAL,
                     SIG_N75, SIG_MAF, SIG_RAIL, SIG_SOI, SIG_SPEED] if self.ecu_profile_id == "361830" else []
        if not self.log:
            return preferred
        available = self.log.available_signals()
        return [s for s in preferred if s in available] + [s for s in available if s not in preferred]

    def _set_reading_channels(self, signals):
        self.reading_signals = list(dict.fromkeys(s for s in signals if isinstance(s, str)))
        self.settings.setValue("log/channel_display", json.dumps({"styles": self.channel_styles, "readings": self.reading_signals}))
        self._update_readout()

    def restore_default_gauges(self):
        self.reading_signals = None
        self.settings.setValue("log/channel_display", json.dumps({"styles": self.channel_styles, "readings": None}))
        self._update_readout()

    def fuel_estimate_tooltip(self, signal):
        if signal not in (FUEL_RATE_SIGNAL, FUEL_CONSUMPTION_SIGNAL):
            return ""
        config = self.fuel_estimate_config
        text = (f"Calculated from ECU total IQ and RPM: {config.cylinders} cylinders, four-stroke engine, "
                f"diesel density {config.density_kg_l:.3f} kg/L.\n"
                "Uses total IQ in mg per cylinder per cycle; pilot quantity is not added again.\n"
                "An estimate, rather than independently measured fuel flow.")
        if signal == FUEL_CONSUMPTION_SIGNAL:
            text += f"\nRequires vehicle speed; unavailable below {config.min_speed_kmh:g} km/h."
        return text

    def configure_fuel_estimates(self):
        dlg = FuelEstimateDialog(self.fuel_estimate_config, self)
        if dlg.exec() == QDialog.Accepted:
            self.set_fuel_estimate_config(dlg.config())

    def set_fuel_estimate_config(self, config):
        self.fuel_estimate_config = config
        self.settings.setValue("log/fuel_estimate_config", json.dumps({
            "cylinders": config.cylinders, "density_kg_l": config.density_kg_l,
            "min_speed_kmh": config.min_speed_kmh,
        }))
        if self.log:
            self.log.set_fuel_estimate_config(config)
        self._display_lookup_log = None
        self._display_lookup = {}
        self._populate_signal_combo()
        self._redraw()

    def configure_gauges(self):
        session_signals = self._available_gauge_signals()
        available = list(dict.fromkeys(session_signals + self._reading_channels()
                                      + [d.result_name for d in self.catalog.definitions]
                                      + [FUEL_RATE_SIGNAL, FUEL_CONSUMPTION_SIGNAL]))
        dlg = GaugeSetupDialog(self._reading_channels(), available, self._signal_name, self,
                               session_signals=session_signals,
                               available_only=self.settings.value("log/gauges_available_only", True, type=bool),
                               tooltip_for=self.fuel_estimate_tooltip)
        if dlg.exec() == QDialog.Accepted:
            self.settings.setValue("log/gauges_available_only", dlg.available_only.isChecked())
            self._set_reading_channels(dlg.selected())
            self.readings_button.setChecked(False)

    def _available_gauge_signals(self):
        available = [s for s in self.log.available_signals() if self.log.has(s)] if self.log else []
        logger = self.direct_logger
        if logger is not None:
            requested = [logger.catalog[arg]["result"] for arg in logger.profile["parameters"]]
            available.extend(requested)
            groups = [{logger.catalog[arg]["result"] for arg in group} for group in logger.groups]
            if any({SIG_MAF, SIG_IQ} <= group for group in groups):
                available.append(CALC_EST_LAMBDA)
            if any({SIG_BOOST, SIG_BOOST_SET} <= group for group in groups):
                available.append(CALC_BOOST_ERROR)
            if any({SIG_RPM, SIG_IQ} <= group for group in groups):
                available.append(FUEL_RATE_SIGNAL)
            if any({SIG_RPM, SIG_IQ, SIG_SPEED} <= group for group in groups):
                available.append(FUEL_CONSUMPTION_SIGNAL)
        return list(dict.fromkeys(available))

    def open_channel_setup(self):
        if not self.log:
            return
        session_signals = self._available_gauge_signals()
        gauge_available = list(dict.fromkeys(session_signals + self._reading_channels()
                                            + [d.result_name for d in self.catalog.definitions]
                                            + [FUEL_RATE_SIGNAL, FUEL_CONSUMPTION_SIGNAL]))
        dlg = ChannelSetupDialog(self._effective_graphs(), self.channel_styles,
                                 self._reading_channels(), self._selectable_log_signals(), self._signal_name, self,
                                 gauge_available=gauge_available, session_signals=session_signals,
                                 available_only=self.settings.value("log/gauges_available_only", True, type=bool),
                                 tooltip_for=self.fuel_estimate_tooltip)
        if dlg.exec() == QDialog.Accepted:
            self.custom_graphs = dlg.graphs
            self.channel_styles = dlg.styles
            self.reading_signals = dlg.readings
            self.settings.setValue("log/gauges_available_only", dlg.gauge_available_only)
            self.settings.setValue("log/channel_display", json.dumps({"styles": self.channel_styles, "readings": self.reading_signals}))
            self._refresh_graph_combo()
            self._redraw()
            # Apply and remember the setup together; the existing Save control also remains available.
            self.save_layout_for_type()

    def _set_live_status(self, text):
        self._pending_status = text

    def _flush_live_status(self):
        if self.live_status.text() != self._pending_status:
            self.live_status.setText(self._pending_status)
            self.live_status.setToolTip(self._pending_status)
        self.live_status.setVisible(bool(self._pending_status))

    def open_dialog(self):
        start = str(Path.home())
        path, _ = QFileDialog.getOpenFileName(self, "Open Log", start, "CSV logs (*.csv *.txt);;All files (*.*)")
        if path:
            self.open_log(path)

    def open_log(self, path: Path | str) -> bool:
        try:
            log = TestOLog(path, self.catalog, fuel_estimate_config=self.fuel_estimate_config)
        except Exception as exc:
            QMessageBox.critical(self, APP_NAME, f"Could not open TestO log:\n{exc}")
            return False
        if not self.stop_live():
            return False
        self._set_live_status("")
        self._flush_live_status()
        self._remember_graphs()
        self.log = log
        self.cursor_index = 0
        self.range_seconds = None
        self.view_xlim = None
        self.custom_graphs = []
        self._loaded_layout_type = None
        self.timer.stop()
        self.play_button.setText("▶ Play")
        self.play_button.setEnabled(log.sample_count > 1)
        self.clear_range_button.setEnabled(False)
        self.zoom_range_button.setEnabled(False)
        self.file_label.setText(
            f"{log.path.name} — {log.sample_count:,} samples / {log.duration:.2f} s"
            + (f" — {log.duplicates_removed:,} duplicate rows removed" if log.duplicates_removed else "")
            + (f" — {log.complementary_rows_merged:,} complementary rows merged" if log.complementary_rows_merged else "")
        )
        self._populate_signal_combo()
        self._update_profile()
        self._refresh_graph_combo()
        self._redraw()
        self.logLoaded.emit(log)
        self.cursorChanged.emit(0)
        self.statusChanged.emit(f"Loaded TestO log: {log.path.name} ({log.sample_count:,} unique samples)")
        return True

    def _populate_signal_combo(self):
        if self._display_lookup_log is not self.log:
            self._display_lookup_log = self.log
            self._display_lookup = {}
        selected = self.signal_combo.currentData()
        self.signal_combo.blockSignals(True)
        self.signal_combo.clear()
        if self.log:
            for signal in self._selectable_log_signals():
                unit = self.log.units.get(signal) or self.catalog.unit_for(signal)
                label = self._signal_name(signal)
                item_text = f"{label}  [{unit}]" if unit else label
                self.signal_combo.addItem(item_text, signal)
                # Full readable/technical identity remains available even if the
                # main toolbar is narrow.
                tooltip = f"{item_text}\n{signal}"
                explanation = self.fuel_estimate_tooltip(signal)
                self.signal_combo.setItemData(self.signal_combo.count() - 1,
                                             tooltip + ("\n" + explanation if explanation else ""), Qt.ToolTipRole)
        idx = self.signal_combo.findData(selected)
        self.signal_combo.setCurrentIndex(idx if idx >= 0 else (0 if self.signal_combo.count() else -1))
        self.signal_combo.blockSignals(False)
        self._update_graph_buttons()

    def _selectable_log_signals(self):
        if not self.log:
            return []
        return [signal for signal in self.log.available_signals()
                if signal not in (FUEL_RATE_SIGNAL, FUEL_CONSUMPTION_SIGNAL) or self.log.has(signal)]

    def set_definition(self, definition: CalibrationDefinition | None):
        old_type = self._layout_type_key()
        self._remember_graphs()
        self.definition = definition
        self._update_profile()
        new_type = self._layout_type_key()
        if new_type != old_type:
            self._loaded_layout_type = None
            self.custom_graphs = []
        self._load_saved_layout_for_type()
        self._refresh_graph_combo()
        self._redraw()

    def _update_profile(self):
        self.profile = profile_for_definition(self.definition, self.log) if self.definition is not None else None
        if self.profile is not None and self.definition is not None:
            family, _addresses, _target = MapEditor._candidate_family(self.definition)
            known = {
                "N75": (SIG_IQ, SIG_RPM), "Boost Target": (SIG_IQ, SIG_RPM),
                "SOI": (SIG_IQ, SIG_RPM), "TQ2IQ": (SIG_TORQUE, SIG_RPM),
                "Lambda": (SIG_MAF, SIG_RPM),
            }
            if family in known and self.ecu_profile_label.strip() != "MS43X001":
                self.profile.x_signal, self.profile.y_signal = known[family]
                self.profile.notes = [n for n in self.profile.notes if not n.startswith("No automatic log binding for ")]
        if self.profile is not None:
            if self.axis_override_x:
                self.profile.x_signal = self.axis_override_x
                self.profile.notes = [n for n in self.profile.notes if not n.startswith("No automatic log binding for X axis")]
            if self.axis_override_y:
                self.profile.y_signal = self.axis_override_y
                self.profile.notes = [n for n in self.profile.notes if not n.startswith("No automatic log binding for Y axis")]
        if self.profile is None:
            self.profile_label.setText("")
            self.missing_label.setText("")
            self.missing_label.hide()
            self._load_saved_layout_for_type()
            return
        self.profile_label.setText(f"<b>{self.profile.name}</b>")
        if not self.log:
            self.missing_label.setText("No log loaded.")
            self.missing_label.show()
            return
        missing_position = [s for s in self.profile.required_position_signals if not self.log.has(s)]
        missing_traces = [s for s in self.profile.traces if not self.log.has(s)]
        notes = list(self.profile.notes)
        parts: list[str] = []
        if missing_position:
            friendly = ", ".join(f"{self.catalog.label_for(s)} ({s})" for s in missing_position)
            parts.append(f"Map-position overlay unavailable: missing in current log — {friendly}.")
        if missing_traces:
            friendly = ", ".join(f"{self.catalog.label_for(s)} ({s})" for s in missing_traces)
            parts.append(f"Table-aware trace missing in current log — {friendly}.")
        if notes:
            parts.append(" ".join(notes))
        self.missing_label.setText("  ".join(parts))
        self.missing_label.setVisible(bool(parts))
        self._load_saved_layout_for_type()

    def set_axis_overrides(self, x_signal: str | None, y_signal: str | None):
        self.axis_override_x = x_signal or None
        self.axis_override_y = y_signal or None
        self._update_profile()
        self._redraw()

    def _layout_type_key(self) -> str | None:
        """Stable per-table-type key used for persisted log graph layouts."""
        if self.profile and self.profile.name:
            return str(self.profile.name).strip()
        if self.definition is not None:
            category = str(getattr(self.definition, "category", "") or "").strip()
            if category:
                return category
        return "General"

    def _layout_settings_key(self) -> str | None:
        key = self._layout_type_key()
        if not key:
            return None
        safe = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in key)
        return f"log_graph_layouts/{safe}"

    def _load_saved_layout_for_type(self):
        key = self._layout_type_key()
        if not key or self._loaded_layout_type == key:
            return
        self._loaded_layout_type = key
        self.custom_graphs = []
        settings_key = self._layout_settings_key()
        raw = self.settings.value(settings_key, "") if settings_key else ""
        if not raw:
            return
        try:
            data = json.loads(str(raw))
            graphs = data.get("graphs", data) if isinstance(data, dict) else data
            if isinstance(graphs, list):
                cleaned: list[list[str]] = []
                for group in graphs:
                    if not isinstance(group, list):
                        continue
                    signals = [str(sig) for sig in group if isinstance(sig, str)]
                    cleaned.append(signals)
                if cleaned:
                    self.custom_graphs = cleaned
            if isinstance(data, dict):
                if isinstance(data.get("styles"), dict):
                    self.channel_styles = data["styles"]
                if not self.settings.contains("log/channel_display") and self.reading_signals is None and isinstance(data.get("readings"), list):
                    self.reading_signals = data["readings"]
        except Exception:
            pass

    def save_layout_for_type(self):
        key = self._layout_type_key()
        settings_key = self._layout_settings_key()
        if not key or not settings_key:
            self.statusChanged.emit("No table type is available for saving a graph layout.")
            return
        graphs = self._effective_graphs()
        self.settings.setValue(settings_key, json.dumps({"type": key, "graphs": graphs,
                              "styles": self.channel_styles, "readings": self.reading_signals}))
        self._loaded_layout_type = key
        self.statusChanged.emit(f"Saved log graph layout for {self.ecu_profile_label} / {key}.")
        self._update_graph_buttons()

    def clear_saved_layout_for_type(self):
        key = self._layout_type_key()
        settings_key = self._layout_settings_key()
        if settings_key:
            self.settings.remove(settings_key)
        self.custom_graphs = []
        self._loaded_layout_type = key
        self._refresh_graph_combo()
        self._redraw()
        if key:
            self.statusChanged.emit(f"Forgot saved log graph layout for {key}; using Auto.")

    def _auto_traces(self) -> list[str]:
        if not self.log:
            return []
        if self.profile:
            traces = [s for s in self.profile.traces if self.log.has(s)]
            if traces:
                return traces
            for s in self.profile.required_position_signals:
                if self.log.has(s):
                    return [s]
        available = self.log.available_signals()
        return available[:1]

    def _effective_graphs(self) -> list[list[str]]:
        if self.custom_graphs:
            return [list(g) for g in self.custom_graphs]
        auto = self._auto_traces()
        if not self.log:
            return [auto] if auto else [[]]
        primary = list(dict.fromkeys(auto + [s for s in (SIG_RPM, SIG_BOOST, SIG_BOOST_SET) if self.log.has(s)]))
        secondary = [s for s in (SIG_IQ, SIG_N75, SIG_MAF, SIG_RAIL, SIG_SOI, SIG_SPEED)
                     if self.log.has(s) and s not in primary]
        return [primary, secondary] if secondary else [primary]

    def _refresh_graph_combo(self):
        current = self.graph_combo.currentIndex()
        groups = self._effective_graphs()
        self.graph_combo.blockSignals(True)
        self.graph_combo.clear()
        for i, group in enumerate(groups):
            suffix = " (auto)" if not self.custom_graphs else ""
            trace_word = "trace" if len(group) == 1 else "traces"
            self.graph_combo.addItem(f"Graph {i + 1}{suffix} · {len(group)} {trace_word}", i)
        self.graph_combo.setCurrentIndex(max(0, min(current, self.graph_combo.count() - 1)))
        self.graph_combo.blockSignals(False)
        self._update_graph_buttons()

    def _selected_signal(self) -> str | None:
        signal = self.signal_combo.currentData()
        return str(signal) if signal else None

    def _ensure_custom_graphs(self):
        if not self.custom_graphs:
            self.custom_graphs = self._effective_graphs()

    def add_trace_to_graph(self):
        if not self.log:
            return
        signal = self._selected_signal()
        if not signal:
            return
        requested_idx = max(0, self.graph_combo.currentIndex())
        self._ensure_custom_graphs()
        idx = max(0, min(requested_idx, len(self.custom_graphs) - 1))
        if signal not in self.custom_graphs[idx]:
            self.custom_graphs[idx].append(signal)
        self._refresh_graph_combo()
        self.graph_combo.setCurrentIndex(idx)
        self._redraw()

        self._remember_graphs()

    def add_new_graph(self):
        if not self.log:
            return
        if len(self._effective_graphs()) >= 8:
            return
        self._ensure_custom_graphs()
        signal = self._selected_signal()
        self.custom_graphs.append([signal] if signal else [])
        idx = len(self.custom_graphs) - 1
        self._refresh_graph_combo()
        self.graph_combo.setCurrentIndex(idx)
        self._redraw()

        self._remember_graphs()

    def remove_trace_from_graph(self):
        if not self.custom_graphs:
            return
        signal = self._selected_signal()
        idx = max(0, min(self.graph_combo.currentIndex(), len(self.custom_graphs) - 1))
        if signal in self.custom_graphs[idx]:
            self.custom_graphs[idx].remove(signal)
        self._refresh_graph_combo()
        self.graph_combo.setCurrentIndex(max(0, min(idx, self.graph_combo.count() - 1)))
        self._redraw()

        self._remember_graphs()

    def remove_graph(self):
        if not self.custom_graphs:
            return
        idx = max(0, min(self.graph_combo.currentIndex(), len(self.custom_graphs) - 1))
        self.custom_graphs.pop(idx)
        self._refresh_graph_combo(); self._redraw()

        self._remember_graphs()

    def reset_auto_graphs(self):
        self.custom_graphs = []
        self._refresh_graph_combo(); self._redraw()

        self._remember_graphs()

    def _update_graph_buttons(self):
        has_log = bool(self.log and self.signal_combo.count())
        self.channel_setup_button.setEnabled(has_log)
        self.add_trace_button.setEnabled(has_log)
        self.new_graph_button.setEnabled(has_log and len(self._effective_graphs()) < 8)
        self.new_graph_button.setToolTip("Add a graph area (up to eight)")
        self.remove_trace_button.setEnabled(bool(self.custom_graphs))
        self.remove_graph_button.setEnabled(bool(self.custom_graphs))
        self.auto_graph_button.setEnabled(bool(self.custom_graphs))
        self.save_layout_button.setEnabled(bool(self.log and self._layout_type_key()))
        settings_key = self._layout_settings_key()
        self.clear_layout_button.setEnabled(bool(settings_key and self.settings.contains(settings_key)))

    def _redraw(self):
        old_xlim = self.view_xlim
        if self.view_xlim is not None and self.axes and self.log:
            try:
                x0, x1 = self.axes[-1].get_xlim()
                if math.isfinite(x0) and math.isfinite(x1) and x1 > x0:
                    old_xlim = (float(x0), float(x1))
            except Exception:
                pass

        self.figure.clear()
        self.axes = []
        self.cursor_lines = []
        self.range_patches = []
        previous_readings = self.graph_readings
        self.graph_readings = []
        # Reserve a channel list beside each graph instead of covering traces with a legend.
        left_margin = min(0.40, max(0.12, 260 / max(self.canvas.width(), 600)))
        self.figure.subplots_adjust(left=left_margin, right=0.99, bottom=0.16, top=0.96, hspace=0.18)
        self.figure.set_facecolor("#f3f5f7")
        if not self.log or self.log.sample_count == 0:
            for readings in previous_readings:
                readings.hide()
                readings.deleteLater()
            ax = self.figure.add_subplot(111)
            self.axes = [ax]
            ax.text(0.5, 0.5, "Open a TestO CSV log", ha="center", va="center", transform=ax.transAxes)
            ax.set_axis_off()
            self.canvas.draw_idle()
            self._update_readout()
            return

        groups = self._effective_graphs()
        groups = groups[:8]  # keep the pane usable even if the user gets enthusiastic
        shared = None
        for gi, traces in enumerate(groups):
            ax = self.figure.add_subplot(len(groups), 1, gi + 1, sharex=shared)
            if shared is None:
                shared = ax
            self.axes.append(ax)
            ax.set_facecolor("#fbfcfd")
            plotted_signals: list[str] = []
            channels = []
            for si, signal in enumerate(traces):
                available = self.log.available_signals()
                style = channel_style(self.channel_styles, signal, available.index(signal) if signal in available else si)
                label = self._signal_name(signal)
                # Keep unavailable saved channels visible in the side list too.
                channels.append((signal, style["color"]))
                if not self.log.has(signal):
                    continue
                arr = self.log.series(signal)
                scaled, _bounds = scale_channel(arr, style)
                # TestO captures interleave parameter groups; plot each channel's received samples.
                valid = np.isfinite(arr)
                ax.plot(self.log.times[valid], scaled[valid], label=label, color=style["color"],
                        linewidth=1.1, marker=".", markersize=2)
                plotted_signals.append(signal)
            if not plotted_signals:
                ax.text(0.5, 0.5, "No signal", ha="center", va="center", transform=ax.transAxes)
            ax.set_ylim(0, 100)
            ax.set_yticks([0, 50, 100])
            ax.set_yticklabels(["", "", ""])
            ax.tick_params(axis="both", labelsize=8, length=2)
            if gi < len(previous_readings) and previous_readings[gi].channels == tuple(channels):
                readings = previous_readings[gi]
            else:
                readings = GraphReadings(gi + 1, channels, self._signal_name, self.canvas,
                                          tooltip_for=self.fuel_estimate_tooltip)
            self.graph_readings.append(readings)
            ax.grid(True, alpha=0.22)
            line = ax.axvline(self.log.times[self.cursor_index], linewidth=1.0, color="#303b49")
            self.cursor_lines.append(line)
            if self.range_seconds:
                a, b = self.range_seconds
                self.range_patches.append(ax.axvspan(a, b, alpha=0.14))
            if gi != len(groups) - 1:
                ax.tick_params(labelbottom=False)
        self.axes[-1].set_xlabel("Time (s)")
        for readings in previous_readings:
            if readings not in self.graph_readings:
                readings.hide()
                readings.deleteLater()

        if self.live_mode:
            self._set_xlim(max(float(self.log.times[0]), self.log.duration - 30.0), self.log.duration, draw=False)
        elif old_xlim is not None:
            self._set_xlim(*old_xlim, draw=False)
        else:
            self._set_xlim(0.0, self.log.duration, draw=False)
        self._position_graph_readings()
        self._update_graph_readings()
        self.canvas.draw_idle()
        self._update_readout()

    def _position_graph_readings(self):
        width, height = self.canvas.width(), self.canvas.height()
        for ax, readings in zip(self.axes, self.graph_readings):
            bounds = ax.get_position()
            x = 8
            y = round((1 - bounds.y1) * height)
            readings.setGeometry(x, y, max(1, round(bounds.x0 * width) - x - 12),
                                 max(1, round(bounds.height * height)))
            readings.show()

    def _set_xlim(self, left: float, right: float, draw: bool = True):
        if not self.log or not self.axes:
            return
        duration = max(float(self.log.duration), 1e-9)
        left, right = sorted((float(left), float(right)))
        span = max(right - left, min(duration, 0.05))
        if span >= duration:
            left, right = 0.0, duration
        else:
            if left < 0:
                right -= left; left = 0.0
            if right > duration:
                left -= right - duration; right = duration
            left = max(0.0, left)
            right = min(duration, max(left + min(span, duration), right))
        self.view_xlim = (left, right)
        for ax in self.axes:
            ax.set_xlim(left, right)
        if draw:
            self.canvas.draw_idle()

    def fit_time(self):
        if self.log:
            self._set_xlim(0.0, self.log.duration)

    def zoom_to_range(self):
        if self.range_seconds:
            self._set_xlim(*self.range_seconds)

    def _mouse_scroll(self, event):
        if not self.log or event.inaxes not in self.axes or event.xdata is None:
            return
        ax = event.inaxes
        left, right = ax.get_xlim()
        span = right - left
        if span <= 0:
            return
        factor = 0.65 if event.button == "up" else (1.0 / 0.65)
        new_span = min(max(span * factor, 0.05), max(self.log.duration, 0.05))
        center = float(event.xdata)
        rel = (center - left) / span
        new_left = center - rel * new_span
        self._set_xlim(new_left, new_left + new_span)

    def _key_press(self, event):
        """Keyboard navigation for the recorded-log cursor.

        Left/right move one unique log sample. Shift moves 10 samples and Ctrl
        moves 100. Up/down zoom in/out around the current cursor. Home/End jump
        to the beginning/end. The visible zoom window follows the cursor.
        """
        if not self.log or not self.log.sample_count:
            return
        key = (event.key or "").lower()
        step = 1
        if "shift" in key:
            step = 10
        if "ctrl" in key or "control" in key:
            step = 100
        base = key.split("+")[-1]
        if base == "left":
            new_index = self.cursor_index - step
        elif base == "right":
            new_index = self.cursor_index + step
        elif base == "up":
            self._zoom_about_cursor(0.65)
            return
        elif base == "down":
            self._zoom_about_cursor(1.0 / 0.65)
            return
        elif base == "home":
            new_index = 0
        elif base == "end":
            new_index = self.log.sample_count - 1
        else:
            return
        self.set_cursor(new_index)
        self._ensure_cursor_visible()

    def _zoom_about_cursor(self, factor: float):
        """Zoom the shared log x-axis around the current vertical cursor."""
        if not self.log or not self.axes or not self.log.sample_count:
            return
        left, right = self.axes[-1].get_xlim()
        span = float(right - left)
        if not math.isfinite(span) or span <= 0:
            return
        duration = max(float(self.log.duration), 0.05)
        new_span = min(max(span * float(factor), 0.05), duration)
        center = float(self.log.times[self.cursor_index])
        # Preserve cursor's relative position in the current view where possible.
        rel = (center - left) / span if span > 0 else 0.5
        rel = min(1.0, max(0.0, rel))
        new_left = center - rel * new_span
        self._set_xlim(new_left, new_left + new_span)

    def _ensure_cursor_visible(self):
        if not self.log or not self.axes or not self.log.sample_count:
            return
        t = float(self.log.times[self.cursor_index])
        left, right = self.axes[-1].get_xlim()
        if left <= t <= right:
            return
        span = max(float(right - left), 0.05)
        # Keep a small margin in the direction of travel instead of pinning the
        # cursor directly against the plot edge.
        if t < left:
            new_left = t - span * 0.08
        else:
            new_left = t - span * 0.92
        self._set_xlim(new_left, new_left + span)

    def set_cursor(self, index: int, emit: bool = True):
        if not self.log or not self.log.sample_count:
            return
        index = max(0, min(int(index), self.log.sample_count - 1))
        self.cursor_index = index
        for line in self.cursor_lines:
            line.set_xdata([self.log.times[index], self.log.times[index]])
        self._update_graph_readings()
        if self.cursor_lines:
            self.canvas.draw_idle()
        self._update_readout()
        if emit:
            self.cursorChanged.emit(index)

    def _update_graph_readings(self):
        """Refresh side lists without resizing or rebuilding the channel rows."""
        if not self.log or not self.log.sample_count:
            return
        i = max(0, min(self.cursor_index, self.log.sample_count - 1))
        for readings in self.graph_readings:
            for signal in readings.rows:
                value, age = self._display_log_value(signal, i)
                unit = self.log.units.get(signal, "") or self.catalog.unit_for(signal)
                readings.update_value(signal, value, unit, age)

    def _display_log_value(self, signal, index):
        if signal in (FUEL_RATE_SIGNAL, FUEL_CONSUMPTION_SIGNAL):
            value, age_seconds = self.log.fuel_estimate_display(signal, index)
            return value, f" ({age_seconds:.1f}s old)" if age_seconds >= 0.5 else ""
        if signal not in self.log.values:
            return math.nan, ""
        value = float(self.log.value(signal, index))
        if not math.isfinite(value):
            if isinstance(self.log, TestOLog):
                # CSV arrays are immutable during playback. Index once, then look up
                # the last received sample without rescanning the file on every frame.
                if self._display_lookup_log is not self.log:
                    self._display_lookup_log = self.log
                    self._display_lookup = {}
                if signal not in self._display_lookup:
                    self._display_lookup[signal] = np.flatnonzero(np.isfinite(self.log.series(signal)))
                previous = self._display_lookup[signal]
                count = np.searchsorted(previous, index, side="right")
            else:
                previous = np.flatnonzero(np.isfinite(self.log.series(signal)[:index+1]))
                count = len(previous)
            if count:
                j = int(previous[count - 1])
                age = self.log.times[index] - self.log.times[j]
                return float(self.log.series(signal)[j]), f" ({age:.1f}s old)" if age >= 0.5 else ""
        return value, ""

    def set_map_value(self, value: float | None, unit: str = ""):
        self.map_value = float(value) if value is not None and math.isfinite(float(value)) else None
        self.map_value_unit = unit or ""
        self._update_readout()

    def add_floating_gauge(self, signal):
        gauge = self.gauges.get(signal)
        if gauge is None:
            gauge = FloatingGauge(self.catalog.label_for(signal), self)
            gauge.setToolTip(self.fuel_estimate_tooltip(signal))
            self.gauges[signal] = gauge
            gauge.finished.connect(lambda *_: self.gauges.pop(signal, None))
        gauge.show()
        gauge.raise_()
        self._update_gauges()
        return gauge

    def _update_gauges(self):
        for signal, gauge in list(self.gauges.items()):
            gauge.setToolTip(self.fuel_estimate_tooltip(signal))
            if self.log and self.log.sample_count and signal in self.log.values:
                value, age = self._display_log_value(signal, self.cursor_index)
                gauge.update_value(value, self.log.units.get(signal, ""), age)
            else:
                gauge.update_value(float("nan"), "", "No data")

    def _update_readout(self):
        self._update_gauges()
        self.reading_strip.set_signals(self._reading_channels())
        for signal in self.reading_strip.cells:
            value, age = self._display_log_value(signal, self.cursor_index) if self.log and self.log.sample_count else (math.nan, "")
            unit = (self.log.units.get(signal, "") if self.log else "") or self.catalog.unit_for(signal)
            self.reading_strip.update_value(signal, value, unit, age)
        if not self.log or not self.log.sample_count:
            self.time_label.setText("Time —")
            self.time_label.setToolTip("Current log cursor time")
            return
        i = self.cursor_index
        self.time_label.setText(f"Time {self.log.times[i]:.3f} s")
        self.time_label.setToolTip("Current log cursor time" +
            (f"\nMap: {self.map_value:.3f} {self.map_value_unit}" if self.map_value is not None else ""))

    def toggle_play(self):
        if not self.log or self.log.sample_count < 2:
            return
        if self.timer.isActive():
            self.timer.stop()
            self.play_button.setText("▶ Play")
            return
        diffs = np.diff(self.log.times)
        valid = diffs[np.isfinite(diffs) & (diffs > 0)]
        interval_ms = int(max(20, min(500, (float(np.median(valid)) if len(valid) else 0.1) * 1000)))
        self.timer.start(interval_ms)
        self.play_button.setText("❚❚ Pause")

    def _play_step(self):
        if not self.log:
            self.timer.stop(); return
        next_index = self.cursor_index + 1
        if next_index >= self.log.sample_count:
            self.timer.stop()
            self.play_button.setText("▶ Play")
            return
        self.set_cursor(next_index)

    def clear_range(self):
        self.range_seconds = None
        self.clear_range_button.setEnabled(False)
        self.zoom_range_button.setEnabled(False)
        self.rangeChanged.emit(math.nan, math.nan)
        self._redraw()

    def _mouse_press(self, event):
        if not self.log or event.inaxes not in self.axes or event.button != 1 or event.xdata is None:
            return
        self.canvas.setFocus(Qt.MouseFocusReason)
        self._press_xdata = float(event.xdata)
        self._press_pixel = float(event.x)
        self._dragged_range = False

    def _mouse_motion(self, event):
        if self._press_xdata is None or not self.log or event.inaxes not in self.axes or event.xdata is None:
            return
        if self._press_pixel is None or abs(float(event.x) - self._press_pixel) < 5:
            return
        a, b = sorted((self._press_xdata, float(event.xdata)))
        self.range_seconds = (max(0.0, a), min(self.log.duration, b))
        self.clear_range_button.setEnabled(True)
        self.zoom_range_button.setEnabled(True)
        self._dragged_range = True
        self._draw_range_selection()

    def _draw_range_selection(self):
        for patch in self.range_patches:
            patch.remove()
        self.range_patches = []
        if self.range_seconds:
            a, b = self.range_seconds
            self.range_patches = [ax.axvspan(a, b, alpha=0.14) for ax in self.axes]
        self.canvas.draw_idle()

    def _mouse_release(self, event):
        if self._press_xdata is None or not self.log:
            return
        x0, px0 = self._press_xdata, self._press_pixel
        self._press_xdata = None
        self._press_pixel = None
        if event.inaxes not in self.axes or event.xdata is None:
            if getattr(self, "_dragged_range", False) and self.range_seconds:
                self.rangeChanged.emit(*self.range_seconds)
            return
        moved = px0 is not None and abs(float(event.x) - px0) >= 5
        if moved:
            a, b = sorted((x0, float(event.xdata)))
            self.range_seconds = (max(0.0, a), min(self.log.duration, b))
            self.clear_range_button.setEnabled(True)
            self.zoom_range_button.setEnabled(True)
            self.rangeChanged.emit(*self.range_seconds)
            self._draw_range_selection()
        else:
            self.set_cursor(self.log.nearest_index(float(event.xdata)))


class MapEditor(QWidget):
    changed = Signal()
    statusChanged = Signal(str)
    cellModeChanged = Signal(str)

    VIEW_2D = 0
    VIEW_3D = 1
    VIEW_SPLIT = 2

    def __init__(self, parent=None):
        super().__init__(parent)
        self.document: CalibrationDocument | None = None
        self.definition: CalibrationDefinition | None = None
        self.engine: EditEngine | None = None
        self.view_mode = self.VIEW_SPLIT
        self.link_resolver = None
        self.step_resolver = None
        self.log_data: TestOLog | None = None
        self.log_index = 0
        self.log_heatmap: np.ndarray | None = None
        self.log_stats: dict[str, np.ndarray] | None = None
        self.table_display_mode = "calibration"
        self.compare_bytes: bytes | None = None
        self.compare_name: str = "Original BIN"
        self.settings = editor_settings(QSettings)

        self.table = CalibrationTable()
        self.surface = SurfaceWidget()
        self.info = InfoPanel()
        self.log_panel = LogViewerPanel()
        self.log_panel.logLoaded.connect(self._log_loaded)
        self.log_panel.cursorChanged.connect(self._log_cursor_changed)
        self.log_panel.rangeChanged.connect(self._log_range_changed)
        self.log_panel.statusChanged.connect(lambda text: self.statusChanged.emit(text))
        self.table_log_marker = QLabel("●", self.table.viewport())
        self.table_log_marker.setStyleSheet("color: #d00000; font-size: 18px; font-weight: bold; background: transparent;")
        self.table_log_marker.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.table_log_marker.adjustSize()
        self.table_log_marker.hide()
        self.table.horizontalScrollBar().valueChanged.connect(lambda _v: self._position_log_marker())
        self.table.verticalScrollBar().valueChanged.connect(lambda _v: self._position_log_marker())
        self.table.horizontalHeader().sectionResized.connect(lambda *_: self._position_log_marker())
        self.table.verticalHeader().sectionResized.connect(lambda *_: self._position_log_marker())
        self.table.selectionFromMouse.connect(self._mouse_select)
        self.table.axisEditRequested.connect(self.edit_axis)
        self.surface.cellPicked.connect(self._mouse_select)

        self.compare_check = QCheckBox("Reference overlay")
        self.compare_check.setChecked(True)
        self.compare_check.toggled.connect(self._compare_changed)
        self.compare_label = QLabel("Reference: original BIN")
        self.compare_label.setToolTip("Load a comparison BIN from File → Open Compare BIN…")
        self.btn_compare_mode = QPushButton("Compare: values")
        self.btn_compare_mode.setCheckable(True)
        self.btn_compare_mode.setChecked(False)
        self.btn_compare_mode.setToolTip("Switch the 2D grid between reference/compare BIN values and working-minus-reference difference")
        self.btn_compare_mode.clicked.connect(self._toggle_compare_mode)
        self.link_check = QCheckBox("Linked edits")
        self.link_check.setChecked(True)
        self.link_check.setToolTip("Temporarily enable/disable propagation to the linked map group")
        self.link_check.toggled.connect(self._link_toggle_changed)
        self.link_label = QLabel("Not linked")
        self.link_label.setToolTip("Configure groups in Settings > Linked map groups")
        self.btn_2d = QPushButton("2D")
        self.btn_3d = QPushButton("3D")
        self.btn_split = QPushButton("Split")
        self.btn_log = QPushButton("Log")
        self.btn_log.setCheckable(True)
        self.btn_log.setChecked(True)
        self.btn_log.setToolTip("Show/hide the synchronized TestO log panel")
        self.btn_log.toggled.connect(self._toggle_log_panel)
        self.btn_info = QPushButton("XDF info")
        self.btn_info.setCheckable(True)
        self.btn_info.setChecked(False)
        self.btn_info.setToolTip("Show/hide the XDF-derived title, metadata and description panel")
        self.btn_info.toggled.connect(self._toggle_info_panel)
        self.btn_axis_binding = QPushButton("Axis binding…")
        self.btn_axis_binding.setToolTip("Override which loaded log parameter drives the current table X/Y axes")
        self.btn_axis_binding.clicked.connect(self.axis_binding_dialog)
        self.btn_select_log_cells = QPushButton("Select log cells")
        self.btn_select_log_cells.setToolTip("Select the bounding rectangle of map cells visited by the currently marked log range")
        self.btn_select_log_cells.clicked.connect(self.select_log_range_cells)
        self.btn_multiply = QPushButton("× Selection")
        self.btn_multiply.setToolTip("Multiply selected calibration cells by a factor such as 0.95 or 1.05")
        self.btn_multiply.clicked.connect(self.multiply_dialog)
        self.btn_infer = QPushButton("Infer table…")
        self.btn_infer.setToolTip("Rank same-family tables against the marked log range; uses the whole log when no range is marked")
        self.btn_infer.clicked.connect(self.infer_active_table)
        self.btn_switch_scan = QPushButton("Switch scan…")
        self.btn_switch_scan.setToolTip("Scan the whole log in short windows for sustained changes in the best-fitting same-family table")
        self.btn_switch_scan.clicked.connect(self.scan_table_switches)
        self.btn_2d.clicked.connect(lambda: self.set_view(self.VIEW_2D))
        self.btn_3d.clicked.connect(lambda: self.set_view(self.VIEW_3D))
        self.btn_split.clicked.connect(lambda: self.set_view(self.VIEW_SPLIT))

        self.cell_mode_combo = QComboBox()
        self.cell_mode_combo.setToolTip("Table cell density. Fit resizes automatically with the window/splitter.")
        self.cell_mode_combo.addItem("Fit", "fit")
        self.cell_mode_combo.addItem("Compact", "compact")
        self.cell_mode_combo.addItem("Normal", "normal")
        self.cell_mode_combo.currentIndexChanged.connect(self._cell_mode_combo_changed)

        self.table_data_combo = QComboBox()
        self.table_data_combo.setToolTip("Show working calibration, reference BIN, working-minus-reference difference, or (for N75 maps) logged duty statistics. Editing is available only in Calibration view.")
        self.table_data_combo.addItem("Calibration", "calibration")
        self.table_data_combo.addItem("Reference BIN", "reference")
        self.table_data_combo.addItem("Δ vs ref", "ref_diff")
        self.table_data_combo.addItem("Log avg", "log_avg")
        self.table_data_combo.addItem("Log Δ", "log_diff")
        self.table_data_combo.addItem("Samples", "samples")
        self.table_data_combo.addItem("Std dev", "std")
        self.table_data_combo.currentIndexChanged.connect(self._table_display_changed)

        top = QVBoxLayout()
        top1 = QHBoxLayout()
        for w in (self.btn_2d, self.btn_3d, self.btn_split, self.btn_log, self.btn_info, self.btn_axis_binding, self.btn_select_log_cells, self.btn_multiply, self.btn_infer, self.btn_switch_scan):
            top1.addWidget(w)
        self.btn_apply_log_delta = QPushButton("Apply log Δ…")
        self.btn_apply_log_delta.setToolTip("Preview and add logged N75 delta to selected cells or all cells with data; supports Undo")
        self.btn_apply_log_delta.clicked.connect(self.apply_log_delta_dialog)
        top1.addWidget(self.btn_apply_log_delta)
        top1.addStretch(1)
        top2 = QHBoxLayout()
        top2.addWidget(QLabel("Cells:")); top2.addWidget(self.cell_mode_combo)
        top2.addWidget(QLabel("Table data:")); top2.addWidget(self.table_data_combo)
        top2.addWidget(self.compare_check); top2.addWidget(self.compare_label); top2.addWidget(self.btn_compare_mode)
        top2.addSpacing(10); top2.addWidget(self.link_check); top2.addWidget(self.link_label)
        top2.addStretch(1)
        top.addLayout(top1); top.addLayout(top2)

        self.view_splitter = QSplitter(Qt.Horizontal)
        self.view_splitter.setHandleWidth(7)
        self.view_splitter.setChildrenCollapsible(False)
        self.view_splitter.addWidget(self.table)
        self.view_splitter.addWidget(self.surface)
        self.view_splitter.setStretchFactor(0, 5)
        self.view_splitter.setStretchFactor(1, 7)
        saved_view = self.settings.value("layout/map_view_splitter", "")
        try:
            sizes = [int(x) for x in json.loads(str(saved_view))]
            self.view_splitter.setSizes(sizes if len(sizes) == 2 else [520, 760])
        except Exception:
            self.view_splitter.setSizes([520, 760])
        self.view_splitter.splitterMoved.connect(lambda *_: self.settings.setValue("layout/map_view_splitter", json.dumps(self.view_splitter.sizes())))

        self.welcome_page = QWidget()
        welcome_layout = QVBoxLayout(self.welcome_page)
        welcome_layout.setAlignment(Qt.AlignCenter)
        welcome_art = QLabel()
        welcome_art.setAlignment(Qt.AlignCenter)
        welcome_art.setAccessibleName('Giant anteater mascot')
        artwork = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1])) / 'assets/editor.png'
        welcome_art.setPixmap(QIcon(str(artwork)).pixmap(320, 220))
        welcome_title = QLabel('BMW DDE5 MS43 Tuner')
        welcome_title.setAlignment(Qt.AlignCenter)
        welcome_title.setStyleSheet('font-size: 22px; font-weight: 600;')
        welcome_hint = QLabel('Open a BIN from File to get started.')
        welcome_hint.setAlignment(Qt.AlignCenter)
        self.welcome_examples = QLabel(
            '<a href="https://github.com/tuned-by-emil/BMW_DDE5_MS43_tuner/tree/main/examples/dde5-361830">'
            'Download example BIN and log</a>')
        self.welcome_examples.setAlignment(Qt.AlignCenter)
        self.welcome_examples.setOpenExternalLinks(True)
        self.welcome_examples.setToolTip('Open the example files and loading instructions on GitHub')
        welcome_layout.addWidget(welcome_art)
        welcome_layout.addWidget(welcome_title)
        welcome_layout.addWidget(welcome_hint)
        welcome_layout.addWidget(self.welcome_examples)
        self.map_pages = QStackedWidget()
        self.map_pages.addWidget(self.welcome_page)
        self.map_pages.addWidget(self.view_splitter)

        self.outer_splitter = ReviewSplitter(Qt.Vertical)
        self.outer_splitter.setHandleWidth(16)
        self.outer_splitter.setOpaqueResize(True)
        self.outer_splitter.setChildrenCollapsible(False)
        self.view_splitter.setMinimumHeight(220)
        self.log_panel.setMinimumHeight(220)
        self.outer_splitter.addWidget(self.map_pages)
        self.outer_splitter.addWidget(self.log_panel)
        self.outer_splitter.addWidget(self.info)
        # The verbose XDF description is useful on demand but expensive in vertical
        # space while reviewing logs, so V1.10 starts with it collapsed.
        self.info.hide()
        saved_outer = self.settings.value("layout/map_outer_splitter", "")
        try:
            sizes = [int(x) for x in json.loads(str(saved_outer))]
            self.outer_splitter.setSizes(sizes if len(sizes) == 3 else [520, 460, 0])
        except Exception:
            self.outer_splitter.setSizes([520, 460, 0])
        self.outer_splitter.splitterMoved.connect(lambda *_: self.settings.setValue("layout/map_outer_splitter", json.dumps(self.outer_splitter.sizes())))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addLayout(top)
        layout.addWidget(self.outer_splitter)

        self.installEventFilter(self)
        self.table.installEventFilter(self)
        self.surface.installEventFilter(self)
        self.surface.canvas.installEventFilter(self)

    def _axis_binding_settings_key(self) -> str | None:
        if not self.definition:
            return None
        return "axis_bindings/" + definition_key(self.definition).replace(":", "_")

    def _load_axis_binding(self) -> tuple[str | None, str | None]:
        key = self._axis_binding_settings_key()
        if not key:
            return None, None
        raw = self.settings.value(key, "")
        if not raw:
            return None, None
        try:
            data = json.loads(str(raw))
            return data.get("x") or None, data.get("y") or None
        except Exception:
            return None, None

    def _apply_saved_axis_binding(self):
        x, y = self._load_axis_binding()
        self.log_panel.set_axis_overrides(x, y)

    def axis_binding_dialog(self):
        if not self.definition:
            QMessageBox.information(self, APP_NAME, "Open a table first.")
            return
        if not self.log_data:
            QMessageBox.information(self, APP_NAME, "Load a TestO log first so the available parameters are known.")
            return
        current = self.log_panel.profile or profile_for_definition(self.definition, self.log_data)
        dlg = QDialog(self)
        dlg.setWindowTitle("Log parameter → table axis binding")
        dlg.resize(720, 180)
        form = QFormLayout()
        combos = []
        for which, selected in (("X axis", current.x_signal), ("Y axis", current.y_signal)):
            combo = QComboBox(); combo.setMinimumWidth(560)
            combo.addItem("Auto / no override", None)
            for sig in self.log_data.available_signals():
                label = self.log_data.info.get(sig) or self.log_panel.catalog.label_for(sig)
                unit = self.log_data.units.get(sig, "")
                combo.addItem(f"{label}{' ['+unit+']' if unit else ''} — {sig}", sig)
            idx = combo.findData(selected)
            if idx >= 0: combo.setCurrentIndex(idx)
            form.addRow(which, combo); combos.append(combo)
        buttons = QHBoxLayout(); save = QPushButton("Save binding"); cancel = QPushButton("Cancel")
        save.clicked.connect(dlg.accept); cancel.clicked.connect(dlg.reject); buttons.addStretch(1); buttons.addWidget(save); buttons.addWidget(cancel)
        lay = QVBoxLayout(dlg); lay.addLayout(form); lay.addLayout(buttons)
        if dlg.exec() != QDialog.Accepted:
            return
        x, y = combos[0].currentData(), combos[1].currentData()
        key = self._axis_binding_settings_key()
        if key:
            self.settings.setValue(key, json.dumps({"x": x, "y": y}))
        self.log_panel.set_axis_overrides(x, y)
        self._recompute_log_heatmap(); self._recompute_log_stats(); self._update_log_overlay(); self.refresh(defer_surface=True)
        self.statusChanged.emit(f"Saved log-axis binding: X={x or 'Auto'}, Y={y or 'Auto'}")

    def select_log_range_cells(self):
        if not self.engine or not self.definition or not self.log_data:
            QMessageBox.information(self, APP_NAME, "Open a map and load a log first.")
            return
        if self.log_panel.range_seconds is None:
            QMessageBox.information(self, APP_NAME, "Drag over the log to mark a range first.")
            return
        a, b = self.log_panel.range_seconds
        self._recompute_log_heatmap(a, b)
        if self.log_heatmap is None or not np.any(self.log_heatmap > 0):
            QMessageBox.information(self, APP_NAME, "No current-map cells could be projected from that log range. Check axis binding.")
            return
        rr, cc = np.where(self.log_heatmap > 0)
        self.engine.selection.set_rect(int(rr.min()), int(cc.min()), int(rr.max()), int(cc.max()))
        self.refresh_selection_only()
        self.statusChanged.emit(f"Selected visited-cell bounding area: rows {rr.min()}–{rr.max()}, cols {cc.min()}–{cc.max()} (visited path remains highlighted).")

    @staticmethod
    def _candidate_family(definition: CalibrationDefinition) -> tuple[str, list[int], str | None]:
        if definition.discovery_unverified or definition.profile_id != '361830':
            return "", [], None
        addr = definition.z_address
        families = [
            ("N75", [0xF7F44,0xF8478,0xF86BC,0xF8900], SIG_N75),
            ("Boost Target", [0xF9394,0xF95D8,0xF981C], SIG_BOOST_SET),
            ("TQ2IQ", [0xE80EA,0xE832E,0xE8572], SIG_IQ),
            ("SOI", [0xEA80C,0xEAA50,0xEAC94,0xEAED8,0xEB11C], SIG_SOI),
            ("Lambda", [0xE77D2,0xE7A16], CALC_EST_LAMBDA),
        ]
        for name, addresses, target in families:
            if addr in addresses:
                return name, addresses, target
        return "", [], None

    def infer_active_table(self):
        if not self.definition or not self.document or not self.log_data:
            QMessageBox.information(self, APP_NAME, "Open a same-family map and load a log first.")
            return
        family, addresses, target_signal = self._candidate_family(self.definition)
        if not addresses or not target_signal:
            QMessageBox.information(self, APP_NAME, "No supported same-family inference is available for this calibration.")
            return
        if not self.log_data.has(target_signal):
            QMessageBox.information(self, APP_NAME, f"Cannot infer {family} — missing logged signal: {short_signal_name(target_signal)}")
            return
        known_axes = {
            "N75": (SIG_IQ, SIG_RPM), "Boost Target": (SIG_IQ, SIG_RPM),
            "SOI": (SIG_IQ, SIG_RPM), "TQ2IQ": (SIG_TORQUE, SIG_RPM),
            "Lambda": (SIG_MAF, SIG_RPM),
        }
        x_sig, y_sig = known_axes[family]
        missing=[short_signal_name(q) for q in (x_sig,y_sig) if not self.log_data.has(q)]
        if missing:
            QMessageBox.information(self, APP_NAME, f"Cannot infer {family} — missing logged signal(s): {', '.join(missing)}")
            return

        # Selected-range inference is the default when the user has marked a log section.
        if self.log_panel.range_seconds is not None:
            a,b=self.log_panel.range_seconds
            i0=self.log_data.nearest_index(min(a,b)); i1=self.log_data.nearest_index(max(a,b))+1
            range_text=f"{min(a,b):.2f}–{max(a,b):.2f} s"
        else:
            i0,i1=0,self.log_data.sample_count
            range_text=f"whole log, 0–{self.log_data.duration:.2f} s"

        lookup = {d.z_address:d for d in self.document.xdf.definitions}
        rows = []
        for addr in addresses:
            d = lookup.get(addr)
            if d is None: continue
            xvals=self.document.axis_values(d.x_axis) if d.x_axis else list(range(d.cols))
            yvals=self.document.axis_values(d.y_axis) if d.rows>1 and d.y_axis else list(range(d.rows))
            if len(xvals)!=d.cols or (d.rows>1 and len(yvals)!=d.rows): continue
            zvals=self.document.map_values(d)
            pred=[]; obs=[]
            for i in range(i0,i1):
                xv=self.log_data.value(x_sig,i); cf=self._fractional_index(xvals,xv)
                if cf is None: continue
                rf=0.0 if d.rows<=1 else self._fractional_index(yvals,self.log_data.value(y_sig,i))
                if rf is None: continue
                pv=self._interp_grid(zvals,rf,cf); ov=self.log_data.value(target_signal,i)
                if math.isfinite(pv) and math.isfinite(ov): pred.append(pv); obs.append(ov)
            if len(pred)<5: continue
            pa=np.asarray(pred,float); oa=np.asarray(obs,float); residual=oa-pa
            if family == "N75":
                offset=float(np.median(residual)); score=float(np.mean(np.abs(residual-offset)))
                detail=f"shape MAE {score:.3f} %, median final-base {offset:+.2f} %"
            else:
                score=float(np.mean(np.abs(residual))); detail=f"MAE {score:.3f}"
            rows.append((score,d,detail,len(pred)))
        rows.sort(key=lambda x:x[0])
        if not rows:
            QMessageBox.information(self, APP_NAME, f"Not enough usable samples to rank {family} in {range_text}.")
            return

        best=rows[0][0]; second=rows[1][0] if len(rows)>1 else math.inf
        gap=second-best
        # Relative gap is a practical confidence aid, not a claim about ECU selector certainty.
        rel=gap/max(abs(second),1e-9) if math.isfinite(second) else 1.0
        if len(rows)>1 and (gap <= 0.01 or rel < 0.02): confidence="LOW / TIE"
        elif rel < 0.08: confidence="LOW"
        elif rel < 0.20: confidence="MEDIUM"
        else: confidence="HIGH"
        lines=[f"Best inferred: {short_map_name(rows[0][1])}", f"Confidence: {confidence}", f"Range: {range_text} ({rows[0][3]} usable samples)", ""]
        lines += [f"{i+1}. {short_map_name(d)} — {detail}" for i,(_s,d,detail,_n) in enumerate(rows)]
        if family=="N75":
            lines.append("\nN75 is ranked by shape after removing the median final-minus-base correction; this does not assume table duty equals final duty.")
        if confidence in ("LOW / TIE","LOW"):
            lines.append("\nCandidates are close in this operating region; a deliberate fuzz or another selected section will separate them better.")
        QMessageBox.information(self, f"{family} — inferred table", "\n".join(lines))

    def _show_scrollable_report(self, title: str, text: str, width: int = 760, height: int = 520):
        """Show long diagnostic output without letting it grow beyond the screen."""
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.resize(width, height)
        layout = QVBoxLayout(dlg)
        report = QPlainTextEdit(dlg)
        report.setReadOnly(True)
        report.setPlainText(text)
        report.setLineWrapMode(QPlainTextEdit.NoWrap)
        layout.addWidget(report, 1)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        close_btn = QPushButton("Close", dlg)
        close_btn.clicked.connect(dlg.accept)
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)
        dlg.exec()

    def scan_table_switches(self):
        """Whole-log sliding-window selector aid. Results are inference, not code proof."""
        if not self.definition or not self.document or not self.log_data:
            QMessageBox.information(self, APP_NAME, "Open a supported family map and load a log first.")
            return
        family, addresses, target_signal = self._candidate_family(self.definition)
        known_axes = {
            "N75": (SIG_IQ, SIG_RPM), "Boost Target": (SIG_IQ, SIG_RPM),
            "SOI": (SIG_IQ, SIG_RPM), "TQ2IQ": (SIG_TORQUE, SIG_RPM),
            "Lambda": (SIG_MAF, SIG_RPM),
        }
        if family not in known_axes or not target_signal or not self.log_data.has(target_signal):
            QMessageBox.information(self, APP_NAME, "This family cannot be switch-scanned with the loaded channels.")
            return
        x_sig, y_sig = known_axes[family]
        missing=[short_signal_name(q) for q in (x_sig,y_sig) if not self.log_data.has(q)]
        if missing:
            QMessageBox.information(self, APP_NAME, f"Cannot scan {family} — missing: {', '.join(missing)}")
            return
        lookup={d.z_address:d for d in self.document.xdf.definitions}
        candidates=[]
        for addr in addresses:
            d=lookup.get(addr)
            if d is None: continue
            xv=self.document.axis_values(d.x_axis) if d.x_axis else list(range(d.cols))
            yv=self.document.axis_values(d.y_axis) if d.rows>1 and d.y_axis else list(range(d.rows))
            if len(xv)==d.cols and (d.rows<=1 or len(yv)==d.rows):
                candidates.append((d,xv,yv,self.document.map_values(d)))
        if len(candidates)<2:
            QMessageBox.information(self, APP_NAME, "Not enough candidate maps are available for a switch scan.")
            return
        duration=self.log_data.duration
        window=6.0 if duration>=30 else max(2.0,duration/5.0)
        step=window/2.0
        centres=np.arange(window/2.0, max(window/2.0+0.001,duration-window/2.0+step*0.5), step)
        wins=[]
        for centre in centres:
            i0=self.log_data.nearest_index(max(0.0,centre-window/2.0))
            i1=self.log_data.nearest_index(min(duration,centre+window/2.0))+1
            ranked=[]
            for d,xvals,yvals,zvals in candidates:
                residual=[]
                for i in range(i0,i1):
                    cf=self._fractional_index(xvals,self.log_data.value(x_sig,i))
                    if cf is None: continue
                    rf=0.0 if d.rows<=1 else self._fractional_index(yvals,self.log_data.value(y_sig,i))
                    if rf is None: continue
                    pv=self._interp_grid(zvals,rf,cf); ov=self.log_data.value(target_signal,i)
                    if math.isfinite(pv) and math.isfinite(ov): residual.append(ov-pv)
                if len(residual)<5: continue
                ra=np.asarray(residual,float)
                if family=="N75":
                    off=float(np.median(ra)); score=float(np.mean(np.abs(ra-off)))
                else:
                    score=float(np.mean(np.abs(ra)))
                ranked.append((score,d,len(residual)))
            ranked.sort(key=lambda q:q[0])
            if len(ranked)<2: continue
            best,second=ranked[0],ranked[1]
            rel=(second[0]-best[0])/max(abs(second[0]),1e-9)
            # Require a useful separation and then two consecutive windows before reporting a state.
            winner=definition_key(best[1]) if rel>=0.08 else None
            wins.append((centre,winner,best[1],rel,best[2]))
        if not wins:
            QMessageBox.information(self, APP_NAME, "No usable windows were found in this log.")
            return
        # Hysteresis: accept only winners repeated in two consecutive windows.
        states=[]
        for i,w in enumerate(wins):
            stable = w[1] is not None and ((i>0 and wins[i-1][1]==w[1]) or (i+1<len(wins) and wins[i+1][1]==w[1]))
            states.append((w[0], w[1] if stable else None, w[2], w[3], w[4]))
        segments=[]
        cur=None; start_t=None; last_t=None; best_rel=0.0
        for centre,key,d,rel,n in states:
            if key!=cur:
                if cur is not None:
                    segments.append((start_t,last_t,cur,best_rel))
                cur=key; start_t=centre; best_rel=rel
            elif cur is not None:
                best_rel=max(best_rel,rel)
            last_t=centre
        if cur is not None: segments.append((start_t,last_t,cur,best_rel))
        def bykey(k):
            for d,*_ in candidates:
                if definition_key(d)==k: return d
            return None
        lines=[f"{family} switch scan", f"Window {window:.1f} s / step {step:.1f} s", ""]
        if not segments:
            lines.append("No sustained, clearly separated table winner was detected. Candidates were tied/ambiguous in the usable windows.")
        else:
            for a,b,key,rel in segments:
                d=bykey(key)
                if d:
                    # Expand centre span by half a step to make timeline easier to read.
                    lines.append(f"{max(0,a-step/2):7.1f}–{min(duration,b+step/2):7.1f} s   {short_map_name(d)}   separation up to {rel*100:.1f}%")
        lines.append("\nSwitch scan is a fit-based diagnostic aid. Fuzzed or naturally separated maps make it much stronger; ambiguous windows are intentionally omitted.")
        self._show_scrollable_report(f"{family} — switch scan", "\n".join(lines))

    def _toggle_log_panel(self, visible: bool):
        self.log_panel.setVisible(visible)
        if visible:
            if self.btn_info.isChecked():
                self.outer_splitter.setSizes([500, 360, 120])
            else:
                self.outer_splitter.setSizes([500, 480, 0])

    def _toggle_info_panel(self, visible: bool):
        self.info.setVisible(visible)
        if visible:
            self.outer_splitter.setSizes([500, 360 if self.log_panel.isVisible() else 0, 120])
        else:
            self.outer_splitter.setSizes([500, 480 if self.log_panel.isVisible() else 0, 0])

    def open_log(self, path: Path | str | None = None):
        if path is None:
            self.log_panel.open_dialog()
        else:
            self.log_panel.open_log(path)

    def _log_loaded(self, log: TestOLog):
        self.log_data = log
        self.log_index = 0
        self.log_panel.set_definition(self.definition)
        self._apply_saved_axis_binding()
        self._recompute_log_heatmap()
        self._recompute_log_stats()
        self._update_log_overlay()

    def _log_cursor_changed(self, index: int):
        self.log_index = int(index)
        self._update_log_overlay()

    def _log_range_changed(self, start: float, end: float):
        self._recompute_log_heatmap(start, end)
        self._recompute_log_stats(start, end)
        if self.definition and self.document and self.engine:
            self.refresh(defer_surface=True)

    @staticmethod
    def _fractional_index(values: list[float], value: float) -> float | None:
        if not values or not math.isfinite(value):
            return None
        if len(values) == 1:
            return 0.0
        for i in range(len(values) - 1):
            a, b = float(values[i]), float(values[i + 1])
            if (a <= value <= b) or (b <= value <= a):
                if b == a:
                    return float(i)
                return i + (value - a) / (b - a)
        # Clamp outside the map rather than hiding the marker; this makes axis mismatch obvious.
        first, last = float(values[0]), float(values[-1])
        if (last >= first and value < first) or (last < first and value > first):
            return 0.0
        return float(len(values) - 1)

    @staticmethod
    def _interp_grid(z: list[list[float]], row_f: float, col_f: float) -> float:
        rows, cols = len(z), len(z[0]) if z else 0
        if not rows or not cols:
            return math.nan
        row_f = max(0.0, min(float(rows - 1), row_f))
        col_f = max(0.0, min(float(cols - 1), col_f))
        r0, c0 = int(math.floor(row_f)), int(math.floor(col_f))
        r1, c1 = min(rows - 1, r0 + 1), min(cols - 1, c0 + 1)
        ry, cx = row_f - r0, col_f - c0
        z00, z10 = z[r0][c0], z[r0][c1]
        z01, z11 = z[r1][c0], z[r1][c1]
        lo = z00 * (1.0 - cx) + z10 * cx
        hi = z01 * (1.0 - cx) + z11 * cx
        return lo * (1.0 - ry) + hi * ry

    def _prepare_log_map(self):
        if not self.log_data or not self.definition or not self.document:
            return None
        profile = self.log_panel.profile or profile_for_definition(self.definition, self.log_data)
        if not profile.x_signal or not self.log_data.has(profile.x_signal):
            return None
        if self.definition.rows > 1 and (not profile.y_signal or not self.log_data.has(profile.y_signal)):
            return None
        xvals, yvals = self._axis_values()
        zvals = self.document.map_values(self.definition)
        return profile, xvals, yvals, zvals

    def _log_position(self, index: int | None = None, prepared=None):
        prepared = prepared or self._prepare_log_map()
        if prepared is None or not self.log_data or not self.definition:
            return None
        profile, xvals, yvals, zvals = prepared
        i = self.log_index if index is None else int(index)
        x = self.log_data.value(profile.x_signal, i)
        cf = self._fractional_index(xvals, x)
        if cf is None:
            return None
        if self.definition.rows <= 1:
            rf = 0.0
            y = None
        else:
            y = self.log_data.value(profile.y_signal, i)
            rf = self._fractional_index(yvals, y)
            if rf is None:
                return None
        z = self._interp_grid(zvals, rf, cf)
        return x, y, z, rf, cf

    def _update_log_overlay(self):
        prepared = self._prepare_log_map()
        pos = self._log_position(prepared=prepared)
        if pos is None:
            self.table_log_marker.hide()
            self.surface.set_log_overlay(None, [])
            self.log_panel.set_map_value(None)
            return
        x, y, z, rf, cf = pos
        trail = []
        if self.log_data:
            t_now = self.log_data.times[self.log_index]
            start = int(np.searchsorted(self.log_data.times, max(0.0, t_now - 1.5)))
            for idx in range(start, self.log_index + 1):
                p = self._log_position(idx, prepared)
                if p is None:
                    continue
                px, py, pz, _rf, _cf = p
                trail.append((px, pz) if self.definition.rows <= 1 else (px, py, pz))
        point = (x, z) if self.definition.rows <= 1 else (x, y, z)
        map_unit = self.definition.z_units or ""
        if not map_unit and prepared is not None and self.log_data:
            profile = prepared[0]
            for signal in profile.traces:
                if self.log_data.has(signal) and self.log_data.units.get(signal):
                    map_unit = self.log_data.units.get(signal, "")
                    break
        self.log_panel.set_map_value(z, map_unit)
        self.surface.set_log_overlay(point, trail)
        self._position_log_marker(rf, cf)

    def _position_log_marker(self, row_f: float | None = None, col_f: float | None = None):
        if not self.definition or not self.table.isVisible():
            self.table_log_marker.hide()
            return
        if row_f is None or col_f is None:
            pos = self._log_position()
            if pos is None:
                self.table_log_marker.hide(); return
            _x, _y, _z, row_f, col_f = pos
        c0 = int(math.floor(col_f)); c1 = min(self.definition.cols - 1, c0 + 1); cx = col_f - c0
        r0 = int(math.floor(row_f)); r1 = min(self.definition.rows - 1, r0 + 1); ry = row_f - r0
        x0 = self.table.columnViewportPosition(c0) + self.table.columnWidth(c0) / 2
        x1 = self.table.columnViewportPosition(c1) + self.table.columnWidth(c1) / 2
        y0 = self.table.rowViewportPosition(r0) + self.table.rowHeight(r0) / 2
        y1 = self.table.rowViewportPosition(r1) + self.table.rowHeight(r1) / 2
        x = x0 * (1.0 - cx) + x1 * cx
        y = y0 * (1.0 - ry) + y1 * ry
        self.table_log_marker.adjustSize()
        self.table_log_marker.move(int(x - self.table_log_marker.width() / 2), int(y - self.table_log_marker.height() / 2))
        self.table_log_marker.show()
        self.table_log_marker.raise_()

    def _recompute_log_heatmap(self, start: float | None = None, end: float | None = None):
        self.log_heatmap = None
        if not self.log_data or not self.definition or not self.document:
            return
        if start is None or end is None:
            if self.log_panel.range_seconds is None:
                return
            start, end = self.log_panel.range_seconds
        if not (math.isfinite(float(start)) and math.isfinite(float(end))):
            return
        profile = self.log_panel.profile or profile_for_definition(self.definition, self.log_data)
        if not profile.x_signal or not self.log_data.has(profile.x_signal):
            return
        if self.definition.rows > 1 and (not profile.y_signal or not self.log_data.has(profile.y_signal)):
            return
        xvals, yvals = self._axis_values()
        counts = np.zeros((self.definition.rows, self.definition.cols), dtype=int)
        i0 = int(np.searchsorted(self.log_data.times, min(start, end), side="left"))
        i1 = int(np.searchsorted(self.log_data.times, max(start, end), side="right"))
        for i in range(i0, min(i1, self.log_data.sample_count)):
            x = self.log_data.value(profile.x_signal, i)
            cf = self._fractional_index(xvals, x)
            if cf is None:
                continue
            c = int(round(cf))
            if self.definition.rows <= 1:
                r = 0
            else:
                y = self.log_data.value(profile.y_signal, i)
                rf = self._fractional_index(yvals, y)
                if rf is None:
                    continue
                r = int(round(rf))
            counts[max(0, min(self.definition.rows - 1, r)), max(0, min(self.definition.cols - 1, c))] += 1
        self.log_heatmap = counts if counts.any() else None

    def _is_n75_definition(self) -> bool:
        if not self.definition:
            return False
        if self.definition.discovery_unverified:
            return False
        category = (self.definition.category or "").casefold()
        title = (self.definition.title or "").casefold()
        return category == "n75" or "n75" in title or "vnt" in title or "boost actuator" in title

    def _recompute_log_stats(self, start: float | None = None, end: float | None = None):
        """Aggregate long-log N75 behavior back into map cells using bilinear weights.

        The logged final N75 command is compared with the interpolated base-table
        value at each sample. This is a controller-correction view, not a suggestion
        to copy the delta 1:1 into the feed-forward table.
        """
        self.log_stats = None
        if not self._is_n75_definition() or not self.log_data or not self.document or not self.definition:
            return
        if not self.log_data.has(SIG_N75):
            return
        profile = self.log_panel.profile or profile_for_definition(self.definition, self.log_data)
        if not profile.x_signal or not self.log_data.has(profile.x_signal):
            return
        if self.definition.rows > 1 and (not profile.y_signal or not self.log_data.has(profile.y_signal)):
            return
        xvals, yvals = self._axis_values()
        zvals = self.document.map_values(self.definition)
        rows, cols = self.definition.rows, self.definition.cols
        weight = np.zeros((rows, cols), dtype=float)
        hits = np.zeros((rows, cols), dtype=int)
        logged_sum = np.zeros((rows, cols), dtype=float)
        logged_sq = np.zeros((rows, cols), dtype=float)
        diff_sum = np.zeros((rows, cols), dtype=float)
        min_seen = np.full((rows, cols), np.nan, dtype=float)
        max_seen = np.full((rows, cols), np.nan, dtype=float)

        if start is None or end is None or not (math.isfinite(float(start)) and math.isfinite(float(end))):
            if self.log_panel.range_seconds is not None:
                start, end = self.log_panel.range_seconds
            else:
                start, end = 0.0, self.log_data.duration
        i0 = int(np.searchsorted(self.log_data.times, min(float(start), float(end)), side="left"))
        i1 = int(np.searchsorted(self.log_data.times, max(float(start), float(end)), side="right"))

        for i in range(i0, min(i1, self.log_data.sample_count)):
            command = self.log_data.value(SIG_N75, i)
            if not math.isfinite(command):
                continue
            cf = self._fractional_index(xvals, self.log_data.value(profile.x_signal, i))
            if cf is None:
                continue
            if rows <= 1:
                rf = 0.0
            else:
                rf = self._fractional_index(yvals, self.log_data.value(profile.y_signal, i))
                if rf is None:
                    continue
            base_interp = self._interp_grid(zvals, rf, cf)
            if not math.isfinite(base_interp):
                continue
            error = command - base_interp
            r0, c0 = int(math.floor(rf)), int(math.floor(cf))
            r1, c1 = min(rows - 1, r0 + 1), min(cols - 1, c0 + 1)
            ry, cx = rf - r0, cf - c0
            contributions = [
                (r0, c0, (1.0 - ry) * (1.0 - cx)),
                (r0, c1, (1.0 - ry) * cx),
                (r1, c0, ry * (1.0 - cx)),
                (r1, c1, ry * cx),
            ]
            merged: dict[tuple[int, int], float] = {}
            for r, c, w in contributions:
                merged[(r, c)] = merged.get((r, c), 0.0) + float(w)
            for (r, c), w in merged.items():
                if w <= 1e-9:
                    continue
                weight[r, c] += w
                hits[r, c] += 1
                logged_sum[r, c] += command * w
                logged_sq[r, c] += command * command * w
                diff_sum[r, c] += error * w
                min_seen[r, c] = command if not math.isfinite(min_seen[r, c]) else min(min_seen[r, c], command)
                max_seen[r, c] = command if not math.isfinite(max_seen[r, c]) else max(max_seen[r, c], command)

        valid = weight > 1e-9
        if not valid.any():
            return
        avg = np.full((rows, cols), np.nan, dtype=float)
        diff = np.full((rows, cols), np.nan, dtype=float)
        std = np.full((rows, cols), np.nan, dtype=float)
        avg[valid] = logged_sum[valid] / weight[valid]
        diff[valid] = diff_sum[valid] / weight[valid]
        variance = np.zeros((rows, cols), dtype=float)
        variance[valid] = np.maximum(0.0, logged_sq[valid] / weight[valid] - avg[valid] ** 2)
        std[valid] = np.sqrt(variance[valid])
        self.log_stats = {
            "log_avg": avg,
            "log_diff": diff,
            "samples": weight.copy(),
            "hits": hits.astype(float),
            "std": std,
            "min": min_seen,
            "max": max_seen,
        }

    def _table_display_changed(self, index: int):
        mode = str(self.table_data_combo.itemData(index) or "calibration")
        log_modes = {"log_avg", "log_diff", "samples", "std"}
        if mode in log_modes and not self._is_n75_definition():
            mode = "calibration"
            self.table_data_combo.blockSignals(True)
            self.table_data_combo.setCurrentIndex(self.table_data_combo.findData("calibration"))
            self.table_data_combo.blockSignals(False)
            self.statusChanged.emit("Logged table statistics are currently implemented for N75/VNT maps.")
        self.table_display_mode = mode
        self._sync_compare_button()
        if mode in log_modes:
            self._recompute_log_stats()
        if self.definition and self.document and self.engine:
            self.refresh(defer_surface=True)

    def apply_log_delta_dialog(self):
        if not self.document or not self.definition or not self.engine or not self._is_n75_definition():
            self.statusChanged.emit("Apply log delta is available for N75/VNT tables with a loaded log.")
            return
        if self.log_panel.live_mode:
            self.statusChanged.emit("Stop logging before previewing and applying log delta.")
            return
        self._recompute_log_stats()
        if self.log_stats is None:
            self.statusChanged.emit("No valid N75 log samples in the current log range.")
            return
        dlg = LogDeltaDialog(self.document, self.definition, self.log_stats,
                             self.engine.selection.cells(), self)
        if dlg.exec() != QDialog.Accepted:
            return
        self.engine.last_warnings = []
        changed = self.document.set_cells(self.definition, dlg.updates,
            f"Apply {dlg.percent.value()}% log delta: {self.definition.title}")
        self.table_data_combo.setCurrentIndex(self.table_data_combo.findData("calibration"))
        self._after_edit(changed)

    def _editing_allowed(self) -> bool:
        if self.table_display_mode == "calibration":
            return True
        self.statusChanged.emit("Switch Table data to Calibration before editing map values.")
        return False

    def set_cell_mode(self, mode: str, notify: bool = False) -> None:
        if mode not in CalibrationTable.CELL_MODES:
            mode = "fit"
        index = self.cell_mode_combo.findData(mode)
        self.cell_mode_combo.blockSignals(True)
        if index >= 0:
            self.cell_mode_combo.setCurrentIndex(index)
        self.cell_mode_combo.blockSignals(False)
        self.table.set_cell_mode(mode)
        if notify:
            self.cellModeChanged.emit(mode)

    def _cell_mode_combo_changed(self, index: int) -> None:
        mode = self.cell_mode_combo.itemData(index) or "fit"
        self.table.set_cell_mode(str(mode))
        self.cellModeChanged.emit(str(mode))

    def set_link_resolver(self, resolver):
        self.link_resolver = resolver
        self.refresh_link_config()

    def set_step_resolver(self, resolver):
        self.step_resolver = resolver

    def set_document(self, document: CalibrationDocument | None):
        if document is not None:
            profile_id = document.xdf.profile_id
            changed_profile = profile_id != self.log_panel.ecu_profile_id
            self.log_panel.set_ecu_profile(profile_id, document.xdf.title)
            if changed_profile:
                self.log_data = None
                self.log_index = 0
        self.document = document
        self.map_pages.setCurrentWidget(self.view_splitter if document is not None else self.welcome_page)
        self.definition = None
        self.engine = None
        self.info.set_definition(None)
        self.link_label.setText("Not linked")
        self.log_panel.set_definition(None)
        self.log_heatmap = None
        self.log_stats = None
        self.clear()

    def refresh_link_config(self):
        if not self.engine or not self.definition:
            self.link_label.setText("Not linked")
            return
        resolved = self.link_resolver(self.definition) if self.link_resolver else None
        if resolved:
            name, mode, targets = resolved
            self.engine.configure_links(targets, mode, self.link_check.isChecked())
            mode_text = "ABSOLUTE" if mode == "absolute" else "RELATIVE %"
            self.link_label.setText(f"🔗 {name} — {mode_text} ({1 + len(targets)} maps)")
        else:
            self.engine.configure_links([], None, False)
            self.link_label.setText("Not linked")

    def _link_toggle_changed(self, checked: bool):
        self.refresh_link_config()

    def clear(self):
        self.table.clear()
        self.table.setRowCount(0)
        self.table.setColumnCount(0)
        self.surface.definition = None
        self.surface.set_log_overlay(None, [])
        self.surface.redraw()
        self.table_log_marker.hide()

    def set_definition(self, definition: CalibrationDefinition):
        if self.document is None:
            return
        self.definition = definition
        self.engine = EditEngine(self.document, definition)
        self.refresh_link_config()
        self.info.set_definition(definition)
        self.surface.set_log_overlay(None, [])
        self.log_panel.set_definition(definition)
        self._apply_saved_axis_binding()
        log_modes = {"log_avg", "log_diff", "samples", "std"}
        if self.table_display_mode in log_modes and not self._is_n75_definition():
            self.table_display_mode = "calibration"
            self.table_data_combo.blockSignals(True)
            self.table_data_combo.setCurrentIndex(self.table_data_combo.findData("calibration"))
            self.table_data_combo.blockSignals(False)
        self._recompute_log_heatmap()
        self._recompute_log_stats()
        self.refresh()
        self.table.setFocus()

    def _axis_values(self):
        assert self.document and self.definition
        d = self.definition
        x = self.document.axis_values(d.x_axis) if d.x_axis else [float(i) for i in range(d.cols)]
        y = self.document.axis_values(d.y_axis) if d.y_axis else [float(i) for i in range(d.rows)]
        if len(x) != d.cols:
            x = list(range(d.cols))
        if len(y) != d.rows:
            y = list(range(d.rows))
        return x, y

    def refresh(self, defer_surface: bool = False):
        if not self.document or not self.definition or not self.engine:
            return
        d = self.definition
        z = self.document.map_values(d)
        base = self._reference_values(d)
        x, y = self._axis_values()
        self.table.blockSignals(True)
        self.table.setRowCount(d.rows)
        self.table.setColumnCount(d.cols)
        xlabels = self.document.axis_labels(d.x_axis) if d.x_axis else [str(i) for i in range(d.cols)]
        ylabels = self.document.axis_labels(d.y_axis) if d.y_axis else [str(i) for i in range(d.rows)]
        if len(xlabels) == d.cols:
            self.table.setHorizontalHeaderLabels(xlabels)
        if len(ylabels) == d.rows:
            self.table.setVerticalHeaderLabels(ylabels)
        self.table.apply_cell_size()
        display = z
        display_decimals = d.decimal_places
        if self.table_display_mode == "reference":
            display = base
            display_decimals = d.decimal_places
        elif self.table_display_mode == "ref_diff":
            display = [[z[r][c] - base[r][c] for c in range(d.cols)] for r in range(d.rows)]
            display_decimals = max(2, d.decimal_places)
        elif self.table_display_mode != "calibration" and self.log_stats is not None and self.table_display_mode in self.log_stats:
            display = self.log_stats[self.table_display_mode]
            display_decimals = 1 if self.table_display_mode == "samples" else max(2, d.decimal_places)
        flat = [float(v) for row in display for v in row if math.isfinite(float(v))]
        lo, hi = (min(flat), max(flat)) if flat else (0.0, 1.0)
        span = hi - lo if hi != lo else 1.0
        cmap = colormaps["viridis"]
        for r in range(d.rows):
            for c in range(d.cols):
                val = z[r][c]
                shown = float(display[r][c])
                item = self.table.item(r, c)
                if item is None:
                    item = QTableWidgetItem()
                    self.table.setItem(r, c, item)
                item.setText("—" if not math.isfinite(shown) else format_number(shown, display_decimals))
                norm = ((shown - lo) / span) if math.isfinite(shown) else 0.0
                rgba = cmap(norm)
                # Lighten for readable table text.
                rr = int(255 * (0.45 + 0.55 * rgba[0]))
                gg = int(255 * (0.45 + 0.55 * rgba[1]))
                bb = int(255 * (0.45 + 0.55 * rgba[2]))
                visits = 0
                if self.log_heatmap is not None and r < self.log_heatmap.shape[0] and c < self.log_heatmap.shape[1]:
                    visits = int(self.log_heatmap[r, c])
                    if visits > 0:
                        peak = max(1, int(self.log_heatmap.max()))
                        strength = min(0.65, 0.18 + 0.47 * (visits / peak) ** 0.5)
                        # Blend toward warm yellow so visited cells stand out without hiding map colours.
                        rr = int(rr * (1.0 - strength) + 255 * strength)
                        gg = int(gg * (1.0 - strength) + 210 * strength)
                        bb = int(bb * (1.0 - strength) + 70 * strength)
                item.setBackground(QColor(rr, gg, bb))
                item.setForeground(QColor(20, 20, 20))
                if not math.isclose(val, base[r][c], rel_tol=0, abs_tol=max(d.base_step * 0.25, 1e-12)):
                    font = item.font(); font.setBold(True); item.setFont(font)
                    tip = f"Reference ({self.compare_name}): {format_number(base[r][c], d.decimal_places)}\nDelta: {val - base[r][c]:+g}"
                    if visits:
                        tip += f"\nLog range visits: {visits}"
                    item.setToolTip(tip)
                else:
                    font = item.font(); font.setBold(False); item.setFont(font)
                    item.setToolTip(f"Log range visits: {visits}" if visits else "")
                if d.profile_id == '361830' and not d.discovery_unverified and d.z_address in (0xF9394, 0xF95D8, 0xF981C) and c < len(x):
                    iq = float(x[c])
                    air_mg, est_lambda = estimate_lambda_from_map_target(val, iq)
                    if math.isfinite(est_lambda):
                        lambda_tip = (
                            f"Estimated λ: {est_lambda:.2f}  (~{air_mg:.0f} mg air/cyl)\n"
                            "Assumptions: absolute MAP, 2.0 L / 4 cyl, VE 90%, IAT 40°C, stoich AFR 14.5, no EGR"
                        )
                        prior = item.toolTip()
                        item.setToolTip((prior + "\n" if prior else "") + lambda_tip)
                if self.log_stats is not None and math.isfinite(float(self.log_stats["samples"][r, c])) and self.log_stats["samples"][r, c] > 0:
                    avg = self.log_stats["log_avg"][r, c]
                    diff = self.log_stats["log_diff"][r, c]
                    std = self.log_stats["std"][r, c]
                    mn = self.log_stats["min"][r, c]
                    mx = self.log_stats["max"][r, c]
                    ws = self.log_stats["samples"][r, c]
                    hits = int(self.log_stats["hits"][r, c])
                    extra = (
                        f"Calibration: {format_number(val, d.decimal_places)}\n"
                        f"Logged N75 avg: {avg:.3f}\n"
                        f"Logged − table(interpolated): {diff:+.3f}\n"
                        f"Std dev: {std:.3f}\nMin/Max: {mn:.3f} / {mx:.3f}\n"
                        f"Weighted samples: {ws:.1f} ({hits} contributing samples)"
                    )
                    prior = item.toolTip()
                    item.setToolTip((prior + "\n" if prior else "") + extra)
        self.table.blockSignals(False)
        self._sync_table_selection()
        self.surface.update_surface(d, x, y, z, base, self.engine.selection, defer=defer_surface)
        self._update_log_overlay()
        self._emit_status()

    def refresh_selection(self):
        """Selection-only refresh; avoids reparsing/recoloring/replotting the map."""
        if not self.engine:
            return
        self._sync_table_selection()
        self.surface.update_selection(self.engine.selection)
        self._position_log_marker()
        self._emit_status()

    def _sync_table_selection(self):
        if not self.engine:
            return
        s = self.engine.selection
        self.table.clearSelection()
        for r, c in s.cells():
            item = self.table.item(r, c)
            if item:
                item.setSelected(True)
        # Do not let setCurrentCell() issue ClearAndSelect and collapse the
        # rectangle we just painted. Current index is focus/navigation only.
        index = self.table.model().index(s.active_row, s.active_col)
        self.table.selectionModel().setCurrentIndex(index, QItemSelectionModel.SelectionFlag.NoUpdate)

    def _mouse_select(self, row: int, col: int, extend: bool):
        if not self.engine:
            return
        if extend:
            self.engine.selection.active_row = row
            self.engine.selection.active_col = col
        else:
            self.engine.selection.set_single(row, col)
        self.engine.selection.clamp(self.definition.rows, self.definition.cols)
        self.refresh_selection()

    def _toggle_compare_mode(self, _checked: bool) -> None:
        # Cycle exactly between the loaded reference values and current-reference delta.
        # If pressed from Calibration/log-stat mode, enter Reference BIN first.
        mode = "ref_diff" if self.table_display_mode == "reference" else "reference"
        idx = self.table_data_combo.findData(mode)
        if idx >= 0:
            self.table_data_combo.setCurrentIndex(idx)
        self._sync_compare_button()

    def _sync_compare_button(self) -> None:
        if not hasattr(self, "btn_compare_mode"):
            return
        is_diff = self.table_display_mode == "ref_diff"
        is_reference = self.table_display_mode == "reference"
        self.btn_compare_mode.blockSignals(True)
        self.btn_compare_mode.setChecked(is_diff)
        if is_diff:
            label = "Compare: Δ"
        elif is_reference:
            label = "Compare: values"
        else:
            label = "Compare view"
        self.btn_compare_mode.setText(label)
        self.btn_compare_mode.blockSignals(False)

    def set_compare_reference(self, payload: bytes | None, name: str | None = None):
        self.compare_bytes = payload
        self.compare_name = name or "Original BIN"
        self.compare_label.setText(f"Reference: {self.compare_name}")
        self.refresh()

    def _reference_values(self, definition: CalibrationDefinition) -> list[list[float]]:
        if self.document is None:
            return []
        if self.compare_bytes is not None:
            try:
                return self.document.map_values_from_source(definition, self.compare_bytes)
            except Exception:
                pass
        return self.document.map_values(definition, original=True)

    def _compare_changed(self, checked):
        self.surface.show_original = checked
        self.surface.redraw()

    def set_view(self, mode: int):
        self.view_mode = mode
        if mode == self.VIEW_2D:
            self.table.show(); self.surface.hide(); self.table.setFocus()
        elif mode == self.VIEW_3D:
            self.table.hide(); self.surface.show(); self.surface.setFocus()
            self.table_log_marker.hide()
        else:
            self.table.show(); self.surface.show(); self.table.setFocus()
        QTimer.singleShot(0, self._update_log_overlay)

    def toggle_2d_3d(self):
        if self.view_mode == self.VIEW_3D:
            self.set_view(self.VIEW_2D)
        else:
            self.set_view(self.VIEW_3D)

    def _emit_status(self):
        if not self.document or not self.definition or not self.engine:
            return
        d, s = self.definition, self.engine.selection
        r, c = s.active_row, s.active_col
        current = self.document.cell_value(d, r, c)
        ref_vals = self._reference_values(d)
        original = ref_vals[r][c] if ref_vals else self.document.cell_value(d, r, c, original=True)
        raw = self.document.cell_raw(d, r, c)
        addr = self.document.cell_address(d, r, c)
        self.statusChanged.emit(
            f"0x{addr:06X}  cell [{r},{c}]  raw={raw}  value={format_number(current, d.decimal_places)}"
            f"  ref={format_number(original, d.decimal_places)}  Δ={current - original:+g}"
        )

    def refresh_selection_only(self):
        self.refresh_selection()

    def _after_edit(self, changed: bool):
        warnings = list(self.engine.last_warnings) if self.engine else []
        if changed:
            self._recompute_log_stats()
            self.refresh(defer_surface=True)
            self.changed.emit()
        if warnings:
            shown = "\n".join(warnings[:8])
            if len(warnings) > 8:
                shown += f"\n… and {len(warnings) - 8} more"
            QMessageBox.warning(self, APP_NAME, "Linked edit warning:\n\n" + shown)

    def nudge(self, sign: int, modifiers: Qt.KeyboardModifiers):
        if not self.engine or not self.definition or not self._editing_allowed():
            return
        if self.step_resolver:
            normal, shifted, fine = self.step_resolver(self.definition)
        else:
            # Sensible physical-unit fallback. Raw XDF LSBs can be tiny (for
            # example 0.01 in the N75 maps), which is not useful for W/Q tuning.
            normal = max(1.0, self.definition.base_step)
            shifted = normal * 10.0
            fine = normal * 0.1
        if modifiers & Qt.ShiftModifier:
            step = shifted
        elif modifiers & Qt.ControlModifier:
            step = fine
        else:
            step = normal
        self._after_edit(self.engine.nudge(sign * step))

    def multiply_dialog(self):
        if not self.engine or not self._editing_allowed():
            return
        factor, ok = QInputDialog.getDouble(self, "Multiply selection", "Factor (e.g. 0.95 or 1.05):", 1.0, -1000000, 1000000, 6)
        if ok:
            self._after_edit(self.engine.multiply(factor))

    def set_value_dialog(self, initial_text: str | None = None):
        if not self.engine or not self.definition or not self.document or not self._editing_allowed():
            return
        s = self.engine.selection
        current = self.document.cell_value(self.definition, s.active_row, s.active_col)
        initial = initial_text if initial_text is not None else format_number(current, self.definition.decimal_places)
        dlg = NumericValueDialog(
            f"Value for {len(s.cells())} selected cell{'s' if len(s.cells()) != 1 else ''}:",
            initial,
            select_all=(initial_text is None),
            parent=self,
        )
        if dlg.exec() != QDialog.Accepted:
            return
        raw_text = dlg.text().strip().replace(",", ".")
        try:
            value = float(raw_text)
        except ValueError:
            QMessageBox.warning(self, APP_NAME, f"'{dlg.text()}' is not a valid number.")
            return
        self._after_edit(self.engine.set_value(value))

    def edit_axis(self, which: str, index: int):
        if not self.definition or not self.document:
            return
        axis = self.definition.x_axis if which == "x" else self.definition.y_axis
        if axis is None or not axis.editable:
            return
        vals = self.document.axis_values(axis)
        value, ok = QInputDialog.getDouble(self, f"Edit {which.upper()} axis", f"Axis cell {index}:", vals[index], -1e12, 1e12, max(0, axis.decimal_places))
        if ok:
            trial = list(vals)
            trial[index] = value
            if len(trial) > 1:
                ascending = vals[-1] >= vals[0]
                monotonic = all((trial[i + 1] > trial[i]) if ascending else (trial[i + 1] < trial[i]) for i in range(len(trial) - 1))
                if not monotonic:
                    answer = QMessageBox.warning(
                        self, APP_NAME,
                        "This axis edit makes the breakpoints non-monotonic or duplicates a breakpoint.\n\n"
                        "ECU interpolation may behave unpredictably. Write it anyway?",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.No,
                    )
                    if answer != QMessageBox.StandardButton.Yes:
                        return
            changed = self.document.set_axis_value(axis, index, value, f"Edit {which.upper()} axis {self.definition.title}")
            self._after_edit(changed)

    def copy_selection(self):
        if not self.engine or not self.document or not self.definition:
            return
        z = self.document.map_values(self.definition)
        s = self.engine.selection
        rows = []
        for r in range(s.r0, s.r1 + 1):
            rows.append("\t".join(format_number(z[r][c], self.definition.decimal_places) for c in range(s.c0, s.c1 + 1)))
        QApplication.clipboard().setText("\n".join(rows))

    def paste_selection(self):
        if not self.engine or not self._editing_allowed():
            return
        text = QApplication.clipboard().text().strip()
        if not text:
            return
        grid: list[list[float]] = []
        try:
            for line in text.splitlines():
                parts = [p.strip() for p in line.replace(";", "\t").split("\t") if p.strip()]
                if not parts:
                    continue
                grid.append([float(p.replace(",", ".")) for p in parts])
        except ValueError:
            QMessageBox.warning(self, APP_NAME, "Clipboard does not contain a numeric grid.")
            return
        self._after_edit(self.engine.paste_grid(grid))

    def home_end(self, to_end: bool, extend: bool):
        if not self.engine or not self.definition:
            return
        s = self.engine.selection
        target = self.definition.cols - 1 if to_end else 0
        if extend:
            s.active_col = target
        else:
            s.set_single(s.active_row, target)
        self.refresh_selection()

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() != QEvent.KeyPress or not self.engine:
            return super().eventFilter(obj, event)
        e: QKeyEvent = event
        key, mods = e.key(), e.modifiers()

        text = e.text() or ""
        edit_key = (
            (not (mods & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier)) and (text.isdigit() or text in {".", ","}))
            or key in (Qt.Key_W, Qt.Key_Plus, Qt.Key_Equal, Qt.Key_Q, Qt.Key_Minus, Qt.Key_H, Qt.Key_V, Qt.Key_I, Qt.Key_S, Qt.Key_M, Qt.Key_Return, Qt.Key_Enter)
            or (key in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down) and bool(mods & Qt.AltModifier))
            or (key == Qt.Key_V and bool(mods & Qt.ControlModifier))
        )
        if self.table_display_mode != "calibration" and edit_key:
            self._editing_allowed()
            return True

        # Direct numeric entry: type the first digit while one or many cells are
        # selected, continue typing in the tiny value dialog, then Enter. This
        # avoids the old Enter-then-type dance for multi-cell edits.
        if not (mods & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier)) and (text.isdigit() or text in {".", ","}):
            self.set_value_dialog(text)
            return True

        # MTune 2D/3D shared keys.
        if key in (Qt.Key_W, Qt.Key_Plus, Qt.Key_Equal) and not (mods & Qt.AltModifier):
            self.nudge(+1, mods); return True
        if key in (Qt.Key_Q, Qt.Key_Minus) and not (mods & Qt.AltModifier):
            self.nudge(-1, mods); return True
        if key in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down):
            dr = -1 if key == Qt.Key_Up else (1 if key == Qt.Key_Down else 0)
            dc = -1 if key == Qt.Key_Left else (1 if key == Qt.Key_Right else 0)
            if mods & Qt.AltModifier:
                self._after_edit(self.engine.copy_direction(dr, dc))
            else:
                self.engine.move_selection(dr, dc, bool(mods & Qt.ShiftModifier))
                self.refresh_selection()
            return True
        if key == Qt.Key_H and not mods:
            self._after_edit(self.engine.interpolate_horizontal()); return True
        if key == Qt.Key_V and not mods:
            self._after_edit(self.engine.interpolate_vertical()); return True
        if key == Qt.Key_I and not mods:
            self._after_edit(self.engine.interpolate_area()); return True
        if key == Qt.Key_S and not (mods & (Qt.ControlModifier | Qt.AltModifier)):
            self._after_edit(self.engine.smooth()); return True
        if key == Qt.Key_D and not mods:
            self.toggle_2d_3d(); return True
        if key == Qt.Key_M and not mods:
            self.multiply_dialog(); return True
        if key in (Qt.Key_Return, Qt.Key_Enter) and not mods:
            self.set_value_dialog(); return True
        if key == Qt.Key_Home:
            self.home_end(False, bool(mods & Qt.ShiftModifier)); return True
        if key == Qt.Key_End:
            self.home_end(True, bool(mods & Qt.ShiftModifier)); return True
        if key == Qt.Key_G and not mods and self.view_mode != self.VIEW_2D:
            self.engine.grow(); self.refresh_selection(); return True
        if key == Qt.Key_B and not mods and self.view_mode != self.VIEW_2D:
            self.engine.reduce(); self.refresh_selection(); return True
        if key == Qt.Key_C and not mods and self.view_mode != self.VIEW_2D:
            s = self.engine.selection; s.set_single(s.active_row, s.active_col); self.refresh_selection(); return True
        if key == Qt.Key_A and mods & Qt.ControlModifier:
            self.engine.selection.set_rect(0, 0, self.definition.rows - 1, self.definition.cols - 1); self.refresh_selection(); return True
        if key == Qt.Key_C and mods & Qt.ControlModifier:
            self.copy_selection(); return True
        if key == Qt.Key_V and mods & Qt.ControlModifier:
            self.paste_selection(); return True
        return super().eventFilter(obj, event)


class NumericValueDialog(QDialog):
    """Deterministic direct-entry dialog that never drops the triggering digit."""

    def __init__(self, label_text: str, initial_text: str, select_all: bool = False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Set selected cells")
        self.setModal(True)
        self.value_edit = QLineEdit(str(initial_text))
        self.value_edit.setAlignment(Qt.AlignmentFlag.AlignRight)
        label = QLabel(label_text)
        ok = QPushButton("OK")
        cancel = QPushButton("Cancel")
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)
        self.value_edit.returnPressed.connect(self.accept)
        buttons = QHBoxLayout(); buttons.addStretch(1); buttons.addWidget(cancel); buttons.addWidget(ok)
        layout = QVBoxLayout(self); layout.addWidget(label); layout.addWidget(self.value_edit); layout.addLayout(buttons)
        self._select_all = bool(select_all)
        QTimer.singleShot(0, self._focus_entry)

    def _focus_entry(self):
        self.value_edit.setFocus(Qt.FocusReason.OtherFocusReason)
        if self._select_all:
            self.value_edit.selectAll()
        else:
            self.value_edit.setCursorPosition(len(self.value_edit.text()))

    def text(self) -> str:
        return self.value_edit.text()


class HelpDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("MTune-style keys")
        self.resize(600, 520)
        label = QLabel(
            "<b>Editing (2D + 3D)</b><br>"
            "W / + : increase &nbsp;&nbsp; Q / - : decrease<br>"
            "Shift + W/Q : larger step &nbsp;&nbsp; Ctrl + W/Q : smaller step<br>"
            "Arrows : move selection &nbsp;&nbsp; Shift+Arrows : expand/shrink<br>"
            "Alt+Arrows : copy selected area one cell in that direction<br>"
            "H : horizontal interpolate &nbsp;&nbsp; V : vertical interpolate<br>"
            "S : smooth &nbsp;&nbsp; Enter : set selected value<br>"
            "Type a digit : immediately start numeric entry for all selected cells<br>"
            "D : switch 2D / 3D &nbsp;&nbsp; Ctrl+Z : undo &nbsp;&nbsp; Ctrl+R : redo<br>"
            "Home/End : first/last horizontal cell &nbsp;&nbsp; Shift+Home/End : extend<br>"
            "Ctrl+C / Ctrl+V : copy/paste numeric grid<br>"
            "Ctrl+S : Quick Save next _rNNN revision &nbsp;&nbsp; Ctrl+Shift+S : Save As<br><br>"
            "<b>Extra V1 keys</b><br>"
            "M : multiply selection (available in both views)<br>"
            "I : bilinear interpolation from the four selection corners<br>"
            "G / B : grow / reduce selection in 3D<br>"
            "C : collapse 3D selection to active cell<br>"
            "Ctrl+A : select whole map<br><br>"
            "<b>Linked map groups</b><br>"
            "Configure in Settings &gt; Linked map groups. Absolute copies the resulting values exactly. "
            "Relative % applies each edited cell's percentage change to the other maps, preserving intentional map-to-map differences. "
            "The Linked edits checkbox temporarily disables/enables propagation.<br><br>"
            "<b>V1.10 workflow</b><br>"
            "Settings &gt; W / Q edit steps sets global or current-map steps. The bundled N75 and boost maps have useful first-run presets. "
            "Use the ★ toolbar button to add/remove the current table from the Favorites tab. Quick Save asks for a one-line revision note and increments _r001, _r002, etc.<br>"
            "Open a CSV log from File &gt; Open Log. F6 or the Log button shows/hides the synchronized log pane. Add traces or stacked graphs without changing the map-position binding. Mouse-wheel zooms the time axis; Fit time resets it; drag a range for the cell-visit heatmap and N75 statistics.<br><br>"
            "<b>Mouse</b><br>"
            "Click selects one cell. Click-drag makes a stable rectangle. Shift+click extends from the existing anchor. "
            "Double-click a table cell or press Enter to enter a value. Double-click an axis header to edit an axis value.<br><br>"
            "Smoothing is intentionally conservative and MTune-like; V1 does not claim MaxxECU's exact internal smoothing weights."
        )
        label.setWordWrap(True)
        layout = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(label)
        layout.addWidget(scroll, 1)
        if self.screen():
            self.resize(720, min(640, self.screen().availableGeometry().height()-100))
        close = QPushButton("Close"); close.clicked.connect(self.accept); layout.addWidget(close)


class AboutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("About BMW DDE5 MS43 Tuner")
        self.resize(510, 290)
        layout = QVBoxLayout(self)
        title = QLabel(APP_NAME)
        title.setStyleSheet("font-size:24px; font-weight:600;")
        layout.addWidget(title)
        layout.addWidget(QLabel(f"by {APP_AUTHOR}"))
        details = QLabel("Calibration editing, 2D / 3D maps, recorded log review and TestO live logging.\n\n"
                         "Bundled definition: Bosch EDC16C31 / BMW 361830.\n"
                         "361830 checksum checking and correction are available under ECU > Direct DDE5 connection.\n\n"
                         "BMW, TunerPro and MaxxECU are third-party names. This editor is an independent project.")
        details.setWordWrap(True)
        layout.addWidget(details, 1)
        details.setTextInteractionFlags(Qt.TextSelectableByMouse)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        layout.addWidget(close)


class RevisionCommentDialog(QDialog):
    def __init__(self, revision_name: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Quick Save Revision")
        self.resize(560, 150)
        self.comment = QLineEdit()
        self.comment.setPlaceholderText("e.g. N75 -2% at 1800-2500 rpm")
        self.skip_session = QCheckBox("Don't ask for a comment again this session")
        info = QLabel(f"Saving <b>{revision_name}</b>. Add a short note for this revision:")
        info.setWordWrap(True)
        save = QPushButton("Save revision")
        save.setDefault(True)
        cancel = QPushButton("Cancel")
        save.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)
        buttons = QHBoxLayout(); buttons.addStretch(1); buttons.addWidget(cancel); buttons.addWidget(save)
        layout = QVBoxLayout(self)
        layout.addWidget(info)
        layout.addWidget(self.comment)
        layout.addWidget(self.skip_session)
        layout.addLayout(buttons)
        self.comment.setFocus()


class EditStepsDialog(QDialog):
    """Configure physical-unit W/Q steps globally and for the current table/group."""

    def __init__(self, defaults: dict, overrides: dict, current: CalibrationDefinition | None,
                 linked_keys: list[str] | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("W / Q edit steps")
        self.resize(620, 420)
        self.defaults = dict(defaults or {})
        self.overrides = json.loads(json.dumps(overrides or {}))
        self.current = current
        self.linked_keys = list(dict.fromkeys(linked_keys or []))

        def field(value: float) -> QLineEdit:
            e = QLineEdit(f"{float(value):g}")
            e.setMaximumWidth(160)
            return e

        self.def_normal = field(self.defaults.get("normal", 1.0))
        self.def_shift = field(self.defaults.get("shift", 10.0))
        self.def_ctrl = field(self.defaults.get("ctrl", 0.1))
        global_form = QFormLayout()
        global_form.addRow("W / Q step:", self.def_normal)
        global_form.addRow("Shift + W / Q step:", self.def_shift)
        global_form.addRow("Ctrl + W / Q fine step:", self.def_ctrl)

        current_key = definition_key(current) if current else None
        existing = self.overrides.get(current_key, {}) if current_key else {}
        self.use_override = QCheckBox("Use custom steps for the current table")
        self.use_override.setChecked(bool(current_key and current_key in self.overrides))
        self.cur_normal = field(existing.get("normal", self.defaults.get("normal", 1.0)))
        self.cur_shift = field(existing.get("shift", self.defaults.get("shift", 10.0)))
        self.cur_ctrl = field(existing.get("ctrl", self.defaults.get("ctrl", 0.1)))
        self.apply_group = QCheckBox("Apply the current-table values to the whole linked group")
        self.apply_group.setEnabled(len(self.linked_keys) > 1)

        current_form = QFormLayout()
        current_form.addRow("W / Q step:", self.cur_normal)
        current_form.addRow("Shift + W / Q step:", self.cur_shift)
        current_form.addRow("Ctrl + W / Q fine step:", self.cur_ctrl)

        note = QLabel(
            "Steps are in the table's displayed physical units, not raw ECU counts. "
            "For example, an N75 table scaled as 0.01 per raw count can use W/Q = 1.0 and Shift+W/Q = 5.0."
        )
        note.setWordWrap(True)

        current_name = current.title if current else "No table currently open"
        current_label = QLabel(f"<b>Current table:</b> {current_name}")
        current_label.setWordWrap(True)
        if current is None:
            self.use_override.setEnabled(False)
            self.cur_normal.setEnabled(False); self.cur_shift.setEnabled(False); self.cur_ctrl.setEnabled(False)

        save = QPushButton("Save settings")
        save.setDefault(True)
        cancel = QPushButton("Cancel")
        save.clicked.connect(self._validate_and_accept)
        cancel.clicked.connect(self.reject)
        buttons = QHBoxLayout(); buttons.addStretch(1); buttons.addWidget(cancel); buttons.addWidget(save)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<b>Default steps</b>"))
        layout.addLayout(global_form)
        layout.addSpacing(8)
        layout.addWidget(current_label)
        layout.addWidget(self.use_override)
        layout.addLayout(current_form)
        layout.addWidget(self.apply_group)
        layout.addSpacing(8)
        layout.addWidget(note)
        layout.addStretch(1)
        layout.addLayout(buttons)

    @staticmethod
    def _number(edit: QLineEdit) -> float:
        value = float(edit.text().strip().replace(",", "."))
        if not math.isfinite(value) or value <= 0:
            raise ValueError
        return value

    def _validate_and_accept(self):
        try:
            defaults = {
                "normal": self._number(self.def_normal),
                "shift": self._number(self.def_shift),
                "ctrl": self._number(self.def_ctrl),
            }
            custom = {
                "normal": self._number(self.cur_normal),
                "shift": self._number(self.cur_shift),
                "ctrl": self._number(self.cur_ctrl),
            }
        except ValueError:
            QMessageBox.warning(self, APP_NAME, "All edit steps must be positive numbers.")
            return
        self.defaults = defaults
        if self.current is not None:
            key = definition_key(self.current)
            if self.use_override.isChecked():
                keys = self.linked_keys if self.apply_group.isChecked() and self.linked_keys else [key]
                for member_key in keys:
                    self.overrides[member_key] = dict(custom)
            else:
                self.overrides.pop(key, None)
        self.accept()


class LinkGroupsDialog(QDialog):
    """Configure persistent linked-map groups for the currently loaded XDF."""

    def __init__(self, xdf: XDFDatabase, groups: list[dict], current: CalibrationDefinition | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Linked map groups")
        self.resize(1050, 720)
        self.xdf = xdf
        self.groups = json.loads(json.dumps(groups or []))
        self.current = current
        self._loading = False

        self.group_list = QListWidget()
        self.group_list.currentRowChanged.connect(self._load_group)

        self.name_edit = QLineEdit()
        self.name_edit.textChanged.connect(self._name_changed)
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Relative % — apply the same percentage change", "relative")
        self.mode_combo.addItem("Absolute — all linked cells become the same value", "absolute")
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)

        self.member_filter = QLineEdit()
        self.member_filter.setPlaceholderText("Filter maps…")
        self.member_filter.textChanged.connect(self._filter_members)
        self.member_list = QListWidget()
        self.member_list.itemChanged.connect(self._members_changed)
        for d in sorted(self.xdf.tables, key=lambda d: (d.category.casefold(), d.z_address, d.title.casefold())):
            item = QListWidgetItem(f"0x{d.z_address:06X}  {d.title}   [{d.category}]   {d.rows}x{d.cols}")
            item.setData(Qt.UserRole, definition_key(d))
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            cats = " → ".join(d.categories) if d.categories else d.category
            item.setToolTip(f"{cats}\n{d.rows} x {d.cols}\n0x{d.z_address:06X}")
            self.member_list.addItem(item)

        add_btn = QPushButton("New group")
        add_btn.clicked.connect(self._new_group)
        del_btn = QPushButton("Delete group")
        del_btn.clicked.connect(self._delete_group)
        close_btn = QPushButton("Save && close")
        close_btn.clicked.connect(self._accept_if_valid)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)

        left = QVBoxLayout()
        left.addWidget(QLabel("Groups"))
        left.addWidget(self.group_list, 1)
        row = QHBoxLayout(); row.addWidget(add_btn); row.addWidget(del_btn)
        left.addLayout(row)
        left_widget = QWidget(); left_widget.setLayout(left)

        form = QFormLayout()
        form.addRow("Group name:", self.name_edit)
        form.addRow("Link mode:", self.mode_combo)
        note = QLabel(
            "<b>Absolute:</b> the final edited values are copied to every member.<br>"
            "<b>Relative %:</b> each source cell's factor (new ÷ old) is applied to the corresponding cell in every other member. "
            "This is the useful mode when one N75 map deliberately sits higher/lower than the others. "
            "A source cell that starts at zero cannot define a percentage change and is skipped with a warning.<br><br>"
            "V1.02 links Z-data by matching cell index. All maps in one group must therefore have the same dimensions."
        )
        note.setWordWrap(True)

        right = QVBoxLayout()
        right.addLayout(form)
        right.addWidget(note)
        right.addSpacing(8)
        right.addWidget(QLabel("Group members"))
        right.addWidget(self.member_filter)
        right.addWidget(self.member_list, 1)
        right_widget = QWidget(); right_widget.setLayout(right)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(left_widget); split.addWidget(right_widget)
        split.setSizes([280, 760])

        bottom = QHBoxLayout(); bottom.addStretch(1); bottom.addWidget(cancel_btn); bottom.addWidget(close_btn)
        layout = QVBoxLayout(self)
        layout.addWidget(split, 1)
        layout.addLayout(bottom)

        self._reload_group_list()
        if not self.groups:
            self._new_group()
        elif current is not None:
            key = definition_key(current)
            row = next((i for i, g in enumerate(self.groups) if key in g.get("members", [])), 0)
            self.group_list.setCurrentRow(row)
        else:
            self.group_list.setCurrentRow(0)

    def _reload_group_list(self):
        current_row = self.group_list.currentRow()
        self.group_list.blockSignals(True)
        self.group_list.clear()
        for i, group in enumerate(self.groups):
            name = str(group.get("name") or f"Group {i + 1}")
            mode = "REL %" if group.get("mode") == "relative" else "ABS"
            self.group_list.addItem(f"{name}   [{mode}]   ({len(group.get('members', []))})")
        self.group_list.blockSignals(False)
        if self.groups:
            self.group_list.setCurrentRow(max(0, min(current_row, len(self.groups) - 1)))

    def _new_group(self):
        members = [definition_key(self.current)] if self.current is not None else []
        self.groups.append({"name": f"Linked group {len(self.groups) + 1}", "mode": "relative", "members": members})
        self._reload_group_list()
        self.group_list.setCurrentRow(len(self.groups) - 1)

    def _delete_group(self):
        row = self.group_list.currentRow()
        if 0 <= row < len(self.groups):
            del self.groups[row]
            self._reload_group_list()
            if self.groups:
                self.group_list.setCurrentRow(min(row, len(self.groups) - 1))
            else:
                self._clear_editor()

    def _clear_editor(self):
        self._loading = True
        self.name_edit.clear()
        for i in range(self.member_list.count()):
            self.member_list.item(i).setCheckState(Qt.Unchecked)
        self._loading = False

    def _load_group(self, row: int):
        if not (0 <= row < len(self.groups)):
            self._clear_editor(); return
        group = self.groups[row]
        self._loading = True
        self.name_edit.setText(str(group.get("name") or f"Group {row + 1}"))
        idx = self.mode_combo.findData(group.get("mode", "relative"))
        self.mode_combo.setCurrentIndex(max(0, idx))
        members = set(group.get("members", []))
        for i in range(self.member_list.count()):
            item = self.member_list.item(i)
            item.setCheckState(Qt.Checked if item.data(Qt.UserRole) in members else Qt.Unchecked)
        self._loading = False

    def _name_changed(self, text: str):
        if self._loading: return
        row = self.group_list.currentRow()
        if 0 <= row < len(self.groups):
            self.groups[row]["name"] = text.strip() or f"Group {row + 1}"
            self._refresh_group_row(row)

    def _mode_changed(self, index: int):
        if self._loading: return
        row = self.group_list.currentRow()
        if 0 <= row < len(self.groups):
            self.groups[row]["mode"] = self.mode_combo.currentData()
            self._refresh_group_row(row)

    def _members_changed(self, _item):
        if self._loading: return
        row = self.group_list.currentRow()
        if 0 <= row < len(self.groups):
            self.groups[row]["members"] = [
                self.member_list.item(i).data(Qt.UserRole)
                for i in range(self.member_list.count())
                if self.member_list.item(i).checkState() == Qt.Checked
            ]
            self._refresh_group_row(row)

    def _refresh_group_row(self, row: int):
        if not (0 <= row < len(self.groups)) or row >= self.group_list.count(): return
        g = self.groups[row]
        name = str(g.get("name") or f"Group {row + 1}")
        mode = "REL %" if g.get("mode") == "relative" else "ABS"
        self.group_list.item(row).setText(f"{name}   [{mode}]   ({len(g.get('members', []))})")

    def _filter_members(self, text: str):
        needle = text.casefold().strip()
        for i in range(self.member_list.count()):
            item = self.member_list.item(i)
            item.setHidden(bool(needle and needle not in item.text().casefold()))

    def _definition_by_key(self, key: str) -> CalibrationDefinition | None:
        return next((d for d in self.xdf.tables if definition_key(d) == key), None)

    def _validate(self) -> str | None:
        used: dict[str, str] = {}
        for idx, g in enumerate(self.groups):
            name = str(g.get("name") or f"Group {idx + 1}")
            members = [m for m in g.get("members", []) if self._definition_by_key(m) is not None]
            g["members"] = members
            if len(members) < 2:
                return f"'{name}' needs at least two maps."
            dims = {(self._definition_by_key(m).rows, self._definition_by_key(m).cols) for m in members}
            if len(dims) != 1:
                return f"'{name}' contains maps with different dimensions. V1.02 requires matching dimensions."
            for key in members:
                if key in used:
                    return f"A map can only belong to one linked group. It is currently in both '{used[key]}' and '{name}'."
                used[key] = name
            if g.get("mode") not in {"relative", "absolute"}:
                g["mode"] = "relative"
        return None

    def _accept_if_valid(self):
        error = self._validate()
        if error:
            QMessageBox.warning(self, APP_NAME, error)
            return
        self.accept()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.resize(1500, 900)
        self.setWindowTitle(APP_NAME)
        self.settings = editor_settings(QSettings)
        self.link_groups: list[dict] = self._load_link_groups()
        self.step_defaults, self.step_overrides = self._load_edit_steps()
        self.favorites: set[str] = self._load_favorites()
        self.ask_revision_comments = True
        self.xdf: XDFDatabase | None = None
        self.document: CalibrationDocument | None = None
        self.current_xdf_path: Path | None = None

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search names or addresses…")
        self.search.textChanged.connect(self.filter_tree)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(['Name', 'Address'])
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.tree.itemActivated.connect(self.open_tree_item)
        self.tree.itemClicked.connect(self.open_tree_item)
        all_page = QWidget()
        all_layout = QVBoxLayout(all_page); all_layout.setContentsMargins(2,2,2,2)
        all_layout.addWidget(self.search); all_layout.addWidget(self.tree)

        self.favorite_search = QLineEdit()
        self.favorite_search.setPlaceholderText("Search favorite names or addresses…")
        self.favorite_search.textChanged.connect(self.filter_favorites)
        self.favorite_tree = QTreeWidget()
        self.favorite_tree.setHeaderLabels(['Name', 'Address'])
        self.favorite_tree.header().setStretchLastSection(False)
        self.favorite_tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.favorite_tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.favorite_tree.itemActivated.connect(self.open_tree_item)
        self.favorite_tree.itemClicked.connect(self.open_tree_item)
        fav_page = QWidget()
        fav_layout = QVBoxLayout(fav_page); fav_layout.setContentsMargins(2,2,2,2)
        fav_layout.addWidget(self.favorite_search); fav_layout.addWidget(self.favorite_tree)

        self.left_tabs = QTabWidget()
        self.left_tabs.addTab(all_page, "All tables")
        self.left_tabs.addTab(fav_page, "★ Favorites")

        left = QWidget()
        # Long calibration names need a genuinely useful navigation width.
        # Keep it resizable, but do not let an old saved V1.13/V1.14 splitter
        # position collapse it back to the ~130 px pane seen in the screenshot.
        left.setMinimumWidth(120)
        self.left_tabs.setMinimumWidth(110)
        ll = QVBoxLayout(left); ll.setContentsMargins(4,4,4,4)
        ll.addWidget(self.left_tabs)

        self.editor = MapEditor()
        self.editor.set_link_resolver(self.resolve_link_group)
        self.editor.set_step_resolver(self.resolve_edit_steps)
        self.editor.set_cell_mode(str(self.settings.value("table_cell_mode", "fit")))
        self.editor.cellModeChanged.connect(self._save_cell_mode)
        self.editor.statusChanged.connect(self._status)
        self.editor.changed.connect(self._document_changed)
        self.root_splitter = QSplitter(Qt.Horizontal)
        # V1.18: make the navigation divider deliberately easy to grab and resize.
        # A 14 px handle is much friendlier on high-DPI Windows screens than the old 7 px handle.
        self.root_splitter.setHandleWidth(14)
        self.root_splitter.setOpaqueResize(True)
        self.root_splitter.setChildrenCollapsible(False)
        self.root_splitter.addWidget(left); self.root_splitter.addWidget(self.editor)
        self.root_splitter.setStretchFactor(0, 0); self.root_splitter.setStretchFactor(1, 1)
        # Keep both panes freely resizable.  Do not impose a maximum width on the navigator.
        left.setMinimumWidth(120)
        self.editor.setMinimumWidth(420)
        saved_root = self.settings.value("layout/root_splitter", "")
        try:
            sizes = [int(x) for x in json.loads(str(saved_root))]
            if len(sizes) != 2:
                sizes = [340, 1160]
            if sizes[0] < 120:
                total = max(1200, sum(max(0, x) for x in sizes))
                sizes = [300, max(700, total - 300)]
            self.root_splitter.setSizes(sizes)
        except Exception:
            self.root_splitter.setSizes([340,1160])
        self.root_splitter.splitterMoved.connect(self._root_splitter_moved)
        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(2)
        central_layout.addWidget(self.root_splitter, 1)
        # Full window width, independent of the map/log splitter and navigator width.
        central_layout.addWidget(self.editor.log_panel.reading_strip)
        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())
        # Optional detailed BIN identity; filename remains in the window title.
        self.bin_identity_label = QLabel("BIN: none")
        self.bin_identity_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.bin_identity_label.setToolTip("No BIN loaded")
        self.statusBar().addPermanentWidget(self.bin_identity_label, 1)

        self._make_actions()
        self._make_toolbar()
        self._install_shortcuts()

        self._update_bin_identity()
        self._status("Open a BIN to propose matching definitions, or load or create an XDF from Definitions.")

    def _load_link_groups(self) -> list[dict]:
        raw = self.settings.value("linked_map_groups", "[]")
        try:
            groups = json.loads(str(raw))
            if isinstance(groups, list):
                return [g for g in groups if isinstance(g, dict)]
        except Exception:
            pass
        return []

    def _save_link_groups(self) -> None:
        self.settings.setValue("linked_map_groups", json.dumps(self.link_groups, ensure_ascii=False))
        self.settings.setValue("linked_map_groups_initialized", True)

    def _load_edit_steps(self) -> tuple[dict, dict]:
        defaults = {"normal": 1.0, "shift": 10.0, "ctrl": 0.1}
        overrides: dict[str, dict] = {}
        raw = self.settings.value("edit_steps", "")
        if raw:
            try:
                payload = json.loads(str(raw))
                if isinstance(payload, dict):
                    saved_defaults = payload.get("defaults", {})
                    if isinstance(saved_defaults, dict):
                        for key in defaults:
                            value = float(saved_defaults.get(key, defaults[key]))
                            if math.isfinite(value) and value > 0:
                                defaults[key] = value
                    saved_overrides = payload.get("overrides", {})
                    if isinstance(saved_overrides, dict):
                        for map_key, values in saved_overrides.items():
                            if not isinstance(values, dict):
                                continue
                            try:
                                parsed = {k: float(values[k]) for k in ("normal", "shift", "ctrl")}
                            except Exception:
                                continue
                            if all(math.isfinite(v) and v > 0 for v in parsed.values()):
                                overrides[str(map_key)] = parsed
            except Exception:
                pass
        return defaults, overrides

    def _save_edit_steps(self) -> None:
        payload = {"defaults": self.step_defaults, "overrides": self.step_overrides}
        self.settings.setValue("edit_steps", json.dumps(payload, ensure_ascii=False))
        self.settings.setValue("edit_steps_initialized", True)

    def _seed_default_edit_steps(self) -> None:
        if self.xdf is None or self.xdf.profile_id != '361830' or self.settings.value("edit_steps_initialized", False, type=bool):
            return
        by_addr = {d.z_address: d for d in self.xdf.tables if not d.discovery_unverified}
        # Physical-unit defaults chosen for the maps used most often on this BMW.
        # They are only first-run presets and remain fully editable in Settings.
        presets = {
            0xF7F44: (1.0, 5.0, 0.1), 0xF8478: (1.0, 5.0, 0.1),
            0xF86BC: (1.0, 5.0, 0.1), 0xF8900: (1.0, 5.0, 0.1),
            0xF9394: (10.0, 50.0, 1.0), 0xF95D8: (10.0, 50.0, 1.0),
            0xF981C: (10.0, 50.0, 1.0),
        }
        for address, values in presets.items():
            d = by_addr.get(address)
            if d is not None:
                self.step_overrides[definition_key(d)] = {
                    "normal": values[0], "shift": values[1], "ctrl": values[2]
                }
        self._save_edit_steps()

    def resolve_edit_steps(self, definition: CalibrationDefinition) -> tuple[float, float, float]:
        values = self.step_overrides.get(definition_key(definition), self.step_defaults)
        return (
            float(values.get("normal", 1.0)),
            float(values.get("shift", 10.0)),
            float(values.get("ctrl", 0.1)),
        )

    def configure_edit_steps(self):
        current = self.editor.definition
        linked_keys: list[str] = []
        if current is not None:
            linked_keys = [definition_key(current)]
            resolved = self.resolve_link_group(current)
            if resolved:
                _name, _mode, targets = resolved
                linked_keys.extend(definition_key(d) for d in targets)
        dlg = EditStepsDialog(self.step_defaults, self.step_overrides, current, linked_keys, self)
        if dlg.exec() == QDialog.Accepted:
            self.step_defaults = dlg.defaults
            self.step_overrides = dlg.overrides
            self._save_edit_steps()
            if current is not None:
                normal, shifted, fine = self.resolve_edit_steps(current)
                self._status(f"Edit steps for current table: W/Q {normal:g}, Shift {shifted:g}, Ctrl {fine:g}")
            else:
                self._status("Default W/Q edit steps saved.")

    def _load_favorites(self) -> set[str]:
        raw = self.settings.value("favorite_maps", "[]")
        try:
            values = json.loads(str(raw))
            if isinstance(values, list):
                return {str(v) for v in values}
        except Exception:
            pass
        return set()

    def _save_favorites(self) -> None:
        self.settings.setValue("favorite_maps", json.dumps(sorted(self.favorites)))

    def toggle_favorite(self):
        d = self.editor.definition
        if d is None:
            self._status("Open a table first, then toggle Favorite.")
            return
        key = definition_key(d)
        if key in self.favorites:
            self.favorites.remove(key)
            action = "Removed from"
        else:
            self.favorites.add(key)
            action = "Added to"
        self._save_favorites()
        self.populate_favorites()
        self._status(f"{action} Favorites: {d.title}")

    def _seed_default_link_groups(self) -> None:
        """First-run presets for the bundled 361830 definition; fully editable in Settings."""
        if self.xdf is None or self.xdf.profile_id != '361830' or self.settings.value("linked_map_groups_initialized", False, type=bool):
            return
        by_addr = {d.z_address: d for d in self.xdf.tables if not d.discovery_unverified}
        boost = [0xF9394, 0xF95D8, 0xF981C]
        n75 = [0xF7F44, 0xF8478, 0xF86BC, 0xF8900]
        groups: list[dict] = []
        if all(a in by_addr for a in boost):
            groups.append({
                "name": "Boost Target",
                "mode": "absolute",
                "members": [definition_key(by_addr[a]) for a in boost],
            })
        if all(a in by_addr for a in n75):
            groups.append({
                "name": "N75 / VNT Base",
                "mode": "absolute",
                "members": [definition_key(by_addr[a]) for a in n75],
            })
        self.link_groups = groups
        self._save_link_groups()

    def resolve_link_group(self, definition: CalibrationDefinition):
        if self.xdf is None:
            return None
        key = definition_key(definition)
        group = next((g for g in self.link_groups if key in g.get("members", [])), None)
        if group is None:
            return None
        lookup = {definition_key(d): d for d in self.xdf.tables}
        targets = [lookup[m] for m in group.get("members", []) if m in lookup and m != key]
        if not targets:
            return None
        return str(group.get("name") or "Linked group"), str(group.get("mode") or "relative"), targets

    def configure_link_groups(self):
        if self.xdf is None:
            QMessageBox.information(self, APP_NAME, "Load an XDF before configuring linked maps.")
            return
        current = self.editor.definition
        dlg = LinkGroupsDialog(self.xdf, self.link_groups, current, self)
        if dlg.exec() == QDialog.Accepted:
            self.link_groups = dlg.groups
            self._save_link_groups()
            self.editor.refresh_link_config()
            count = len(self.link_groups)
            self._status(f"Saved {count} linked map group{'s' if count != 1 else ''}.")

    def _make_actions(self):
        menu = self.menuBar().addMenu("&File")
        self.act_xdf = QAction("Open &XDF…", self); self.act_xdf.triggered.connect(self.open_xdf)
        self.act_bundled = QAction("Load bundled BMW 361830 definition", self); self.act_bundled.triggered.connect(self.load_bundled_definition)
        self.act_bundled.setText('Load local BMW 361830 definition')
        self.act_bundled.setEnabled(bundled_definition_path().is_file())
        self.act_bin = QAction("Open &BIN…", self); self.act_bin.triggered.connect(self.open_bin)
        self.act_compare_bin = QAction("Open Compare BIN…", self); self.act_compare_bin.triggered.connect(self.open_compare_bin)
        self.act_log = QAction("Open &Log…", self); self.act_log.triggered.connect(self.open_log)
        self.act_live_log = QAction("Live logging…", self)
        self.act_live_log.triggered.connect(self.open_live_logging)
        self.act_quick_save = QAction("Quick Save Revision", self); self.act_quick_save.triggered.connect(self.quick_save_revision); self.act_quick_save.setEnabled(False)
        self.act_save = QAction("Save BIN &As…", self); self.act_save.triggered.connect(self.save_as); self.act_save.setEnabled(False)
        self.act_reload = QAction("Revert BIN to original", self); self.act_reload.triggered.connect(self.revert_original); self.act_reload.setEnabled(False)
        menu.addActions([self.act_xdf, self.act_bundled, self.act_bin, self.act_compare_bin, self.act_log, self.act_live_log, self.act_quick_save, self.act_save, self.act_reload])
        self.act_export_flash = QAction("Export BIN for flashing…", self)
        self.act_export_flash.triggered.connect(self.export_flash_bin)
        menu.addAction(self.act_export_flash)
        menu.addSeparator()
        quit_action = QAction("Exit", self); quit_action.triggered.connect(self.close); menu.addAction(quit_action)

        ecu_menu = self.menuBar().addMenu("&ECU")
        self.act_ecu_direct = QAction("Direct DDE5 connection / change review…", self)
        self.act_ecu_direct.triggered.connect(self.open_ecu_programming)
        ecu_menu.addAction(self.act_ecu_direct)

        edit = self.menuBar().addMenu("&Edit")
        self.act_undo = QAction("Undo", self); self.act_undo.triggered.connect(self.undo)
        self.act_redo = QAction("Redo", self); self.act_redo.triggered.connect(self.redo)
        edit.addActions([self.act_undo, self.act_redo])
        edit.addSeparator()
        self.act_drift_response = QAction('MS43X drift throttle response preset…', self)
        self.act_drift_response.triggered.connect(self.preview_drift_response)
        edit.addAction(self.act_drift_response)

        definitions = self.menuBar().addMenu("&Definitions")
        self.act_new_xdf = QAction("New XDF…", self)
        self.act_new_xdf.triggered.connect(lambda: self.show_xdf_editor(new=True))
        self.act_edit_xdf = QAction("XDF editor…", self)
        self.act_edit_xdf.triggered.connect(lambda: self.show_xdf_editor())
        self.act_find_maps = QAction("Find suspected tables…", self)
        self.act_find_maps.triggered.connect(self.find_suspected_tables)
        self.act_suggest_xdf = QAction('Suggest matching XDF…', self)
        self.act_suggest_xdf.triggered.connect(self.suggest_matching_xdf)
        self.act_import_ols = QAction('Import WinOLS project…', self)
        self.act_import_ols.triggered.connect(self.import_ols_project)
        definitions.addActions([self.act_new_xdf, self.act_edit_xdf, self.act_find_maps, self.act_suggest_xdf, self.act_import_ols])

        view = self.menuBar().addMenu("&View")
        a2 = QAction("2D table", self); a2.triggered.connect(lambda: self.editor.set_view(MapEditor.VIEW_2D))
        a3 = QAction("3D surface", self); a3.triggered.connect(lambda: self.editor.set_view(MapEditor.VIEW_3D))
        asp = QAction("Split", self); asp.triggered.connect(lambda: self.editor.set_view(MapEditor.VIEW_SPLIT))
        view.addActions([a2, a3, asp])
        view.addSeparator()
        self.act_gauges = QAction("Bottom gauges…", self)
        self.act_gauges.triggered.connect(self.editor.log_panel.configure_gauges)
        view.addAction(self.act_gauges)
        self.act_fullscreen = QAction("Full screen", self)
        self.act_fullscreen.setCheckable(True)
        self.act_fullscreen.setShortcut(QKeySequence("F11"))
        self.act_fullscreen.toggled.connect(lambda enabled: self.showFullScreen() if enabled else self.showMaximized())
        view.addAction(self.act_fullscreen)
        self.act_status_bar = QAction("Show status bar", self)
        self.act_status_bar.setCheckable(True)
        self.act_status_bar.setChecked(self.settings.value("layout/show_status_bar", False, type=bool))
        self.statusBar().setVisible(self.act_status_bar.isChecked())
        self.act_status_bar.toggled.connect(self._toggle_status_bar)
        view.addAction(self.act_status_bar)
        view.addSeparator()
        fit_cells = QAction("Cell size: Fit", self); fit_cells.triggered.connect(lambda: self.editor.set_cell_mode("fit", True))
        compact_cells = QAction("Cell size: Compact", self); compact_cells.triggered.connect(lambda: self.editor.set_cell_mode("compact", True))
        normal_cells = QAction("Cell size: Normal", self); normal_cells.triggered.connect(lambda: self.editor.set_cell_mode("normal", True))
        view.addActions([fit_cells, compact_cells, normal_cells])

        settings_menu = self.menuBar().addMenu("&Settings")
        self.act_links = QAction("Linked map groups…", self)
        self.act_links.triggered.connect(self.configure_link_groups)
        self.act_steps = QAction("W / Q edit steps…", self)
        self.act_steps.triggered.connect(self.configure_edit_steps)
        settings_menu.addAction(self.act_links)
        settings_menu.addAction(self.act_steps)
        self.act_fuel_estimates = QAction("Fuel estimates…", self)
        self.act_fuel_estimates.triggered.connect(self.editor.log_panel.configure_fuel_estimates)
        settings_menu.addAction(self.act_fuel_estimates)

        self.act_favorite = QAction("★ Toggle current table favorite", self)
        self.act_favorite.triggered.connect(self.toggle_favorite)
        view.addSeparator()
        view.addAction(self.act_favorite)

        review_menu = self.menuBar().addMenu("&Analysis")
        ai_action = QAction("ChatGPT tuning tips…", self)
        ai_action.triggered.connect(self.show_chatgpt_tips)
        review_menu.addAction(ai_action)
        review_menu.addSeparator()
        review_action = QAction("Calibration consistency review...", self)
        review_action.triggered.connect(self.show_calibration_review)
        review_menu.addAction(review_action)
        eoi_action = QAction("EOI estimate...", self)
        eoi_action.triggered.connect(self.show_eoi)
        review_menu.addAction(eoi_action)

        helpm = self.menuBar().addMenu("&Help")
        hk = QAction("MTune-style keys", self); hk.triggered.connect(lambda: HelpDialog(self).exec()); helpm.addAction(hk)
        hk.setShortcut(QKeySequence("F1"))
        about = QAction("About BMW DDE5 MS43 Tuner…", self)
        about.triggered.connect(lambda: AboutDialog(self).exec())
        helpm.addAction(about)

    def show_chatgpt_tips(self):
        if not hasattr(self, "ai_dock"):
            from PySide6.QtWidgets import QDockWidget
            from .ai_panel import AIPanel
            self.ai_dock = QDockWidget("ChatGPT tuning tips", self)
            self.ai_dock.setObjectName("chatgptTipsDock")
            self.ai_panel = AIPanel(self)
            self.ai_dock.setWidget(self.ai_panel)
            self.ai_dock.setMinimumWidth(380)
            self.addDockWidget(Qt.RightDockWidgetArea, self.ai_dock)
            self.resizeDocks([self.ai_dock], [440], Qt.Horizontal)
        self.ai_dock.show()
        self.ai_dock.raise_()

    def show_xdf_editor(self, *, new=False, seeds=()):
        from .xdf_gui import XDFEditorDialog
        current = self.editor.definition
        dialog = XDFEditorDialog(None if new else self.xdf,
                                 bytes(self.document.data) if self.document else None, self,
                                 seeds=seeds, current_id=current.unique_id if current and not new else None)
        if dialog.exec() == QDialog.Accepted and dialog.applied_database is not None:
            self._apply_xdf_database(dialog.applied_database, dialog.current_id)

    def _apply_xdf_database(self, database, selected_id=None):
        """Replace definitions without replacing the user's BIN or edit history."""
        if (self.document is not None and database.profile_id != self.editor.log_panel.ecu_profile_id
                and not self.editor.log_panel.stop_live()):
            raise EditorError("Wait for live logging to stop before changing ECU profiles.")
        current_id = selected_id or (self.editor.definition.unique_id if self.editor.definition else None)
        self.xdf = database
        self.current_xdf_path = database.path
        self._seed_default_link_groups()
        self._seed_default_edit_steps()
        self.settings.setValue("last_xdf_dir", str(database.path.parent))
        if self.document is not None:
            self.document.xdf = database
            self.document._original_map_cache.clear()
            self.document._original_axis_cache.clear()
        self.editor.set_document(self.document)
        self.populate_tree()
        if self.document is not None:
            selected = next((d for d in database.definitions if d.unique_id == current_id), None)
            if selected is None and database.definitions:
                selected = database.definitions[0]
            if selected:
                try:
                    self.editor.set_definition(selected)
                except Exception as exc:
                    self._status(f"XDF applied; selected definition could not be displayed: {exc}")
                    self._update_bin_identity()
                    return
        self._update_bin_identity()
        self._status(f"Using XDF: {database.path.name} · BIN bytes and edit history preserved")

    def find_suspected_tables(self):
        if self.document is None:
            QMessageBox.information(self, APP_NAME, "Open a BIN to search for suspected tables. For a new file layout, create a New XDF first.")
            return
        from .xdf_gui import PotentialMapsDialog
        dialog = PotentialMapsDialog(bytes(self.document.data), self.xdf, self)
        if dialog.exec() == QDialog.Accepted:
            self.show_xdf_editor(seeds=dialog.selected_definitions)

    def _current_xdf_compatible(self, data, matches):
        if self.xdf is None:
            return False
        if is_ms43x(self.xdf):
            try:
                database_for_size(self.xdf, len(data))
                base = 0x70000 if len(data) == 0x80000 else 0
                return (data[base + 8:base + 14] == b'43X001'
                        and data[base + 64:base + 76] == b'ca43X001.DAT')
            except EditorError:
                return False
        size = len(data)
        for definition in self.xdf.definitions:
            spans = [(definition.z_address, definition.size * definition.z_width)]
            spans += [(axis.address, axis.count * (axis.element_bits // 8))
                      for axis in (definition.x_axis, definition.y_axis) if axis and axis.address is not None]
            if any(start < 0 or count < 0 or start + count > size for start, count in spans):
                return False
        current = self.xdf.path.resolve()
        if any(entry.path.resolve() == current for entry in discover_entries()):
            return any(match.path.resolve() == current for match in matches)
        if self.xdf.profile_id == '361830':
            metadata = bin_metadata(data)
            region = self.xdf.root.find('./XDFHEADER/REGION')
            expected = int(region.get('size', '0'), 0) if region is not None else 0
            numbers = metadata['software_numbers']
            if expected and expected != size or numbers and not any(n.endswith('361830') for n in numbers):
                return False
        return True

    @staticmethod
    def _empty_xdf_for_bin(data, path):
        from .xdf_edit import XDFWorkspace
        workspace = XDFWorkspace(bin_size=len(data))
        workspace.set_metadata(Path(path).stem + ' — definition workspace', APP_AUTHOR,
            f"Source BIN SHA-256: {bin_metadata(data)['sha256']}. No matching XDF has been selected. "
            'Discover or load definitions for this software and address layout.', False)
        workspace.database.path = Path(path).with_suffix('.xdf')
        return workspace.database

    def _choose_bin_definition(self, data, path, *, require_proposal=False):
        matches = propose_matches(data)
        compatible = self._current_xdf_compatible(data, matches)
        if (not require_proposal and len(matches) == 1 and matches[0].score >= 93
                and matches[0].entry.metadata.get('family') == 'ms43x001'):
            if compatible and is_ms43x(self.xdf):
                return self.xdf, self.current_xdf_path, 'Detected MS43X001; kept current matching XDF.'
            database = XDFDatabase(matches[0].path)
            return database, database.path, 'Automatically loaded matching MS43X001 XDF.'
        if matches:
            dialog = DefinitionProposalDialog(data, Path(path).name, matches, self.xdf, compatible, self)
            if dialog.exec() != QDialog.Accepted:
                return None
            if dialog.choice == 'library':
                database = XDFDatabase(dialog.selected_match.path)
                return database, database.path, 'Selected matching XDF: ' + database.title
            if dialog.choice == 'current':
                return self.xdf, self.current_xdf_path, 'Kept current XDF: ' + self.xdf.title
            if dialog.choice == 'empty':
                return self._empty_xdf_for_bin(data, path), None, 'Opened without definitions.'
            return None
        if require_proposal:
            QMessageBox.information(self, APP_NAME,
                'No validated library definition matches this BIN snapshot. The current XDF and BIN edits are retained.')
            return None
        if compatible:
            return self.xdf, self.current_xdf_path, 'No library match; kept the manually loaded XDF.'
        return self._empty_xdf_for_bin(data, path), None, 'No matching library XDF; use Definitions to load or discover maps.'

    def suggest_matching_xdf(self):
        if self.document is None:
            QMessageBox.information(self, APP_NAME, 'Open a BIN first to propose matching definitions.')
            return
        try:
            result = self._choose_bin_definition(bytes(self.document.data), self.document.path, require_proposal=True)
            if result is None:
                return
            database, selected_path, note = result
            self._apply_xdf_database(database)
            self.current_xdf_path = selected_path
            self._status(note + ' BIN bytes and edit history preserved.')
        except Exception as exc:
            QMessageBox.critical(self, APP_NAME, f'Could not select matching XDF:\n{exc}')

    def show_eoi(self):
        log = self.editor.log_panel.log
        if log is None or isinstance(log, LiveTestOLog):
            QMessageBox.information(self, APP_NAME, "Open a recorded TestO log first. EOI currently evaluates recorded snapshots, not a live stream.")
            return
        from .eoi_gui import EOIDialog
        EOIDialog(log, self.document, self).exec()

    def show_calibration_review(self):
        if self.document is None:
            QMessageBox.information(self, APP_NAME, "Open a BIN first.")
            return
        from .audit import calibration_review
        try:
            report = calibration_review(self.document)
        except Exception as exc:
            QMessageBox.warning(self, APP_NAME, f"Could not review calibration: {exc}")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Calibration consistency review")
        dlg.resize(940, 680)
        layout = QVBoxLayout(dlg)
        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setPlainText(report)
        layout.addWidget(text)
        close = QPushButton("Close")
        close.clicked.connect(dlg.accept)
        layout.addWidget(close)
        dlg.exec()

    def _make_toolbar(self):
        tb = QToolBar("Main")
        self.addToolBar(tb)
        tb.addAction(self.act_xdf); tb.addAction(self.act_bin); tb.addAction(self.act_compare_bin); tb.addAction(self.act_log); tb.addAction(self.act_quick_save); tb.addAction(self.act_save)
        tb.addSeparator(); tb.addAction(self.act_undo); tb.addAction(self.act_redo)
        tb.addSeparator(); tb.addAction(self.act_favorite); tb.addAction(self.act_links); tb.addAction(self.act_steps)
        tb.addSeparator()
        tb.addWidget(QLabel("Nav width:"))
        self.nav_width_spin = QSpinBox()
        self.nav_width_spin.setRange(120, 900)
        self.nav_width_spin.setSingleStep(20)
        self.nav_width_spin.setSuffix(" px")
        self.nav_width_spin.setToolTip("Width of the left table-navigation pane. You can also drag the splitter divider directly.")
        try:
            current = int(self.settings.value("layout/navigator_width", 340))
        except Exception:
            current = 340
        self.nav_width_spin.setValue(max(120, min(900, current)))
        self.nav_width_spin.valueChanged.connect(self._set_navigator_width)
        tb.addWidget(self.nav_width_spin)
        help_action = QAction("Keys", self); help_action.triggered.connect(lambda: HelpDialog(self).exec()); tb.addAction(help_action)

    def _set_navigator_width(self, width: int) -> None:
        if not hasattr(self, "root_splitter"):
            return
        sizes = self.root_splitter.sizes()
        total = max(sum(sizes), width + 420)
        right = max(420, total - int(width))
        self.root_splitter.setSizes([int(width), right])
        self.settings.setValue("layout/root_splitter", json.dumps(self.root_splitter.sizes()))
        self.settings.setValue("layout/navigator_width", int(width))

    def _root_splitter_moved(self, *_args) -> None:
        sizes = self.root_splitter.sizes()
        self.settings.setValue("layout/root_splitter", json.dumps(sizes))
        if sizes:
            self.settings.setValue("layout/navigator_width", int(sizes[0]))
        if hasattr(self, "nav_width_spin") and sizes:
            self.nav_width_spin.blockSignals(True)
            self.nav_width_spin.setValue(max(120, min(900, int(sizes[0]))))
            self.nav_width_spin.blockSignals(False)

    def _install_shortcuts(self):
        # MaxxECU uses Ctrl+R for redo, not Ctrl+Y.
        self.act_undo.setShortcut(QKeySequence("Ctrl+Z"))
        self.act_redo.setShortcut(QKeySequence("Ctrl+R"))
        self.act_quick_save.setShortcut(QKeySequence("Ctrl+S"))
        self.act_save.setShortcut(QKeySequence("Ctrl+Shift+S"))
        QShortcut(QKeySequence("F6"), self, activated=lambda: self.editor.btn_log.setChecked(not self.editor.btn_log.isChecked()))

    def _save_cell_mode(self, mode: str) -> None:
        self.settings.setValue("table_cell_mode", mode)

    def _toggle_status_bar(self, visible):
        self.settings.setValue("layout/show_status_bar", visible)
        self.statusBar().setVisible(visible)

    def _status(self, text: str):
        self.statusBar().showMessage(text)

    def _update_bin_identity(self) -> None:
        """Keep the active BIN unmistakably visible independent of transient status text."""
        self.act_undo.setEnabled(bool(self.document and self.document.undo_stack))
        self.act_redo.setEnabled(bool(self.document and self.document.redo_stack))
        self.act_drift_response.setEnabled(bool(self.document and is_ms43x(self.document.xdf)))
        if self.document is None:
            if hasattr(self, "bin_identity_label"):
                self.bin_identity_label.setText("BIN: none")
                self.bin_identity_label.setToolTip("No BIN loaded")
            title = f"{APP_NAME} — {self.xdf.title}" if self.xdf else APP_NAME
            self.setWindowTitle(title)
            return

        doc = self.document
        try:
            payload = bytes(doc.data)
            sha256 = hashlib.sha256(payload).hexdigest()
        except Exception:
            sha256 = "unavailable"
        state = "MODIFIED" if doc.dirty else "CLEAN"
        dirty = " *" if doc.dirty else ""
        size = len(doc.data)
        name = doc.path.name
        self.bin_identity_label.setText(
            f"BIN: {name}  |  {size:,} B  |  SHA-256 {sha256[:12]}  |  {state}"
        )
        self.bin_identity_label.setToolTip(
            f"Active BIN: {doc.path.resolve()}\n"
            f"Size: {size:,} bytes\n"
            f"Current SHA-256: {sha256}\n"
            f"State: {state}\n"
            "361830 checksum correction is available under ECU > Direct DDE5 connection. Other profiles require their own checksum workflow."
        )
        xdf_part = f" — {self.xdf.title}" if self.xdf else ""
        self.setWindowTitle(f"{APP_NAME} — [{name}]{dirty}{xdf_part}")

    def _confirm_discard_edits(self) -> bool:
        if self.document is None or not self.document.dirty:
            return True
        return QMessageBox.question(self, APP_NAME,
            "There are unsaved edits. Discard them and replace the active document?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes

    def _load_xdf_path(self, path: Path | str, remember: bool = True) -> bool:
        try:
            xdf = XDFDatabase(path)
        except Exception as exc:
            QMessageBox.critical(self, APP_NAME, f"Could not read XDF:\n{exc}")
            return False
        if not self._confirm_discard_edits():
            return False
        self.xdf = xdf
        self.current_xdf_path = Path(path)
        self._seed_default_link_groups()
        self._seed_default_edit_steps()
        if remember:
            self.settings.setValue("last_xdf_dir", str(Path(path).parent))
        self.document = None
        self.editor.set_document(None)
        if hasattr(self, "act_quick_save"):
            self.act_quick_save.setEnabled(False)
            self.act_save.setEnabled(False)
            self.act_reload.setEnabled(False)
        self.populate_tree()
        self._update_bin_identity()
        self._status(f"Loaded XDF: {len(self.xdf.tables)} tables + {len(self.xdf.constants)} constants. Now open a BIN.")
        return True

    def open_xdf(self):
        start = self.settings.value("last_xdf_dir", str(Path.home()))
        path, _ = QFileDialog.getOpenFileName(self, "Open TunerPro XDF", start, "TunerPro XDF (*.xdf);;All files (*.*)")
        if path:
            self._load_xdf_path(path)

    def import_ols_project(self):
        from .ols_import_gui import OLSImportDialog
        protected = [self.current_xdf_path]
        if self.document is not None:
            protected.append(self.document.path)
        dialog = OLSImportDialog(self, protected_paths=protected)
        dialog.exec()
        if dialog.exported_paths:
            self._status(f"Exported {len(dialog.exported_paths)} files from the WinOLS project.")

    def load_bundled_definition(self):
        path = bundled_definition_path()
        if not path.exists():
            QMessageBox.information(self, APP_NAME,
                'Install your optional local 361830 definition or open an XDF through File.\n'
                'See docs/LOCAL_RESOURCES.md.')
            return
        self._load_xdf_path(path, remember=False)

    def open_log(self):
        start = self.settings.value("last_log_dir", str(Path.home()))
        path, _ = QFileDialog.getOpenFileName(self, "Open Log", start, "CSV logs (*.csv *.txt);;All files (*.*)")
        if not path:
            return
        if self.editor.log_panel.open_log(path):
            self.settings.setValue("last_log_dir", str(Path(path).parent))
            self.editor.btn_log.setChecked(True)

    def open_compare_bin(self):
        if self.document is None:
            QMessageBox.information(self, APP_NAME, "Open the working BIN first.")
            return
        start = self.settings.value("last_bin_dir", str(Path.home()))
        path, _ = QFileDialog.getOpenFileName(self, "Open comparison/reference BIN", start, "BIN files (*.bin);;All files (*.*)")
        if not path:
            return
        try:
            payload = Path(path).read_bytes()
            if len(payload) != len(self.document.data):
                raise ValueError(f"Reference BIN is {len(payload):,} bytes; working BIN is {len(self.document.data):,} bytes")
        except Exception as exc:
            QMessageBox.critical(self, APP_NAME, f"Could not load comparison BIN:\n{exc}")
            return
        self.editor.set_compare_reference(payload, Path(path).name)
        self.settings.setValue("last_bin_dir", str(Path(path).parent))
        self._status(f"Reference BIN loaded: {Path(path).name}. Use Δ vs ref or Reference overlay.")

    def open_bin(self):
        start = self.settings.value("last_bin_dir", str(Path.home()))
        path, _ = QFileDialog.getOpenFileName(self, "Open ECU BIN", start, "BIN files (*.bin);;All files (*.*)")
        if not path:
            return
        try:
            snapshot = Path(path).read_bytes()
            result = self._choose_bin_definition(snapshot, path)
            if result is None:
                return
            database, xdf_path, note = result
            database = database_for_size(database, len(snapshot))
            doc = CalibrationDocument(database, path)
            if doc.original != snapshot:
                raise EditorError('The BIN changed while selecting definitions. Reopen the file to check its current bytes.')
            # Validate definitions lie within this BIN. Do not require every axis to be read here.
            max_end = max((d.z_address + d.size * d.z_width for d in doc.xdf.definitions), default=0)
            if max_end > len(doc.data):
                raise EditorError(f"XDF references 0x{max_end:X}, but BIN is only 0x{len(doc.data):X} bytes")
        except Exception as exc:
            QMessageBox.critical(self, APP_NAME, f"Could not open BIN:\n{exc}")
            return
        if not self._confirm_discard_edits():
            return
        if (doc.xdf.profile_id != self.editor.log_panel.ecu_profile_id
                and not self.editor.log_panel.stop_live()):
            self._status("Wait for live logging to stop before opening a different ECU profile.")
            return
        self.xdf = doc.xdf
        self.current_xdf_path = xdf_path
        self._seed_default_link_groups()
        self._seed_default_edit_steps()
        self.populate_tree()
        self.document = doc
        self.editor.set_document(doc)
        self._refresh_ms43_ve_labels()
        self.editor.set_compare_reference(None, "Original BIN")
        self.settings.setValue("last_bin_dir", str(Path(path).parent))
        self.act_quick_save.setEnabled(True); self.act_save.setEnabled(True); self.act_reload.setEnabled(True)
        self._update_bin_identity()
        self._status(f"Opened {Path(path).name} ({len(doc.data):,} bytes). " + note)
        first = self.tree.topLevelItem(0)
        while first and first.childCount():
            first = first.child(0)
        if first:
            self.open_tree_item(first, 0)

    def save_as(self):
        if self.document is None:
            return
        if is_ms43x(self.document.xdf):
            self.export_flash_bin()
            return
        default = self.document.path.with_name(self.document.path.stem + "_edited.bin")
        path, _ = QFileDialog.getSaveFileName(self, "Save edited BIN", str(default), "BIN files (*.bin);;All files (*.*)")
        if not path:
            return
        try:
            self.document.save_as(path)
        except Exception as exc:
            QMessageBox.critical(self, APP_NAME, f"Save failed:\n{exc}")
            return
        self._document_changed()
        QMessageBox.information(self, APP_NAME, "BIN saved.\n\nSaving preserves the current bytes. For the supported 361830 layout, use ECU > Direct DDE5 connection to check and correct checksums before saving. Image signatures and flash acceptance are separate checks.")
        self._status(f"Saved {Path(path).name} — current bytes preserved")

    def export_flash_bin(self):
        if self.document is None:
            return
        try:
            plan = export_plan(self.document)
            suffix = "64K_calibration" if plan.partial else "full"
            default = self.document.path.with_name(self.document.path.stem + "_" + suffix + ".bin")
            filters = "Automatic export (*.bin);;Full working BIN (*.bin)"
            path, selected = QFileDialog.getSaveFileName(self, "Export BIN — " + plan.reason,
                                                        str(default), filters)
            if not path:
                return
            plan = export_binary(self.document, path, full=selected.startswith("Full"))
        except (EditorError, OSError) as exc:
            QMessageBox.critical(self, APP_NAME, f"Could not export BIN:\n{exc}")
            return
        self._status(f"Exported {plan.size:,} bytes to {Path(path).name}. {plan.reason}")
        QMessageBox.information(self, APP_NAME,
            f"Exported {plan.size:,} bytes.\n{plan.reason}\n\n"
            "The full working document and unsaved-edit state are retained. "
            "This export preserves current bytes; verify checksums and flash with the appropriate ECU tool.")

    @staticmethod
    def _revision_root(path: Path) -> tuple[str, int]:
        match = re.match(r"^(.*?)(?:_r(\d+))?$", path.stem, re.IGNORECASE)
        if not match:
            return path.stem, 0
        return match.group(1), int(match.group(2) or 0)

    def _next_revision_target(self) -> tuple[Path, int, str]:
        assert self.document is not None
        source = self.document.path
        root, current = self._revision_root(source)
        highest = current
        suffix = source.suffix or ".bin"
        pattern = re.compile(rf"^{re.escape(root)}_r(\d+){re.escape(suffix)}$", re.IGNORECASE)
        try:
            candidates = source.parent.iterdir()
        except OSError:
            candidates = []
        for candidate in candidates:
            if not candidate.is_file():
                continue
            match = pattern.match(candidate.name)
            if match:
                highest = max(highest, int(match.group(1)))
        revision = highest + 1
        target = source.with_name(f"{root}_r{revision:03d}{suffix}")
        return target, revision, root

    def _append_revision_history(self, target: Path, revision: int, root: str, comment: str) -> None:
        history_path = target.with_name(f"{root}_revisions.json")
        history: list[dict] = []
        if history_path.exists():
            try:
                loaded = json.loads(history_path.read_text(encoding="utf-8"))
                if isinstance(loaded, list):
                    history = [item for item in loaded if isinstance(item, dict)]
            except Exception:
                history = []
        active = self.editor.definition.title if self.editor.definition else ""
        changed_bytes = 0
        if self.document is not None:
            changed_bytes = sum(a != b for a, b in zip(self.document.data, self.document.original))
        history.append({
            "revision": revision,
            "filename": target.name,
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
            "comment": comment,
            "active_table": active,
            "changed_bytes_vs_original": changed_bytes,
        })
        history_path.write_text(json.dumps(history, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def quick_save_revision(self):
        if self.document is None:
            return
        target, revision, root = self._next_revision_target()
        comment = ""
        if self.ask_revision_comments:
            dlg = RevisionCommentDialog(target.name, self)
            if dlg.exec() != QDialog.Accepted:
                return
            comment = dlg.comment.text().strip()
            if dlg.skip_session.isChecked():
                self.ask_revision_comments = False
        try:
            self.document.save_as(target)
        except Exception as exc:
            QMessageBox.critical(self, APP_NAME, f"Quick Save failed:\n{exc}")
            return
        history_warning = ""
        try:
            self._append_revision_history(target, revision, root, comment)
        except Exception as exc:
            history_warning = f" (revision note could not be written: {exc})"
        self._document_changed()
        note = f" — {comment}" if comment else ""
        self._status(f"Quick saved r{revision:03d}: {target.name}{note} — current bytes preserved{history_warning}")

    def revert_original(self):
        if self.document is None:
            return
        if QMessageBox.question(self, APP_NAME, "Discard all edits and restore the originally opened BIN in memory?") != QMessageBox.Yes:
            return
        self.document.restore_original()
        self.editor.refresh(); self._document_changed()

    def undo(self):
        if not self.document:
            return
        label = self.document.undo()
        if label:
            self.editor.refresh(); self._document_changed(); self._status(f"Undo: {label}")

    def redo(self):
        if not self.document:
            return
        label = self.document.redo()
        if label:
            self.editor.refresh(); self._document_changed(); self._status(f"Redo: {label}")

    def _document_changed(self):
        self._update_bin_identity()
        self._refresh_ms43_ve_labels()

    def preview_drift_response(self):
        from .ms43_drift import build_preview, apply_preview
        from .ms43_drift_gui import DriftPreviewDialog
        doc = self.document
        if doc is None:
            return
        try:
            preview = build_preview(doc)
            if DriftPreviewDialog(preview, self).exec() != QDialog.Accepted:
                return
            if self.document is not doc:
                raise EditorError('The active BIN changed. Open a fresh preview.')
            if apply_preview(doc, preview):
                self.editor.refresh()
                self._document_changed()
                self._status('Applied drift throttle response preset — Undo reverses all preset changes.')
        except EditorError as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))

    def populate_tree(self):
        self.tree.clear()
        self._ms43_ve_items = []
        if not self.xdf:
            return
        if is_ms43x(self.xdf):
            self._populate_ms43_tree()
            self.populate_favorites()
            return
        groups: dict[str, QTreeWidgetItem] = {}
        for d in sorted(self.xdf.definitions, key=lambda x: (x.category.casefold(), x.z_address, x.title.casefold())):
            group = groups.get(d.category)
            if group is None:
                category_names = {'SOI': 'Injection timing', 'Nm to IQ': 'Torque to fuel quantity',
                                  'N75': 'Turbo control', 'Air Maf': 'Air and EGR',
                                  'Gear': 'Gear torque limits', 'Injector Cal': 'Injector calibration',
                                  'Misc': 'Other settings', 'Boost Closed Loop [CC]': 'Shared controller (unverified)',
                                  'Rail Closed Loop [CC]': 'Rail pressure controller'}
                category = category_names.get(d.category, d.category) if d.profile_id == '361830' else d.category
                group = QTreeWidgetItem([category])
                group.setFlags(group.flags() & ~Qt.ItemIsSelectable)
                self.tree.addTopLevelItem(group)
                groups[d.category] = group
            item = QTreeWidgetItem([short_map_name(d), f'0x{d.z_address:X}'])
            item.setData(0, Qt.UserRole, d.unique_id or f"{d.kind}:{d.z_address:X}")
            cats = " → ".join(d.categories) if d.categories else d.category
            item.setToolTip(0, f"{d.title}\n0x{d.z_address:06X} — {d.rows}x{d.cols} — {cats}\n{d.description or ''}")
            group.addChild(item)
        self.tree.expandAll()
        self.populate_favorites()

    def _populate_ms43_tree(self):
        groups = {}
        def group_for(path):
            if path not in groups:
                item = QTreeWidgetItem([path[-1]])
                item.setToolTip(0, tooltip_html(GROUP_HELP.get(path[-1], "MS43X settings: " + " → ".join(path))))
                item.setFlags(item.flags() & ~Qt.ItemIsSelectable)
                if len(path) == 1:
                    self.tree.addTopLevelItem(item)
                else:
                    group_for(path[:-1]).addChild(item)
                groups[path] = item
            return groups[path]
        for group in ("Drift setup", "Fuel", "Ignition", "Throttle and pedal", "Torque",
                      "Cam timing", "Engine limiters", "Boost", "Air and load", "Motorsport",
                      "Configuration", "Diagnostics", "Advanced"):
            group_for((group,))
        def add(d, path):
            item = QTreeWidgetItem([readable_name(d), f'0x{d.z_address:X}'])
            item.setData(0, Qt.UserRole, d.unique_id or f"{d.kind}:{d.z_address:X}")
            item.setToolTip(0, tooltip_html(setting_tooltip(d)))
            group_for(path).addChild(item)
            ve = re.fullmatch(r'ip_map_ve_([1-8])__map__n', d.title)
            if ve:
                self._ms43_ve_items.append((item,d,int(ve[1])))
        # The displacement shortcut is deliberately first for an engine conversion.
        ordered = sorted(self.xdf.definitions, key=lambda d: (d.title != 'c_eng_disp', category_path(d), readable_name(d), d.z_address))
        for d in ordered:
            add(d, category_path(d))
            shortcut = drift_shortcuts(d)
            if shortcut:
                add(d, shortcut)
        for path, item in groups.items():
            item.setExpanded(len(path) == 1 and path[0] in ("Drift setup", "Fuel", "Ignition"))
        self._refresh_ms43_ve_labels()

    def _refresh_ms43_ve_labels(self):
        if not self.document or not is_ms43x(self.document.xdf) or self.document.xdf is not self.xdf:
            return
        try:
            labels = ve_selection_labels(self.document)
        except EditorError:
            return
        for item, definition, number in getattr(self, '_ms43_ve_items', []):
            note = labels.get(number, 'selector unavailable')
            item.setText(0, f"VE Table {number} — {note}")
            item.setToolTip(0, tooltip_html(setting_tooltip(definition, usage_note=note)))

    def populate_favorites(self):
        self.favorite_tree.clear()
        if not self.xdf:
            return
        definitions = [d for d in self.xdf.definitions if definition_key(d) in self.favorites]
        for d in sorted(definitions, key=lambda x: (x.category.casefold(), x.title.casefold(), x.z_address)):
            item = QTreeWidgetItem([f"★ {short_map_name(d)}", f'0x{d.z_address:X}'])
            item.setData(0, Qt.UserRole, d.unique_id or f"{d.kind}:{d.z_address:X}")
            cats = " → ".join(d.categories) if d.categories else d.category
            item.setToolTip(0, f"{d.title}\n0x{d.z_address:06X} — {d.rows}x{d.cols} — {cats}\n{d.description or ''}")
            if is_ms43_definition(d):
                item.setToolTip(0, tooltip_html(setting_tooltip(d)))
            self.favorite_tree.addTopLevelItem(item)
        if not definitions:
            placeholder = QTreeWidgetItem(["No favorites yet — open a table and click ★ Favorite"] )
            placeholder.setFlags(placeholder.flags() & ~Qt.ItemIsSelectable)
            self.favorite_tree.addTopLevelItem(placeholder)
        self.filter_favorites(self.favorite_search.text())

    def filter_favorites(self, text: str):
        needle = text.casefold().strip()
        for i in range(self.favorite_tree.topLevelItemCount()):
            item = self.favorite_tree.topLevelItem(i)
            searchable = (' '.join(item.text(column) for column in range(self.favorite_tree.columnCount())) + " " + item.toolTip(0)).casefold()
            item.setHidden(bool(needle and needle not in searchable))

    def filter_tree(self, text: str):
        needle = text.casefold().strip()
        def visit(item, parent_matches=False):
            searchable = ' '.join(item.text(column) for column in range(self.tree.columnCount())) + ' ' + item.toolTip(0)
            matches = parent_matches or not needle or needle in searchable.casefold()
            if item.childCount():
                visible = [visit(item.child(i), matches) for i in range(item.childCount())]
                show = any(visible)
                if needle and show:
                    item.setExpanded(True)
            else:
                show = matches
            item.setHidden(not show)
            return show
        for i in range(self.tree.topLevelItemCount()):
            visit(self.tree.topLevelItem(i))

    def open_tree_item(self, item: QTreeWidgetItem, column: int):
        if not self.xdf or not self.document or item.childCount() > 0:
            return
        key = item.data(0, Qt.UserRole)
        for d in self.xdf.definitions:
            if (d.unique_id or f"{d.kind}:{d.z_address:X}") == key:
                try:
                    self.editor.set_definition(d)
                except Exception as exc:
                    QMessageBox.critical(self, APP_NAME, f"Could not open calibration:\n{exc}")
                return

    def open_live_logging(self):
        self.editor.btn_log.setChecked(True)
        self.editor.log_panel.open_live_dialog()

    def open_ecu_programming(self):
        panel = self.editor.log_panel
        if panel.direct_logger is not None or panel.live_thread is not None:
            QMessageBox.information(self, APP_NAME, "Stop live logging before opening the direct ECU connection.")
            return
        # The requested direct workflow must not auto-start a competing flasher.
        doc = self.document
        review_snapshot = bytes(doc.data) if doc else None
        dialog = EcuProgrammingDialog(
            bytes(doc.original) if doc else None,
            review_snapshot,
            str(doc.path) if doc else '', self)
        dialog.exec()
        corrected = dialog.corrected_image
        if corrected is not None and doc is not None:
            if self.document is not doc or bytes(doc.data) != review_snapshot:
                QMessageBox.warning(self, APP_NAME, 'The loaded image changed during review. Reopen the checksum review.')
                return
            if len(corrected) != len(doc.data):
                QMessageBox.critical(self, APP_NAME, 'Checksum result size does not match the loaded image.')
                return
            updates = [(i, bytes([after])) for i, (before, after)
                       in enumerate(zip(doc.data, corrected)) if before != after]
            if doc.apply_bytes('Correct 361830 arithmetic checksums', updates):
                self.editor.refresh()
                self._document_changed()
                self._status('361830 arithmetic checksums corrected in memory; Undo is available. Save to keep the corrected bytes.')


    def closeEvent(self, event):
        if self.document and self.document.dirty and not getattr(self, "_closing_after_logging", False) and not getattr(self, "_closing_after_ai", False):
            ans = QMessageBox.question(self, APP_NAME, "There are unsaved in-memory edits. Exit anyway?", QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if ans != QMessageBox.Yes:
                event.ignore(); return
        if hasattr(self, "ai_panel") and not self.ai_panel.shutdown():
            if not getattr(self, "_closing_after_ai", False):
                self.ai_panel.idle.connect(self.close)
                self._closing_after_ai = True
            event.ignore()
            return
        if not self.editor.log_panel.stop_live():
            if not getattr(self, "_closing_after_logging", False):
                panel = self.editor.log_panel
                if panel.direct_logger is not None:
                    panel.direct_logger.stopped.connect(self.close)
                else:
                    panel.live_thread.finished.connect(self.close)
                self._closing_after_logging = True
            event.ignore()
            return
        self.editor.log_panel._remember_graphs()
        self.editor.log_panel.settings.sync()
        event.accept()


def run() -> int:
    if sys.platform == 'win32':
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('EmilLarsson.BMWDDE5MS43Tuner')
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationName(APP_AUTHOR)
    icon = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1])) / "assets" / "editor.ico"
    app.setWindowIcon(QIcon(str(icon)))
    win = MainWindow()
    win.showMaximized()
    return app.exec()
