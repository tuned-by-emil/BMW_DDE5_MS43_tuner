import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import csv, json, tempfile, unittest, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QEventLoop, QTimer, QSettings
from PySide6.QtNetwork import QHostAddress
from PySide6.QtWebSockets import QWebSocketServer
from bmw_bin_editor.live_logging import (LiveLogger, LoggingDialog, make_groups,
    validate_profile, decode_reply, load_parameters, BASE, CsvCapture)
from bmw_bin_editor.logdata import TestOLog

APP=QApplication.instance() or QApplication([])
CAT=load_parameters(Path(__file__).resolve().parents[1]/'definitions/live_parameters.json')
PROFILE=dict(version=1,ecu='D50M47A',name='Test',parameters=BASE,interval_ms=0)

def reply(group, request_id='live-0', short=False):
    results=[dict(name='JOB_STATUS',value='OKAY'),dict(name='_TEL_AUFTRAG',value='B812F1162C10'+('' if short else ''.join(CAT[a]['id'][2:] for a in group))),dict(name='_TEL_ANTWORT',value='test')]
    results += [dict(name=CAT[a]['result'],value=str(100+list(CAT).index(a))) for a in group]
    return dict(ack='runJobOnceResult',ecu='D50M47A',jobname='STATUS_MESSWERTBLOCK_LESEN',requestId=request_id,results=results)

class Tests(unittest.TestCase):
    def test_profiles_and_groups(self):
        groups=make_groups(BASE,CAT)
        self.assertEqual(list(map(len,groups)),[10,6])
        self.assertTrue(all(g[:2]==['INMOT','IMEIA'] for g in groups))
        self.assertEqual(set(sum(groups,[])),set(BASE))
        self.assertEqual(validate_profile(json.loads(json.dumps(PROFILE)),CAT),PROFILE)
        for p in [dict(PROFILE,ecu='OTHER'),dict(PROFILE,parameters=['bad']),dict(PROFILE,interval_ms=-1)]:
            with self.assertRaises(ValueError):validate_profile(p,CAT)

    def test_reject_cached_or_partial(self):
        group=make_groups(BASE,CAT)[0]
        self.assertEqual(len(decode_reply(reply(group),group,CAT)[0]),10)
        with self.assertRaises(ValueError):decode_reply(reply(group,short=True),group,CAT)
        m=reply(group);m['results'].pop()
        with self.assertRaises(KeyError):decode_reply(m,group,CAT)

    def test_csv_precision_sparse_and_long_interval(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'log.csv';groups=make_groups(BASE,CAT)
            c=CsvCapture(path,PROFILE,groups,CAT)
            for t,g in [(0,0),(30.123456,1)]:
                values,fields=decode_reply(reply(groups[g]),groups[g],CAT)
                c.append(t,'2026-09-11T12:00:00.123456+02:00',g+1,str(g),values,fields)
            c.close()
            with path.open(encoding='utf-8-sig') as f:rows=list(csv.DictReader(f))
            self.assertEqual(rows[1]['time'],'30.123456000')
            self.assertEqual(rows[1][CAT['IPLAD']['result']],'')
            log=TestOLog(path)
            self.assertAlmostEqual(log.duration,30.123456)
            with self.assertRaises(FileExistsError):CsvCapture(path,PROFILE,groups,CAT)

    def run_server(self, corrupt=False, disconnect=False):
        server=QWebSocketServer('test',QWebSocketServer.NonSecureMode)
        self.assertTrue(server.listen(QHostAddress.SpecialAddress.LocalHost,0))
        peers=[];args=[];errors=[];received=[];stopped=[]
        with tempfile.TemporaryDirectory() as td:
            output=Path(td)/'capture.csv'
            logger=LiveLogger(dict(profile=PROFILE,endpoint=f'ws://127.0.0.1:{server.serverPort()}',output=str(output)),CAT)
            loop=QEventLoop()
            def new_peer():
                ws=server.nextPendingConnection();peers.append(ws)
                ws.sendTextMessage(json.dumps(dict(connected=True,socketId=123)))
                def message(text):
                    m=json.loads(text)
                    if m['cmd']=='listJobs':ws.sendTextMessage(json.dumps(dict(ack='listJobs',requestId=m['requestId'],jobs=[])))
                    else:
                        self.assertEqual(m['cmd'],'runJobOnce')
                        self.assertTrue(m['arg'].startswith('JA;'))
                        args.append(m['arg'])
                        if disconnect:ws.close();return
                        result=reply(m['arg'].split(';')[1:],m['requestId'],short=corrupt)
                        ws.sendTextMessage(json.dumps(result))
                        ws.sendTextMessage(json.dumps(result)) # same request delivered twice
                        broadcast=dict(result,requestorId='ws-once-123')
                        broadcast.pop('ack')
                        ws.sendTextMessage(json.dumps(broadcast))
                ws.textMessageReceived.connect(message)
            server.newConnection.connect(new_peer)
            def sample(t,d):
                received.append((t,d))
                if len(received)==4:logger.stop()
            logger.sampleReady.connect(sample)
            logger.error.connect(errors.append)
            logger.stopped.connect(lambda: (stopped.append(True),QTimer.singleShot(0,loop.quit)))
            logger.start();QTimer.singleShot(3000,loop.quit);loop.exec()
            logger.stop()
            self.assertTrue(stopped)
            if corrupt or disconnect:
                self.assertTrue(errors);self.assertFalse(received)
            else:
                self.assertFalse(errors,errors);self.assertEqual(len(received),4)
                self.assertNotEqual(args[0],args[1]);self.assertEqual(args[0],args[2])
                with output.open(encoding='utf-8-sig') as f:self.assertEqual(len(list(csv.DictReader(f))),4)
            for ws in peers:ws.abort()
            server.close()

    def test_round_robin_and_duplicate_filter(self):self.run_server()
    def test_corruption_stops_before_recording(self):self.run_server(corrupt=True)
    def test_disconnect(self):self.run_server(disconnect=True)

    def test_dialog(self):
        import bmw_bin_editor.live_logging as mod
        with tempfile.TemporaryDirectory() as td:
            old=mod.QSettings
            mod.QSettings=lambda *a:QSettings(str(Path(td)/'profiles.ini'),QSettings.IniFormat)
            try:
                d=LoggingDialog(CAT);self.assertEqual(len(d.selected()),14)
                d.saved['Custom']=dict(PROFILE,name='Custom');d.persist()
                second=LoggingDialog(CAT);self.assertIn('Custom',second.saved)
                second.profile_box.setCurrentText('Custom');self.assertEqual(second.profile()['name'],'Custom')
                d.show();APP.processEvents();d.grab().save(str(Path(__file__).resolve().parents[2]/'logging126.png'))
                d.close();second.close()
            finally:mod.QSettings=old

if __name__=='__main__':unittest.main()
