"""Modeless DDE5 RAM controls; one qualified worker owns the connection."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import queue
import threading

from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout,
                              QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                              QPlainTextEdit, QPushButton, QVBoxLayout)

from .dde5_live_ram import LiveRamController
from .dde5_ram_calibration import validate_hardware
from .serial_transport import validate_port


def _failure(message):
    return {'status': 'failed', 'error': message, 'details': {'qualified': False},
            'steps': [], 'frames': [], 'restore': {'reference_page': 'unknown',
            'write': 'unknown', 'restoration_required': True, 'power_cycle_required': True}}


class LiveRamWorker(QThread):
    resultReady = Signal(str, object, bool)
    progress = Signal(str)

    def __init__(self, port, original, hardware, definition, *, echo=True, parent=None):
        super().__init__(parent)
        self.port, self.original = port, bytes(original)
        self.hardware, self.definition = deepcopy(hardware), deepcopy(definition)
        self.echo = echo
        self.cancel_event = threading.Event()
        self.commands = queue.Queue()
        self.cleanup_report = None

    def submit(self, action, **payload):
        self.commands.put((action, deepcopy(payload)))

    def request_disconnect(self, *, cancel=False):
        if cancel:
            self.cancel_event.set()
        self.commands.put(('disconnect', {}))

    def run(self):
        controller = None
        try:
            controller = LiveRamController(self.port, self.original, self.hardware,
                echo=self.echo, cancelled=self.cancel_event.is_set, progress=self.progress.emit)
            report = controller.qualify(self.definition)
            qualified = (controller.qualified is True and report.get('status') == 'passed'
                         and report.get('details', {}).get('qualified') is True)
            self.resultReady.emit('qualify', report, qualified)
            while qualified and not self.cancel_event.is_set():
                try:
                    action, payload = self.commands.get(timeout=.1)
                except queue.Empty:
                    continue
                if action == 'disconnect':
                    break
                if action not in ('register_table', 'apply', 'read', 'restore', 'select_page'):
                    raise ValueError('Unsupported live RAM action.')
                report = getattr(controller, action)(**payload)
                qualified = controller.qualified is True and report.get('status') == 'passed'
                self.resultReady.emit(action, report, qualified)
        except Exception as exc:
            report = _failure(f'Unexpected live RAM error ({type(exc).__name__}).')
            report['details']['exception_message'] = str(exc)
            self.resultReady.emit('error', report, False)
        finally:
            try:
                self.cleanup_report = controller.disconnect() if controller is not None else _failure(
                    'The connection could not be initialized; cleanup is unconfirmed.')
            except Exception as exc:
                self.cleanup_report = _failure(f'Disconnect error ({type(exc).__name__}).')
                self.cleanup_report['details']['exception_message'] = str(exc)
            self.resultReady.emit('disconnect', self.cleanup_report, False)


def table_spans(definition, include_axes=True):
    spans = [(definition.z_address, definition.size * definition.z_width)]
    if include_axes:
        for axis in (definition.x_axis, definition.y_axis):
            if axis is not None and axis.editable:
                spans.append((axis.address, axis.count * (axis.element_bits // 8)))
    if any(type(start) is not int or type(size) is not int or start < 0 or size <= 0
           for start, size in spans):
        raise ValueError('The selected table or axis has an invalid byte span.')
    return spans


def checked_updates(report, definition, include_axes, snapshot):
    updates = report.get('updates')
    if updates is None:
        updates = report.get('details', {}).get('updates')
    if not isinstance(updates, list):
        raise ValueError('The RAM readback report has no verified byte updates.')
    spans = table_spans(definition, include_axes)
    result, seen = [], {}
    for item in updates:
        if not isinstance(item, dict):
            raise ValueError('The RAM readback contains an invalid update record.')
        start = item.get('file_offset')
        try:
            data = bytes.fromhex(item['hex'])
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError('The RAM readback contains invalid byte data.') from exc
        if (type(start) is not int or not data or start < 0 or start + len(data) > len(snapshot)
                or not all(any(a <= target < a + size for a, size in spans)
                           for target in range(start, start + len(data)))):
            raise ValueError('The RAM readback is outside the captured table and axes.')
        for target, value in enumerate(data, start):
            if target in seen and seen[target] != value:
                raise ValueError('The RAM readback contains conflicting overlapping updates.')
            seen[target] = value
        result.append((start, data))
    if set(seen) != {target for start, size in spans for target in range(start, start + size)}:
        raise ValueError('The RAM readback does not cover the complete captured table and axes.')
    return result


class Dde5LiveRamDialog(QDialog):
    ownershipChanged = Signal(bool)
    stopped = Signal(bool)

    def __init__(self, get_context, apply_readback, parent=None):
        super().__init__(parent)
        self.get_context, self.apply_readback = get_context, apply_readback
        self.worker = None
        self._busy = self._qualified = self._close_requested = False
        self._document = self._original = self._pending_read = None
        self._disconnecting = False
        self._recovery_unconfirmed = self._power_cycle_required = False
        self.reports = []
        self.setWindowTitle('DDE5 live RAM editing')
        self.setModal(False)
        self.resize(780, 760)
        layout = QVBoxLayout(self)
        self._note(layout, 'Edit maps in the main editor while this window is open. Live controls '
                   'unlock only after a reversible changed-byte/readback/restoration test passes '
                   'on this connection. Nothing connects or uploads automatically.')
        self.table_label = QLabel()
        self.table_label.setWordWrap(True)
        layout.addWidget(self.table_label)
        form = QFormLayout()
        self.port = QLineEdit()
        self.port.setPlaceholderText('For example COM3')
        form.addRow('K-line port (9600 baud)', self.port)
        self.echo = QCheckBox('Cable returns transmitted bytes (echo)')
        self.echo.setChecked(True)
        form.addRow('', self.echo)
        self.capacity = QComboBox()
        self.capacity.addItem('32 KiB verified usable SRAM', 32768)
        self.capacity.addItem('64 KiB verified usable SRAM', 65536)
        form.addRow('Hardware capacity', self.capacity)
        self.reference = QLineEdit()
        self.reference.setPlaceholderText('Wiring / bank configuration / usable capacity evidence')
        form.addRow('Hardware evidence', self.reference)
        layout.addLayout(form)
        self.hardware_ack = QCheckBox('I verified the SRAM wiring, bank configuration and usable capacity.')
        self.engine_ack = QCheckBox('The engine is stopped and the reference page is in use.')
        self.exclusive_ack = QCheckBox('No other CCP, diagnostic or calibration client is using this ECU.')
        for control in (self.hardware_ack, self.engine_ack, self.exclusive_ack):
            layout.addWidget(control)
        self.qualify_button = QPushButton('Connect and qualify with reversible write test…')
        layout.addWidget(self.qualify_button)
        self.include_axes = QCheckBox('Include editable X/Y axes with table operations')
        self.include_axes.setChecked(True)
        layout.addWidget(self.include_axes)
        row = QHBoxLayout()
        self.register_button = QPushButton('Prepare selected table')
        self.upload_button = QPushButton('Upload current table edits…')
        self.read_button = QPushButton('Read RAM into editor')
        for button in (self.register_button, self.upload_button, self.read_button):
            row.addWidget(button)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.working_button = QPushButton('Use RAM working page')
        self.reference_button = QPushButton('Use reference page')
        self.restore_button = QPushButton('Restore selected RAM table…')
        for button in (self.working_button, self.reference_button, self.restore_button):
            row.addWidget(button)
        layout.addLayout(row)
        self._note(layout, 'Uploads are explicit and cover only the current table and chosen editable '
                   'axes. Exact readback verifies transmitted bytes. Successful upload/read/restore '
                   'operations leave the RAM working page active. Disconnect restores the reference page.')
        self.status = QLabel('Disconnected. Live editing is locked.')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        layout.addWidget(self.output, 1)
        row = QHBoxLayout()
        self.cancel_button = QPushButton('Cancel operation and disconnect')
        self.disconnect_button = QPushButton('Disconnect / restore reference')
        self.save_button = QPushButton('Save JSON reports…')
        self.close_button = QPushButton('Close')
        for button in (self.cancel_button, self.disconnect_button, self.save_button, self.close_button):
            row.addWidget(button)
        layout.addLayout(row)
        self.qualify_button.clicked.connect(self.qualify)
        for action, button in (('register_table', self.register_button), ('apply', self.upload_button),
                               ('read', self.read_button), ('restore', self.restore_button)):
            button.clicked.connect(lambda _checked=False, action=action: self.command(action))
        self.working_button.clicked.connect(lambda: self.command('select_page', page='working'))
        self.reference_button.clicked.connect(lambda: self.command('select_page', page='reference'))
        self.cancel_button.clicked.connect(lambda: self.disconnect(cancel=True))
        self.disconnect_button.clicked.connect(self.disconnect)
        self.save_button.clicked.connect(self.save_reports)
        self.close_button.clicked.connect(self.reject)
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
        for control in (self.port, self.reference):
            control.textChanged.connect(self._declaration_changed)
        self.capacity.currentIndexChanged.connect(self._declaration_changed)
        for control in (self.hardware_ack, self.engine_ack, self.exclusive_ack):
            control.toggled.connect(self.refresh_controls)
        self.timer = QTimer(self)
        self.timer.setInterval(400)
        self.timer.timeout.connect(self.refresh_controls)
        self.timer.start()
        self.refresh_controls()

    @staticmethod
    def _note(layout, text):
        label = QLabel(text)
        label.setWordWrap(True)
        layout.addWidget(label)

    def _declaration_changed(self, *_):
        self.hardware_ack.setChecked(False)
        self.refresh_controls()

    def capture_context(self):
        source = self.get_context()
        if not source or source.get('profile_id') != '361830' or source.get('definition') is None:
            raise ValueError('Open a 361830 BIN and select a verified table.')
        definition = deepcopy(source['definition'])
        if definition.discovery_unverified:
            raise ValueError('Select a verified table for RAM editing.')
        original, edited = bytes(source['original']), bytes(source['edited'])
        if len(original) != 0x100000 or len(edited) != len(original):
            raise ValueError('A full 1 MiB 361830 FLS BIN is required.')
        if self.worker is not None and (source['document'] is not self._document or original != self._original):
            raise ValueError('The source document changed. Disconnect and qualify again.')
        table_spans(definition, self.include_axes.isChecked())
        return dict(document=source['document'], original=original, edited=edited,
                    definition=definition, include_axes=self.include_axes.isChecked())

    def _hardware(self):
        return validate_hardware(dict(capacity_bytes=self.capacity.currentData(),
            reference=self.reference.text(), confirmed=self.hardware_ack.isChecked()))

    def refresh_controls(self, *_):
        context_ok = False
        try:
            context = self.capture_context()
            self.table_label.setText(f'Current table: {context["definition"].title} — '
                                     f'0x{context["definition"].z_address:06X} in the BIN')
            context_ok = True
        except (ValueError, KeyError, TypeError) as exc:
            self.table_label.setText(str(exc))
        ready = context_ok
        try:
            validate_port(self.port.text())
            self._hardware()
            ready = ready and self.engine_ack.isChecked() and self.exclusive_ack.isChecked()
        except ValueError:
            ready = False
        owned = self.worker is not None
        for control in (self.port, self.echo, self.capacity, self.reference,
                        self.hardware_ack, self.engine_ack, self.exclusive_ack):
            control.setEnabled(not owned)
        self.qualify_button.setEnabled(not owned and ready)
        active = owned and self._qualified and not self._busy and not self._disconnecting
        for control in (self.register_button, self.upload_button, self.read_button, self.restore_button):
            control.setEnabled(active and context_ok)
        self.include_axes.setEnabled(not self._busy and not self._disconnecting)
        self.working_button.setEnabled(active)
        self.reference_button.setEnabled(active)
        self.disconnect_button.setEnabled(owned and not self._disconnecting)
        self.cancel_button.setEnabled(owned and self._busy and not self._disconnecting)
        self.save_button.setEnabled(bool(self.reports) and not self._busy)

    def qualify(self):
        if self.worker is not None:
            return
        try:
            context = self.capture_context()
            port, hardware = validate_port(self.port.text()), self._hardware()
            if not self.engine_ack.isChecked() or not self.exclusive_ack.isChecked():
                raise ValueError('Confirm the stopped engine, reference page and exclusive client use.')
        except (ValueError, KeyError, TypeError) as exc:
            self.status.setText(str(exc))
            return
        address = 0x800000 + context['definition'].z_address
        if QMessageBox.question(self, 'Qualify this live RAM connection',
            f'On {port} with the engine stopped, prepare the RAM manager, copy unchanged '
            f'baseline blocks and test a changed byte at 0x{address:06X} in the table’s first cell. '
            'The test verifies readback, restores the original byte, verifies it, and restores '
            'the reference page. Current editor changes are excluded.\n\n'
            'Only a complete passing test unlocks live controls for this open connection. '
            'A lost connection may prevent restoration. Start?',
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        self._document, self._original = context['document'], context['original']
        self._busy, self._qualified, self._disconnecting = True, False, False
        self._close_requested = False
        self.status.setText('Qualifying this connection; live editing remains locked…')
        worker = LiveRamWorker(port, context['original'], hardware, context['definition'],
                                echo=self.echo.isChecked(), parent=self)
        self.worker = worker
        worker.progress.connect(self.output.appendPlainText)
        worker.resultReady.connect(self._result)
        worker.finished.connect(self._finished)
        self.ownershipChanged.emit(True)
        self.refresh_controls()
        worker.start()

    def command(self, action, *, page=None):
        if self.worker is None or not self._qualified or self._busy or self._disconnecting:
            return
        try:
            if action == 'select_page':
                if page not in ('working', 'reference'):
                    raise ValueError('Choose the RAM working or reference page.')
                payload = dict(page=page)
            else:
                context = self.capture_context()
                payload = dict(definition=context['definition'], include_axes=context['include_axes'])
                if action not in ('register_table', 'apply', 'read', 'restore'):
                    raise ValueError('Unsupported live RAM action.')
                if action in ('apply', 'restore'):
                    verb = 'Upload current editor bytes to' if action == 'apply' else 'Restore original baseline bytes in'
                    if QMessageBox.question(self, 'Confirm live RAM update',
                        f'{verb} {context["definition"].title}'
                        f'{" and its editable axes" if context["include_axes"] else ""}?\n\n'
                        'The operation verifies RAM readback and leaves the RAM working page active. '
                        'Flash is unchanged.', QMessageBox.Yes | QMessageBox.No,
                        QMessageBox.No) != QMessageBox.Yes:
                        return
                if action == 'apply':
                    payload['edited'] = context['edited']
                if action == 'read':
                    self._pending_read = context
        except (ValueError, KeyError, TypeError) as exc:
            self.status.setText(str(exc))
            return
        self._busy = True
        self.status.setText(f'Running {action.replace("_", " ")}…')
        self.refresh_controls()
        self.worker.submit(action, **payload)

    def _result(self, action, report, qualified):
        self.reports.append(dict(action=action, report=report))
        self._busy = action == 'disconnect'
        self._qualified = bool(qualified and report.get('status') == 'passed' and not self._disconnecting)
        if action == 'qualify':
            self._qualified = self._qualified and report.get('details', {}).get('qualified') is True
            if self._qualified:
                self._recovery_unconfirmed = self._power_cycle_required = False
        summary = f'{action.replace("_", " ").capitalize()}: {report.get("status", "failed")}.'
        if report.get('error'):
            summary += ' ' + str(report['error'])
        if action == 'read':
            context, self._pending_read = self._pending_read, None
            if report.get('status') == 'passed' and self._qualified and context is not None:
                try:
                    current = self.capture_context()
                    if current['document'] is not context['document'] or current['edited'] != context['edited']:
                        raise ValueError('The editor changed during RAM readback; no editor bytes were replaced.')
                    updates = checked_updates(report, context['definition'], context['include_axes'], context['edited'])
                    self.apply_readback(context['document'], context['edited'], updates)
                    summary += ' Verified RAM bytes applied; any editor changes use one undo command.'
                except (ValueError, KeyError, TypeError, RuntimeError) as exc:
                    summary += ' ' + str(exc)
                    report['gui_readback_error'] = str(exc)
        details = report.get('details') or {}
        if details.get('page'):
            summary += f' Active page: {details["page"]}.'
        if details.get('working_changes_retained') or report.get('restore', {}).get('write') == 'retained_verified':
            summary += ' Verified edits remain in RAM until restored; the working page is active.'
        restore = report.get('restore') or {}
        port_uncertain = (details.get('port_closed') is False
                          and not (details.get('connection_retained') and self._qualified))
        self._recovery_unconfirmed = (self._recovery_unconfirmed or port_uncertain
            or restore.get('reference_page') in ('required', 'unknown')
            or restore.get('write') in ('required', 'unknown')
            or bool(restore.get('restoration_required')) or bool(restore.get('power_cycle_required')))
        self._power_cycle_required = self._power_cycle_required or bool(restore.get('power_cycle_required'))
        if restore.get('reference_page') in ('required', 'unknown'):
            summary += ' Reference-page restoration is unconfirmed.'
        if restore.get('write') in ('required', 'unknown'):
            summary += ' Byte restoration is unconfirmed.'
        if restore.get('restoration_required'):
            summary += ' Restoration requires attention; save the reports.'
        if restore.get('power_cycle_required'):
            summary += ' A power cycle is required before normal use.'
        if port_uncertain:
            summary += ' COM port cleanup is unconfirmed.'
        if action == 'disconnect' and self._recovery_unconfirmed:
            summary += ' Earlier restoration or cleanup remains unconfirmed; review the preceding reports.'
            if self._power_cycle_required and not restore.get('power_cycle_required'):
                summary += ' A power cycle is still required before normal use.'
        if action == 'qualify' and self._qualified:
            summary += ' Live controls unlocked for this connection.'
        elif not self._qualified:
            summary += ' Live editing is locked.'
        self.status.setText(summary)
        self.output.appendPlainText(summary)
        self.refresh_controls()

    def disconnect(self, _checked=False, *, cancel=False):
        if self.worker is None:
            return
        self._qualified = False
        self._disconnecting = True
        self.status.setText('Disconnecting; restoring the reference page and closing the port…')
        self.worker.request_disconnect(cancel=cancel)
        self.refresh_controls()

    def _finished(self):
        worker = self.worker
        if worker is None:
            return
        cleanup = worker.cleanup_report or _failure('The worker ended without a cleanup report.')
        clean = (cleanup.get('status') == 'passed'
                 and not self._recovery_unconfirmed
                 and cleanup.get('restore', {}).get('reference_page') in ('verified', 'not_needed')
                 and not cleanup.get('restore', {}).get('power_cycle_required')
                 and not cleanup.get('restore', {}).get('restoration_required')
                 and cleanup.get('details', {}).get('port_closed') is not False)
        self.worker = None
        self._busy = self._qualified = self._disconnecting = False
        self._pending_read = None
        worker.deleteLater()
        self.hardware_ack.setChecked(False)
        self.engine_ack.setChecked(False)
        self.exclusive_ack.setChecked(False)
        self.ownershipChanged.emit(False)
        self.refresh_controls()
        self.stopped.emit(clean)
        if self._close_requested and clean:
            QTimer.singleShot(0, self.close)

    def shutdown(self):
        if self.worker is None:
            return True
        self._close_requested = True
        self.disconnect(cancel=self._busy)
        return False

    def save_reports(self):
        if self._busy or not self.reports:
            return
        name, _ = QFileDialog.getSaveFileName(self, 'Save live RAM reports',
                                             'dde5-live-ram.json', 'JSON (*.json)')
        if name:
            try:
                Path(name).write_text(json.dumps({'format': 'DDE5-live-RAM-GUI-v1',
                    'reports': self.reports}, indent=2) + '\n', encoding='utf-8')
            except (OSError, ValueError, TypeError) as exc:
                QMessageBox.critical(self, 'Save reports', f'Could not save reports:\n{exc}')

    def accept(self):
        if self.shutdown():
            super().accept()

    def reject(self):
        if self.shutdown():
            super().reject()

    def closeEvent(self, event):
        if self.shutdown():
            self.timer.stop()
            super().closeEvent(event)
        else:
            event.ignore()

    def showEvent(self, event):
        self.timer.start()
        super().showEvent(event)
