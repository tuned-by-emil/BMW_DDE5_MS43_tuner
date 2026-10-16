"""Portable startup verification using only generated synthetic calibration data."""
from pathlib import Path
import json
import os
import tempfile


def run_check(report_path):
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    from . import gui, __version__, live_logging
    from .core import CalibrationDefinition, CalibrationDocument
    from .xdf_edit import XDFWorkspace
    from .serial_self_check import run_serial_self_check
    app = QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory(prefix='bmw-public-check-') as temp:
        root = Path(temp)
        previous = gui.QSettings, live_logging.QSettings
        def isolated_settings(_org, application):
            return QSettings(str(root / (application + '.ini')), QSettings.IniFormat)
        gui.QSettings = live_logging.QSettings = isolated_settings
        window = None
        try:
            settings = isolated_settings('', 'BMWBinEditor')
            settings.setValue('settings/migrated', True)
            window = gui.MainWindow()
            empty = window.document is None and window.xdf is None
            workspace = XDFWorkspace(bin_size=128)
            workspace.set_metadata('Synthetic example', 'Synthetic fixture', 'Generated test only', True)
            workspace.add(CalibrationDefinition('Example table', 'Synthetic only', 'Example', 16, 2, 3))
            database = workspace.save_as(root / 'example.xdf')
            d = database.definitions[0]
            source = root / 'example.bin'
            source.write_bytes(bytes(128))
            doc = CalibrationDocument(database, source)
            before = bytes(doc.data)
            doc.set_cells(d, {(0, 0): 42}, 'Synthetic edit')
            edited = doc.map_values(d)[0][0] == 42
            doc.undo()
            undone = bytes(doc.data) == before and not doc.dirty
            window.xdf = database
            window.document = doc
            window.editor.set_document(doc)
            window.populate_tree()
            window._document_changed()
            window.show()
            app.processEvents()
            result = dict(ok=False, version=__version__, empty_startup=empty,
                          synthetic_edit=edited, synthetic_undo=undone,
                          synthetic_gui=window.tree.topLevelItemCount() > 0,
                          private_definitions_required=False, physical_port_opened=False,
                          serial_process_check=run_serial_self_check())
            result['ok'] = all(result[key] for key in ('empty_startup', 'synthetic_edit', 'synthetic_undo', 'synthetic_gui')) and result['serial_process_check']['ok']
        finally:
            if window is not None:
                if window.document is not None: window.document.dirty = False
                window.close()
            gui.QSettings, live_logging.QSettings = previous
    target = Path(report_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    return result['ok']
