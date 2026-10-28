"""Explicit, opt-in checks for the local DDE5 MPC patch test build."""
from __future__ import annotations

import json
from pathlib import Path
import threading

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog,
                              QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                              QMessageBox, QPlainTextEdit, QPushButton,
                              QTabWidget, QVBoxLayout, QWidget)

from .dde5_patch_test import run_patch_check, validate_reservation
from .serial_transport import validate_port
from . import BUILD_CHANNEL


class PatchCheckThread(QThread):
    completed = Signal(object)
    progress = Signal(str)

    def __init__(self, port, mode, *, rate=115200, echo=True, reservation=None,
                 parent=None):
        super().__init__(parent)
        self.port = port
        self.mode = mode
        self.rate = rate
        self.echo = echo
        self.reservation = dict(reservation) if reservation is not None else None
        self.cancel_event = threading.Event()

    def request_cancel(self):
        self.cancel_event.set()

    def run(self):
        try:
            report = run_patch_check(
                self.port, mode=self.mode, rate=self.rate, echo=self.echo,
                reservation=self.reservation, cancelled=self.cancel_event.is_set,
                progress=self.progress.emit)
            if not isinstance(report, dict):
                raise TypeError('The check returned an invalid report.')
        except Exception as exc:
            # Keep an exportable failure even if an unexpected backend exception
            # bypasses its ordinary cleanup/reporting path.
            report = {
                'status': 'failed', 'mode': self.mode, 'port': self.port,
                'error': f'Unexpected check error ({type(exc).__name__}). The check did not complete.',
                'steps': [], 'frames': [],
                'restore': {'status': 'unknown', 'verified': False},
                'details': {'unexpected_exception': True,
                            'exception_message': str(exc),
                            'reservation': self.reservation},
            }
        self.completed.emit(report)


