"""Thread-owned direct cable measurement logging, with existing gauges and CSV."""
from __future__ import annotations

from datetime import datetime
from copy import deepcopy
import time
from PySide6.QtCore import QObject, QThread, Signal, Slot
from .live_logging import CsvCapture, make_groups, validate_profile
from .serial_measurements import SerialMeasurements
from .serial_transport import KlineSession, SerialCancelled, SerialCommunicationError, validate_port
from .serial_process import ProcessKlineSession


class _Reader(QThread):
    sampleReady = Signal(float, object)
    status = Signal(str)
    error = Signal(str)

    def __init__(self, config, catalog, parent=None, *, session_factory=ProcessKlineSession):
        super().__init__(parent)
        self.config, self.catalog = deepcopy(config), deepcopy(catalog)
        self.session_factory = session_factory
        self.port = validate_port(self.config.get('port', ''))
        if type(self.config.get('echo', True)) is not bool:
            raise ValueError('Cable echo must be enabled or disabled.')
        self.profile = validate_profile(self.config['profile'], self.catalog)
        if type(self.config.get('max_rate', False)) is not bool:
            raise ValueError('Maximum serial rate must be enabled or disabled.')
        if self.config.get('max_rate', False):
            self.profile['interval_ms'] = 0
        self.groups = make_groups(self.profile['parameters'], self.catalog)
        self.decoder = SerialMeasurements(self.catalog)
        self.decoder.validate_groups(self.groups)
        self.channel_ids = []
        # Resolve and validate every group before opening a port or creating CSV.
        for group in self.groups:
            payload = self.decoder.selection_payload(group)
            self.channel_ids.append(tuple(int.from_bytes(payload[n:n+2], 'big') for n in range(2, len(payload), 2)))
        self.count, self.capture = 0, None

    def run(self):
        try:
            self.status.emit(f'Connecting to {self.port} · 9,600 baud…')
            with self.session_factory(self.port, echo=self.config.get('echo', True),
                    cancelled=self.isInterruptionRequested) as session:
                if self.isInterruptionRequested():
                    return
                self.status.emit('Checking DDE identity…')
                identity = session.identify()
                if identity != {'hardware': KlineSession.EXPECTED_HARDWARE,
                                'software': KlineSession.EXPECTED_SOFTWARE}:
                    raise SerialCommunicationError('Direct serial logging requires DDE software 1037361830 / hardware 0281010565.')
                if self.isInterruptionRequested():
                    return
                self.status.emit(f"DDE {identity['software']} connected · direct serial")
                if self.config.get('output'):
                    self.capture = CsvCapture(self.config['output'], self.profile, self.groups, self.catalog,
                        flush_interval=0.25, context=dict(transport='direct_serial', baudrate=9600,
                            identity=identity, cable_echo=self.config.get('echo', True),
                            polling='single_cached_group' if len(self.groups) == 1 else 'alternating_full_selections'))
                first, index = None, 0
                first_by_group = [None] * len(self.groups)
                last_by_group = [None] * len(self.groups)
                counts_by_group = [0] * len(self.groups)
                last_status = None
                while not self.isInterruptionRequested():
                    sent = time.monotonic()
                    group = self.groups[index]
                    reply = session.read_measurements(self.channel_ids[index])
                    if self.isInterruptionRequested():
                        break
                    values = self.decoder.decode_measurements(reply.payload, group)
                    received = time.monotonic()
                    if first is None:
                        first = received
                    seconds = received - first
                    fields = {'JOB_STATUS': 'OKAY', '_TEL_AUFTRAG': reply.request_frame.hex().upper(),
                              '_TEL_ANTWORT': reply.response_frame.hex().upper()}
                    if self.capture:
                        self.capture.append(seconds, datetime.now().astimezone().isoformat(timespec='microseconds'),
                            index + 1, f'serial-{self.count}', values, fields)
                    self.count += 1
                    counts_by_group[index] += 1
                    if first_by_group[index] is None:
                        first_by_group[index] = received
                    last_by_group[index] = received
                    self.sampleReady.emit(seconds, values)
                    rate = (self.count - 1) / seconds if seconds > 0 else 0
                    if last_status is None or received - last_status >= 0.25:
                        if len(self.groups) == 1:
                            detail = f'{len(group)} values · {rate:.1f} Hz/group'
                        else:
                            group_rates = []
                            for number, (group_first, group_last, group_count) in enumerate(zip(first_by_group, last_by_group, counts_by_group), 1):
                                elapsed = group_last - group_first if group_first is not None else 0
                                group_rate = (group_count - 1) / elapsed if elapsed > 0 and group_count > 1 else 0
                                group_rates.append(f'G{number}: {group_rate:.1f} Hz')
                            detail = ' / '.join(group_rates)
                        self.status.emit(f'Serial · {self.port} · {self.count} replies · {rate:.1f} Hz total · '
                            f'{detail} · {seconds:.1f} s')
                        last_status = received
                    index = (index + 1) % len(self.groups)
                    due = sent + self.profile['interval_ms'] / 1000
                    while not self.isInterruptionRequested() and time.monotonic() < due:
                        self.msleep(max(1, min(10, int((due - time.monotonic()) * 1000))))
        except SerialCancelled:
            if not self.isInterruptionRequested():
                self.error.emit('Serial read cancelled.')
        except Exception as error:
            if not self.isInterruptionRequested():
                self.error.emit(str(error))
        finally:
            if self.capture:
                try:
                    self.capture.close()
                except OSError as error:
                    self.error.emit('Could not finalize CSV: ' + str(error))


class SerialLiveLogger(QObject):
    sampleReady = Signal(float, object)
    status = Signal(str)
    error = Signal(str)
    stopped = Signal()

    def __init__(self, config, catalog, parent=None, *, session_factory=ProcessKlineSession, reader_factory=_Reader):
        super().__init__(parent)
        self.worker = reader_factory(config, catalog, self, session_factory=session_factory)
        self.active, self.complete = False, False
        self.worker.sampleReady.connect(self._sample)
        self.worker.status.connect(self._status)
        self.worker.error.connect(self.error)
        self.worker.finished.connect(self._finished)

    @property
    def catalog(self):
        return self.worker.catalog

    @property
    def profile(self):
        return self.worker.profile

    @property
    def groups(self):
        return self.worker.groups

    @property
    def count(self):
        return self.worker.count

    @property
    def capture(self):
        return self.worker.capture

    def start(self):
        if self.active or self.complete or self.worker.isRunning():
            raise RuntimeError('Create a new serial logger to reconnect.')
        self.active = True
        self.worker.start()

    def stop(self):
        self.active = False
        self.worker.requestInterruption()
        return self.complete

    @Slot(float, object)
    def _sample(self, seconds, values):
        if self.active:
            self.sampleReady.emit(seconds, values)

    @Slot(str)
    def _status(self, message):
        if self.active:
            self.status.emit(message)

    @Slot()
    def _finished(self):
        self.active, self.complete = False, True
        self.stopped.emit()
