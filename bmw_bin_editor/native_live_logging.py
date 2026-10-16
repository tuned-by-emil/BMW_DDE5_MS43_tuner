"""Direct installed EDIABAS live reads; one session owned by one worker thread."""
from __future__ import annotations
import time
from datetime import datetime
from PySide6.QtCore import QObject, QThread, Signal, Slot
from .ecu_programming import NativeEdiabas
from .live_logging import validate_profile, make_groups, decode_reply, CsvCapture


class _Reader(QThread):
    sampleReady = Signal(float, object)
    status = Signal(str)
    error = Signal(str)

    def __init__(self, config, catalog, parent=None, *, api_factory=NativeEdiabas):
        super().__init__(parent)
        self.config, self.catalog, self.api_factory = config, catalog, api_factory
        self.profile = validate_profile(config['profile'], catalog)
        self.groups = make_groups(self.profile['parameters'], catalog)
        self.count = 0
        self.capture = None

    def run(self):
        try:
            self.status.emit('Connecting to EDIABAS…')
            with self.api_factory(self.config.get('bin_dir', r'C:\EDIABAS\BIN')) as api:
                if self.isInterruptionRequested():
                    return
                if self.config.get('output'):
                    self.capture = CsvCapture(self.config['output'], self.profile, self.groups, self.catalog)
                first = None
                index = 0
                while not self.isInterruptionRequested():
                    group = self.groups[index]
                    sent = time.monotonic()
                    response = api.read_job('D50M47A', 'STATUS_MESSWERTBLOCK_LESEN', timeout=5,
                        parameters='JA;'+';'.join(group), cancelled=self.isInterruptionRequested)
                    if self.isInterruptionRequested():
                        break
                    fields = {}
                    for row in response['result_sets']:
                        for name, value in row.items():
                            if name in fields and fields[name] != value:
                                raise ValueError('Conflicting diagnostic result: '+name)
                            fields[name] = value
                    values, fields = decode_reply({'results': [dict(name=k, value=v) for k,v in fields.items()]}, group, self.catalog)
                    received = time.monotonic()
                    if first is None:
                        first = received
                    seconds = received-first
                    if self.capture:
                        self.capture.append(seconds, datetime.now().astimezone().isoformat(timespec='microseconds'),
                            index+1, f'native-{self.count}', values, fields)
                    self.count += 1
                    self.sampleReady.emit(seconds, values)
                    rate = (self.count-1)/seconds if seconds > 0 else 0
                    self.status.emit(f'EDIABAS · {self.count} replies · {rate:.1f} Hz total · ~{rate/len(self.groups):.1f} Hz/group · {seconds:.1f} s')
                    index = (index+1) % len(self.groups)
                    due = sent+self.profile['interval_ms']/1000
                    while not self.isInterruptionRequested() and time.monotonic() < due:
                        self.msleep(10)
        except Exception as exc:
            if not self.isInterruptionRequested():
                self.error.emit(str(exc))
        finally:
            if self.capture:
                try:
                    self.capture.close()
                except OSError as exc:
                    self.error.emit('Could not finalize CSV: '+str(exc))


class NativeLiveLogger(QObject):
    sampleReady = Signal(float, object)
    status = Signal(str)
    error = Signal(str)
    stopped = Signal()

    def __init__(self, config, catalog, parent=None, *, api_factory=NativeEdiabas):
        super().__init__(parent)
        self.worker = _Reader(config, catalog, self, api_factory=api_factory)
        self.active = False
        self.complete = False
        self.worker.sampleReady.connect(self._sample)
        self.worker.status.connect(self._status)
        self.worker.error.connect(self.error)
        self.worker.finished.connect(self._finished)

    @property
    def count(self):
        return self.worker.count

    @property
    def capture(self):
        return self.worker.capture

    def start(self):
        self.active = True
        self.worker.start()

    def stop(self):
        self.active = False
        self.worker.requestInterruption()
        # The owner must remain alive until stopped: emitted after QThread exits.
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
        self.active = False
        self.complete = True
        self.stopped.emit()
