"""D50M47A logging profiles, capture, setup and TestO transport."""
from __future__ import annotations
from .settings import editor_settings, ECUSettings
from .dde5_labels import PARAMETER_NAMES
import csv
import json
import math
import re
import time
from datetime import datetime
from pathlib import Path
from PySide6.QtCore import QObject, Signal, QTimer, QUrl, Qt, QSettings
from PySide6.QtWebSockets import QWebSocket
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QLineEdit, QPushButton, QComboBox, QSpinBox, QCheckBox, QTreeWidget,
    QTreeWidgetItem, QFileDialog, QMessageBox, QInputDialog, QAbstractItemView)

ANCHORS = ['INMOT', 'IMEIA']
BASE = ANCHORS + ['IPLAD', 'SPLAD', 'IALDS', 'IPRDR', 'ILMMG', 'IWHE1',
                   'SMOMO', 'IVKMH', 'IWVE1', 'IMVE1', 'ITKUM', 'IMHE1']
FAST = BASE[:10]


def available_serial_ports():
    """Enumerate port names lazily; never open a cable to populate the dialog."""
    from serial.tools import list_ports
    return sorted({port.device for port in list_ports.comports()}, key=str.casefold)


def load_parameters(path):
    rows = json.loads(Path(path).read_text(encoding='utf-8'))
    result = {r['arg']: r for r in rows}
    selector = result.get('Ad_NumTyp_FreeApplic2_CSTR')
    if selector and selector.get('id') == '0x00E0':
        selector['label'] = 'ECU torque gear selector (CAN-derived)'
    return result


def make_groups(selected, catalog):
    if not isinstance(selected, list) or not selected or any(not isinstance(x, str) for x in selected):
        raise ValueError('Choose parameters to log.')
    selected = list(dict.fromkeys(ANCHORS + selected))
    if any(x not in catalog for x in selected):
        raise ValueError('Profile contains unknown D50M47A parameters.')
    extras = [x for x in selected if x not in ANCHORS]
    if len(extras) > 64:
        raise ValueError('Choose at most 66 parameters (8 groups).')
    return [ANCHORS + extras[i:i+8] for i in range(0, len(extras), 8)] or [ANCHORS[:]]


def validate_profile(profile, catalog):
    if not isinstance(profile, dict) or profile.get('ecu') != 'D50M47A':
        raise ValueError('This logger supports D50M47A profiles only.')
    if profile.get('version') != 1:
        raise ValueError('Unsupported logging profile version.')
    name = profile.get('name')
    if not isinstance(name, str) or not name.strip() or len(name) > 100:
        raise ValueError('Profile name must contain 1–100 characters.')
    make_groups(profile.get('parameters'), catalog)
    interval = profile.get('interval_ms', 0)
    if type(interval) is not int or not 0 <= interval <= 5000:
        raise ValueError('Minimum request interval must be 0–5000 ms.')
    return dict(version=1, ecu='D50M47A', name=name.strip(),
                parameters=list(dict.fromkeys(ANCHORS + profile['parameters'])), interval_ms=interval)


def decode_reply(message, group, catalog):
    fields = {r['name']: r.get('value') for r in message.get('results', [])}
    if fields.get('JOB_STATUS') != 'OKAY':
        raise ValueError('ECU read failed: ' + str(fields.get('JOB_STATUS', message.get('resultName'))))
    expected = ''.join(catalog[a]['id'].removeprefix('0x').upper().zfill(4) for a in group)
    request = str(fields.get('_TEL_AUFTRAG', '')).upper()
    # Numeric mode 3 is rewritten to short cached reads by some TestO paths.
    # Require proof of the full requested selection, rather than trusting OKAY.
    if not request.endswith('2C10' + expected):
        raise ValueError('The diagnostic request reused or changed the measurement block. Logging stopped to avoid mislabelled values.')
    values = {}
    for arg in group:
        key = catalog[arg]['result']
        value = float(fields[key])
        if not math.isfinite(value):
            raise ValueError('Missing or invalid value for ' + key)
        values[key] = value
    return values, fields


