
import unittest as _optional_unittest
from pathlib import Path as _OptionalPath
if not (_OptionalPath(__file__).resolve().parents[1] / 'definitions/ms43x/Siemens_MS43_MS43X001_Logging.adx').is_file():
    raise _optional_unittest.SkipTest('Optional local calibration resource not installed: definitions/ms43x/Siemens_MS43_MS43X001_Logging.adx')

import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import json
import math
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import xml.etree.ElementTree as ET
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication
from bmw_bin_editor import gui
from bmw_bin_editor.adx import bundled_ms43_adx_path
from bmw_bin_editor.ms43_logging_definition import (MS43Definition,compile_expression,DATA_REQUEST,FAST_REQUEST,SLOW_REQUEST,ACK)
from bmw_bin_editor.ms43_serial import MS43Session
from bmw_bin_editor.ms43_live_logging import MS43Reader,MS43LiveLogger,DEFAULT_CHANNELS
from bmw_bin_editor.ms43_logging_gui import MS43LoggingDialog
from bmw_bin_editor.serial_transport import xor_checksum,SerialCommunicationError,SerialCancelled
from bmw_bin_editor.settings import ECUSettings
from bmw_bin_editor.logdata import TestOLog,profile_for_definition
from bmw_bin_editor.core import XDFDatabase

APP=QApplication.instance() or QApplication([])
ROOT=Path(__file__).resolve().parents[1]


def packet():
    data=bytearray(73)
    data[0]=6;data[2]=2;data[3]=4
    data[4:6]=((3<<13)|3000).to_bytes(2,'little')
    data[8:10]=(12000).to_bytes(2,'little')
    data[0x10:0x12]=(8*256).to_bytes(2,'little')
    data[0x26:0x28]=(600).to_bytes(2,'little');data[0x28:0x2a]=(750).to_bytes(2,'little')
    data[0x2a]=128;data[0x2b]=64;data[0x2c:0x32]=bytes([128]*6)
    data[0x32:0x38]=bytes([128]*6)
    data[0x3f]=140
    body=bytes.fromhex('124DA0')+data
    return body+bytes([xor_checksum(body)])


class Clock:
    def __init__(self):self.now=0.
    def __call__(self):return self.now
    def sleep(self,d):self.now+=d


class Cable:
    def __init__(self,clock,*,echo=True,frame=None,chunk=7,fail_ack=False,on_data=None):
        self.clock=clock;self.echo=echo;self.frame=packet() if frame is None else frame
        self.chunk=chunk;self.fail_ack=fail_ack;self.on_data=on_data
        self.rx=bytearray();self.tx=[];self.baudrate=9600;self.is_open=False;self.closed=False
        self.kwargs={};self.reads=0
    def factory(self,**kwargs):self.kwargs=kwargs;self.baudrate=kwargs['baudrate'];return self
    def open(self):self.is_open=True
    def reset_input_buffer(self):self.rx.clear()
    def write(self,data):
        self.tx.append((self.baudrate,data))
        if self.echo:self.rx.extend(data)
        if data==DATA_REQUEST:
            self.rx.extend(self.frame)
            if self.on_data:self.on_data()
        elif not self.fail_ack:self.rx.extend(ACK)
        return len(data)
    def read(self,n):
        self.clock.sleep(.0001)
        data=bytes(self.rx[:min(n,self.chunk)]);del self.rx[:len(data)];self.reads+=1
        return data
    def close(self):self.closed=True;self.is_open=False


class DecoderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.definition=MS43Definition()
    def test_little_endian_overlay_units_linked_values_and_digital_states(self):
        v=self.definition.decode_frame(packet())
        self.assertEqual(len(v),131)
        self.assertEqual(v['N'],3000)
        self.assertAlmostEqual(v['MAP'],12000*.0082921489)
        self.assertEqual(v['ACTIVEVE'],8)
        self.assertEqual(v['FLSTATE'],1);self.assertEqual(v['PLSTATE'],0);self.assertEqual(v['VANOSACTIVE'],1)
        self.assertEqual(v['OBPROTSTATE'],1)
        self.assertAlmostEqual(v['TIAVG'],3.6,places=5)
        self.assertAlmostEqual(v['INJDC'],9.,places=5)
        self.assertEqual(v['IGAAVG'],24.)
        self.assertEqual(v['IGAKNKAVG'],0.)
        self.assertEqual(v['LUTVE1'],0);self.assertEqual(v['LUTVE8'],1)
        self.assertEqual(v['MAPVE1'],0);self.assertEqual(v['MAPVE8'],v['MAP'])
        self.assertEqual(v['NIS'],0);self.assertEqual(v['NFL'],3000)
    def test_all_lookup_keys_and_unknown_state(self):
        for table in range(1,9):
            frame=bytearray(packet());frame[3+0x10:3+0x12]=(table*256).to_bytes(2,'little')
            frame[-1]=xor_checksum(frame[:-1]);v=self.definition.decode_frame(bytes(frame))
            for i in range(1,9):self.assertEqual(v['MAPVE'+str(i)],v['MAP'] if i==table else 0)
        self.assertTrue(math.isnan(self.definition.decode_payload(bytes(73))['LUTVE1']))
    def test_corrupt_checksum_truncated_wrong_address_rejected(self):
        for frame in [packet()[:-1],b'\x13'+packet()[1:],packet()[:-1]+bytes([packet()[-1]^1])]:
            with self.assertRaises(SerialCommunicationError):self.definition.decode_frame(frame)
    def test_no_executable_expressions_and_links_validated_before_com(self):
        for formula in ["__import__('os')",'X.__class__','X[0]','2**10000000']:
            with self.assertRaises(ValueError):compile_expression(formula,{'X'})
        with tempfile.TemporaryDirectory() as td:
            root=ET.parse(bundled_ms43_adx_path());root.find('ADXVALUE/MATH/VAR').set('type','link')
            root.find('ADXVALUE/MATH/VAR').set('linkIDHash','0xDEADBEEF')
            p=Path(td)/'bad.adx';root.write(p)
            with self.assertRaises(ValueError):MS43Definition(p)


