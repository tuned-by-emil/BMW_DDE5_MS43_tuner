"""Offline MS43X packaging check: simulated bytes, never a physical port."""
from pathlib import Path
import json
import sys
import tempfile
from .ms43_logging_definition import MS43Definition,FAST_REQUEST,SLOW_REQUEST,DATA_REQUEST,ACK
from .ms43_serial import MS43Session
from .serial_transport import xor_checksum


def run_check(report_path):
    definition=MS43Definition()
    payload=bytearray(73);payload[4:6]=(3000).to_bytes(2,'little');payload[8:10]=(12000).to_bytes(2,'little')
    payload[0x10:0x12]=(8*256).to_bytes(2,'little')
    body=bytes.fromhex('124DA0')+payload;frame=body+bytes([xor_checksum(body)])
    class Cable:
        def __init__(self):self.rx=bytearray();self.sent=[];self.is_open=False;self.baudrate=9600
        def factory(self,**kwargs):return self
        def open(self):self.is_open=True
        def reset_input_buffer(self):self.rx.clear()
        def write(self,data):
            self.sent.append((self.baudrate,data));self.rx.extend(data+(frame if data==DATA_REQUEST else ACK));return len(data)
        def read(self,n):
            result=bytes(self.rx[:min(n,3)]);del self.rx[:len(result)];return result
        def close(self):self.is_open=False
    cable=Cable()
    with MS43Session('COM1',serial_factory=cable.factory) as session:
        values=definition.decode_frame(session.read().response_frame)
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    from .ms43_logging_gui import MS43LoggingDialog
    from .settings import ECUSettings
    app=QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory() as td:
        dialog=MS43LoggingDialog(settings=ECUSettings(QSettings(str(Path(td)/'test.ini'),QSettings.IniFormat),'ms43-self-check'))
        dialog.preview.setChecked(True);config=dialog.config();channels=dialog.channels.topLevelItemCount();dialog.close()
    from .core import XDFDatabase
    from .ms43_help import setting_tooltip
    database=XDFDatabase(definition.path.with_name('Siemens_MS43_MS43X001_512K.xdf'))
    anti_jerk=next(d for d in database.definitions if d.title=='ip_iga_aj_ron98__n__maf')
    tooltips='raw zero is -48' in setting_tooltip(anti_jerk) and 'Source:' in setting_tooltip(anti_jerk)
    result=dict(ok=False,ms43_tooltips=tooltips,frozen=bool(getattr(sys,'frozen',False)),physical_port_opened=False,
        adx_sha256=definition.sha256,channel_count=channels,rpm=values['N'],map_kpa=values['MAP'],active_ve=values['ACTIVEVE'],
        baud_reset=not cable.is_open and cable.baudrate==9600,
        commands=[{'baud':baud,'hex':request.hex().upper()} for baud,request in cable.sent],transport=config['transport'])
    result['ok']=(tooltips and values['N']==3000 and values['ACTIVEVE']==8 and channels==131 and result['baud_reset']
                  and cable.sent==[(9600,FAST_REQUEST),(125000,DATA_REQUEST),(125000,SLOW_REQUEST)])
    Path(report_path).write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    return result['ok']