class CsvCapture:
    def __init__(self, path, profile, groups, catalog, *, flush_interval=0.0, context=None):
        if not isinstance(flush_interval, (int, float)) or not math.isfinite(flush_interval) or flush_interval < 0:
            raise ValueError('CSV flush interval must be a finite nonnegative number.')
        self.flush_interval = float(flush_interval)
        self.last_flush = time.monotonic()
        self.path = Path(path)
        self.fields = list(dict.fromkeys(catalog[a]['result'] for g in groups for a in g))
        self.handle = self.path.open('x', encoding='utf-8-sig', newline='')
        self.writer = csv.DictWriter(self.handle, fieldnames=['time', 'time_unit', 'receivedAt',
            'group', 'requestId', 'JOB_STATUS', '_TEL_AUFTRAG', '_TEL_ANTWORT'] + self.fields)
        try:
            self.writer.writeheader()
            self.handle.flush()
            with self.path.with_suffix(self.path.suffix + '.profile.json').open('x', encoding='utf-8') as meta:
                metadata = dict(profile=profile, groups=groups, parameters=[catalog[a] for a in profile['parameters']],
                    time_basis='Computer receipt time; groups are not simultaneous')
                if context is not None:
                    metadata['capture'] = dict(context)
                    metadata['capture']['csv_flush_interval_seconds'] = self.flush_interval
                json.dump(metadata, meta, indent=2)
        except Exception:
            self.handle.close()
            raise

    def append(self, seconds, timestamp, group, request_id, values, fields):
        # Unmeasured channels stay blank; never pass held values off as fresh data.
        self.writer.writerow(dict(time=f'{seconds:.9f}', time_unit='s', receivedAt=timestamp,
            group=group, requestId=request_id, JOB_STATUS='OKAY',
            _TEL_AUFTRAG=fields.get('_TEL_AUFTRAG', ''), _TEL_ANTWORT=fields.get('_TEL_ANTWORT', ''), **values))
        now = time.monotonic()
        if self.flush_interval == 0 or now - self.last_flush >= self.flush_interval:
            self.handle.flush()
            self.last_flush = now

    def close(self):
        self.handle.close()


