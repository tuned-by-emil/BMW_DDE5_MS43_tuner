"""Connection and channel profiles for the MS43X001 DS2 logger."""
from datetime import datetime
import json
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QFormLayout,QLabel,QComboBox,
    QCheckBox,QSpinBox,QLineEdit,QTreeWidget,QTreeWidgetItem,QPushButton,QFileDialog,QMessageBox,QInputDialog)
from .live_logging import available_serial_ports
from .ms43_live_logging import DEFAULT_CHANNELS,validate_ms43_profile
from .ms43_logging_definition import MS43Definition
from .serial_transport import validate_port


class MS43LoggingDialog(QDialog):
    def __init__(self,parent=None,*,settings,cable_defaults=None):
        super().__init__(parent)
        self.settings=settings;self.definition=MS43Definition();self.catalog=self.definition.catalog
        self.setWindowTitle('MS43X001 live logging — direct K-line');self.resize(800,720)
        self.saved={}
        try:
            for profile in json.loads(str(settings.value('ms43/profiles','[]'))):
                validated=validate_ms43_profile(profile,self.catalog);self.saved[validated['name']]=validated
        except (ValueError,TypeError):pass
        self.presets={
            'Engine and throttle response':dict(version=1,ecu='MS43X001',name='Engine and throttle response',
                parameters=[k for k in DEFAULT_CHANNELS if k in self.catalog],interval_ms=50),
            'VE tuning':dict(version=1,ecu='MS43X001',name='VE tuning',parameters=[k for k in
                ['N','MAP','ACTIVEVE','PVS','TPS','MAFTI','CAMAVIN','CAMAVEX','AFTARGET','TIBANK1','TIBANK2','INJDC','TIA','TCO'] if k in self.catalog],interval_ms=50),
            'All ADX channels':dict(version=1,ecu='MS43X001',name='All ADX channels',parameters=list(self.catalog),interval_ms=50)}
        self.profile_box=QComboBox();self.profile_box.addItems(list(dict.fromkeys(list(self.presets)+list(self.saved))))
        save=QPushButton('Save profile as…');save.clicked.connect(self.save_profile)
        delete=QPushButton('Delete profile');delete.clicked.connect(self.delete_profile)
        row=QHBoxLayout();row.addWidget(QLabel('Channel profile'));row.addWidget(self.profile_box,1);row.addWidget(save);row.addWidget(delete)
        self.port=QComboBox();self.port.setEditable(True)
        try:self.port.addItems(available_serial_ports())
        except OSError:pass
        fallback=cable_defaults.value('live126/serial_port','COM1') if cable_defaults else 'COM1'
        self.port.setCurrentText(str(settings.value('ms43/port',fallback)))
        self.echo=QCheckBox('Cable echoes transmitted bytes')
        echo_default=cable_defaults.value('live126/serial_echo',True,type=bool) if cable_defaults else True
        self.echo.setChecked(settings.value('ms43/echo',echo_default,type=bool))
        self.initial_baud=QComboBox();self.initial_baud.addItem('Normal connection: 9,600 → 125,000 baud',9600)
        self.initial_baud.addItem('ECU already at 125,000 baud (reconnect)',125000)
        self.initial_baud.setCurrentIndex(max(0,self.initial_baud.findData(int(settings.value('ms43/initial_baud',9600)))))
        self.interval=QSpinBox();self.interval.setRange(0,5000);self.interval.setSuffix(' ms');self.interval.setSpecialValueText('Maximum rate')
        form=QFormLayout();form.addRow('COM port',self.port);form.addRow('',self.echo);form.addRow('Initial ECU speed',self.initial_baud)
        form.addRow('Minimum request interval',self.interval)
        self.channels=QTreeWidget();self.channels.setHeaderLabels(['Capture channel','Units']);self.channels.setRootIsDecorated(False)
        for ident,entry in self.catalog.items():
            item=QTreeWidgetItem([entry['label'],entry['unit']]);item.setData(0,Qt.UserRole,ident)
            item.setCheckState(0,Qt.Unchecked);item.setToolTip(0,ident+'\n'+entry['description']);self.channels.addTopLevelItem(item)
        self.channels.setColumnWidth(0,550)
        self.search=QLineEdit();self.search.setPlaceholderText('Search parameters by readable name, identifier or unit…')
        self.search.textChanged.connect(self.filter_channels)
        all_button=QPushButton('Select all');none_button=QPushButton('Clear selection')
        all_button.clicked.connect(lambda:self.select(list(self.catalog)));none_button.clicked.connect(lambda:self.select([]))
        channel_buttons=QHBoxLayout();channel_buttons.addWidget(all_button);channel_buttons.addWidget(none_button);channel_buttons.addStretch()
        self.preview=QCheckBox('Live preview only (do not record files)');self.preview.setChecked(False)
        self.output=QLineEdit(str(Path(str(settings.value('ms43/output_folder',Path.home()/'Documents')))/
            ('MS43X001_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f')+'.csv')))
        browse=QPushButton('Browse…');browse.clicked.connect(self.choose_output)
        output_row=QHBoxLayout();output_row.addWidget(self.output,1);output_row.addWidget(browse)
        self.preview.toggled.connect(lambda checked:self.output.setEnabled(not checked))
        self.keep_awake=QCheckBox('Keep computer awake while logging');self.keep_awake.setChecked(True)
        info=QLabel('Reads one complete MS43X extended packet per request. Channel selection controls display/CSV, '
            'not packet size. Recording also saves raw DS2 traffic for first-car diagnostics.\n'
            'MS43X001 permits connecting while the engine is running. Hardware connection is not yet verified on your car.')
        info.setWordWrap(True)
        start=QPushButton('Start logging');start.clicked.connect(self.accept);cancel=QPushButton('Cancel');cancel.clicked.connect(self.reject)
        actions=QHBoxLayout();actions.addStretch();actions.addWidget(start);actions.addWidget(cancel)
        layout=QVBoxLayout(self);layout.addLayout(row);layout.addLayout(form);layout.addWidget(self.search);layout.addWidget(self.channels,1);layout.addLayout(channel_buttons)
        layout.addWidget(self.preview);layout.addLayout(output_row);layout.addWidget(self.keep_awake);layout.addWidget(info);layout.addLayout(actions)
        self.profile_box.currentTextChanged.connect(self.load_profile)
        remembered=str(settings.value('ms43/last_profile','Engine and throttle response'))
        self.profile_box.setCurrentText(remembered);self.load_profile(self.profile_box.currentText())

    def filter_channels(self,text):
        needle=text.strip().casefold()
        for i in range(self.channels.topLevelItemCount()):
            item=self.channels.topLevelItem(i)
            item.setHidden(needle not in (item.text(0)+' '+item.text(1)+' '+str(item.data(0,Qt.UserRole))).casefold())

    def select(self,selected):
        for i in range(self.channels.topLevelItemCount()):
            item=self.channels.topLevelItem(i);item.setCheckState(0,Qt.Checked if item.data(0,Qt.UserRole) in selected else Qt.Unchecked)

    def selected(self):
        return [self.channels.topLevelItem(i).data(0,Qt.UserRole) for i in range(self.channels.topLevelItemCount())
                if self.channels.topLevelItem(i).checkState(0)==Qt.Checked]

    def load_profile(self,name):
        profile=self.saved.get(name,self.presets.get(name))
        if profile:self.select(profile['parameters']);self.interval.setValue(profile['interval_ms'])

    def profile(self,name=None):
        return validate_ms43_profile(dict(version=1,ecu='MS43X001',name=name or self.profile_box.currentText(),
            parameters=self.selected(),interval_ms=self.interval.value()),self.catalog)

    def save_profile(self):
        name,okay=QInputDialog.getText(self,'Save MS43X profile','Profile name')
        if not okay:return
        try:profile=self.profile(name)
        except ValueError as exc:QMessageBox.warning(self,'MS43X',str(exc));return
        self.saved[profile['name']]=profile;self.persist()
        if self.profile_box.findText(profile['name'])<0:self.profile_box.addItem(profile['name'])
        self.profile_box.setCurrentText(profile['name'])

    def persist(self):
        self.settings.setValue('ms43/profiles',json.dumps(list(self.saved.values())));self.settings.sync()

    def delete_profile(self):
        name=self.profile_box.currentText()
        if name in self.saved:
            del self.saved[name];self.persist()
            if name not in self.presets:self.profile_box.removeItem(self.profile_box.currentIndex())
            else:self.load_profile(name)

    def choose_output(self):
        path,_=QFileDialog.getSaveFileName(self,'MS43X CSV recording',self.output.text(),'CSV (*.csv)')
        if path:self.output.setText(path)

    def config(self):
        port=validate_port(self.port.currentText())
        output='' if self.preview.isChecked() else self.output.text().strip()
        if not self.preview.isChecked() and (not output or not Path(output).is_absolute() or not Path(output).parent.is_dir()):
            raise ValueError('Choose an absolute CSV path in an existing folder, or select live preview.')
        if output and any(Path(output+suffix).exists() for suffix in ('','.profile.json','.ds2.jsonl')):
            raise ValueError('Choose a new recording filename; an output or sidecar already exists.')
        return dict(transport='ms43x',port=port,echo=self.echo.isChecked(),initial_baud=self.initial_baud.currentData(),
            output=output,profile=self.profile(),keep_awake=self.keep_awake.isChecked(),lid_closed=False)

    def accept(self):
        try:config=self.config()
        except ValueError as exc:QMessageBox.warning(self,'MS43X logging',str(exc));return
        for key,value in [('port',config['port']),('echo',config['echo']),('initial_baud',config['initial_baud']),('last_profile',self.profile_box.currentText())]:
            self.settings.setValue('ms43/'+key,value)
        if config['output']:self.settings.setValue('ms43/output_folder',str(Path(config['output']).parent))
        self.settings.sync();super().accept()
