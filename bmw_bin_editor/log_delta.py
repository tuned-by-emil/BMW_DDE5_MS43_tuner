"""Preview a frozen log correction before making one undoable table edit."""
import math
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QComboBox, QSpinBox, QTableWidget, QTableWidgetItem, QPushButton,
    QAbstractItemView, QHeaderView)


def plan_delta(document, definition, stats, cells, percent, minimum_hits):
    if not 0 <= percent <= 100 or minimum_hits < 1:
        raise ValueError('Invalid delta settings')
    updates, preview = {}, []
    for r, c in sorted(set(cells)):
        old = document.cell_value(definition, r, c)
        delta, hits = float(stats['log_diff'][r, c]), float(stats['hits'][r, c])
        target = old + delta * percent / 100
        reason = ''
        if not math.isfinite(delta) or not math.isfinite(hits) or hits < minimum_hits:
            reason = 'Skipped: insufficient data'
        elif not math.isfinite(target) or not 0 <= target <= 100:
            reason = 'Skipped: outside 0–100% duty'
        else:
            raw = document.physical_to_raw(target, definition.z_equation,
                definition.z_element_bits, definition.z_signed)
            target = document.raw_to_physical(raw, definition.z_equation)
            if not 0 <= target <= 100:
                reason = 'Skipped: outside 0–100% duty'
            elif raw == document.cell_raw(definition, r, c):
                reason = 'Unchanged'
            else:
                updates[r, c] = target
        preview.append((r, c, old, delta, hits, target, reason or 'Apply'))
    return updates, preview


class LogDeltaDialog(QDialog):
    def __init__(self, document, definition, stats, selected, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Apply log delta — ' + definition.title)
        self.resize(900, 540)
        self.updates = {}
        layout = QVBoxLayout(self)
        description = QLabel('New BIN value = current BIN value + log delta × apply percentage.\n'
            'N75 delta is in percentage points (50 + 10 = 60). It includes controller corrections; '
            'the log does not prove which base table was active.\n'
            'Uses the marked log time range, or the whole log if none is marked. '
            'Applies to this table only, regardless of Linked edits. Undo restores the change.')
        description.setWordWrap(True)
        layout.addWidget(description)
        row = QHBoxLayout()
        self.scope = QComboBox()
        self.scope.addItems(['Selected cells', 'All cells with log data'])
        self.percent = QSpinBox(); self.percent.setRange(0, 100); self.percent.setValue(100)
        self.percent.setSuffix('%')
        self.minimum = QSpinBox(); self.minimum.setRange(1, 10000000); self.minimum.setValue(1)
        for widget in (QLabel('Scope:'), self.scope, QLabel('Apply:'), self.percent,
                       QLabel('Minimum sample hits:'), self.minimum):
            row.addWidget(widget)
        layout.addLayout(row)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(['Cell (row, col)', 'Current', 'Log Δ (pp)', 'Hits', 'New BIN', 'Applied Δ (pp)', 'Result'])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        layout.addWidget(self.table)
        self.summary = QLabel(); layout.addWidget(self.summary)
        buttons = QHBoxLayout(); buttons.addStretch()
        cancel = QPushButton('Cancel'); cancel.clicked.connect(self.reject)
        self.apply = QPushButton('Apply to open BIN'); self.apply.clicked.connect(self.accept)
        buttons.addWidget(cancel); buttons.addWidget(self.apply); layout.addLayout(buttons)

        def refresh():
            cells = selected if self.scope.currentIndex() == 0 else [
                (r, c) for r in range(definition.rows) for c in range(definition.cols)
                if math.isfinite(float(stats['log_diff'][r, c]))]
            self.updates, preview = plan_delta(document, definition, stats, cells,
                                               self.percent.value(), self.minimum.value())
            self.table.setRowCount(len(preview))
            for i, (r, c, old, delta, hits, target, reason) in enumerate(preview):
                values = [f'{r+1}, {c+1}', f'{old:.3f}', f'{delta:+.3f}' if math.isfinite(delta) else '—',
                    f'{hits:.0f}', f'{target:.3f}' if reason in ('Apply', 'Unchanged') else '—',
                    f'{target-old:+.3f}' if reason == 'Apply' else '—', reason]
                for j, value in enumerate(values):
                    self.table.setItem(i, j, QTableWidgetItem(value))
            self.summary.setText(f'{len(self.updates)} cells will change; {len(preview)-len(self.updates)} skipped or unchanged. Values shown include BIN rounding.')
            self.apply.setEnabled(bool(self.updates))
        self.scope.currentIndexChanged.connect(refresh)
        self.percent.valueChanged.connect(refresh)
        self.minimum.valueChanged.connect(refresh)
        refresh()