class LiveLogger(QObject):
    sampleReady = Signal(float, object)
    status = Signal(str)
    error = Signal(str)
    stopped = Signal()

    def __init__(self, config, catalog, parent=None):
        super().__init__(parent)
        self.config = config
        self.catalog = catalog
        self.profile = validate_profile(config['profile'], catalog)
        self.groups = make_groups(self.profile['parameters'], catalog)
        self.socket = QWebSocket('', parent=self)
        self.socket.textMessageReceived.connect(self.on_message)
        self.socket.disconnected.connect(self.on_disconnect)
        self.socket.errorOccurred.connect(lambda *_: self.fail(self.socket.errorString()))
        self.timer = QTimer(self); self.timer.setSingleShot(True); self.timer.timeout.connect(self.request_next)
        self.deadline = QTimer(self); self.deadline.setSingleShot(True)
        self.deadline.timeout.connect(lambda: self.fail('TestO did not reply within 5 seconds.'))
        self.inspect_timer = QTimer(self); self.inspect_timer.setSingleShot(True)
        self.inspect_timer.timeout.connect(self.begin)
        self.active = False
        self.pending = None
        self.socket_id = None
        self.queue_clear = False
        self.started_polling = False
        self.count = 0
        self.index = 0
        self.capture = None
        self.seen_first = None

    def start(self):
        self.active = True
        try:
            url = QUrl(self.config['endpoint'])
            if url.scheme() not in ('ws', 'wss') or not url.host():
                raise ValueError('Enter a valid ws:// or wss:// TestO address.')
            self.active = True
            self.status.emit('Connecting to TestO…')
            self.deadline.start(5000)
            self.socket.open(url)
        except Exception as e:
            self.fail(str(e))

    def send(self, payload):
        self.socket.sendTextMessage(json.dumps(payload))

    def on_message(self, text):
        if not self.active:
            return
        try:
            m = json.loads(text)
            if m.get('connected'):
                self.socket_id = m['socketId']
                self.send(dict(cmd='listJobs', requestId='live-inspect'))
                return
            if m.get('requestId') == 'live-inspect' and m.get('ack') == 'listJobs':
                if m.get('jobs'):
                    raise ValueError('Stop the existing TestO jobs before starting this logger.')
                self.queue_clear = True
                self.deadline.stop()
                self.inspect_timer.start(400)
                return
            if m.get('error'):
                raise ValueError(str(m.get('message', m)))
            if m.get('results') and m.get('ack') != 'runJobOnceResult':
                if (self.started_polling and m.get('requestorId') == f'ws-once-{self.socket_id}'
                        and m.get('ecu', '').upper() == 'D50M47A'
                        and m.get('jobname') == 'STATUS_MESSWERTBLOCK_LESEN'):
                    return  # TestO broadcasts our one-shot result a second time.
                raise ValueError('Another TestO job is streaming. Stop it before using live logging.')
            if m.get('ack') != 'runJobOnceResult':
                return
            if not self.pending or m.get('requestId') != self.pending[0]:
                return  # duplicate or unrelated response; never count twice
            if m.get('ecu', '').upper() != 'D50M47A' or m.get('jobname') != 'STATUS_MESSWERTBLOCK_LESEN':
                raise ValueError('Unexpected ECU/job in response.')
            request_id, group_index, sent = self.pending
            values, fields = decode_reply(m, self.groups[group_index], self.catalog)
            received = time.monotonic()
            timestamp = datetime.now().astimezone().isoformat(timespec='microseconds')
            if self.seen_first is None:
                self.seen_first = received
            seconds = received - self.seen_first
            if self.capture:
                self.capture.append(seconds, timestamp, group_index+1, request_id, values, fields)
            self.pending = None
            self.deadline.stop()
            self.count += 1
            self.sampleReady.emit(seconds, values)
            rate = (self.count-1)/seconds if seconds > 0 else 0
            dest = str(self.capture.path) if self.capture else 'Live preview only'
            self.status.emit(f'{self.count} replies · {rate:.1f} Hz total · ~{rate/len(self.groups):.1f} Hz/group · {seconds:.1f} s')
            self.index = (group_index+1) % len(self.groups)
            delay = max(0, self.profile['interval_ms'] - int((received-sent)*1000))
            self.timer.start(delay)
        except Exception as e:
            self.fail(str(e))

    def begin(self):
        if not self.active or not self.queue_clear or self.socket_id is None:
            return
        try:
            if self.config.get('output'):
                self.capture = CsvCapture(self.config['output'], self.profile, self.groups, self.catalog)
            self.started_polling = True
            self.request_next()
        except Exception as e:
            self.fail(str(e))

    def request_next(self):
        if not self.active or self.pending:
            return
        request_id = f'live-{self.count}'
        self.pending = (request_id, self.index, time.monotonic())
        self.deadline.start(5000)
        self.send(dict(cmd='runJobOnce', requestId=request_id, socketId=self.socket_id,
            prg='D50M47A', job='STATUS_MESSWERTBLOCK_LESEN', arg='JA;'+';'.join(self.groups[self.index])))

    def on_disconnect(self):
        if self.active:
            self.fail('TestO disconnected. The captured rows have been saved.')

    def fail(self, message):
        if not self.active:
            return
        self.stop()
        self.error.emit(message)

    def stop(self):
        if not self.active:
            return
        self.active = False
        self.timer.stop(); self.deadline.stop(); self.inspect_timer.stop()
        self.socket.abort()
        if self.capture:
            try:
                self.capture.close()
            except OSError as e:
                self.error.emit('Could not finalize CSV: '+str(e))
        self.stopped.emit()


