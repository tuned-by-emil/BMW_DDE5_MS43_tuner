"""Change live selections at reply boundaries without rewriting captured rows."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import csv
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from bmw_bin_editor.live_logging import (
    FAST, BASE, CsvCapture, LiveLogger, load_parameters, make_groups,
    parameter_update_plan,
)
from bmw_bin_editor.native_live_logging import NativeLiveLogger
from bmw_bin_editor.ms43_live_logging import MS43LiveLogger
from bmw_bin_editor.serial_live_logging import SerialLiveLogger
from bmw_bin_editor.serial_transport import KlineSession, SerialReply
from test_live_logging import reply
from test_ms43_logging import packet

ROOT = Path(__file__).resolve().parents[1]
CAT = load_parameters(ROOT / 'definitions/live_parameters.json')
APP = QApplication.instance() or QApplication([])


def profile(parameters=FAST):
    return dict(version=1, ecu='D50M47A', name='Updates', parameters=list(parameters), interval_ms=40)


def read_rows(path):
    with Path(path).open(encoding='utf-8-sig', newline='') as handle:
        return list(csv.DictReader(handle))


class ParameterUpdateTests(unittest.TestCase):
    def test_block_cost_repeats_anchors_and_crosses_ten_value_boundary(self):
        fast = parameter_update_plan(FAST, CAT, current=FAST[:9])
        self.assertEqual((fast['old_blocks'], fast['new_blocks'], fast['adds_block']), (1, 1, False))
        extra = parameter_update_plan(FAST + [BASE[10]], CAT, current=FAST)
        self.assertEqual((extra['old_blocks'], extra['new_blocks'], extra['adds_block']), (1, 2, True))
        self.assertEqual([len(group) for group in extra['groups']], [10, 3])
        self.assertEqual(extra['groups'][1][:2], FAST[:2])

    def test_segment_preserves_rows_elapsed_time_and_avoids_existing_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'run.csv'
            old = CsvCapture(path, profile(), make_groups(FAST, CAT), CAT)
            old.append(0, 'first', 1, 'old', {CAT['INMOT']['result']: 1000}, {})
            original = path.read_bytes()
            collision = path.with_name('run.part2.csv')
            collision.write_text('keep')
            new = old.next_segment(profile(BASE), make_groups(BASE, CAT), CAT)
            self.assertEqual(new.path.name, 'run.part3.csv')
            self.assertTrue(old.handle.closed)
            new.append(12.345, 'later', 2, 'new', {CAT['ITKUM']['result']: 80}, {})
            new.close()
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(collision.read_text(), 'keep')
            self.assertEqual(read_rows(path)[0]['requestId'], 'old')
            row = read_rows(new.path)[0]
            self.assertEqual(row['time'], '12.345000000')
            self.assertEqual(row[CAT['INMOT']['result']], '')
            meta = json.loads(new.path.with_suffix('.csv.profile.json').read_text())
            self.assertEqual(meta['capture']['segment_of'], 'run.csv')

    def test_testo_inflight_reply_keeps_original_selection_until_next_request(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'run.csv'
            logger = LiveLogger(dict(profile=profile(), endpoint='ws://localhost', output=''), CAT)
            logger.active = True
            logger.socket_id = 'test'
            logger.capture = CsvCapture(path, logger.profile, logger.groups, CAT)
            sent, changed, samples, errors = [], [], [], []
            logger.send = sent.append
            logger.profileChanged.connect(changed.append)
            logger.sampleReady.connect(lambda t, values: samples.append(values))
            logger.error.connect(errors.append)
            logger.request_next()
            requested = logger.pending[0]
            logger.update_parameters(BASE)
            self.assertEqual(logger.profile['parameters'], FAST)
            logger.on_message(json.dumps(reply(FAST, requested)))
            logger.timer.stop()
            self.assertEqual(changed, [])
            self.assertEqual(set(samples[0]), {CAT[a]['result'] for a in FAST})
            logger.request_next()
            self.assertEqual(len(changed), 1)
            self.assertEqual(logger.profile['parameters'], BASE)
            self.assertEqual(sent[-1]['arg'], 'JA;' + ';'.join(make_groups(BASE, CAT)[0]))
            logger.stop()
            self.assertEqual(errors, [])
            self.assertEqual(len(read_rows(path)), 1)
            self.assertEqual(len(read_rows(path.with_name('run.part2.csv'))), 0)

    def test_stopping_with_pending_update_keeps_original_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'run.csv'
            logger = LiveLogger(dict(profile=profile(), endpoint='ws://localhost', output=''), CAT)
            logger.active = True
            logger.socket_id = 'test'
            logger.capture = CsvCapture(path, logger.profile, logger.groups, CAT)
            logger.send = lambda _message: None
            logger.request_next()
            logger.update_parameters(BASE)
            logger.stop()
            self.assertTrue(logger.capture.handle.closed)
            self.assertFalse(path.with_name('run.part2.csv').exists())

    def run_worker_update(self, transport):
        """The first read queues a change while a reply is still outstanding."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'run.csv'
            holder, errors, samples, changed, requests = {}, [], [], [], []
            config = dict(port='COM7', echo=True, output=str(path), profile=profile())
            if transport == 'ms43':
                config['profile'] = dict(version=1, ecu='MS43X001', name='Update',
                                         parameters=['N'], interval_ms=40)
                selected, extra_result = ['N', 'MAP'], 'MAP'
            else:
                selected, extra_result = FAST + [BASE[10]], CAT[BASE[10]]['result']

            def during_read(request):
                requests.append(request)
                if len(requests) == 1:
                    holder['logger'].update_parameters(selected)

            class Session:
                cleanup_error = None
                def __enter__(self): return self
                def __exit__(self, *_): pass
                def identify(self):
                    return dict(hardware=KlineSession.EXPECTED_HARDWARE,
                                software=KlineSession.EXPECTED_SOFTWARE)
                def read_measurements(self, identifiers):
                    during_read(identifiers)
                    payload = b'\x6c\x10' + b'\x20\x00' * len(identifiers)
                    return SerialReply(payload, b'request', b'response')
                def read(self):
                    during_read('ms43')
                    return SimpleNamespace(request_frame=b'request', response_frame=packet())
                def read_job(self, _ecu, _job, **kwargs):
                    group = kwargs['parameters'].split(';')[1:]
                    during_read(group)
                    data = reply(group)['results']
                    return dict(result_sets=[{r['name']: r['value'] for r in data}])

            if transport == 'native':
                logger = NativeLiveLogger(config, CAT, api_factory=lambda *_a, **_k: Session())
            else:
                logger_type = MS43LiveLogger if transport == 'ms43' else SerialLiveLogger
                logger = logger_type(config, CAT, session_factory=lambda *_a, **_k: Session())
            holder['logger'] = logger
            logger.error.connect(errors.append)
            logger.profileChanged.connect(changed.append)
            loop, deadline = QEventLoop(), QTimer()
            deadline.setSingleShot(True)
            deadline.timeout.connect(loop.quit)
            logger.stopped.connect(loop.quit)

            def sample(seconds, values):
                samples.append((seconds, values))
                if extra_result in values:
                    logger.stop()

            logger.sampleReady.connect(sample)
            try:
                logger.start()
                deadline.start(2000)
                loop.exec()
                self.assertTrue(logger.complete, 'Logger did not stop before deadline')
                self.assertEqual(errors, [])
                self.assertEqual(len(changed), 1)
                self.assertNotIn(extra_result, samples[0][1])
                self.assertIn(extra_result, samples[-1][1])
                self.assertEqual(logger.profile['parameters'], selected)
                old, new = read_rows(path), read_rows(path.with_name('run.part2.csv'))
                self.assertEqual(len(old), 1)
                self.assertEqual(len(old) + len(new), len(samples))
                self.assertGreater(float(new[0]['time']), float(old[0]['time']))
                self.assertEqual(logger.groups, [selected] if transport == 'ms43' else make_groups(selected, CAT))
            finally:
                deadline.stop()
                logger.stop()
                self.assertTrue(logger.worker.wait(2000))

    def test_native_thread_applies_new_block_between_replies(self):
        self.run_worker_update('native')

    def test_ms43_thread_adds_selection_without_reconnecting(self):
        self.run_worker_update('ms43')

    def test_direct_serial_changes_cached_selection_between_replies(self):
        self.run_worker_update('serial')


if __name__ == '__main__':
    unittest.main()
