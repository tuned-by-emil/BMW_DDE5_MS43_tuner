"""Maximum polling without losing profile preferences, samples or CSV rows."""
import csv
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from test_serial_live_logging import SerialLiveLoggingTests, CableRecorder, CAT, profile
from test_serial_logging_gui import SerialLoggingGuiTests
from bmw_bin_editor.live_logging import CsvCapture, FAST, BASE, make_groups, decode_reply
from test_live_logging import reply


class WorkerPerformanceTests(SerialLiveLoggingTests):
    # Reuse the real Qt/transport integration harness; only these tests are collected here.
    def test_maximum_rate_overrides_worker_delay_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'maximum.csv'
            original = {}
            def enable(config, catalog):
                original.update(config['profile'])
            # The helper below supplies max_rate before worker construction.
            recorder = CableRecorder()
            samples, errors, rows, statuses = self.run_logger(recorder, path,
                interval=5000, max_rate=True, after_construct=enable)
            self.assertFalse(errors)
            self.assertEqual(len(samples), 2)
            self.assertEqual(len(rows), 2)
            self.assertEqual(original['interval_ms'], 5000)
            metadata = json.loads(path.with_suffix('.csv.profile.json').read_text())
            self.assertEqual(metadata['profile']['interval_ms'], 0)
            self.assertEqual(metadata['capture']['csv_flush_interval_seconds'], .25)
            self.assertEqual(metadata['capture']['polling'], 'single_cached_group')
            self.assertEqual(recorder.payloads[-1], b'\x2c\x10')

    def test_fifteen_unique_channels_rotate_ten_and_seven(self):
        channels = BASE + ['IUBAT']
        self.assertEqual(len(set(channels)), 15)
        self.assertEqual([len(g) for g in make_groups(channels, CAT)], [10, 7])
        with tempfile.TemporaryDirectory() as directory:
            recorder = CableRecorder()
            samples, errors, rows, _ = self.run_logger(recorder, Path(directory)/'rotate.csv',
                parameters=channels, max_rate=True, samples_to_stop=4)
            self.assertFalse(errors)
            self.assertGreaterEqual(len(samples), 4)
            # A worker read can finish and reach CSV while its sample is still
            # queued for the GUI. Stop suppresses that queued GUI delivery;
            # retaining the completed CSV row is correct.
            self.assertGreaterEqual(len(rows), len(samples))
            self.assertEqual([row['group'] for row in rows],
                             [str(index % 2 + 1) for index in range(len(rows))])
            groups = make_groups(channels, CAT)
            full = [b'\x2c\x10' + b''.join(int(CAT[arg]['id'], 16).to_bytes(2, 'big')
                                         for arg in group) for group in groups]
            requests = recorder.payloads[2:]
            self.assertEqual(requests, [full[index % 2] for index in range(len(requests))])
            self.assertEqual([bytes.fromhex(row['_TEL_AUFTRAG'])[4:-1] for row in rows],
                             requests[:len(rows)])
            # One owner issues one read at a time and exits immediately after
            # an interrupted reply, so at most its last request can lack CSV.
            self.assertGreaterEqual(len(requests), len(rows))
            self.assertLessEqual(len(requests), len(rows) + 1)


class PreferenceTests(SerialLoggingGuiTests):
    def test_max_rate_retains_profile_interval_and_other_transports(self):
        dialog = self.dialog()
        dialog.interval.setValue(500)
        dialog.transport.setCurrentIndex(2)
        self.assertTrue(dialog.serial_max_rate.isChecked())
        self.assertFalse(dialog.interval.isEnabled())
        self.assertEqual(dialog.config()['profile']['interval_ms'], 500)
        self.assertTrue(dialog.config()['max_rate'])
        dialog.serial_max_rate.setChecked(False)
        self.assertTrue(dialog.interval.isEnabled())
        dialog.accept()
        second = self.dialog()
        self.assertFalse(second.serial_max_rate.isChecked())
        second.transport.setCurrentIndex(1)
        self.assertFalse(second.serial_max_rate.isEnabled())
        self.assertTrue(second.interval.isEnabled())


class BufferedCaptureTests(unittest.TestCase):
    def test_buffer_flush_and_final_close_preserve_rows_and_context(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'buffer.csv'
            context = {'transport':'direct_serial'}
            groups = make_groups(FAST, CAT)
            with patch('bmw_bin_editor.live_logging.time.monotonic', return_value=100):
                capture = CsvCapture(path, profile(), groups, CAT, flush_interval=.25, context=context)
                values, fields = decode_reply(reply(groups[0]), groups[0], CAT)
                with patch.object(capture.handle, 'flush', wraps=capture.handle.flush) as flush:
                    capture.append(0,'now',1,'one',values,fields)
                    flush.assert_not_called()
                    with patch('bmw_bin_editor.live_logging.time.monotonic', return_value=100.3):
                        capture.append(.3,'now',1,'two',values,fields)
                    flush.assert_called_once()
                    capture.append(.4,'now',1,'three',values,fields)
                capture.close()
            self.assertEqual(context, {'transport':'direct_serial'})
            with path.open(encoding='utf-8-sig') as handle:
                self.assertEqual(len(list(csv.DictReader(handle))), 3)


def load_tests(loader, tests, pattern):
    # Avoid collecting inherited cases already covered by the main test modules.
    suite = unittest.TestSuite()
    for cls in (WorkerPerformanceTests, PreferenceTests, BufferedCaptureTests):
        for name in cls.__dict__:
            if name.startswith('test_'):
                suite.addTest(cls(name))
    return suite
