"""MS43X live reader using ADX conversions, existing CSV and logger lifecycle."""
from copy import deepcopy
from datetime import datetime
import json
import math
from pathlib import Path
import time
from PySide6.QtCore import QThread, Signal
from .live_logging import CsvCapture, ParameterUpdatingReader
from .serial_live_logging import SerialLiveLogger
from .serial_transport import SerialCancelled, validate_port
from .ms43_serial import MS43Session
from .ms43_logging_definition import MS43Definition

DEFAULT_CHANNELS = ['N','PVS','TPS','MAP','MAPREQ','MAF','MAFTI','MAFIGA','ACTIVEVE',
    'TCO','TIA','TOIL','VB','AFTARGET','TIBANK1','TIBANK2','INJDC','IGAAVG',
    'IGAKNKAVG','CAMAVIN','CAMAVEX','LAM1','LAM2','VS','GEAR','ISSTATE','PLSTATE','FLSTATE']


def validate_ms43_profile(profile, catalog):
    if not isinstance(profile,dict) or profile.get('ecu')!='MS43X001' or profile.get('version')!=1:
        raise ValueError('Expected an MS43X001 logging profile.')
    name=profile.get('name','')
    if not isinstance(name,str) or not name.strip() or len(name)>100:raise ValueError('Enter a profile name (1–100 characters).')
    selected=profile.get('parameters')
    if not isinstance(selected,list) or not selected or any(not isinstance(x,str) or x not in catalog for x in selected):
        raise ValueError('Choose at least one valid MS43X logging channel.')
    interval=profile.get('interval_ms',50)
    if type(interval) is not int or not 0<=interval<=5000:raise ValueError('Request interval must be 0–5000 ms.')
    return dict(version=1,ecu='MS43X001',name=name.strip(),parameters=list(dict.fromkeys(selected)),interval_ms=interval)


class MS43Reader(ParameterUpdatingReader, QThread):
    sampleReady=Signal(float,object)
    status=Signal(str)
    error=Signal(str)
    profileChanged=Signal(object)

    def __init__(self,config,catalog,parent=None,*,session_factory=MS43Session):
        super().__init__(parent)
        self.config=deepcopy(config)
        self.definition=MS43Definition()
        # Decode only the matching bundled definition, never a diesel parameter file.
        self.catalog=self.definition.catalog
        self.profile=validate_ms43_profile(config['profile'],self.catalog)
        self.port=validate_port(config.get('port',''))
        if type(config.get('echo',True)) is not bool:raise ValueError('Invalid cable echo option.')
        if config.get('initial_baud',9600) not in (9600,125000):raise ValueError('Invalid initial MS43X baud rate.')
        self.groups=[self.profile['parameters']]
        self.session_factory=session_factory
        self.count=0;self.capture=None;self.raw_trace=None
        self.init_parameter_updates()

    def validate_parameters(self, selected):
        profile = validate_ms43_profile(dict(self.profile, parameters=selected), self.catalog)
        return profile, [profile['parameters']]

    def run(self):
        trace=None;session=None
        try:
            if self.config.get('output'):
                path=Path(self.config['output'])
                self.raw_trace=path.with_suffix(path.suffix+'.ds2.jsonl')
                trace=self.raw_trace.open('x',encoding='utf-8',buffering=1)
                self.capture=CsvCapture(path,self.profile,self.groups,self.catalog,flush_interval=0.25,
                    context=dict(transport='ms43x_ds2',ecu='MS43X001',baudrate=125000,
                        initial_baud=self.config.get('initial_baud',9600),cable_echo=self.config.get('echo',True),
                        adx_sha256=self.definition.sha256,raw_trace=self.raw_trace.name,
                        sampling='One complete ECU packet per sample; computer receipt timestamps',
                        hardware_validation='Not yet validated on the vehicle'))
            def record(**event):
                if trace:
                    trace.write(json.dumps(dict(monotonic=time.perf_counter(),**event))+'\n')
            self.status.emit(f"Connecting MS43X on {self.port} · {self.config.get('initial_baud',9600):,} baud…")
            session=self.session_factory(self.port,echo=self.config.get('echo',True),
                initial_baud=self.config.get('initial_baud',9600),cancelled=self.isInterruptionRequested,trace=record)
            with session:
                first=None;last_status=None
                while not self.isInterruptionRequested():
                    self.apply_parameters()
                    sent=time.perf_counter()
                    reply=session.read()
                    values=self.definition.decode_frame(reply.response_frame)
                    received=time.perf_counter()
                    if first is None:first=received
                    seconds=received-first
                    selected={k:values[k] for k in self.profile['parameters']}
                    if self.capture:
                        # Missing lookup states stay blank, not zero or extrapolated.
                        self.capture.append(seconds,datetime.now().astimezone().isoformat(timespec='microseconds'),
                            1,f'ms43-{self.count}',{k:v if math.isfinite(v) else '' for k,v in selected.items()},
                            {'_TEL_AUFTRAG':reply.request_frame.hex().upper(),'_TEL_ANTWORT':reply.response_frame.hex().upper()})
                    self.count+=1
                    self.sampleReady.emit(seconds,selected)
                    if last_status is None or received-last_status>=0.25:
                        rate=(self.count-1)/seconds if seconds else 0
                        self.status.emit(f'MS43X · {self.port} · 125,000 baud · {self.count:,} replies · {rate:.1f} Hz')
                        last_status=received
                    due=sent+self.profile['interval_ms']/1000
                    while not self.isInterruptionRequested() and time.perf_counter()<due:
                        self.msleep(max(1,min(10,int((due-time.perf_counter())*1000))))
        except SerialCancelled:
            if not self.isInterruptionRequested():self.error.emit('MS43X read cancelled.')
        except Exception as exc:
            if not self.isInterruptionRequested():self.error.emit(str(exc))
        finally:
            if session is not None and session.cleanup_error:
                self.error.emit('Could not confirm baud reset: '+session.cleanup_error+
                    '. If reconnecting at 9600 fails, select the already-at-125000 option.')
            if self.capture:
                try:self.capture.close()
                except OSError as exc:self.error.emit('Could not finalize MS43X CSV: '+str(exc))
            if trace:trace.close()


class MS43LiveLogger(SerialLiveLogger):
    def __init__(self,config,catalog,parent=None,*,session_factory=MS43Session):
        super().__init__(config,catalog,parent,session_factory=session_factory,reader_factory=MS43Reader)
