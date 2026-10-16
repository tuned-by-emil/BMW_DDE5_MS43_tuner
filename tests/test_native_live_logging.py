import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import ctypes as ct
import csv, tempfile, time, unittest
from pathlib import Path
from PySide6.QtCore import QEventLoop, QTimer, QSettings
from PySide6.QtWidgets import QApplication
from bmw_bin_editor.ecu_programming import NativeEdiabas, ProgrammingError
from bmw_bin_editor.native_live_logging import NativeLiveLogger
from bmw_bin_editor.live_logging import BASE, load_parameters, LoggingDialog, make_groups
from test_ecu_programming import FakeApi, Function

ROOT=Path(__file__).resolve().parents[1]
CAT=load_parameters(ROOT/'definitions/live_parameters.json')
PROFILE=dict(version=1,ecu='D50M47A',name='Native test',parameters=BASE,interval_ms=50)
APP=QApplication.instance() or QApplication([])

class MeasurementApi(FakeApi):
    def __init__(self, states=(1,)):
        super().__init__(states=states)
        self.rows=[{}, {'JOB_STATUS':b'OKAY', '_TEL_AUFTRAG':bytes.fromhex('B812F12C100019000C'), '_TEL_ANTWORT':b'\x6c\x10\x00\x01', 'VALUE':b'125.5'}]
        setattr(self,'__apiResultNumber',Function(lambda h,p,s:self.output(p,len(self.rows[s]))))
        setattr(self,'__apiResultName',Function(lambda h,b,i,s:self.text(b,list(self.rows[s])[i-1].encode())))
        setattr(self,'__apiResultText',Function(lambda h,b,n,s,f:self.text(b,self.rows[s][n.decode()])))
        def binary(h,b,p,n,s):
            raw=self.rows[s][n.decode()]
            ct.memmove(b,raw,len(raw));p._obj.value=len(raw);return 1
        setattr(self,'__apiResultBinary',Function(binary))

class ReaderApi:
    calls=[]
    closed=False
    corrupt=False
    busy=False
    def __init__(self,*_):pass
    def __enter__(self):return self
    def __exit__(self,*_):type(self).closed=True
    def read_job(self,ecu,job,timeout,parameters,cancelled):
        type(self).calls.append(parameters)
        if self.busy:
            while not cancelled():time.sleep(.005)
            raise ProgrammingError('Cancelled')
        group=parameters.split(';')[1:]
        row={'JOB_STATUS':'OKAY','_TEL_AUFTRAG':'B812F12C10'+('' if self.corrupt else ''.join(CAT[a]['id'][2:] for a in group)), '_TEL_ANTWORT':'6C10'}
        row.update({CAT[a]['result']:str(100+len(type(self).calls)) for a in group})
        return {'result_sets':[{},row]}

