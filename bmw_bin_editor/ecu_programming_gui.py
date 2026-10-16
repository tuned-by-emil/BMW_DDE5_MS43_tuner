"""DDE5 image checks, native identity reads and a no-erase authentication test."""
from pathlib import Path
import hashlib
import json
import threading

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (QComboBox, QDialog, QFileDialog, QFormLayout,
                              QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                              QPlainTextEdit, QPushButton, QVBoxLayout)
from .ecu_programming import prepare_change_report, probe_ecu, WRITE_BLOCKERS
from .dde5_flash import review_loaded_image, correct_loaded_image
from .dde5_programming import authenticate_ecu


class ProbeThread(QThread):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, bin_dir, parent):
        super().__init__(parent)
        self.bin_dir = bin_dir

    def run(self):
        try:
            self.completed.emit(probe_ecu(self.bin_dir))
        except Exception as exc:
            self.failed.emit(str(exc))


class AuthenticationThread(QThread):
    completed = Signal(object)
    failed = Signal(str)
    progress = Signal(str)

    def __init__(self, bin_dir, trusted_fls, parent):
        super().__init__(parent)
        self.bin_dir = bin_dir
        self.trusted_fls = bytes(trusted_fls)
        self.cancel_event = threading.Event()

    def request_cancel(self):
        self.cancel_event.set()

    def run(self):
        try:
            report = authenticate_ecu(self.bin_dir, self.trusted_fls,
                                      cancelled=self.cancel_event.is_set,
                                      progress=self.progress.emit)
            self.completed.emit(report)
        except Exception as exc:
            # Ordinary backend failures have sanitized, exportable reports. Do not
            # print unexpected exception arguments: they may contain protocol data.
            self.failed.emit(f'Unexpected authentication error ({type(exc).__name__}). '
                             'The test did not complete. No erase or write was requested.')


