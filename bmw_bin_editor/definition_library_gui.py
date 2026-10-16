"""Review a software-specific definition proposal before replacing the XDF."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QHBoxLayout, QHeaderView,
                              QLabel, QPlainTextEdit, QPushButton, QTreeWidget,
                              QTreeWidgetItem, QVBoxLayout)

from .bin_analysis import bin_metadata
from .definition_library import software_identifiers
from .xdf_gui import fit_dialog


class DefinitionProposalDialog(QDialog):
    def __init__(self, data, name, matches, current=None, current_compatible=False, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Choose matching XDF')
        fit_dialog(self, 960, 560)
        self.matches = list(matches)
        self.selected_match = None
        self.choice = None
        layout = QVBoxLayout(self)
        title = QLabel(f'Definition proposals for {name}')
        title.setWordWrap(True)
        layout.addWidget(title)
        metadata = bin_metadata(data)
        identity = ', '.join(software_identifiers(data)) or 'not found'
        label = QLabel(f"{len(data):,} bytes · Software numbers: {identity} · SHA-256 {metadata['sha256'][:16]}")
        label.setWordWrap(True); label.setToolTip(metadata['sha256'])
        layout.addWidget(label)
        note = QLabel('Review the software and layout evidence before selecting a definition. '
                      'Generated XDFs contain the supported, validated entries from their source packs.')
        note.setWordWrap(True); layout.addWidget(note)
        self.listing = QTreeWidget()
        self.listing.setRootIsDecorated(False)
        self.listing.setSelectionMode(QAbstractItemView.SingleSelection)
        self.listing.setHeaderLabels(['XDF definition', 'Match', 'Evidence rank'])
        self.listing.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.listing.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.listing.header().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        for index, match in enumerate(self.matches):
            item = QTreeWidgetItem([match.entry.title, str(match.confidence), str(match.score)])
            item.setData(0, Qt.UserRole, index)
            item.setToolTip(0, match.entry.title + '\n' + str(match.path))
            self.listing.addTopLevelItem(item)
        layout.addWidget(self.listing, 1)
        self.evidence = QPlainTextEdit(); self.evidence.setReadOnly(True)
        self.evidence.setMaximumHeight(180)
        layout.addWidget(self.evidence)
        current_label = QLabel('Current XDF: ' + (current.title if current else 'none'))
        current_label.setWordWrap(True); layout.addWidget(current_label)
        buttons = QHBoxLayout()
        self.use_button = QPushButton('Use selected XDF')
        self.use_button.setDefault(True)
        self.use_button.clicked.connect(lambda: self.choose('library'))
        self.keep_button = QPushButton('Keep current XDF')
        self.keep_button.setEnabled(bool(current is not None and current_compatible))
        if not self.keep_button.isEnabled():
            self.keep_button.setToolTip('The current definition is absent or incompatible with this BIN.')
        self.keep_button.clicked.connect(lambda: self.choose('current'))
        self.empty_button = QPushButton('Open without definitions')
        self.empty_button.clicked.connect(lambda: self.choose('empty'))
        cancel = QPushButton('Cancel'); cancel.clicked.connect(self.reject)
        for button in (self.use_button, self.keep_button, self.empty_button, cancel):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.listing.currentItemChanged.connect(self.select_match)
        self.listing.itemDoubleClicked.connect(lambda *_: self.choose('library'))
        if self.listing.topLevelItemCount():
            self.listing.setCurrentItem(self.listing.topLevelItem(0))
        else:
            self.select_match(None)

    def select_match(self, item, *_):
        self.selected_match = self.matches[item.data(0, Qt.UserRole)] if item else None
        self.use_button.setEnabled(bool(self.selected_match and self.selected_match.usable))
        if self.selected_match:
            match = self.selected_match
            self.evidence.setPlainText(match.entry.title + '\n\n' + '\n'.join(match.reasons) + f'\n\nXDF: {match.path}')
        else:
            self.evidence.setPlainText('No validated definition matches this BIN.')

    def choose(self, choice):
        if choice == 'library' and not self.use_button.isEnabled():
            return
        if choice == 'current' and not self.keep_button.isEnabled():
            return
        self.choice = choice
        self.accept()
