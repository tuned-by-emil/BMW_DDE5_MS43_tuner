import math
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QSpinBox


class FloatingGauge(QDialog):
    def __init__(self, name, parent=None):
        super().__init__(parent, Qt.Tool)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setWindowTitle(name)
        self.resize(330, 180)
        layout = QVBoxLayout(self)
        title = QLabel(name); title.setWordWrap(True)
        layout.addWidget(title)
        self.value_label = QLabel('—'); self.value_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.value_label, 1)
        self.detail_label = QLabel(''); self.detail_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.detail_label)
        row = QHBoxLayout(); row.addWidget(QLabel('Font size:'))
        self.font_size = QSpinBox(); self.font_size.setRange(18, 96); self.font_size.setValue(36)
        self.font_size.valueChanged.connect(self._font_changed)
        row.addWidget(self.font_size); row.addStretch()
        layout.addLayout(row)
        self._font_changed(36)

    def _font_changed(self, size):
        font = QFont(self.font()); font.setPointSize(size); font.setBold(True)
        self.value_label.setFont(font)

    def update_value(self, value, unit, age):
        self.value_label.setText(f'{value:.2f}' if math.isfinite(value) else '—')
        self.detail_label.setText((unit + ' ' + age).strip())