class EcuProgrammingDialog(QDialog):
    def __init__(self, original=None, edited=None, image_name='', parent=None):
        super().__init__(parent)
        self.setWindowTitle('DDE5 image checks / direct ECU authentication')
        self.resize(850, 720)
        self.original = bytes(original) if original is not None else None
        self.edited = bytes(edited) if edited is not None else None
        self.image_name = image_name
        self.report = None
        self.corrected_image = None
        self.worker = None
        self._busy = False
        self._authenticating = False
        layout = QVBoxLayout(self)
        heading = QLabel('Check the loaded 361830 image and preview checksum corrections without a cable. '
                         'ECU identity and authentication tests use your BMW K-line/K+DCAN cable '
                         'and installed EDIABAS. The authentication test sends no erase, write or reset.')
        heading.setWordWrap(True)
        layout.addWidget(heading)
        form = QFormLayout()
        self.api_dir = QLineEdit(r'C:\EDIABAS\BIN')
        form.addRow('EDIABAS Bin folder', self.api_dir)
        self.space = QComboBox()
        self.space.addItems(['External FLS', 'MPC', 'Config / calibration flash'])
        if self.edited is not None and len(self.edited) in (0x20000, 0x1F000):
            self.space.setCurrentIndex(2)
        elif image_name.lower().endswith('.mpc'):
            self.space.setCurrentIndex(1)
        form.addRow('Loaded image address space', self.space)
        self.companion = QLineEdit()
        self.companion_label = QLabel()
        self.companion_browse = QPushButton('Browse…')
        companion_row = QHBoxLayout()
        companion_row.addWidget(self.companion, 1)
        companion_row.addWidget(self.companion_browse)
        form.addRow(self.companion_label, companion_row)
        layout.addLayout(form)
        config = QLabel('Uses STD:OBD and its installed port settings. Reading identity does not '
                        'authorize a write or establish that the loaded BIN matches this ECU.')
        config.setWordWrap(True)
        layout.addWidget(config)
        buttons = QHBoxLayout()
        self.read_button = QPushButton('Read ECU identity')
        self.review_button = QPushButton('Review current changes')
        self.save_button = QPushButton('Save report…')
        self.read_button.clicked.connect(self.read_identity)
        self.review_button.clicked.connect(self.review_changes)
        self.save_button.clicked.connect(self.save_report)
        for button in (self.read_button, self.review_button, self.save_button):
            button.setAutoDefault(False)
            buttons.addWidget(button)
        layout.addLayout(buttons)
        auth_buttons = QHBoxLayout()
        self.auth_button = QPushButton('Test ECU authentication — no erase')
        self.cancel_auth_button = QPushButton('Cancel authentication test')
        self.auth_button.setAutoDefault(False)
        self.cancel_auth_button.setAutoDefault(False)
        self.auth_button.clicked.connect(self.test_authentication)
        self.cancel_auth_button.clicked.connect(self.cancel_authentication)
        auth_buttons.addWidget(self.auth_button)
        auth_buttons.addWidget(self.cancel_auth_button)
        layout.addLayout(auth_buttons)
        self.auth_status = QLabel('Authentication has not been tested. ECU writing is unavailable.')
        self.auth_status.setWordWrap(True)
        layout.addWidget(self.auth_status)
        image_buttons = QHBoxLayout()
        self.check_button = QPushButton('Check 361830 image')
        self.correct_button = QPushButton('Preview checksum correction')
        self.apply_button = QPushButton('Apply corrected checksums to editor')
        self.check_button.clicked.connect(self.check_image)
        self.correct_button.clicked.connect(self.correct_checksums)
        self.apply_button.clicked.connect(self.apply_correction)
        for button in (self.check_button, self.correct_button, self.apply_button):
            button.setAutoDefault(False)
            image_buttons.addWidget(button)
        layout.addLayout(image_buttons)
        self.summary = QLabel(self._selection_hint())
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        layout.addWidget(self.output)
        self.checksum_note = QLabel()
        self.checksum_note.setWordWrap(True)
        layout.addWidget(self.checksum_note)
        self.write_button = QPushButton('Write to ECU — unavailable')
        self.write_button.setEnabled(False)
        self.write_button.setToolTip('\n'.join(WRITE_BLOCKERS))
        layout.addWidget(self.write_button)
        self.space.currentIndexChanged.connect(self.invalidate_report)
        self.companion.textChanged.connect(self.invalidate_report)
        self.companion_browse.clicked.connect(self.browse_companion)
        self._update_companion_label()
        self._refresh_controls()

    def address_space(self):
        return ('FLS', 'MPC', 'CONFIG')[self.space.currentIndex()]

    def _selection_hint(self):
        if self.address_space() == 'CONFIG':
            return ('Check or correct the config / calibration checksum without an MPC image. '
                    'This selection checks only the calibration range at 0x8E0000; '
                    'a partial image does not establish whole-image validity.')
        if self.address_space() == 'FLS':
            return ('Check or correct the external FLS checksums without an MPC image. '
                    'Add the matching MPC image to also check the complete program signature.')
        return ('Choose the full matching FLS image to check or correct MPC checksums. '
                'Review current changes also works without a companion image.')

    def _update_companion_label(self):
        space = self.address_space()
        if space == 'CONFIG':
            self.companion_label.setText('Companion image (not used)')
            self.companion.setPlaceholderText('Config checksum checks use only the loaded image')
            self.checksum_note.setText('Config correction adjusts the arithmetic checksum; it does not '
                                       'create a factory signature. A partial file does not contain '
                                       'the complete external-flash erase sector.')
        elif space == 'FLS':
            self.companion_label.setText('Companion MPC image (optional)')
            self.companion.setPlaceholderText('Optional: full MPC image from the same ECU/software')
            self.checksum_note.setText('Checksum correction adjusts the external-flash arithmetic sums; '
                                       'it does not create a factory signature. The preview preserves '
                                       'complete affected external-flash sectors. ECU writing is unavailable.')
        else:
            self.companion_label.setText('Companion FLS image (required)')
            self.companion.setPlaceholderText('Required: full FLS image from the same ECU/software')
            self.checksum_note.setText('Checksum correction adjusts the arithmetic sums; it does not '
                                       'create a factory signature. The MPC preview includes every '
                                       'affected 32 KiB sector. Selective writing remains unavailable.')

    def _refresh_controls(self):
        has_image = self.original is not None and self.edited is not None
        idle = not self._busy
        self.read_button.setEnabled(idle)
        self.review_button.setEnabled(idle and has_image)
        has_companion = bool(self.companion.text().strip())
        space = self.address_space()
        full_fls = has_image and len(self.original) == 0x100000
        auth_image_available = (full_fls if space in ('FLS', 'CONFIG') else has_companion)
        self.auth_button.setEnabled(idle and has_image and auth_image_available)
        self.auth_button.setToolTip('Authentication requires a complete preserved 1 MiB FLS image.')
        self.cancel_auth_button.setEnabled(self._authenticating and self.worker is not None
                                           and not self.worker.cancel_event.is_set())
        image_check_available = space != 'MPC' or has_companion
        self.check_button.setEnabled(idle and has_image and image_check_available)
        self.correct_button.setEnabled(idle and has_image and image_check_available)
        self.apply_button.setEnabled(idle and self.corrected_image is not None)
        self.save_button.setEnabled(idle and self.report is not None)
        for control in (self.space, self.api_dir):
            control.setEnabled(idle)
        for control in (self.companion, self.companion_browse):
            control.setEnabled(idle and space != 'CONFIG')
        self.write_button.setEnabled(False)

    def browse_companion(self):
        if self._busy or self.address_space() == 'CONFIG':
            return
        companion_space = 'MPC' if self.address_space() == 'FLS' else 'FLS'
        initial = self.companion.text().strip()
        if not initial and self.image_name:
            initial = str(Path(self.image_name).with_suffix('.'+companion_space))
        path, _ = QFileDialog.getOpenFileName(self, f'Choose companion {companion_space} image',
                                             initial, 'ECU images (*.MPC *.FLS *.bin);;All files (*.*)')
        if path:
            self.companion.setText(path)

    def _read_companion(self):
        if self.original is None or self.edited is None:
            raise ValueError('Open an image in the editor first.')
        if self.address_space() == 'CONFIG':
            return None
        path = self.companion.text().strip()
        if not path:
            if self.address_space() == 'FLS':
                return None
            raise ValueError('Choose the full companion FLS image first.')
        return Path(path).read_bytes()

    def invalidate_report(self, *_):
        self.report = None
        self.corrected_image = None
        self.output.clear()
        self.auth_status.setText('Authentication has not been tested for this image selection. '
                                 'ECU writing is unavailable.')
        self.summary.setText(self._selection_hint())
        self._update_companion_label()
        self._refresh_controls()

    def read_identity(self):
        if self._busy:
            return
        self.report = None
        self.corrected_image = None
        self.auth_status.setText('Authentication has not been tested by this identity read. '
                                 'ECU writing is unavailable.')
        self.output.setPlainText('Reading identity…')
        self.summary.setText('Reading ECU identity. Image checks and correction are paused until the read finishes.')
        self._busy = True
        self._refresh_controls()
        self.worker = ProbeThread(self.api_dir.text().strip(), self)
        self.worker.completed.connect(self.show_report)
        self.worker.failed.connect(self.show_error)
        self.worker.finished.connect(self.probe_finished)
        self.worker.start()

    def probe_finished(self):
        finished_worker = self.worker
        self.worker = None
        self._busy = False
        self._refresh_controls()
        if finished_worker is not None:
            finished_worker.deleteLater()

    def _authentication_snapshot(self):
        if self.original is None or self.edited is None:
            raise ValueError('Open your preserved MPC or FLS image in the editor first.')
        bin_dir = self.api_dir.text().strip()
        if not bin_dir or not Path(bin_dir).is_dir():
            raise ValueError('Choose your installed EDIABAS Bin folder first.')
        if self.address_space() in ('FLS', 'CONFIG'):
            trusted_fls = bytes(self.original)
        else:
            if not self.companion.text().strip():
                raise ValueError('Choose the full preserved companion FLS image before authentication.')
            try:
                trusted_fls = bytes(self._read_companion())
            except OSError:
                raise ValueError('The companion FLS image could not be read. Check its file path.') from None
        if len(trusted_fls) != 0x100000:
            raise ValueError('Authentication needs the complete 1 MiB preserved FLS image '
                             'from the matching ECU. A partial or MPC image cannot be used.')
        return bin_dir, trusted_fls

    def test_authentication(self):
        if self._busy:
            return
        try:
            # Freeze the preserved FLS bytes before any ECU worker starts. Edited
            # or checksum-corrected FLS bytes are never used as key evidence.
            bin_dir, trusted_fls = self._authentication_snapshot()
        except (ValueError, OSError) as exc:
            self.auth_status.setText('Authentication did not start. ECU writing is unavailable.')
            self.show_error(str(exc), context='Authentication prerequisites are missing')
            return
        self.report = None
        self.corrected_image = None
        self._busy = True
        self._authenticating = True
        self.output.setPlainText('Starting the authentication test. No erase, write or reset will be sent.')
        self.summary.setText('Authentication is running. The image controls and identity read are paused.')
        self.auth_status.setText('Authentication test in progress. ECU writing is unavailable.')
        self.worker = AuthenticationThread(bin_dir, trusted_fls, self)
        self.worker.completed.connect(self.show_authentication_report)
        self.worker.failed.connect(self.show_authentication_error)
        self.worker.progress.connect(self.authentication_progress)
        self.worker.finished.connect(self.authentication_finished)
        self._refresh_controls()
        self.worker.start()

    def cancel_authentication(self):
        if not self._authenticating or self.worker is None:
            return
        self.worker.request_cancel()
        self.auth_status.setText('Cancellation requested. Waiting for the current EDIABAS job to finish. '
                                 'No erase or write will be sent.')
        self._refresh_controls()

    def authentication_progress(self, message):
        if self._authenticating:
            self.output.appendPlainText(str(message))

    def show_authentication_report(self, report):
        report = dict(report)
        # Authentication is a test result, never permission to write an image.
        report['flash_ready'] = False
        report['programming_enabled'] = False
        authenticated = report.get('authenticated') is True
        cancelled = report.get('cancelled') is True
        self.show_report(report)
        if authenticated:
            error = report.get('error')
            if isinstance(error, str) and error.strip():
                error = error.strip()
                if not error.endswith(('.', '!', '?')):
                    error += '.'
                text = 'ECU authentication was accepted, but the test reported an error: '+error+' '
            else:
                text = 'ECU authentication succeeded during this test. '
            text += 'Image acceptance and ECU writing '
            text += 'remain unverified. No erase, write or reset was sent.'
        elif cancelled:
            text = 'Authentication test cancelled. No erase, write or reset was sent. '
            text += 'ECU writing is unavailable.'
        else:
            text = 'ECU authentication did not succeed. See the report for the failed stage. '
            text += 'No erase, write or reset was sent. ECU writing is unavailable.'
        if report.get('volatile_authentication_may_remain') is True:
            text += ' The ECU may retain volatile authentication after the connection closes.'
        self.auth_status.setText(text)
        self.summary.setText(text)

    def show_authentication_error(self, message):
        self.auth_status.setText('Authentication test did not complete. ECU writing is unavailable.')
        self.show_error(message, context='Authentication test failed')

    def authentication_finished(self):
        finished_worker = self.worker
        self.worker = None
        self._busy = False
        self._authenticating = False
        self._refresh_controls()
        if finished_worker is not None:
            finished_worker.deleteLater()

    def show_error(self, error, *, context='ECU read failed'):
        self.report = None
        self.corrected_image = None
        self.summary.setText(context+'. No checksum correction is staged.')
        self.output.setPlainText(context+':\n'+str(error))
        self._refresh_controls()

    @staticmethod
    def _report_summary(report):
        parts = []
        if 'changed_bytes' in report:
            parts.append(f"Changed bytes versus the file baseline: {report['changed_bytes']:,}.")
        validation = report
        for key in ('current', 'image_validation', 'validation', 'image_checks', 'checks'):
            if isinstance(report.get(key), dict):
                validation = report[key]
                break
        additive = validation.get('all_additive_valid')
        selected_additive = validation.get('selected_space_additive_valid')
        if isinstance(selected_additive, bool):
            loaded_space = report.get('address_space', 'loaded image')
            if loaded_space == 'CONFIG':
                loaded_space = 'Config / calibration flash'
            parts.append(f'{loaded_space} arithmetic checksums: '+('pass.' if selected_additive else 'correction needed.'))
            if selected_additive and additive is False:
                parts.append('The companion image still has a failing arithmetic checksum.')
        elif isinstance(additive, bool):
            label = ('Covered arithmetic checksums' if validation.get('coverage_complete') is False
                     else 'Paired arithmetic checksums')
            parts.append(label+': '+('pass.' if additive else 'correction needed.'))
        scope = report.get('validation_scope', validation.get('validation_scope'))
        if not isinstance(scope, str):
            scope = report.get('checksum_scope', validation.get('checksum_scope'))
        if isinstance(scope, str) and scope:
            parts.append('Checksum scope: '+scope.rstrip('.')+'.')
        prepared = report.get('corrected_preview', {})
        if selected_additive is False and prepared.get('selected_space_additive_valid') is True:
            parts.append('The corrected loaded image passes its arithmetic checksums.')
        elif selected_additive is None and additive is False and prepared.get('all_additive_valid') is True:
            parts.append('The corrected preview passes the covered arithmetic checksums.')
        for key, label in (('program_signature', 'Program signature'),
                           ('calibration_signature', 'Calibration signature')):
            value = validation.get(key)
            if isinstance(value, dict) and isinstance(value.get('valid'), bool):
                parts.append(label+(': valid.' if value['valid'] else ': invalid.'))
            elif isinstance(value, dict):
                reason = value.get('reason')
                parts.append(label+': not checked'+
                             (' ('+str(reason).rstrip('.')+').' if reason else '.'))
        plan = next((report[key] for key in ('partial_mpc_plan', 'sector_plan', 'partial_plan')
                     if isinstance(report.get(key), dict)), None)
        if plan:
            count = plan.get('selected_sector_count', plan.get('sector_count'))
            size = plan.get('planned_sector_payload_bytes', plan.get('total_bytes', plan.get('transfer_bytes')))
            plan_space = plan.get('address_space', report.get('address_space', 'MPC'))
            if plan.get('payload_coverage_complete') is False:
                parts.append('The partial config file does not contain complete external-flash sector bytes.')
            elif isinstance(count, int) and isinstance(size, int):
                parts.append(f'Preserved {plan_space} sector payloads: {count} sector(s), {size:,} bytes.')
            erase = plan.get('native_program_erase_total_bytes')
            if isinstance(erase, int) and plan.get('native_partial_supported') is False:
                parts.append(f'Factory program erase covers {erase:,} bytes; selective erase is unavailable.')
        if not parts:
            parts.append('Read completed. Full results are below.')
        parts.append('ECU writing is unavailable.')
        return ' '.join(parts)

    def show_report(self, report, *, note='', keep_correction=False):
        rendered = json.dumps(report, indent=2)
        if not keep_correction:
            self.corrected_image = None
        self.report = report
        self.output.setPlainText(rendered)
        self.summary.setText((note+' ' if note else '')+self._report_summary(report))
        self._refresh_controls()

    def review_changes(self):
        if self._busy:
            return
        try:
            self.show_report(prepare_change_report(self.original, self.edited, self.address_space()))
        except Exception as exc:
            self.show_error(str(exc), context='Change review failed')

    def check_image(self):
        if self._busy:
            return
        try:
            companion = self._read_companion()
            candidate = self.corrected_image if self.corrected_image is not None else self.edited
            report = review_loaded_image(self.original, candidate, self.address_space(), companion)
            self.show_report(report, keep_correction=True,
                             note='Showing the staged correction.' if self.corrected_image is not None else '')
        except Exception as exc:
            self.show_error(str(exc), context='Image check failed')

    def correct_checksums(self):
        if self._busy:
            return
        try:
            companion = self._read_companion()
            corrected, changes = correct_loaded_image(self.edited, self.address_space(), companion)
            corrected = bytes(corrected)
            if len(corrected) != len(self.edited):
                raise ValueError('Checksum correction changed the image size.')
            report = review_loaded_image(self.original, corrected, self.address_space(), companion)
            report = dict(report)
            report['staged_additive_correction'] = {
                'changes': changes,
                'source_sha256': hashlib.sha256(self.edited).hexdigest(),
                'corrected_sha256': hashlib.sha256(corrected).hexdigest(),
                'applied_to_editor': False,
                'note': 'Arithmetic correction only; no factory signature is generated.',
            }
            # Validate the whole report before making any correction available.
            json.dumps(report)
            changed = corrected != self.edited
            self.corrected_image = corrected if changed else None
            note = ('Correction staged. Apply it to create one undoable editor change.' if changed
                    else 'Arithmetic checksums need no correction; the editor is unchanged.')
            self.show_report(report, keep_correction=True, note=note)
        except Exception as exc:
            self.show_error(str(exc), context='Checksum correction failed')

    def apply_correction(self):
        if not self._busy and self.corrected_image is not None:
            self.accept()

    def save_report(self):
        if self.report is None:
            return
        if self._busy:
            return
        path, _ = QFileDialog.getSaveFileName(self, 'Save DDE5 image / connection report',
                                             'dde5_programming_review.json', 'JSON (*.json)')
        if path:
            try:
                Path(path).write_text(json.dumps(self.report, indent=2), encoding='utf-8')
            except OSError as exc:
                QMessageBox.warning(self, self.windowTitle(), str(exc))

    def reject(self):
        if self._busy or self.worker and self.worker.isRunning():
            return
        self.corrected_image = None
        super().reject()

    def accept(self):
        if self._busy or self.worker and self.worker.isRunning():
            return
        super().accept()

    def closeEvent(self, event):
        if self._busy or self.worker and self.worker.isRunning():
            event.ignore()
        else:
            self.corrected_image = None
            super().closeEvent(event)
