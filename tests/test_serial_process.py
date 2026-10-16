"""Actual spawn/IPC with pure in-memory serial; never uses a physical cable."""
import multiprocessing
import time
import unittest
from bmw_bin_editor.serial_process import ProcessKlineSession
from bmw_bin_editor.serial_transport import SerialCancelled, SerialCommunicationError
from serial_process_fixture import timed_session_factory


class ProcessTests(unittest.TestCase):
    def session(self, **kwargs):
        return ProcessKlineSession('COM7',session_factory=timed_session_factory,**kwargs)

    def test_identity_and_selection_cache_and_process_cleanup(self):
        session=self.session()
        with session:
            self.assertEqual(session.identify()['software'],'1037361830')
            first=session.read_measurements([0x2d,0x55])
            repeat=session.read_measurements([0x2d,0x55])
            changed=session.read_measurements([0x2d,0x3a])
            self.assertFalse(first.cached_selection)
            self.assertTrue(repeat.cached_selection)
            self.assertEqual(repeat.request_frame[4:-1],b'\x2c\x10')
            self.assertFalse(changed.cached_selection)
            self.assertEqual(changed.payload,b'\x6c\x10\x00\x00\x00\x00')
        self.assertIsNone(session.process)
        self.assertIsNone(session.connection)

    def test_remote_error_faults_without_reusing_a_selection(self):
        session=self.session()
        with session:
            session.identify()
            with self.assertRaisesRegex(SerialCommunicationError,'Simulated cable fault'):
                session.read_measurements([0xff])
            with self.assertRaisesRegex(SerialCommunicationError,'failed'):
                session.read_measurements([0x2d])

    def test_cancellation_closes_spawned_process(self):
        cancelled=[]
        session=self.session(cancelled=lambda:bool(cancelled))
        with session:
            session.identify()
            cancelled.append(True)
            with self.assertRaises(SerialCancelled):
                session.read_measurements([0x2d])
        self.assertIsNone(session.process)

    def test_cancelled_start_never_spawns(self):
        session=self.session(cancelled=lambda:True)
        with self.assertRaises(SerialCancelled):
            session.__enter__()
        self.assertIsNone(session.process)

    def test_default_logger_uses_process_transport(self):
        from bmw_bin_editor.serial_live_logging import SerialLiveLogger
        from test_serial_live_logging import profile, CAT
        logger=SerialLiveLogger(dict(profile=profile(),port='COM7',echo=True,output=''),CAT)
        self.assertIs(logger.worker.session_factory,ProcessKlineSession)
        logger.deleteLater()
