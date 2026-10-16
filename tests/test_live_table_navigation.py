"""Real logger worker and map navigation with a timed in-memory cable."""

from optional_resources import requires_resource
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QEventLoop, QTimer, Qt
from PySide6.QtWidgets import QTreeWidgetItem
from test_serial_logging_gui import SerialLoggingGuiTests, CAT, PROFILE
from bmw_bin_editor.core import CalibrationDocument, XDFDatabase
from bmw_bin_editor.serial_live_logging import SerialLiveLogger
from bmw_bin_editor.serial_process import ProcessKlineSession
from serial_process_fixture import timed_session_factory


class NavigationTests(SerialLoggingGuiTests):
    @requires_resource('definitions/EDC16C31_361830_v1_22_R005_REVIEW.xdf')
    def test_timed_logging_continues_while_selecting_tables(self):
        gui, window = self.window()
        root=Path(__file__).resolve().parents[1]
        window.xdf=XDFDatabase(root/'definitions/EDC16C31_361830_v1_22_R005_REVIEW.xdf')
        binary=Path(self.files.name)/'synthetic.bin'
        binary.write_bytes(bytes(0x100000))
        window.document=CalibrationDocument(window.xdf,binary)
        window.editor.set_document(window.document)
        sessions=[]
        def session_factory(port, *, echo, cancelled):
            session=ProcessKlineSession(port,echo=echo,cancelled=cancelled,session_factory=timed_session_factory)
            sessions.append(session)
            return session
        def factory(config,catalog,parent):
            return SerialLiveLogger(config,catalog,parent,session_factory=session_factory)
        panel=window.editor.log_panel
        errors=[]
        chosen=[]
        samples=[]
        definitions=[next(d for d in window.xdf.tables if d.z_address==address)
                     for address in (0xF7F44,0xEAA50,0xF9394)]
        loop=QEventLoop()
        timer=QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        def sample(seconds,values):
            samples.append(seconds)
            if len(samples) in (3,6,9):
                definition=definitions[len(chosen)]
                item=QTreeWidgetItem([definition.title])
                item.setData(0,Qt.UserRole,definition.unique_id or f'{definition.kind}:{definition.z_address:X}')
                window.open_tree_item(item,0)
                chosen.append(definition)
            if len(samples)>=12:
                panel.stop_live()
        with patch.object(gui,'SerialLiveLogger',side_effect=factory), patch.object(gui.QMessageBox,'warning') as warning:
            panel.start_direct_live(dict(profile=PROFILE,transport='serial',port='COM7',echo=True,output=''),CAT)
            logger=panel.direct_logger
            logger.sampleReady.connect(sample)
            logger.error.connect(errors.append)
            logger.stopped.connect(loop.quit)
            timer.start(7000)
            try:
                loop.exec()
            finally:
                timer.stop()
                logger.stop()
                self.assertTrue(logger.worker.wait(1500))
            self.assertFalse(errors)
            warning.assert_not_called()
            self.assertEqual(len(chosen),3)
            self.assertGreaterEqual(len(samples),12)
            self.assertTrue(sessions)
            self.assertTrue(all(session.process is None and session.connection is None for session in sessions))


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromName('test_timed_logging_continues_while_selecting_tables', NavigationTests)