class NativeTests(unittest.TestCase):
    def test_native_parameters_and_binary_telegrams(self):
        fake=MeasurementApi()
        with NativeEdiabas(library=fake) as api:
            rows=api.read_job('D50M47A','STATUS_MESSWERTBLOCK_LESEN',parameters='JA;INMOT;IMEIA')['result_sets']
        self.assertEqual(fake.jobs[0][3],b'JA;INMOT;IMEIA')
        self.assertEqual(rows[1]['_TEL_AUFTRAG'],'B812F12C100019000C')
        self.assertEqual(rows[1]['_TEL_ANTWORT'],'6C100001')
        self.assertEqual(rows[1]['VALUE'],'125.5')
        self.assertEqual(fake.ended,[42])

    def test_reject_parameters_before_job(self):
        fake=MeasurementApi()
        with NativeEdiabas(library=fake) as api:
            for args in ['', 'NEIN;INMOT','JA;INMOT;','JA;'+';'.join(['INMOT']*11),'JA;INMOT\x00']:
                with self.assertRaises(ProgrammingError):
                    api.read_job('D50M47A','STATUS_MESSWERTBLOCK_LESEN',parameters=args)
            with self.assertRaises(ProgrammingError):api.read_job('28dde504','IDENT',parameters='JA;INMOT')
        self.assertEqual(fake.jobs,[])

    def test_cancel_inflight_aborts_and_releases(self):
        fake=MeasurementApi(states=[0]);cancel=iter([False,False,True])
        with NativeEdiabas(library=fake) as api:
            with self.assertRaisesRegex(ProgrammingError,'cancelled'):
                api.read_job('D50M47A','STATUS_MESSWERTBLOCK_LESEN',parameters='JA;INMOT',cancelled=lambda:next(cancel))
        self.assertEqual(fake.aborted,[42]);self.assertEqual(fake.ended,[42])

    def run_logger(self, *, corrupt=False,busy=False):
        ReaderApi.calls=[];ReaderApi.closed=False;ReaderApi.corrupt=corrupt;ReaderApi.busy=busy
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'capture.csv'
            logger=NativeLiveLogger(dict(profile=PROFILE,output=str(path)),CAT,api_factory=ReaderApi)
            samples=[];errors=[];stopped=[];loop=QEventLoop()
            def sample(t,values):
                samples.append(values)
                if len(samples)==2:logger.stop()
            logger.sampleReady.connect(sample);logger.error.connect(errors.append)
            logger.stopped.connect(lambda:(stopped.append(not logger.worker.isRunning()),loop.quit()))
            logger.start()
            if busy:QTimer.singleShot(50,logger.stop)
            QTimer.singleShot(2000,loop.quit);loop.exec()
            if logger.worker.isRunning():logger.stop();logger.worker.wait(1000)
            self.assertEqual(stopped,[True]);self.assertTrue(ReaderApi.closed)
            self.assertTrue(logger.stop())
            with path.open(encoding='utf-8-sig') as f:rows=list(csv.DictReader(f))
            if corrupt or busy:
                self.assertEqual(rows,[]);self.assertEqual(samples,[])
                self.assertEqual(bool(errors),corrupt)
            else:
                self.assertEqual(errors,[]);self.assertEqual(len(rows),2)
                self.assertEqual(ReaderApi.calls[:2],['JA;'+';'.join(g) for g in make_groups(BASE,CAT)])
                self.assertEqual(rows[1][CAT['IPLAD']['result']],'')
                self.assertEqual(rows[0]['group'],'1');self.assertEqual(rows[1]['group'],'2')
            logger.deleteLater();APP.processEvents()

    def test_direct_group_rotation_and_csv(self):self.run_logger()
    def test_cached_selection_rejected(self):self.run_logger(corrupt=True)
    def test_stop_busy_session(self):self.run_logger(busy=True)

    def test_native_dialog_does_not_require_websocket(self):
        from unittest.mock import patch
        import bmw_bin_editor.live_logging as module
        with tempfile.TemporaryDirectory() as td:
            settings=QSettings(str(Path(td)/'test.ini'),QSettings.IniFormat)
            with patch.object(module,'editor_settings',lambda _:settings):
                dialog=LoggingDialog(CAT)
                dialog.save_csv.setChecked(False)
                dialog.bin_dir.setText(td);dialog.endpoint.setText('invalid websocket')
                self.assertEqual(dialog.config()['transport'],'ediabas')
                dialog.transport.setCurrentIndex(1)
                with self.assertRaises(ValueError):dialog.config()
                dialog.close()

    def test_editor_defers_close_until_native_thread_exits(self):
        from unittest.mock import patch
        import bmw_bin_editor.gui as gui
        import bmw_bin_editor.settings as settings_module
        with tempfile.TemporaryDirectory() as td:
            settings=QSettings(str(Path(td)/'ui.ini'),QSettings.IniFormat)
            with patch.object(gui,'editor_settings',lambda _:settings), patch.object(settings_module,'editor_settings',lambda _:settings):
                window=gui.MainWindow()
                ReaderApi.calls=[];ReaderApi.closed=False;ReaderApi.busy=True;ReaderApi.corrupt=False
                factory=lambda config,catalog,parent:NativeLiveLogger(config,catalog,parent,api_factory=ReaderApi)
                with patch.object(gui,'NativeLiveLogger',factory):
                    panel=window.editor.log_panel
                    panel.start_direct_live(dict(profile=PROFILE,transport='ediabas',output=''),CAT)
                    logger=panel.direct_logger
                    deadline=time.monotonic()+2
                    while not ReaderApi.calls and time.monotonic()<deadline:
                        APP.processEvents();time.sleep(.005)
                    self.assertTrue(ReaderApi.calls)
                    window.close()
                    self.assertTrue(window._closing_after_logging)
                    while panel.direct_logger is not None and time.monotonic()<deadline:
                        APP.processEvents();time.sleep(.005)
                    self.assertIsNone(panel.direct_logger)
                    self.assertTrue(ReaderApi.closed)
                    self.assertTrue(panel.live_button.isEnabled())
                window.close();window.deleteLater();APP.processEvents()

if __name__=='__main__':unittest.main()
