"""Guided, explicit bench checks of the DDE5 RAM calibration manager."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import threading

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog,
                              QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                              QMessageBox, QPlainTextEdit, QPushButton,
                              QVBoxLayout)

from .dde5_ram_calibration import build_ram_plan, run_ram_bench, validate_hardware
from .serial_transport import validate_port
from . import BUILD_CHANNEL


def _unexpected_report(message):
    return {
        'status': 'failed', 'error': message, 'steps': [], 'frames': [],
        'details': {'unexpected_exception': True},
        'restore': {'reference_page': 'unknown', 'write': 'unknown', 'session': 'unknown',
                    'power_cycle_required': True, 'restoration_required': True},
    }


class RamBenchThread(QThread):
    completed = Signal(object)
    progress = Signal(str)

    def __init__(self, port, *, plan, hardware, echo=True, probe_change=False, parent=None):
        super().__init__(parent)
        self.port = port
        self.plan = deepcopy(plan)
        self.hardware = deepcopy(hardware)
        self.echo = echo
        self.probe_change = probe_change
        self.cancel_event = threading.Event()

    def request_cancel(self):
        self.cancel_event.set()

    def run(self):
        try:
            report = run_ram_bench(
                self.port, plan=self.plan, hardware=self.hardware, echo=self.echo,
                probe_change=self.probe_change, cancelled=self.cancel_event.is_set,
                progress=self.progress.emit)
            if not isinstance(report, dict):
                raise TypeError('The bench check returned an invalid report.')
        except Exception as exc:
            report = _unexpected_report(
                f'Unexpected bench error ({type(exc).__name__}). The checks did not complete.')
            report['details']['exception_message'] = str(exc)
        self.completed.emit(report)


class Dde5RamCalibrationDialog(QDialog):
    """Plan from frozen editor snapshots; never open a COM port on construction."""

    def __init__(self, original, definition, edited=None, image_name='', parent=None):
        super().__init__(parent)
        self.original = bytes(original)
        self.edited = bytes(edited) if edited is not None else self.original
        self.definition = deepcopy(definition)
        self.image_name = str(image_name)
        self.plan = None
        self.plan_error = None
        self.local_diff_count = None
        self.report = None
        self.worker = None
        self._busy = False
        self._pending_report = None
        self._active_plan = None
        self._active_context = None
        suffix = ' — local test build' if BUILD_CHANNEL == 'patch-test' else ''
        self.setWindowTitle('DDE5 RAM calibration bench' + suffix)
        self.resize(850, 810)
        layout = QVBoxLayout(self)
        self._note(layout,
                   'Engine-off bench checks for the 361830 RAM calibration manager. '
                   'The plan uses the originally opened BIN. Current editor changes are excluded. '
                   'Opening this window only builds an offline plan; it does not connect to the ECU.')
        self.plan_summary = QLabel()
        self.plan_summary.setWordWrap(True)
        layout.addWidget(self.plan_summary)
        form = QFormLayout()
        self.port = QLineEdit()
        self.port.setPlaceholderText('For example COM3')
        form.addRow('K-line cable port', self.port)
        self.echo = QCheckBox('Cable returns transmitted bytes (echo)')
        self.echo.setChecked(True)
        form.addRow('', self.echo)
        self.capacity = QComboBox()
        self.capacity.addItem('32 KiB (32768 bytes)', 32768)
        self.capacity.addItem('64 KiB (65536 bytes)', 65536)
        form.addRow('Verified usable SRAM capacity', self.capacity)
        self.hardware_reference = QLineEdit()
        self.hardware_reference.setPlaceholderText('Wiring / bank configuration / usable capacity evidence')
        form.addRow('Hardware evidence reference', self.hardware_reference)
        layout.addLayout(form)
        self.hardware_confirmed = QCheckBox('I verified the SRAM wiring, bank configuration and usable capacity.')
        layout.addWidget(self.hardware_confirmed)
        self._note(layout,
                   'This hardware declaration is yours; the application cannot certify the wiring, '
                   'bank configuration or usable capacity. Chip capacity alone is insufficient. '
                   'The bench check inspects the ECU manager before copying any block.')
        self.engine_ack = QCheckBox('The engine is stopped and no RAM working page is in use.')
        layout.addWidget(self.engine_ack)
        self.exclusive_ack = QCheckBox('No other CCP, diagnostic or calibration client is using this ECU.')
        layout.addWidget(self.exclusive_ack)
        self.probe_change = QCheckBox('Test one changed byte, verify it, then restore it')
        self.probe_change.setChecked(False)
        layout.addWidget(self.probe_change)
        self._note(layout,
                   'The optional change flips one bit in the first byte of the table’s first cell, '
                   'checks the cell through the mapped readback route, then restores and checks it. '
                   'It does not upload your editor changes. Bulk changed-value upload is unavailable.')
        self.prerequisites = QLabel()
        self.prerequisites.setWordWrap(True)
        layout.addWidget(self.prerequisites)
        self.run_button = QPushButton('Run unchanged-copy / page checks…')
        layout.addWidget(self.run_button)
        self.status = QLabel('No bench check has been run.')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        layout.addWidget(self.output, 1)
        buttons = QHBoxLayout()
        self.cancel_button = QPushButton('Cancel bench check')
        self.save_button = QPushButton('Save JSON report…')
        self.close_button = QPushButton('Close')
        for button in (self.cancel_button, self.save_button, self.close_button):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        for button in (self.run_button, self.cancel_button, self.save_button, self.close_button):
            button.setAutoDefault(False)
        self.port.textChanged.connect(self._declaration_changed)
        self.hardware_reference.textChanged.connect(self._declaration_changed)
        self.capacity.currentIndexChanged.connect(self._capacity_changed)
        self.hardware_confirmed.toggled.connect(self._refresh_controls)
        self.engine_ack.toggled.connect(self._refresh_controls)
        self.exclusive_ack.toggled.connect(self._refresh_controls)
        self.probe_change.toggled.connect(self._refresh_controls)
        self.run_button.clicked.connect(self.start_bench)
        self.cancel_button.clicked.connect(self.cancel_bench)
        self.save_button.clicked.connect(self.save_report)
        self.close_button.clicked.connect(self.reject)
        self._build_plan()

    @staticmethod
    def _note(layout, text):
        label = QLabel(text)
        label.setWordWrap(True)
        layout.addWidget(label)

    def _capacity_changed(self, *_):
        self.hardware_confirmed.setChecked(False)
        self.probe_change.setChecked(False)
        self._build_plan()

    def _declaration_changed(self, *_):
        self.hardware_confirmed.setChecked(False)
        self.probe_change.setChecked(False)
        self._refresh_controls()

    def _build_plan(self):
        try:
            if len(self.original) != len(self.edited):
                raise ValueError('The original and current editor snapshots have different lengths.')
            self.plan = build_ram_plan(self.original, self.definition,
                                       capacity_bytes=self.capacity.currentData())
            if self.plan['required_bytes'] > self.plan['capacity_bytes']:
                raise ValueError('The complete directory and block allocation exceeds the declared SRAM capacity.')
            self.plan_error = None
            table = self.plan['table']
            file_base = table['logical_address'] - table['file_offset']
            spans = [(block['logical_start'] - file_base,
                      block['logical_end_exclusive'] - file_base) for block in self.plan['blocks']]
            if any(start < 0 or end > len(self.original) or end <= start for start, end in spans):
                raise ValueError('The RAM plan has a block outside the loaded BIN.')
            self.local_diff_count = sum(
                a != b for start, end in spans
                for a, b in zip(self.original[start:end], self.edited[start:end]))
            ranges = ', '.join(f'0x{block["logical_start"]:06X}–'
                               f'0x{block["logical_end_exclusive"] - 1:06X} '
                               f'({block["bytes"]:,} bytes)' for block in self.plan['blocks'])
            self.plan_summary.setText(
                f'BIN: {Path(self.image_name).name or "Originally opened BIN"}\n'
                f'Table: {table["title"]} — {table["bytes"]:,} bytes at '
                f'0x{table["logical_address"]:06X}\n'
                f'Complete managed block(s): {ranges}\n'
                f'RAM budget: {self.plan["directory_bytes"]:,} directory bytes; '
                f'{self.plan["required_bytes"]:,} total required / '
                f'{self.plan["capacity_bytes"]:,} declared available.\n'
                f'Current editor differences in these blocks: {self.local_diff_count:,} bytes; '
                'excluded from the bench copy.')
        except Exception as exc:
            self.plan = None
            self.plan_error = str(exc)
            self.local_diff_count = None
            self.plan_summary.setText(f'Offline plan unavailable: {exc}')
        self._refresh_controls()

    def _hardware(self):
        return validate_hardware({
            'capacity_bytes': self.capacity.currentData(),
            'reference': self.hardware_reference.text(),
            'confirmed': self.hardware_confirmed.isChecked(),
        })

    def _refresh_controls(self, *_):
        error = self.plan_error
        if error is None:
            try:
                validate_port(self.port.text())
                self._hardware()
                if not self.engine_ack.isChecked():
                    raise ValueError('Confirm that the engine is stopped and no RAM working page is in use.')
                if not self.exclusive_ack.isChecked():
                    raise ValueError('Confirm that no other CCP, diagnostic or calibration client is using this ECU.')
            except ValueError as exc:
                error = str(exc)
        self.prerequisites.setText(error or 'Ready for an explicit bench-check confirmation.')
        for control in (self.port, self.echo, self.capacity, self.hardware_reference,
                        self.hardware_confirmed, self.engine_ack, self.exclusive_ack, self.probe_change):
            control.setEnabled(not self._busy)
        self.run_button.setText('Run copy / page / changed-byte checks…' if self.probe_change.isChecked()
                                else 'Run unchanged-copy / page checks…')
        self.run_button.setEnabled(not self._busy and self.plan is not None and error is None)
        self.cancel_button.setEnabled(self._busy and self.worker is not None
                                      and not self.worker.cancel_event.is_set())
        self.save_button.setEnabled(not self._busy and self.report is not None)

    def _context(self):
        return {'image_name': self.image_name,
                'baseline_source': 'originally_opened_bin',
                'original_sha256': hashlib.sha256(self.original).hexdigest(),
                'editor_sha256': hashlib.sha256(self.edited).hexdigest(),
                'editor_changed_bytes_in_blocks': self.local_diff_count,
                'editor_changes_uploaded': False,
                'engine_stopped_acknowledged': self.engine_ack.isChecked(),
                'exclusive_client_acknowledged': self.exclusive_ack.isChecked()}

    def start_bench(self):
        if self._busy:
            return
        try:
            if self.plan is None:
                raise ValueError(self.plan_error or 'A valid offline plan is required.')
            port = validate_port(self.port.text())
            hardware = self._hardware()
            if not self.engine_ack.isChecked():
                raise ValueError('Confirm that the engine is stopped and no RAM working page is in use.')
            if not self.exclusive_ack.isChecked():
                raise ValueError('Confirm that no other CCP, diagnostic or calibration client is using this ECU.')
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        blocks = '\n'.join(f'0x{block["logical_start"]:06X}–'
                           f'0x{block["logical_end_exclusive"] - 1:06X} '
                           f'({block["bytes"]:,} bytes)' for block in self.plan['blocks'])
        change = self.probe_change.isChecked()
        change_text = ''
        if change:
            address = self.plan['probe_address']
            change_text = (f'\n\nAlso change the byte at 0x{address:06X} with XOR 0x01, '
                           f'verify the table’s first {self.plan["probe_bytes"]}-byte cell, '
                           'then restore the original byte and verify the cell again.')
        answer = QMessageBox.question(
            self, 'Confirm RAM calibration bench checks',
            f'With the engine stopped, check {port} at 9600 baud and copy these complete, '
            f'unchanged baseline blocks through the ECU RAM calibration manager:\n{blocks}\n\n'
            'Inspect the manager, exercise the RAM/reference selection, then restore and '
            'verify the reference page.' + change_text + '\n\n'
            f'Declared usable SRAM with verified wiring/bank configuration: {hardware["capacity_bytes"]:,} bytes.\n'
            f'Hardware evidence: {hardware["reference"]}\n'
            f'Current editor changes excluded: {self.local_diff_count:,} bytes.\n\n'
            'A lost connection may prevent restoration. Start these bench checks?',
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        self.report = None
        self._pending_report = None
        self._active_plan = deepcopy(self.plan)
        self._active_context = self._context()
        self._busy = True
        self.output.clear()
        self.status.setText(f'Running engine-off RAM calibration bench checks on {port}…')
        worker = RamBenchThread(port, plan=self._active_plan, hardware=hardware,
                                echo=self.echo.isChecked(), probe_change=change, parent=self)
        self.worker = worker
        worker.progress.connect(self.output.appendPlainText)
        worker.completed.connect(self._receive_report)
        worker.finished.connect(self._finished)
        self._refresh_controls()
        worker.start()

    def _receive_report(self, report):
        self._pending_report = report

    def _finished(self):
        worker = self.worker
        if worker is None:
            return
        self.report = self._pending_report or _unexpected_report('The bench checks ended without a report.')
        self.report.setdefault('plan', self._active_plan)
        self.report['gui_context'] = self._active_context
        self._pending_report = self._active_plan = self._active_context = None
        self._busy = False
        self.worker = None
        worker.deleteLater()
        self.hardware_confirmed.setChecked(False)
        self.engine_ack.setChecked(False)
        self.exclusive_ack.setChecked(False)
        self.probe_change.setChecked(False)
        self._show_report()
        self._refresh_controls()

    def _show_report(self):
        report = self.report
        summary = {'passed': 'Bench checks passed.', 'failed': 'Bench checks failed.',
                   'cancelled': 'Bench checks cancelled.'}.get(report.get('status'),
                                                              'Bench checks did not complete.')
        if report.get('error'):
            summary += ' ' + str(report['error'])
        restore = report.get('restore') or {}
        if restore.get('reference_page') == 'verified':
            summary += ' The reference page is restored and verified.'
        elif restore.get('reference_page') in ('required', 'unknown'):
            summary += ' Reference-page restoration is unconfirmed.'
        if restore.get('write') == 'verified':
            summary += ' The changed byte is restored and verified.'
        elif restore.get('write') in ('required', 'unknown'):
            summary += ' Original-byte restoration is unconfirmed.'
        if restore.get('restoration_required'):
            summary += ' Restoration still requires attention; save the report.'
        if restore.get('power_cycle_required'):
            summary += ' A power cycle is required before normal use.'
        if restore.get('session') == '86_left_active':
            summary += ' Diagnostic session 86 remains active; RAM allocations remain.'
        elif restore.get('session') == 'unknown':
            summary += ' The ECU diagnostic session is unconfirmed.'
        details = report.get('details') or {}
        if details.get('port_closed') is False:
            summary += ' COM port cleanup is unconfirmed.'
        if details.get('unexpected_exception'):
            summary += ' Port/session cleanup is unconfirmed.'
        self.status.setText(summary)
        self.output.appendPlainText(summary)
        self.output.appendPlainText('Exact verification evidence and restoration state are in the JSON report.')

    def cancel_bench(self):
        if self.worker is not None and self._busy:
            self.worker.request_cancel()
            self.status.setText('Cancellation requested. Waiting for any required restoration and port cleanup…')
            self._refresh_controls()

    def save_report(self):
        if self._busy or self.report is None:
            return
        name, _ = QFileDialog.getSaveFileName(self, 'Save RAM calibration bench report',
                                             'dde5-ram-calibration-bench.json', 'JSON (*.json)')
        if not name:
            return
        try:
            Path(name).write_text(json.dumps(self.report, indent=2, ensure_ascii=False) + '\n',
                                  encoding='utf-8')
        except (OSError, TypeError, ValueError) as exc:
            QMessageBox.critical(self, 'Save report', f'Could not save the report:\n{exc}')

    def _can_close(self):
        if self._busy:
            self.cancel_bench()
            self.status.setText('Cancellation requested. Keep this window open until restoration '
                                'and port cleanup finish, then close it.')
            return False
        return True

    def accept(self):
        if self._can_close():
            super().accept()

    def reject(self):
        if self._can_close():
            super().reject()

    def closeEvent(self, event):
        if self._can_close():
            super().closeEvent(event)
        else:
            event.ignore()