class Dde5PatchTestDialog(QDialog):
    """Opening this dialog never opens a port or sends an ECU request."""

    def __init__(self, parent=None):
        super().__init__(parent)
        suffix = ' — local test build' if BUILD_CHANNEL == 'patch-test' else ''
        self.setWindowTitle('DDE5 patch checks' + suffix)
        self.resize(820, 730)
        self.worker = None
        self.report = None
        self._pending_report = None
        self._busy = False
        layout = QVBoxLayout(self)
        heading = QLabel(
            ('Local test build for the ' if BUILD_CHANNEL == 'patch-test' else 'Checks for the ')
            + '361830 DDE5 MPC read / baud / restricted RAM-write patch. '
            'Choose and start each check explicitly. Opening this window does not connect to the ECU.')
        heading.setWordWrap(True)
        layout.addWidget(heading)
        connection = QFormLayout()
        self.port = QLineEdit()
        self.port.setPlaceholderText('For example COM3')
        connection.addRow('K-line cable port', self.port)
        self.echo = QCheckBox('Cable returns transmitted bytes (echo)')
        self.echo.setChecked(True)
        connection.addRow('', self.echo)
        layout.addLayout(connection)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        read_tab, read_layout = self._tab('Read checks')
        self._note(read_layout,
                   'Read ECU identity, verify the live MPC patch words and inspect internal state. '
                   'This check sends no memory-write command and does not test a higher baud rate.')
        self.read_button = QPushButton('Run read checks')
        read_layout.addWidget(self.read_button)
        read_layout.addStretch()

        baud_tab, baud_layout = self._tab('Baud round trip')
        self._note(baud_layout,
                   'Check the patch, enter the higher-rate session, verify communication, then '
                   'return the cable and ECU to 9600 baud. The session can reset the RAM calibration '
                   'directory and allocation state. This check sends no memory-write test command.')
        baud_form = QFormLayout()
        self.rate = QComboBox()
        self.rate.addItem('115200 baud', 115200)
        self.rate.addItem('125000 baud', 125000)
        baud_form.addRow('Test rate', self.rate)
        baud_layout.addLayout(baud_form)
        self.baud_ack = QCheckBox('The engine is stopped and no RAM working page is in use.')
        baud_layout.addWidget(self.baud_ack)
        self.baud_button = QPushButton('Run baud round trip')
        baud_layout.addWidget(self.baud_button)
        baud_layout.addStretch()

        write_tab, write_layout = self._tab('Optional reserved RAM test')
        self._note(write_layout,
                   'This optional check changes exactly four bytes, reads the test pattern, then '
                   'restores and verifies the original bytes. The MPC patch reserves no buffer. '
                   'You must independently establish that these exact four bytes are reserved '
                   'exclusively for this test in the installed firmware.')
        write_form = QFormLayout()
        self.address = QLineEdit()
        self.address.setPlaceholderText('Verified address in hexadecimal — no default')
        write_form.addRow('Reserved 4-byte address', self.address)
        self.reference = QLineEdit()
        self.reference.setPlaceholderText('Firmware / reservation evidence identifying these four bytes')
        write_form.addRow('Reservation reference', self.reference)
        write_layout.addLayout(write_form)
        self._note(write_layout,
                   'Allowed addresses are four-byte aligned within 0x3F9FD4–0x3FA177. '
                   'Being inside this calibration-manager pool does not establish ownership.')
        self.reservation_confirmed = QCheckBox(
            'I verified the exclusive 4-byte reservation for this firmware.')
        write_layout.addWidget(self.reservation_confirmed)
        self.write_ack = QCheckBox('The engine is stopped and no RAM working page is in use.')
        write_layout.addWidget(self.write_ack)
        self.reservation_status = QLabel()
        self.reservation_status.setWordWrap(True)
        write_layout.addWidget(self.reservation_status)
        self.write_button = QPushButton('Test reserved 4-byte RAM buffer…')
        write_layout.addWidget(self.write_button)
        write_layout.addStretch()

        self.status = QLabel('No check has been run. Choose a COM port to enable read checks.')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        layout.addWidget(self.output, 1)
        buttons = QHBoxLayout()
        self.cancel_button = QPushButton('Cancel check')
        self.save_button = QPushButton('Save JSON report…')
        self.close_button = QPushButton('Close')
        for button in (self.cancel_button, self.save_button, self.close_button):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        for button in (self.read_button, self.baud_button, self.write_button,
                       self.cancel_button, self.save_button, self.close_button):
            button.setAutoDefault(False)
        self.read_button.clicked.connect(lambda: self.start_check('read'))
        self.baud_button.clicked.connect(lambda: self.start_check('baud'))
        self.write_button.clicked.connect(lambda: self.start_check('write'))
        self.cancel_button.clicked.connect(self.cancel_check)
        self.save_button.clicked.connect(self.save_report)
        self.close_button.clicked.connect(self.reject)
        for field in (self.port, self.address, self.reference):
            field.textChanged.connect(self._selection_changed)
        for checkbox in (self.baud_ack, self.write_ack, self.reservation_confirmed):
            checkbox.toggled.connect(self._refresh_controls)
        self._refresh_controls()

    def _tab(self, title):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        self.tabs.addTab(widget, title)
        return widget, layout

    @staticmethod
    def _note(layout, text):
        label = QLabel(text)
        label.setWordWrap(True)
        layout.addWidget(label)

    def _selection_changed(self):
        self.reservation_confirmed.setChecked(False)
        if not self._busy and self.report is None:
            try:
                validate_port(self.port.text())
                self.status.setText('No check has been run. Choose a check and start it explicitly.')
            except ValueError:
                self.status.setText('No check has been run. Choose a COM port to enable read checks.')
        self._refresh_controls()

    def _reservation(self):
        return validate_reservation({
            'address': self.address.text().strip(),
            'reference': self.reference.text().strip(),
            'confirmed': self.reservation_confirmed.isChecked(),
        })

    def _refresh_controls(self, *_):
        try:
            validate_port(self.port.text())
            valid_port = True
        except ValueError:
            valid_port = False
        try:
            reservation = self._reservation()
            reservation_valid = True
            hint = (f'Reservation recorded for 0x{reservation["address"]:06X}–'
                    f'0x{reservation["address"] + 3:06X}. Ownership is verified by you.')
        except ValueError as exc:
            reservation_valid = False
            hint = str(exc)
        self.reservation_status.setText(hint)
        for control in (self.port, self.echo, self.rate, self.address, self.reference,
                        self.baud_ack, self.write_ack, self.reservation_confirmed):
            control.setEnabled(not self._busy)
        self.read_button.setEnabled(not self._busy and valid_port)
        self.baud_button.setEnabled(not self._busy and valid_port and self.baud_ack.isChecked())
        self.write_button.setEnabled(not self._busy and valid_port and reservation_valid
                                     and self.write_ack.isChecked())
        self.cancel_button.setEnabled(self._busy and self.worker is not None
                                      and not self.worker.cancel_event.is_set())
        self.save_button.setEnabled(not self._busy and self.report is not None)

    def start_check(self, mode):
        if self._busy:
            return
        if mode not in ('read', 'baud', 'write'):
            return
        try:
            port = validate_port(self.port.text())
            reservation = None
            if mode == 'baud' and not self.baud_ack.isChecked():
                raise ValueError('Confirm that the engine is stopped and no RAM working page is in use.')
            if mode == 'write':
                reservation = self._reservation()
                if not self.write_ack.isChecked():
                    raise ValueError('Confirm that the engine is stopped and no RAM working page is in use.')
                address = reservation['address']
                answer = QMessageBox.question(
                    self, 'Confirm reserved RAM test',
                    f'Write a test pattern to exactly 4 bytes at 0x{address:06X}–'
                    f'0x{address + 3:06X} on {port}, then restore and verify the original bytes?\n\n'
                    f'Reservation: {reservation["reference"]}\n\n'
                    'The application cannot verify exclusive ownership. A lost connection may '
                    'prevent restoration. Proceed only with your verified reservation.',
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
                if answer != QMessageBox.Yes:
                    return
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        self.report = None
        self._pending_report = None
        self._busy = True
        self.output.clear()
        self.status.setText(f'Running {mode} check on {port}…')
        worker = PatchCheckThread(port, mode, rate=self.rate.currentData(),
                                  echo=self.echo.isChecked(), reservation=reservation, parent=self)
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
        self.report = self._pending_report
        self._pending_report = None
        self._busy = False
        self.worker = None
        worker.deleteLater()
        self.reservation_confirmed.setChecked(False)
        self.baud_ack.setChecked(False)
        self.write_ack.setChecked(False)
        self._show_report()
        self._refresh_controls()

    def _show_report(self):
        if self.report is None:
            self.status.setText('The check ended without a report. Restoration is unconfirmed.')
            return
        status = self.report.get('status', 'failed')
        summary = {'passed': 'Check passed.', 'failed': 'Check failed.',
                   'cancelled': 'Check cancelled.'}.get(status, 'Check did not complete.')
        error = self.report.get('error')
        if error:
            summary += ' ' + str(error)
        restore = self.report.get('restore') or {}
        # Preserve all exact restoration evidence in the JSON; surface uncertainty
        # prominently without guessing that a passed check restored every resource.
        if restore:
            self.output.appendPlainText('Restoration details are included in the JSON report.')
        if restore.get('write') == 'unknown' or restore.get('restoration_required'):
            summary += ' Original RAM restoration is unconfirmed. Save the report before continuing.'
        if restore.get('baud') == 'unknown':
            summary += ' ECU baud restoration is unconfirmed.'
        if restore.get('power_cycle_required'):
            summary += ' A power cycle is required before normal use.'
        if restore.get('session') == '86_left_active':
            summary += ' Diagnostic session 86 remains active.'
        elif restore.get('session') == 'unknown':
            summary += ' The ECU diagnostic session is unconfirmed.'
        details = self.report.get('details') or {}
        if details.get('port_closed') is False:
            summary += ' COM port cleanup is unconfirmed.'
        if details.get('unexpected_exception'):
            summary += ' Port/session cleanup and any restoration are unconfirmed; save the report.'
        self.status.setText(summary)
        self.output.appendPlainText(summary)

    def cancel_check(self):
        if self.worker is None or not self._busy:
            return
        self.worker.request_cancel()
        self.status.setText('Cancellation requested. Waiting for port cleanup and any required restoration…')
        self._refresh_controls()

    def save_report(self):
        if self._busy or self.report is None:
            return
        name, _ = QFileDialog.getSaveFileName(self, 'Save DDE5 patch-check report',
                                             'dde5-patch-check.json', 'JSON (*.json)')
        if not name:
            return
        try:
            Path(name).write_text(json.dumps(self.report, indent=2, ensure_ascii=False) + '\n',
                                  encoding='utf-8')
        except (OSError, TypeError, ValueError) as exc:
            QMessageBox.critical(self, 'Save report', f'Could not save the report:\n{exc}')

    def _can_close(self):
        if self._busy:
            self.cancel_check()
            self.status.setText('Cancellation requested. Keep this window open until port cleanup '
                                'and any required restoration finish, then close it.')
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