class LoggingDialog(QDialog):
    def __init__(self, catalog, parent=None, settings=None):
        super().__init__(parent)
        self.catalog = catalog
        self.settings = settings if settings is not None else ECUSettings(editor_settings(QSettings))
        self.setWindowTitle('Live logging — D50M47A')
        self.resize(1180, 760)
        self.saved = {}
        try:
            for p in json.loads(str(self.settings.value('live126/profiles', '[]'))):
                valid = validate_profile(p, catalog); self.saved[valid['name']] = valid
        except (ValueError, TypeError):
            pass
        self.presets = {'Balanced engine (14)': dict(version=1, ecu='D50M47A', name='Balanced engine (14)', parameters=BASE, interval_ms=0),
                        'Fast engine (10)': dict(version=1, ecu='D50M47A', name='Fast engine (10)', parameters=FAST, interval_ms=0)}
        presets_path = Path(__file__).resolve().parents[1] / 'definitions' / 'live_logging_presets.json'
        if presets_path.is_file():
            for preset in json.loads(presets_path.read_text(encoding='utf-8')):
                valid = validate_profile(preset, catalog)
                self.presets[valid['name']] = valid
        self.profile_box = QComboBox()
        self.profile_box.addItems(list(dict.fromkeys(list(self.presets)+list(self.saved))))
        top = QHBoxLayout(); top.addWidget(QLabel('Profile')); top.addWidget(self.profile_box, 1)
        for label, slot in [('Save as…', self.save_profile), ('Delete', self.delete_profile), ('Import…', self.import_profile), ('Export…', self.export_profile)]:
            b = QPushButton(label); b.clicked.connect(slot); top.addWidget(b)
        self.transport = QComboBox()
        self.transport.addItem('Direct EDIABAS · BMW K-line / K+DCAN', 'ediabas')
        self.transport.addItem('TestO WebSocket', 'testo')
        self.transport.addItem('Direct serial · BMW K-line', 'serial')
        self.transport.setCurrentIndex(max(0, self.transport.findData(str(self.settings.value('live126/transport', 'ediabas')))))
        self.bin_dir = QLineEdit(str(self.settings.value('live126/ediabas_bin', r'C:\EDIABAS\BIN')))
        self.endpoint = QLineEdit(str(self.settings.value('live126/endpoint', 'ws://127.0.0.1:33333')))
        self.port = QComboBox(); self.port.setEditable(True)
        self.port.setCurrentText(str(self.settings.value('live126/serial_port', 'COM1') or 'COM1'))
        self.refresh_ports_button = QPushButton('Refresh ports')
        self.refresh_ports_button.clicked.connect(self.refresh_serial_ports)
        port_row = QHBoxLayout(); port_row.addWidget(self.port, 1); port_row.addWidget(self.refresh_ports_button)
        self.echo = QCheckBox('Cable echoes transmitted bytes')
        self.echo.setChecked(self.settings.value('live126/serial_echo', True, type=bool))
        self.echo.setToolTip('Keep enabled for cables that return the bytes they transmit.')
        self.serial_max_rate = QCheckBox('Poll at maximum serial rate')
        self.serial_max_rate.setChecked(self.settings.value('live126/serial_max_rate', True, type=bool))
        self.serial_max_rate.setToolTip('Use your selected parameters without the profile’s extra request delay. The ECU’s reply spacing is retained.')
        self.interval = QSpinBox(); self.interval.setRange(0, 5000); self.interval.setSuffix(' ms'); self.interval.setSpecialValueText('As fast as replies arrive')
        form = QFormLayout(); form.addRow('Connection', self.transport); form.addRow('EDIABAS BIN folder', self.bin_dir); form.addRow('TestO address', self.endpoint)
        form.addRow('Serial port', port_row); form.addRow('', self.echo); form.addRow('', self.serial_max_rate)
        form.addRow('Minimum request interval', self.interval)
        self.search = QLineEdit(); self.search.setPlaceholderText('Search English name, BMW name or hex code…')
        self.selected_only = QCheckBox('Show selected only')
        self.selected_only.toggled.connect(lambda: self.filter(self.search.text()))
        self.tree = QTreeWidget(); self.tree.setHeaderLabels(['English name', 'BMW name', 'Hex code']); self.tree.setRootIsDecorated(False)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        for arg, d in sorted(catalog.items(), key=lambda x: PARAMETER_NAMES.get(x[0], x[1]['label']).casefold()):
            item = QTreeWidgetItem([PARAMETER_NAMES.get(arg, d['label']), d['result'], d['id']])
            item.setData(0, Qt.UserRole, arg)
            details = d['label'] + '\n' + d['result'] + ' · ' + arg + '\nUnits: ' + (d['unit'] or '—')
            for column in range(3):
                item.setToolTip(column, details)
            item.setData(0, Qt.UserRole + 1, details)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(0, Qt.Unchecked)
            if arg in ANCHORS:
                item.setFlags(item.flags() & ~Qt.ItemIsUserCheckable)
            self.tree.addTopLevelItem(item)
        self.tree.setColumnWidth(0, 390)
        self.tree.setColumnWidth(1, 600)
        self.tree.setColumnWidth(2, 95)
        self.summary = QLabel(); self.summary.setWordWrap(True)
        self.save_csv = QCheckBox('Store to CSV while logging'); self.save_csv.setChecked(True)
        self.keep_awake = QCheckBox('Keep laptop awake while logging')
        self.keep_awake.setChecked(self.settings.value('live126/keep_awake', True, type=bool))
        self.lid_closed = QCheckBox('Keep logging with the lid closed (battery and mains)')
        self.lid_closed.setChecked(self.settings.value('live126/lid_closed', True, type=bool))
        self.lid_closed.setToolTip('Temporarily sets lid close to Do nothing. Restores the prior lid actions when logging stops. Windows policy may block this; check Power status before closing the lid. A crash is recovered the next time the editor starts.')
        folder = Path(str(self.settings.value('live126/last_output_folder', str(Path.home()/'Documents'))))
        if not folder.is_dir():
            folder = Path.home()/'Documents'
        self.output = QLineEdit(str(folder/('Live_D50M47A_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'.csv')))
        browse = QPushButton('Choose file…'); browse.clicked.connect(self.browse)
        destination = QHBoxLayout(); destination.addWidget(self.output, 1); destination.addWidget(browse)
        self.note = QLabel('RPM and injection quantity repeat in every group. Groups contain at most 10 values and are read in turn.\nTestO must be running with its WebSocket server enabled. Stop other TestO logging first.')
        self.note.setWordWrap(True)
        self.transport.currentIndexChanged.connect(self.update_transport)
        self.serial_max_rate.toggled.connect(self.update_transport)
        self.interval.valueChanged.connect(self.update_summary)
        self.update_transport()
        buttons = QHBoxLayout(); buttons.addStretch(); start=QPushButton('Start logging'); start.clicked.connect(self.accept)
        cancel=QPushButton('Cancel'); cancel.clicked.connect(self.reject); buttons.addWidget(start); buttons.addWidget(cancel)
        search_row=QHBoxLayout(); search_row.addWidget(self.search,1); search_row.addWidget(self.selected_only)
        layout=QVBoxLayout(self); layout.addLayout(top); layout.addLayout(form); layout.addLayout(search_row); layout.addWidget(self.tree, 1)
        layout.addWidget(self.summary); layout.addWidget(self.keep_awake); layout.addWidget(self.lid_closed); layout.addWidget(self.save_csv); layout.addLayout(destination); layout.addWidget(self.note); layout.addLayout(buttons)
        self.profile_box.currentTextChanged.connect(self.load_profile)
        self.tree.itemChanged.connect(self.update_summary)
        self.search.textChanged.connect(self.filter)
        self.load_profile(self.profile_box.currentText())

    def update_transport(self, *_):
        transport = self.transport.currentData()
        self.bin_dir.setEnabled(transport == 'ediabas')
        self.endpoint.setEnabled(transport == 'testo')
        self.port.setEnabled(transport == 'serial')
        self.refresh_ports_button.setEnabled(transport == 'serial')
        self.echo.setEnabled(transport == 'serial')
        self.serial_max_rate.setEnabled(transport == 'serial')
        self.interval.setEnabled(transport != 'serial' or not self.serial_max_rate.isChecked())
        notes = {
            'ediabas': 'Uses installed EDIABAS cable settings. Close other diagnostic tools before connecting.',
            'testo': 'TestO must be running with its WebSocket server enabled. Stop other TestO logging first.',
            'serial': 'Direct cable access at 9,600 baud. One group uses fast repeat reads; two groups alternate and each refreshes more slowly. Close other diagnostic tools before connecting.',
        }
        self.note.setText('RPM and injection quantity repeat in every group. Groups contain at most 10 values and are read in turn.\n' +
            notes.get(transport, 'Choose a supported connection.'))
        self.update_summary()

    def refresh_serial_ports(self, *_):
        selected = self.port.currentText()
        try:
            ports = available_serial_ports()
        except Exception as error:
            # Port discovery is optional: typed COM names work without discovery.
            self.refresh_ports_button.setToolTip('Port list unavailable: ' + str(error))
            return
        self.refresh_ports_button.setToolTip('')
        self.port.clear()
        self.port.addItems(ports)
        self.port.setCurrentText(selected)

    def items(self):
        return [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]

    def selected(self):
        chosen = {i.data(0, Qt.UserRole) for i in self.items() if i.checkState(0) == Qt.Checked}
        # Keep recommendation ordering so related channels remain in one reply.
        return [a for a in BASE if a in chosen]+sorted(chosen-set(BASE))

    def profile(self, name=None):
        return validate_profile(dict(version=1, ecu='D50M47A', name=name or self.profile_box.currentText(),
            parameters=self.selected(), interval_ms=self.interval.value()), self.catalog)

    def load_profile(self, name):
        p=self.saved.get(name, self.presets.get(name))
        if not p: return
        self.tree.blockSignals(True)
        for i in self.items():
            i.setCheckState(0, Qt.Checked if i.data(0, Qt.UserRole) in p['parameters'] else Qt.Unchecked)
        self.tree.blockSignals(False)
        self.interval.setValue(p['interval_ms']); self.update_summary()

    def update_summary(self, *_):
        try:
            groups=make_groups(self.selected(), self.catalog)
            self.summary.setText(f'{len(self.selected())} parameters · {len(groups)} group(s) · '+ ' / '.join(str(len(g)) for g in groups)+' values per group. More groups reduce each parameter’s refresh rate.')
            if self.transport.currentData() == 'serial':
                strategy = 'Fast single request' if len(groups) == 1 else f'{len(groups)} alternating requests'
                timing = 'maximum polling rate' if self.serial_max_rate.isChecked() else f'{self.interval.value()} ms minimum request interval'
                self.summary.setText(self.summary.text() + f'\nSerial: {strategy} · {timing}.')
            self.tree.blockSignals(True)
            for item in self.items():
                arg=item.data(0, Qt.UserRole)
                group_text = ', '.join(str(n+1) for n,g in enumerate(groups) if arg in g)
                details = item.data(0, Qt.UserRole + 1) + '\nCapture groups: ' + (group_text or 'not selected')
                for column in range(3):
                    item.setToolTip(column, details)
            self.tree.blockSignals(False)
        except ValueError as e:
            self.summary.setText(str(e))

    def filter(self, text):
        for item in self.items():
            searchable = ' '.join(item.text(column) for column in range(3)) + ' ' + item.toolTip(0)
            item.setHidden((self.selected_only.isChecked() and item.checkState(0) != Qt.Checked) or text.strip().casefold() not in searchable.casefold())

    def persist(self):
        self.settings.setValue('live126/profiles', json.dumps(list(self.saved.values())))
        self.settings.sync()

    def save_profile(self):
        name, ok=QInputDialog.getText(self, 'Save logging profile', 'Profile name:')
        if not ok: return
        try:
            p=self.profile(name)
            if p['name'] in self.presets and p['name'] not in self.saved: raise ValueError('Choose a new name for your custom profile.')
            if p['name'] in self.saved and QMessageBox.question(self, 'Replace profile', 'Replace the saved profile?') != QMessageBox.Yes: return
            self.saved[p['name']]=p; self.persist()
            if self.profile_box.findText(p['name']) < 0: self.profile_box.addItem(p['name'])
            self.profile_box.setCurrentText(p['name'])
        except ValueError as e: QMessageBox.warning(self, 'Profile', str(e))

    def delete_profile(self):
        name=self.profile_box.currentText()
        if name not in self.saved: return
        if QMessageBox.question(self, 'Delete profile', f'Delete “{name}”?') != QMessageBox.Yes: return
        del self.saved[name]; self.persist()
        if name in self.presets:
            self.load_profile(name)
        else:
            self.profile_box.removeItem(self.profile_box.currentIndex())

    def import_profile(self):
        path,_=QFileDialog.getOpenFileName(self, 'Import logging profile', '', 'JSON (*.json)')
        if not path: return
        try:
            p=validate_profile(json.loads(Path(path).read_text(encoding='utf-8-sig')), self.catalog)
            if p['name'] in self.presets or p['name'] in self.saved: raise ValueError('That profile name already exists; rename it before importing.')
            self.saved[p['name']]=p; self.persist(); self.profile_box.addItem(p['name']); self.profile_box.setCurrentText(p['name'])
        except (ValueError, OSError) as e: QMessageBox.warning(self, 'Import profile', str(e))

    def export_profile(self):
        try:
            p=self.profile()
            path,_=QFileDialog.getSaveFileName(self, 'Export logging profile', p['name']+'.json', 'JSON (*.json)')
            if path: Path(path).write_text(json.dumps(p, indent=2), encoding='utf-8')
        except (ValueError, OSError) as e: QMessageBox.warning(self, 'Export profile', str(e))

    def browse(self):
        path,_=QFileDialog.getSaveFileName(self, 'New CSV capture', self.output.text(), 'CSV (*.csv)')
        if path: self.output.setText(path)

    def config(self):
        profile=self.profile()
        output=self.output.text().strip() if self.save_csv.isChecked() else ''
        if self.save_csv.isChecked():
            if not output or not Path(output).parent.is_dir(): raise ValueError('Choose an existing folder for your CSV file.')
            if Path(output).exists() or Path(output+'.profile.json').exists(): raise ValueError('Choose a new filename; existing captures are never overwritten.')
        power = dict(keep_awake=self.keep_awake.isChecked(), lid_closed=self.lid_closed.isChecked())
        transport=self.transport.currentData()
        if transport == 'ediabas':
            folder=self.bin_dir.text().strip()
            if not Path(folder).is_dir(): raise ValueError('Choose the installed EDIABAS BIN folder.')
            return dict(profile=profile, transport=transport, bin_dir=folder, output=output, **power)
        if transport == 'serial':
            port = self.port.currentText().strip().upper()
            if not re.fullmatch(r'COM[1-9][0-9]{0,3}', port):
                raise ValueError('Enter a serial port such as COM1 or COM12.')
            from .serial_measurements import SerialMeasurements
            SerialMeasurements(self.catalog).validate_groups(make_groups(profile['parameters'], self.catalog))
            return dict(profile=profile, transport=transport, port=port, echo=self.echo.isChecked(),
                        max_rate=self.serial_max_rate.isChecked(), output=output, **power)
        if transport != 'testo':
            raise ValueError('Choose a supported logging connection.')
        url=QUrl(self.endpoint.text().strip())
        if url.scheme() not in ('ws','wss') or not url.host(): raise ValueError('Enter a valid TestO WebSocket address.')
        return dict(profile=profile, transport=transport, endpoint=url.toString(), output=output, **power)

    def accept(self):
        try:
            config = self.config()
            if config['output']:
                self.settings.setValue('live126/last_output_folder', str(Path(config['output']).resolve().parent))
            self.settings.setValue('live126/keep_awake', self.keep_awake.isChecked())
            self.settings.setValue('live126/lid_closed', self.lid_closed.isChecked())
            self.settings.setValue('live126/endpoint', self.endpoint.text().strip())
            self.settings.setValue('live126/transport', self.transport.currentData())
            self.settings.setValue('live126/ediabas_bin', self.bin_dir.text().strip())
            self.settings.setValue('live126/serial_port', self.port.currentText().strip().upper())
            self.settings.setValue('live126/serial_echo', self.echo.isChecked())
            self.settings.setValue('live126/serial_max_rate', self.serial_max_rate.isChecked())
            self.settings.sync()
            super().accept()
        except ValueError as e: QMessageBox.warning(self, 'Live logging', str(e))
