"""Read-only review dialog; caller applies only after explicit acceptance."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QLabel, QTreeWidget,
                              QTreeWidgetItem, QDialogButtonBox, QHeaderView)
from .ms43_drift import SOURCE


class DriftPreviewDialog(QDialog):
    def __init__(self, preview, parent=None):
        super().__init__(parent)
        self.setWindowTitle('MS43X drift throttle response — review changes')
        self.resize(1120, 720)
        layout = QVBoxLayout(self)
        changed = preview.changed
        count = len({c.symbol for c in changed})
        label = QLabel(f'{count} settings / {len(changed)} cells would change. '
                       'Values below are engineering values; bytes are shown exactly as stored. '
                       'Apply edits the open BIN in memory as one Undo operation. Save/export is separate.')
        label.setWordWrap(True)
        layout.addWidget(label)
        note = QLabel('Best effort MS43X001 preset based on base-MS43 documentation and the supplied XDF. '
                      'Dashpot cancellation is inferred from the documented algorithm; vehicle behavior is untested. '
                      'Scope: pedal increase, pedal-release dashpot, gear-based closing correction and anti-jerk ignition correction. '
                      'Other throttle rate limits remain independently calibrated. '
                      f'<a href="{SOURCE}">Read MS4X source</a>')
        note.setWordWrap(True)
        note.setOpenExternalLinks(True)
        layout.addWidget(note)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(['Setting / cell (1-based)', 'File offset', 'Current', 'Proposed', 'Bytes: current → proposed'])
        self.tree.setSelectionMode(QTreeWidget.ExtendedSelection)
        self.tree.setTextElideMode(Qt.ElideNone)
        groups = {}
        for c in preview.cells:
            if c.symbol not in groups:
                group = QTreeWidgetItem([c.name + '\n' + c.symbol])
                group.setToolTip(0, c.reason + '\nSource: ' + SOURCE)
                self.tree.addTopLevelItem(group)
                reason = QTreeWidgetItem(['Why: ' + c.reason])
                reason.setToolTip(0, c.reason)
                group.addChild(reason)
                reason.setFirstColumnSpanned(True)
                groups[c.symbol] = group
            suffix = '' if c.before != c.after else ' (already set)'
            item = QTreeWidgetItem([f'Row {c.row+1}, column {c.col+1}' + suffix,
                                   f'0x{c.address:06X}', f'{c.old_value:.10g} {c.units}',
                                   f'{c.new_value:.10g} {c.units}',
                                   c.before.hex(' ').upper() + ' → ' + c.after.hex(' ').upper()])
            groups[c.symbol].addChild(item)
        self.tree.expandAll()
        self.tree.header().setSectionResizeMode(QHeaderView.Interactive)
        for i, width in enumerate([460, 95, 140, 120, 190]):
            self.tree.setColumnWidth(i, width)
        layout.addWidget(self.tree)
        buttons = QDialogButtonBox(QDialogButtonBox.Apply | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Apply).setText('Apply previewed changes')
        buttons.button(QDialogButtonBox.Apply).setEnabled(bool(changed))
        buttons.button(QDialogButtonBox.Apply).clicked.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.Cancel).setDefault(True)
        layout.addWidget(buttons)
