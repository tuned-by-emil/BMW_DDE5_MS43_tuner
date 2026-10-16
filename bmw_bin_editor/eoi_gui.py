from pathlib import Path
import csv,hashlib,json
import numpy as np
from PySide6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QFormLayout,QGroupBox,QComboBox,QLabel,QPushButton,QMessageBox,QFileDialog,QScrollArea,QWidget
from matplotlib.figure import Figure
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg,NavigationToolbar2QT
from .eoi import estimate_end,table_series

class SourceBox(QGroupBox):
    def __init__(self,title,log,doc,preferred):
        super().__init__(title);self.log=log;self.doc=doc
        form=QFormLayout(self);form.setRowWrapPolicy(QFormLayout.WrapLongRows);self.mode=QComboBox();self.mode.setMinimumContentsLength(20);self.mode.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon);self.mode.addItems(['Logged signal','Selected table (assumed active)']);form.addRow('Source',self.mode)
        self.signal=self.combo_signals(preferred);form.addRow('Signal',self.signal)
        self.table=QComboBox();self.table.setMaximumWidth(420);self.table.setMinimumContentsLength(20);self.table.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon);self.table.addItem('Choose a table',None)
        if doc:
            for d in doc.xdf.tables:self.table.addItem(d.title,d)
        form.addRow('Table',self.table)
        self.x=self.combo_signals();self.y=self.combo_signals();form.addRow('X-axis signal',self.x);form.addRow('Y-axis signal',self.y)
        self.note=QLabel('');self.note.setWordWrap(True);form.addRow(self.note)
        self.mode.currentIndexChanged.connect(self.update_fields);self.table.currentIndexChanged.connect(self.update_fields);self.update_fields()
    def combo_signals(self,preferred=()):
        c=QComboBox();c.setMaximumWidth(420);c.setMinimumContentsLength(20);c.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon);c.addItem('Choose a signal',None)
        for s in self.log.available_signals():c.addItem(f'{self.log.catalog.label_for(s)} [{self.log.units.get(s, "")}]',s)
        for s in preferred:
            i=c.findData(s)
            if i>=0:c.setCurrentIndex(i);break
        return c
    def update_fields(self,*args):
        tables=self.mode.currentIndex()==1;self.signal.setEnabled(not tables)
        for w in [self.table,self.x,self.y]:w.setEnabled(tables)
        d=self.table.currentData()
        self.note.setText((f'X: {d.x_axis.units}; Y: {d.y_axis.units}; output: {d.z_units}. Physical units must match selected signals. Outside-axis samples are omitted.' if d else 'Select a table and map each physical axis explicitly.') if tables else 'Uses the logged value; does not infer an active table from a match.')
    def values(self):
        if self.mode.currentIndex()==0:
            key=self.signal.currentData()
            if not key:raise ValueError(f'{self.title()}: choose a logged signal.')
            return self.log.series(key),{'source':'log','signal':key}
        d=self.table.currentData();x=self.x.currentData();y=self.y.currentData()
        if not self.doc or not d or not x or not y:raise ValueError(f'{self.title()}: choose a BIN, table and both axis signals.')
        return table_series(self.doc,d,self.log.series(x),self.log.series(y)),{'source':'assumed active table','table':d.title,'address':hex(d.z_address),'x_signal':x,'y_signal':y,'x_units':d.x_axis.units,'y_units':d.y_axis.units,'output_units':d.z_units}

