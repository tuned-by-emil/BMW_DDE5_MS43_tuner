"""Edit the active acquisition selection without changing cable settings."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QVBoxLayout)
from .live_logging import ANCHORS, parameter_update_plan


class LiveChannelDialog(QDialog):
    def __init__(self, catalog, profile, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Live logging channels')
        self.resize(620, 520)
        self.catalog, self.profile = catalog, profile
        self.diesel = profile.get('ecu') == 'D50M47A'
        layout = QVBoxLayout(self)
        hint = QLabel('Choose the channels to collect. Changes apply after the current ECU reply. '
                      'Removing a bottom gauge only changes the display; use this list to stop collecting a channel.')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.search = QLineEdit()
        self.search.setPlaceholderText('Find a logging channel…')
        layout.addWidget(self.search)
        self.channels = QListWidget()
        selected = set(profile['parameters'])
        for arg, row in sorted(catalog.items(), key=lambda pair: pair[1].get('label', pair[0]).casefold()):
            label = row.get('label') or row['result']
            unit = row.get('unit', '')
            item = QListWidgetItem(f'{label}  [{unit}]' if unit else label, self.channels)
            item.setData(Qt.UserRole, arg)
            item.setToolTip(f"{arg}\n{row['result']}\n{row.get('description', '')}")
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if arg in selected else Qt.Unchecked)
            if self.diesel and arg in ANCHORS:
                item.setCheckState(Qt.Checked)
                item.setFlags(item.flags() & ~Qt.ItemIsEnabled)
        layout.addWidget(self.channels)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.search.textChanged.connect(self._filter)
        self.channels.itemChanged.connect(self._summarize)
        self._summarize()

    def selected(self):
        checked = {self.channels.item(i).data(Qt.UserRole) for i in range(self.channels.count())
                   if self.channels.item(i).checkState() == Qt.Checked}
        # Retain existing group order before appending new choices.
        return [a for a in self.profile['parameters'] if a in checked] + [a for a in self.catalog
                if a in checked and a not in self.profile['parameters']]

    def _filter(self, text):
        for i in range(self.channels.count()):
            item = self.channels.item(i)
            item.setHidden(text.casefold() not in (item.text() + ' ' + item.toolTip()).casefold())

    def _summarize(self):
        selected = self.selected()
        valid = bool(selected)
        text = f'{len(selected)} channels selected'
        if self.diesel:
            try:
                plan = parameter_update_plan(selected, self.catalog, self.profile['parameters'])
                text += f" · {plan['new_blocks']} realtime block(s), up to 10 values per block."
                if plan['adds_block']:
                    text += '\nAnother block significantly slows logging; you will be asked to confirm.'
            except ValueError as error:
                text = str(error)
                valid = False
        else:
            text += ' · one MS43X packet per sample.'
        self.summary.setText(text)
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(valid)