class TransportTests(unittest.TestCase):
    def session(self,cable,clock,**kw):
        return MS43Session('COM7',serial_factory=cable.factory,clock=clock,sleep=clock.sleep,**kw)
    def test_echo_chunked_reply_handshake_and_shutdown_at_correct_rates(self):
        clock=Clock();cable=Cable(clock,chunk=1);events=[]
        with self.session(cable,clock,trace=lambda **e:events.append(e)) as s:
            v=MS43Definition().decode_frame(s.read().response_frame);self.assertEqual(v['N'],3000)
        self.assertEqual(cable.tx,[(9600,FAST_REQUEST),(125000,DATA_REQUEST),(125000,SLOW_REQUEST)])
        self.assertEqual(cable.baudrate,9600);self.assertTrue(cable.closed)
        self.assertEqual(cable.kwargs['parity'],'E');self.assertFalse(cable.dtr);self.assertFalse(cable.rts)
        self.assertEqual([e['baud'] for e in events if e['event']=='baud'],[125000,9600])
    def test_no_echo_and_already_fast_connection(self):
        clock=Clock();cable=Cable(clock,echo=False)
        with self.session(cable,clock,echo=False,initial_baud=125000) as s:s.read()
        self.assertEqual(cable.tx,[(125000,DATA_REQUEST),(125000,SLOW_REQUEST)])
    def test_corrupt_data_stops_and_still_resets_baud(self):
        clock=Clock();bad=packet()[:-1]+bytes([packet()[-1]^1]);cable=Cable(clock,frame=bad)
        with self.assertRaises(SerialCommunicationError):
            with self.session(cable,clock) as s:s.read()
        self.assertEqual(cable.tx[-1],(125000,SLOW_REQUEST));self.assertTrue(cable.closed)
    def test_ack_timeout_closes_without_polling(self):
        clock=Clock();cable=Cable(clock,fail_ack=True)
        with self.assertRaises(SerialCommunicationError):
            with self.session(cable,clock):self.fail('must not connect')
        self.assertEqual(cable.tx,[(9600,FAST_REQUEST)]);self.assertTrue(cable.closed)
    def test_echo_mismatch_is_reported(self):
        clock=Clock();cable=Cable(clock,echo=False)
        with self.assertRaises(SerialCommunicationError):
            with self.session(cable,clock):pass
        self.assertTrue(cable.closed)
    def test_cancellation_cleanup_is_bounded_and_ignores_cancel_flag(self):
        clock=Clock();cable=Cable(clock);state={'cancel':False}
        with self.session(cable,clock,cancelled=lambda:state['cancel']) as s:
            state['cancel']=True
            with self.assertRaises(SerialCancelled):s.read()
        self.assertIsNone(s.cleanup_error);self.assertTrue(cable.closed)
        self.assertEqual(cable.tx[-1],(125000,SLOW_REQUEST))
    def test_reset_failure_is_explicit_and_port_still_closes(self):
        clock=Clock();cable=Cable(clock)
        with self.session(cable,clock) as s:cable.fail_ack=True
        self.assertIsNotNone(s.cleanup_error);self.assertTrue(cable.closed)


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.folder=Path(self.tmp.name)
        self.definition=MS43Definition()
    def tearDown(self):self.tmp.cleanup()
    def config(self):
        return dict(port='COM7',echo=True,initial_baud=9600,output=str(self.folder/'run.csv'),
            profile=dict(version=1,ecu='MS43X001',name='Test',parameters=['N','MAP','ACTIVEVE','FLSTATE','INJDC'],interval_ms=0))
    def reader(self,config):
        clock=Clock();cable=Cable(clock);holder={};data_count=[0]
        def on_data():
            data_count[0]+=1
            if data_count[0]==4:holder['reader'].requestInterruption()
        cable.on_data=on_data
        def session(port,**kw):return MS43Session(port,**kw,serial_factory=cable.factory,clock=clock,sleep=clock.sleep)
        reader=MS43Reader(config,self.definition.catalog,session_factory=session);holder['reader']=reader
        return reader,cable,session
    def test_reader_records_raw_traffic_metadata_and_replayable_csv(self):
        reader,cable,_=self.reader(self.config());errors=[];reader.error.connect(errors.append)
        reader.start();self.assertTrue(reader.wait(3000));APP.processEvents()
        self.assertEqual(errors,[]);self.assertEqual(reader.count,3)
        log=TestOLog(self.folder/'run.csv',self.definition.signal_catalog)
        self.assertEqual(log.sample_count,3);self.assertEqual(log.values['N'].tolist(),[3000.]*3)
        self.assertEqual(log.values['FLSTATE'].tolist(),[1.]*3)
        metadata=json.loads((self.folder/'run.csv.profile.json').read_text())
        self.assertEqual(metadata['capture']['adx_sha256'],self.definition.sha256)
        events=[json.loads(line) for line in (self.folder/'run.csv.ds2.jsonl').read_text().splitlines()]
        self.assertTrue(any(e.get('hex')==FAST_REQUEST.hex().upper() for e in events))
        self.assertTrue(any(e.get('hex')==SLOW_REQUEST.hex().upper() for e in events))
        self.assertTrue(cable.closed)
    def test_dialog_search_and_saved_profiles_are_ecu_scoped(self):
        base=QSettings(str(self.folder/'settings.ini'),QSettings.IniFormat)
        settings=ECUSettings(base,'ms43-test')
        dialog=MS43LoggingDialog(settings=settings);dialog.preview.setChecked(True)
        self.assertEqual(dialog.channels.topLevelItemCount(),131)
        dialog.search.setText('Engine Speed')
        visible=[dialog.channels.topLevelItem(i).text(0) for i in range(131) if not dialog.channels.topLevelItem(i).isHidden()]
        self.assertIn('Engine Speed',visible);self.assertNotIn('Coolant Temperature',visible)
        dialog.select(['N','MAP']);profile=dialog.profile('Custom');dialog.saved['Custom']=profile;dialog.persist()
        other=MS43LoggingDialog(settings=settings);self.assertIn('Custom',other.saved)
        self.assertFalse(ECUSettings(base,'361830').contains('ms43/profiles'))
        self.assertEqual(dialog.config()['transport'],'ms43x')
        dialog.close();other.close()
    def test_main_log_panel_receives_samples_and_exposes_current_channel_list(self):
        import bmw_bin_editor.ms43_live_logging as backend
        base=QSettings(str(self.folder/'panel.ini'),QSettings.IniFormat)
        base.setValue('settings/migrated',True)
        clock=Clock();cable=Cable(clock);holder={};requests=[0]
        def on_data():
            requests[0]+=1
            if requests[0]==4:holder['logger'].worker.requestInterruption()
        cable.on_data=on_data
        def session(port,**kw):return MS43Session(port,**kw,serial_factory=cable.factory,clock=clock,sleep=clock.sleep)
        actual=backend.MS43LiveLogger
        def logger_factory(config,catalog,parent):
            logger=actual(config,catalog,parent,session_factory=session)
            holder['logger']=logger
            return logger
        with patch.object(gui,'QSettings',lambda *a:base), patch.object(backend,'MS43LiveLogger',side_effect=logger_factory):
            panel=gui.LogViewerPanel()
            panel.set_ecu_profile('ms43-test','MS43X001')
            with patch.object(panel,'_start_logging_power'), patch.object(panel,'_stop_logging_power'):
                panel.start_direct_live(dict(self.config(),transport='ms43x'),self.definition.catalog)
                self.assertIn('N',panel._available_gauge_signals())
                deadline=time.perf_counter()+5
                while panel.direct_logger is not None and time.perf_counter()<deadline:
                    APP.processEvents();time.sleep(.005)
                self.assertIsNone(panel.direct_logger)
                self.assertEqual(panel.log.sample_count,3)
                self.assertEqual(panel.log.value('N',2),3000.)
                self.assertFalse(panel.live_mode)
                self.assertTrue(cable.closed)
            panel.close()

    def test_dde_profile_rejected_without_opening_port(self):
        config=self.config();config['profile']['ecu']='D50M47A'
        with self.assertRaises(ValueError):MS43Reader(config,self.definition.catalog)
    @unittest.skipUnless((ROOT/'definitions/ms43x/Siemens_MS43_MS43X001_512K.xdf').is_file(), 'Optional original MS43X XDF not installed')
    def test_ms43_profiles_do_not_bind_to_diesel_signal_names(self):
        database=XDFDatabase(ROOT/'definitions/ms43x/Siemens_MS43_MS43X001_512K.xdf')
        ve=next(d for d in database.definitions if d.title=='ip_map_ve_1__map__n')
        p=profile_for_definition(ve)
        self.assertIn('ACTIVEVE',p.traces);self.assertIsNone(p.x_signal)
        self.assertFalse(any(s.startswith('STAT_') for s in p.traces))

if __name__=='__main__':unittest.main()