class EOIDialog(QDialog):
    def __init__(self,log,doc,parent=None):
        super().__init__(parent);self.log=log;self.doc=doc;self.result=None;self.setWindowTitle('EOI estimate — read-only')
        # Keep all source settings reachable on laptops and scaled displays.
        screen = self.screen()
        available = screen.availableGeometry() if screen else None
        self.resize(min(1000, max(1, available.width() - 32)) if available else 1000,
                    min(850, max(1, available.height() - 64)) if available else 850)
        layout=QVBoxLayout(self)
        self.settings_scroll = QScrollArea()
        self.settings_scroll.setWidgetResizable(True)
        self.settings_scroll.setMinimumHeight(100)
        self.settings_scroll.setFrameShape(QScrollArea.NoFrame)
        settings = QWidget()
        settings_layout = QVBoxLayout(settings)
        settings_layout.setContentsMargins(0, 0, 4, 0)
        self.settings_scroll.setWidget(settings)
        layout.addWidget(self.settings_scroll, 1)
        note=QLabel('Estimated end relative to TDC, not measured hydraulic EOI. Start and duration must describe the SAME injection event and compatible timing references. Energization duration gives only an end-of-command approximation; injector delays and combustion phasing are not included.');note.setWordWrap(True);settings_layout.addWidget(note)
        row=QHBoxLayout();self.soi=SourceBox('Start angle',log,doc,['STAT_BEZUGSWINKEL_BEGINN_HAUPTEINSPRITZUNG1_WERT']);self.dur=SourceBox('Injection duration',log,doc,['STAT_ANSTEUERDAUER_HAUPTEINSPRITZUNG1_WERT','STAT_ANSTEUERDAUER_HAUPTEINSPRITZUNG1_GESCHAETZT_WERT']);row.addWidget(self.soi);row.addWidget(self.dur);settings_layout.addLayout(row)
        form=QFormLayout();self.rpm=self.soi.combo_signals(['STAT_MOTORDREHZAHL_WERT']);form.addRow('RPM signal',self.rpm)
        self.sign=QComboBox();self.sign.addItem('Choose start-angle convention',None);self.sign.addItem('Positive before TDC (BTDC)','BTDC');self.sign.addItem('Positive after TDC (ATDC)','ATDC');form.addRow('Start sign',self.sign)
        self.units=QComboBox();self.units.addItem('Choose duration units',None)
        for label,key in [('Microseconds (us)','us'),('Milliseconds (ms)','ms'),('Crank degrees','deg')]:self.units.addItem(label,key)
        form.addRow('Duration units',self.units);settings_layout.addLayout(form)
        self.summary=QLabel('Open a recorded log with a duration channel, or select a duration table and its required logged axes. Rail pressure alone cannot determine injection duration.');self.summary.setWordWrap(True);settings_layout.addWidget(self.summary)
        buttons=QHBoxLayout();self.calculate_button=QPushButton('Calculate / refresh');self.calculate_button.clicked.connect(self.calculate);buttons.addWidget(self.calculate_button);self.export=QPushButton('Export estimate + assumptions');self.export.setEnabled(False);self.export.clicked.connect(self.save);buttons.addWidget(self.export);buttons.addStretch(1);self.close_button=QPushButton('Close');self.close_button.clicked.connect(self.reject);buttons.addWidget(self.close_button);layout.addLayout(buttons)
        self.figure=Figure(figsize=(9,3));self.canvas=FigureCanvasQTAgg(self.figure);self.canvas.setMinimumHeight(120);layout.addWidget(NavigationToolbar2QT(self.canvas,self));layout.addWidget(self.canvas,1)
        for box in [self.soi,self.dur]:
            for combo in [box.mode,box.signal,box.table,box.x,box.y]:combo.currentIndexChanged.connect(self.invalidate)
        for combo in [self.rpm,self.sign,self.units]:combo.currentIndexChanged.connect(self.invalidate)
    def invalidate(self,*args):
        self.result=None;self.export.setEnabled(False);self.figure.clear();self.canvas.draw_idle();self.summary.setText('Settings changed. Calculate to refresh the estimate.')
    def calculate(self):
        self.invalidate()
        try:
            rpmkey=self.rpm.currentData()
            if not rpmkey:raise ValueError('Choose an RPM signal.')
            s,sm=self.soi.values();d,dm=self.dur.values();rpm=self.log.series(rpmkey)
            e=estimate_end(rpm,s,d,self.units.currentData(),self.sign.currentData());valid=np.isfinite(e)
            if not valid.any():raise ValueError('No usable samples: check missing channels, table coverage, units and positive duration/RPM.')
            meta={'log':str(self.log.path),'bin_sha256':hashlib.sha256(bytes(self.doc.data)).hexdigest() if self.doc else None,'start':sm,'duration':dm,'rpm_signal':rpmkey,'duration_unit':self.units.currentData(),'start_convention':self.sign.currentData(),'output':'degrees ATDC; negative = before TDC','assumptions':'Same event and compatible start/duration references; zero injector-delay correction. Command-end approximation if duration is energization. Table activity assumed, not detected. No extrapolation. Snapshot; recalculate after BIN/log changes.'}
            self.result=(self.log.times.copy(),s.copy(),d.copy(),rpm.copy(),e,meta)
            ax=self.figure.add_subplot(111);ax.plot(self.log.times,e,label='Estimated end (not measured EOI)');ax.axhline(0,color='gray',lw=.8);ax.set_xlabel('Time [s]');ax.set_ylabel('End [deg ATDC]');ax.legend();self.figure.tight_layout();self.canvas.draw()
            self.summary.setText(f'{valid.sum():,}/{len(e):,} valid samples. Median {np.median(e[valid]):.2f} deg ATDC; range {e[valid].min():.2f} to {e[valid].max():.2f}. Negative means before TDC. Snapshot: refresh after edits.');self.export.setEnabled(True)
        except (ValueError,KeyError,IndexError,TypeError) as exc:QMessageBox.warning(self,'EOI estimate',str(exc))
    def save(self):
        if self.result is None:return
        name,_=QFileDialog.getSaveFileName(self,'Save estimate','EOI_estimate.csv','CSV (*.csv)')
        if not name:return
        try:
            t,s,d,r,e,meta=self.result
            with open(name,'w',newline='',encoding='utf-8') as f:
                w=csv.writer(f);w.writerow(['seconds','start_input','duration_input','rpm','estimated_end_deg_ATDC']);w.writerows(zip(t,s,d,r,e))
            Path(name+'.assumptions.json').write_text(json.dumps(meta,indent=2),encoding='utf-8')
        except OSError as exc:QMessageBox.warning(self,'Export failed',str(exc))
