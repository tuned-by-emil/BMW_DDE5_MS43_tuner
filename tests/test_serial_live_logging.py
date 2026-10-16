"""Qt worker integration with an in-memory cable; never opens a COM port."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import copy
import csv
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication
from bmw_bin_editor.live_logging import BASE, FAST, load_parameters, make_groups
from bmw_bin_editor.serial_live_logging import SerialLiveLogger
from bmw_bin_editor.serial_transport import (
    KlineSession, SerialCancelled, SerialReply, xor_checksum,
)

ROOT = Path(__file__).resolve().parents[1]
CAT = load_parameters(ROOT / 'definitions/live_parameters.json')
APP = QApplication.instance() or QApplication([])


def profile(parameters=FAST, interval=50):
    return dict(version=1, ecu='D50M47A', name='Serial integration test',
                parameters=list(parameters), interval_ms=interval)


def response_frame(payload):
    body = bytes((0xB8, 0xF1, 0x12, len(payload))) + payload
    return body + bytes((xor_checksum(body),))


class CableRecorder:
    """Minimal transparent-cable fake consumed by the real KlineSession."""
    WORDS = {0x2D: 8000, 0x55: 32768, 0x3A: 16000, 0x8A: 17600,
             0x7B: 7350, 0xC2: 32768, 0x50: 44000, 0xBF: 30000,
             0x57: 25000, 0x32: 32768, 0xB1: 30000, 0xB7: 4000,
             0x2C: 6000, 0xC1: 22000}

    def __init__(self, *, hardware='0281010565', software='1037361830', corrupt_read=None):
        self.hardware, self.software = hardware, software
        self.corrupt_read = corrupt_read
        self.created, self.payloads, self.lifecycle = [], [], []
        self.selected, self.measurements = None, 0
        self.closed = False

    def serial_factory(self, **kwargs):
        owner = threading.get_ident()
        self.created.append((owner, kwargs))
        recorder = self

        class Port:
            def __init__(self):
                self.is_open = False
                self.buffer = bytearray()

            @property
            def in_waiting(self):
                return len(self.buffer)

            def open(self):
                self.is_open = True
                recorder.lifecycle.append(('open', threading.get_ident()))

            def reset_input_buffer(self):
                self.buffer.clear()

            def write(self, frame):
                payload = frame[4:-1]
                recorder.payloads.append(payload)
                if payload == b'\x1A\x92':
                    reply = b'\x5A\x92' + recorder.hardware.encode('ascii')
                elif payload == b'\x1A\x94':
                    reply = b'\x5A\x94' + recorder.software.encode('ascii')
                elif payload[:2] == b'\x2C\x10':
                    if len(payload) > 2:
                        recorder.selected = tuple(int.from_bytes(payload[n:n + 2], 'big')
                                                  for n in range(2, len(payload), 2))
                    if recorder.selected is None:
                        raise AssertionError('A cached measurement poll came before selection.')
                    recorder.measurements += 1
                    reply = b'\x6C\x10' + b''.join(recorder.WORDS.get(ident, 12345).to_bytes(2, 'big')
                                                for ident in recorder.selected)
                else:
                    raise AssertionError('Unexpected request in read-only logger: ' + payload.hex())
                encoded = response_frame(reply)
                if recorder.measurements and recorder.measurements == recorder.corrupt_read:
                    encoded = encoded[:-1] + bytes((encoded[-1] ^ 1,))
                self.buffer.extend(frame + encoded)
                return len(frame)

            def read(self, length):
                result = bytes(self.buffer[:length])
                del self.buffer[:length]
                return result

            def close(self):
                self.is_open = False
                recorder.closed = True
                recorder.lifecycle.append(('close', threading.get_ident()))

        return Port()

    def session_factory(self, port, *, echo, cancelled):
        return KlineSession(port, echo=echo, cancelled=cancelled, serial_factory=self.serial_factory)


class BlockingRecorder:
    """A transport that waits for cancellation during one chosen operation."""
    def __init__(self, phase, identity=None):
        self.phase = phase
        self.identity = identity
        self.entered = threading.Event()
        self.closed = False
        self.lifecycle = []
        self.calls = 0

    def session_factory(self, port, *, echo, cancelled):
        recorder = self

        class Session:
            def __enter__(self):
                recorder.lifecycle.append(('open', threading.get_ident()))
                return self

            def __exit__(self, *_):
                recorder.closed = True
                recorder.lifecycle.append(('close', threading.get_ident()))

            def wait_cancelled(self):
                recorder.entered.set()
                deadline = time.monotonic() + 2
                while not cancelled() and time.monotonic() < deadline:
                    time.sleep(.002)
                if not cancelled():
                    raise AssertionError('Cancellation was not delivered to the transport.')
                raise SerialCancelled('Cancelled pending read')

            def identify(self):
                if recorder.phase == 'identify':
                    self.wait_cancelled()
                if recorder.identity is not None:
                    return dict(recorder.identity)
                return dict(hardware=KlineSession.EXPECTED_HARDWARE,
                            software=KlineSession.EXPECTED_SOFTWARE)

            def read_measurements(self, ids):
                recorder.calls += 1
                self.wait_cancelled()

        return Session()


class SerialLiveLoggingTests(unittest.TestCase):
    def run_logger(self, recorder, path, *, parameters=FAST, samples_to_stop=2, cancel_phase=False,
                   interval=50, catalog=CAT, after_construct=None, max_rate=False):
        config = dict(port='COM7', echo=True, profile=profile(parameters, interval), output=str(path), max_rate=max_rate)
        logger = SerialLiveLogger(config, catalog, session_factory=recorder.session_factory)
        if after_construct:
            after_construct(config, catalog)
        samples, errors, events, statuses = [], [], [], []
        stopped_states = []
        loop = QEventLoop()
        deadline = QTimer()
        deadline.setSingleShot(True)
        deadline.timeout.connect(loop.quit)
        poll = QTimer()
        poll.setInterval(2)

        def sample(seconds, values):
            samples.append((seconds, values))
            events.append('sample')
            if len(samples) >= samples_to_stop:
                logger.stop()

        def error(message):
            errors.append(message)
            events.append('error')

        def stopped():
            events.append('stopped')
            # Assert on the test thread after the loop; exceptions raised in a
            # Qt signal callback can be printed without failing unittest.
            stopped_states.append((recorder.closed, logger.worker.isRunning(),
                                   logger.capture is None or logger.capture.handle.closed))
            loop.quit()

        def cancel_if_entered():
            if recorder.entered.is_set():
                logger.stop()
                poll.stop()

        logger.sampleReady.connect(sample)
        logger.error.connect(error)
        logger.status.connect(statuses.append)
        logger.stopped.connect(stopped)
        if cancel_phase:
            poll.timeout.connect(cancel_if_entered)
            poll.start()
        try:
            logger.start()
            deadline.start(2500)
            loop.exec()
            self.assertIn('stopped', events, 'Worker did not finish before the test deadline.')
            self.assertEqual(stopped_states, [(True, False, True)])
            self.assertTrue(logger.complete)
            self.assertFalse(logger.active)
            self.assertTrue(logger.stop())
            # Drain queued signals: stopping must suppress any queued samples.
            before = len(samples)
            for _ in range(3):
                APP.processEvents()
            self.assertEqual(len(samples), before)
            self.assertEqual(events[-1], 'stopped')
            owner = recorder.lifecycle[0][1]
            self.assertNotEqual(owner, threading.get_ident())
            self.assertEqual(recorder.lifecycle, [('open', owner), ('close', owner)])
            rows = []
            if path.exists():
                with path.open(encoding='utf-8-sig', newline='') as handle:
                    rows = list(csv.DictReader(handle))
            return samples, errors, rows, statuses
        finally:
            deadline.stop()
            poll.stop()
            logger.stop()
            self.assertTrue(logger.worker.wait(1500), 'Failed to stop the test worker.')
            logger.deleteLater()
            APP.processEvents()

    def test_fast_profile_selects_once_then_uses_real_transport_cache(self):
        recorder = CableRecorder()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'capture.csv'
            samples, errors, rows, statuses = self.run_logger(recorder, path, interval=0)
            self.assertEqual(errors, [])
            self.assertEqual(len(samples), 2)
            self.assertEqual(len(rows), 2)
            selection = b'\x2C\x10' + b''.join(int(CAT[arg]['id'], 16).to_bytes(2, 'big') for arg in FAST)
            self.assertEqual(recorder.payloads, [b'\x1A\x92', b'\x1A\x94', selection, b'\x2C\x10'])
            self.assertEqual(samples[0][0], 0.0)
            self.assertGreater(samples[1][0], samples[0][0])
            self.assertAlmostEqual(samples[0][1][CAT['INMOT']['result']], 1000.016)
            self.assertAlmostEqual(samples[0][1][CAT['IPLAD']['result']], 2000.032)
            self.assertEqual(set(samples[0][1]), {CAT[arg]['result'] for arg in FAST})
            self.assertEqual([row['requestId'] for row in rows], ['serial-0', 'serial-1'])
            self.assertTrue(rows[1]['_TEL_AUFTRAG'].startswith('B812F1022C10'))
            self.assertTrue(any('9,600' in status for status in statuses))
            self.assertEqual(recorder.created[0][1]['baudrate'], 9600)
            self.assertEqual(recorder.created[0][1]['parity'], 'E')

    def test_base_groups_rotate_with_sparse_csv_and_canonical_results(self):
        recorder = CableRecorder()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'capture.csv'
            samples, errors, rows, _ = self.run_logger(recorder, path, parameters=BASE, samples_to_stop=3)
            self.assertEqual(errors, [])
            self.assertEqual(len(rows), 3)
            groups = make_groups(BASE, CAT)
            full = [b'\x2C\x10' + b''.join(int(CAT[arg]['id'], 16).to_bytes(2, 'big') for arg in group)
                    for group in groups]
            self.assertEqual(recorder.payloads[2:], [full[0], full[1], full[0]])
            self.assertEqual([row['group'] for row in rows], ['1', '2', '1'])
            self.assertEqual(rows[1][CAT['IPLAD']['result']], '')
            self.assertEqual(rows[0][CAT['ITKUM']['result']], '')
            self.assertAlmostEqual(float(rows[1][CAT['ITKUM']['result']]), 50.58356)
            for (_, values), group in zip(samples, [groups[0], groups[1], groups[0]]):
                self.assertEqual(set(values), {CAT[arg]['result'] for arg in group})
            self.assertEqual([row['JOB_STATUS'] for row in rows], ['OKAY'] * 3)
            self.assertEqual([row['time_unit'] for row in rows], ['s'] * 3)
            self.assertEqual(float(rows[0]['time']), 0.0)
            self.assertLess(float(rows[1]['time']), float(rows[2]['time']))

    def test_all_group_definitions_validated_before_com_or_csv(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'capture.csv'
            recorder = CableRecorder()
            cases = []
            changed = copy.deepcopy(CAT)
            changed['ITKUM']['id'] = '0x012C'  # Invalid second-group entry.
            cases.append((BASE, changed))
            changed = copy.deepcopy(CAT)
            changed['IMHE1']['result'] = 'WRONG'
            cases.append((BASE, changed))
            # Force two conflicting canonical result names into separate groups.
            collision = ['IWSTO'] + [arg for arg in BASE if arg not in ['INMOT', 'IMEIA']][:7] + ['RWSTR']
            groups = make_groups(collision, CAT)
            self.assertNotEqual(next(i for i, g in enumerate(groups) if 'IWSTO' in g),
                                next(i for i, g in enumerate(groups) if 'RWSTR' in g))
            cases.append((collision, CAT))
            for selected, catalog in cases:
                with self.subTest(parameters=selected), self.assertRaises(ValueError):
                    SerialLiveLogger(dict(port='COM7', echo=True, profile=profile(selected), output=str(path)),
                                     catalog, session_factory=recorder.session_factory)
            self.assertEqual(recorder.created, [])
            self.assertEqual(recorder.payloads, [])
            self.assertFalse(path.exists())

    def test_identity_mismatch_closes_before_any_csv_or_sample(self):
        for changed in [dict(hardware='0281010566'), dict(software='1037361858')]:
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'capture.csv'
                recorder = CableRecorder(**changed)
                samples, errors, rows, _ = self.run_logger(recorder, path)
                self.assertEqual(samples, [])
                self.assertEqual(rows, [])
                self.assertFalse(path.exists())
                self.assertEqual(len(errors), 1)
                self.assertIn('1037361830', errors[0])
                self.assertEqual(recorder.payloads, [b'\x1A\x92', b'\x1A\x94'])

    def test_cancel_during_identity_closes_without_output(self):
        recorder = BlockingRecorder('identify')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'capture.csv'
            samples, errors, rows, _ = self.run_logger(recorder, path, cancel_phase=True)
            self.assertTrue(recorder.entered.is_set())
            self.assertEqual((samples, errors, rows), ([], [], []))
            self.assertFalse(path.exists())
            self.assertEqual(recorder.calls, 0)

    def test_worker_rejects_bad_identity_even_from_an_injected_session(self):
        recorder = BlockingRecorder(None, identity=dict(hardware='0281010565', software='1037361858'))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'capture.csv'
            samples, errors, rows, _ = self.run_logger(recorder, path)
            self.assertEqual((samples, rows), ([], []))
            self.assertFalse(path.exists())
            self.assertEqual(len(errors), 1)
            self.assertIn('1037361830', errors[0])
            self.assertEqual(recorder.calls, 0)

    def test_caller_mutations_cannot_change_session_csv_or_decoding(self):
        recorder = CableRecorder()
        catalog = copy.deepcopy(CAT)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'capture.csv'
            wrong_path = Path(directory) / 'changed-output.csv'

            def mutate(config, caller_catalog):
                config['echo'] = False
                config['output'] = str(wrong_path)
                config['profile']['parameters'].clear()
                caller_catalog['INMOT']['id'] = '0x002C'
                caller_catalog['INMOT']['unit'] = 'bar'
                caller_catalog['INMOT']['result'] = 'WRONG_RESULT'
                caller_catalog['INMOT']['label'] = 'Modified during logging'

            samples, errors, rows, _ = self.run_logger(recorder, path, catalog=catalog,
                                                      after_construct=mutate)
            self.assertEqual(errors, [])
            self.assertEqual(len(rows), 2)
            self.assertFalse(wrong_path.exists())
            self.assertEqual(set(samples[0][1]), {CAT[arg]['result'] for arg in FAST})
            self.assertAlmostEqual(float(rows[0][CAT['INMOT']['result']]), 1000.016)
            self.assertNotIn('WRONG_RESULT', rows[0])
            saved = json.loads(path.with_suffix('.csv.profile.json').read_text(encoding='utf-8'))
            self.assertEqual(saved['profile']['parameters'], FAST)
            metadata = next(row for row in saved['parameters'] if row['arg'] == 'INMOT')
            self.assertEqual(metadata['unit'], 'rpm')
            self.assertEqual(metadata['result'], CAT['INMOT']['result'])
            self.assertEqual(metadata['id'], '0x002D')

    def test_cancel_during_pending_read_closes_csv_without_samples(self):
        recorder = BlockingRecorder('read')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'capture.csv'
            samples, errors, rows, _ = self.run_logger(recorder, path, cancel_phase=True)
            self.assertEqual((samples, errors, rows), ([], [], []))
            self.assertTrue(path.exists())
            self.assertEqual(recorder.calls, 1)

    def test_corrupt_first_reply_closes_session_and_csv_before_stopped(self):
        recorder = CableRecorder(corrupt_read=1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'capture.csv'
            samples, errors, rows, _ = self.run_logger(recorder, path)
            self.assertEqual((samples, rows), ([], []))
            self.assertEqual(len(errors), 1)
            self.assertIn('checksum', errors[0])
            self.assertEqual(recorder.measurements, 1)

    def test_corruption_after_sample_stops_without_later_samples(self):
        recorder = CableRecorder(corrupt_read=2)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'capture.csv'
            samples, errors, rows, _ = self.run_logger(recorder, path, samples_to_stop=3)
            self.assertEqual(len(samples), 1)
            self.assertEqual(len(rows), 1)
            self.assertEqual(len(errors), 1)
            self.assertEqual(recorder.measurements, 2)


if __name__ == '__main__':
    unittest.main()
