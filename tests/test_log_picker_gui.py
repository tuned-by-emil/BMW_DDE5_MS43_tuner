import os,sys,tempfile
from pathlib import Path
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QSettings
from PySide6.QtGui import QFontDatabase,QFont
from bmw_bin_editor import gui
app=QApplication.instance() or QApplication([])
font=Path('C:/Windows/Fonts/arial.ttf')
if font.exists():QFontDatabase.addApplicationFont(str(font));app.setFont(QFont('Arial',10))
with tempfile.TemporaryDirectory() as td:
 gui.QSettings=lambda *a:QSettings(str(Path(td)/'test.ini'),QSettings.IniFormat)
 path=Path(sys.argv[1]) if len(sys.argv)>1 else Path(td)/'sample.csv'
 if len(sys.argv)==1:path.write_text('time;STAT_MOTORDREHZAHL_WERT;STAT_MENGE_VOREINSPRITZUNG1_WERT\n0;850;1.3\n100;900;1.4\n')
 panel=gui.LogViewerPanel();assert panel.open_log(path)
 panel.compact_button.setChecked(False)
 panel.resize(1000,650);panel.show();app.processEvents()
 assert panel.width()==1000,(panel.minimumSizeHint().width(),panel.width())
 assert panel.signal_combo.count()==len(panel.log.available_signals())
 assert len({panel.signal_combo.itemText(i) for i in range(panel.signal_combo.count())})==panel.signal_combo.count()
 assert panel.signal_combo.isVisible() and panel.signal_combo.width()>=220
 panel.signal_combo.showPopup();app.processEvents();assert panel.signal_combo.view().isVisible();panel.signal_combo.hidePopup()
 signal='STAT_MENGE_VOREINSPRITZUNG1_WERT';i=panel.signal_combo.findData(signal);assert i>=0
 panel.signal_combo.setCurrentIndex(i);panel.new_graph_button.click();app.processEvents()
 assert panel.custom_graphs[-1]==[signal]
 assert len(panel.axes)==len(panel.custom_graphs)
 panel.graph_combo.setCurrentIndex(0);panel.add_trace_button.click();app.processEvents()
 assert signal in panel.custom_graphs[0]
 if len(sys.argv)>2:panel.grab().save(sys.argv[2])
 print(f'PASS: {panel.signal_combo.count()} signals; dropdown opens; trace and graph plot selected signal; minimum width {panel.minimumSizeHint().width()} px')
 panel.close()
