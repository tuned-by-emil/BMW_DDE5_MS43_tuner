if __name__ == "__main__":
    from multiprocessing import freeze_support
    freeze_support()
    from bmw_bin_editor.gui import run
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == '--self-check-public':
        from bmw_bin_editor.public_self_check import run_check
        raise SystemExit(0 if run_check(sys.argv[2]) else 1)
    if len(sys.argv) == 3 and sys.argv[1] == '--self-check-ms43':
        from bmw_bin_editor.ms43_self_check import run_check
        raise SystemExit(0 if run_check(sys.argv[2]) else 1)
    if len(sys.argv) == 3 and sys.argv[1] == '--self-check-ai':
        import json
        import tempfile
        import time
        from pathlib import Path
        from PySide6.QtCore import QSettings
        from PySide6.QtWidgets import QApplication
        import jwt
        from cryptography.hazmat.primitives.asymmetric import rsa
        from bmw_bin_editor import gui, chatgpt_client
        from bmw_bin_editor.ai_panel import AIPanel
        from bmw_bin_editor.ai_context import build_snapshot
        from bmw_bin_editor.logdata import LiveTestOLog, SIG_RPM, SIG_IQ
        import os
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        with tempfile.TemporaryDirectory(prefix='bmw-ai-check-') as temp:
            gui.QSettings = lambda *_: QSettings(str(Path(temp) / 'settings.ini'), QSettings.IniFormat)
            app = QApplication([])
            window = gui.MainWindow()
            panel = AIPanel(window, store=chatgpt_client.AccountStore(temp))
            log = LiveTestOLog(); log.append_sample(0, {SIG_RPM: 1800}); log.append_sample(1, {SIG_IQ: 15})
            window.editor.log_panel.log = log
            window.editor.log_panel.cursor_index = 1
            snapshot = build_snapshot(None, None, None, window.editor.log_panel, source='saved')
            # Verify the packaged deterministic evidence and atomic review/apply path.
            import struct
            from types import SimpleNamespace
            from bmw_bin_editor.core import CalibrationDefinition, CalibrationDocument
            from bmw_bin_editor.ai_evidence import EvidenceSession
            from bmw_bin_editor.ai_patch import apply_proposal
            from bmw_bin_editor.local_results import format_local_results
            definition = CalibrationDefinition('N75 base command', 'Code-confirmed base command', 'N75', 0, 2, 2, z_equation='X/100')
            binary = Path(temp) / 'self-check.bin'
            original = struct.pack('<4H',1000,2000,3000,4000)
            binary.write_bytes(original)
            document = CalibrationDocument(SimpleNamespace(default_lsb_first=True,path=Path(temp)/'self-check.xdf',tables=[definition]),binary)
            capture = build_snapshot(document, definition, None, window.editor.log_panel, source='saved', question='Smooth the selected N75 cells')
            evidence = EvidenceSession(capture, question=capture['analysis_question'])
            inspected = evidence.inspect_tables()
            staged = evidence.propose_patch('smoothing','Self-check cell',[dict(table_address=0,table_id=None,row=0,col=1,old=20,new=21,reason='Explicit requested numerical blend',expected_effect='Numerical blend only')])
            changed, _ = apply_proposal(document,capture,staged['proposal'])
            atomic_patch = changed and len(document.undo_stack)==1 and document.cell_value(definition,0,1)==21
            document.undo()
            atomic_patch = atomic_patch and bytes(document.data)==original and binary.read_bytes()==original
            local = format_local_results(capture['log']['computed_analysis'])
            local_results = 'Power: unavailable' in local and len(local.splitlines())==4
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())); public['kid'] = 'self-check'
            token = jwt.encode(dict(iss=chatgpt_client.AUTH, sub='test', aud='issued', nonce='test', exp=time.time()+60),
                               key, algorithm='RS256', headers={'kid': 'self-check'})
            previous_json = chatgpt_client._json
            try:
                chatgpt_client._json = lambda *_a, **_k: {'keys': [public]}
                verified = chatgpt_client.validate_id_token(token, 'issued', 'test',
                    {'jwks_uri': chatgpt_client.AUTH + '/.well-known/jwks.json'})['sub'] == 'test'
            finally:
                chatgpt_client._json = previous_json
            try:
                sample = chatgpt_client.protect(b'non-secret-self-check')
                dpapi = chatgpt_client.protect(sample, decrypt=True) == b'non-secret-self-check'
            except chatgpt_client.ChatGPTError:
                dpapi = False
            report = dict(ok=verified and panel.worker is None and not panel.auto.isChecked() and atomic_patch and local_results and inspected["ok"],
                          signed_identity_verification=verified, chatgpt_panel=True,
                          evidence_tools=len(evidence.tools[0]["tools"]), atomic_patch_undo=atomic_patch, local_results=local_results, reasoning_default=panel.effort.currentData(),
                          automatic_tips_default_off=not panel.auto.isChecked(),
                          sparse_log_rows=snapshot['log']['rows'], dpapi_available=dpapi,
                          frozen=bool(getattr(sys, 'frozen', False)), network_used=False)
            panel.shutdown(); panel.close(); window.close()
        Path(sys.argv[2]).write_text(json.dumps(report, indent=2), encoding='utf-8')
        raise SystemExit(0 if report['ok'] else 1)
    if len(sys.argv) == 4 and sys.argv[1] == '--check-ols':
        # Read-only integration check for source and portable release verification.
        import json
        from pathlib import Path
        from bmw_bin_editor.ols_import import load_ols
        from bmw_bin_editor.ols_to_xdf import convert_ols
        from bmw_bin_editor.definition_library import propose_matches
        try:
            project = load_ols(sys.argv[2])
            conversion = convert_ols(project)
            report = conversion.report
            report['library_proposals'] = [item.entry.title for item in propose_matches(project.versions[0].data)]
            report['frozen'] = bool(getattr(sys, 'frozen', False))
            report['ok'] = True
        except Exception as exc:
            report = {'ok': False, 'error': str(exc)}
        Path(sys.argv[3]).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
        raise SystemExit(0 if report['ok'] else 1)
    if len(sys.argv) == 3 and sys.argv[1] == '--self-check':
        import json
        import math
        import os
        import tempfile
        import struct
        from pathlib import Path
        from PySide6.QtCore import QSettings
        from PySide6.QtWidgets import QApplication
        from bmw_bin_editor import APP_NAME, APP_AUTHOR, __version__, gui, live_logging
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        with tempfile.TemporaryDirectory(prefix='bmw-editor-check-') as temp:
            # Release verification must not migrate user preferences.
            def isolated_settings(organization, application):
                return QSettings(str(Path(temp) / (application + '.ini')), QSettings.IniFormat)
            gui.QSettings = live_logging.QSettings = isolated_settings
            app = QApplication([])
            window = gui.MainWindow()
            window.showMaximized()
            app.processEvents()
            empty_startup = (window.xdf is None and window.current_xdf_path is None
                             and window.document is None and window.tree.topLevelItemCount() == 0
                             and window.favorite_tree.topLevelItemCount() == 0)
            # Keep verifying the retained definition through its explicit load action.
            window.load_bundled_definition()
            catalog = live_logging.load_parameters(gui.bundled_signal_catalog_path().parent / 'live_parameters.json')
            dialog = live_logging.LoggingDialog(catalog)
            from bmw_bin_editor.xdf_gui import XDFEditorDialog
            from bmw_bin_editor.bin_analysis import analyze_maps
            from bmw_bin_editor.definition_library import LibraryEntry, discover_entries, propose_matches, sha256
            from bmw_bin_editor.scaling_evidence import load_scaling_profiles
            words = [3, 4, 100, 200, 300, 0, 1000, 2000, 3000] + [500+r*50+c*10 for r in range(3) for c in range(4)]
            sample = struct.pack('>' + 'H' * len(words), *words)
            definition_dialog = XDFEditorDialog(None, sample)
            definition_dialog.new_definition('table')
            xdf_saved = definition_dialog.save_xdf(path=Path(temp)/'self-check.xdf')
            from bmw_bin_editor.ols_import_gui import OLSImportDialog
            from bmw_bin_editor.ols_import import _helper_path
            from bmw_bin_editor.core import CalibrationDefinition, CalibrationDocument
            from bmw_bin_editor.xdf_edit import XDFWorkspace
            ols_dialog = OLSImportDialog(window)
            native_ols_import = (_helper_path().is_file() and window.act_import_ols.isEnabled()
                                 and ols_dialog.save_xdf.isChecked() and ols_dialog.save_bin.isChecked()
                                 and not ols_dialog.export_button.isEnabled())
            column_workspace = XDFWorkspace(bin_size=12)
            column_workspace.add(CalibrationDefinition('Column check', '', 'Check', 0, 2, 3,
                                                       storage_order='column_major'))
            column_xdf = column_workspace.save_as(Path(temp)/'column.xdf')
            column_bin = Path(temp)/'column.bin'
            column_bin.write_bytes(struct.pack('>6H', 1, 4, 2, 5, 3, 6))
            column_doc = CalibrationDocument(column_xdf, column_bin)
            column_definition = column_xdf.definitions[0]
            column_layout = (column_doc.map_raw(column_definition) == [[1, 2, 3], [4, 5, 6]]
                             and column_doc.cell_address(column_definition, 1, 1) == 6)
            column_doc.set_cells(column_definition, {(1, 1): 25}, 'Column check')
            column_layout &= column_doc.data[6:8] == b'\x00\x19'
            column_doc.undo()
            column_layout &= column_doc.map_raw(column_definition) == [[1, 2, 3], [4, 5, 6]]
            ols_dialog.reject()
            potential_maps = analyze_maps(sample)
            rpm = [500, 750, 1000, 1500, 2000, 2500, 3500, 5000]
            load = [0, 500, 1000, 2000, 3500, 5000, 6500, 7500]
            family_words = [8, 8] + rpm + load + [1000 + col*200 + row*20 for row in range(8) for col in range(8)]
            family_sample = struct.pack('>' + 'H'*len(family_words), *family_words)
            family_candidates = analyze_maps(family_sample)
            check_entry = LibraryEntry('startup-check', 'Synthetic startup definition',
                Path(temp)/'self-check.xdf', len(sample),
                {'original_sha256': [sha256(sample)], 'map_count': 1})
            definition_matches = propose_matches(sample, entries=[check_entry])
            scaling_profiles = load_scaling_profiles()
            timing_words = [8, 8] + rpm + load + [int(round((-2 + row*3.9 + col*.4) / (3/128))) & 0xffff
                                                   for row in range(8) for col in range(8)]
            timing_sample = struct.pack('>' + 'H'*len(timing_words), *timing_words)
            timing_candidates = analyze_maps(timing_sample, scaling_profiles=scaling_profiles)
            learned_soi = next((s for c in timing_candidates for s in c.suggestions
                                if s.family == 'Main SOI' and s.scaling_profile_count), None)
            from bmw_bin_editor.core import safe_math
            from bmw_bin_editor.logdata import (TestOLog, SIG_IQ, SIG_RPM, SIG_SPEED,
                                                FUEL_RATE_SIGNAL, FUEL_CONSUMPTION_SIGNAL)
            n75_addresses = {0xF7F44, 0xF8478, 0xF86BC, 0xF8900, 0xFA3DC}
            n75_definitions = [d for d in window.xdf.tables if d.z_address in n75_addresses]
            retained_n75_scaling = (len(n75_definitions) == 5 and all(
                d.z_units == '%' and math.isclose(safe_math(d.z_equation, 8192), 100.0)
                and math.isclose(safe_math(d.z_equation, 4096), 50.0)
                for d in n75_definitions))
            fuel_csv = Path(temp) / 'fuel-check.csv'
            fuel_csv.write_text(f'time,time_unit,{SIG_RPM},{SIG_IQ},{SIG_SPEED}\n'
                                '0,s,2306.912,31.405,129.042\n', encoding='utf-8')
            fuel_log = TestOLog(fuel_csv, window.editor.log_panel.catalog)
            fuel_l_h = fuel_log.value(FUEL_RATE_SIGNAL, 0)
            fuel_l_100km = fuel_log.value(FUEL_CONSUMPTION_SIGNAL, 0)
            fuel_estimates = (math.isclose(fuel_l_h, 10.411770734371258, rel_tol=1e-9)
                              and math.isclose(fuel_l_100km, 8.068513146395171, rel_tol=1e-9))
            log_panel = window.editor.log_panel
            log_panel.log = fuel_log
            log_panel._populate_signal_combo()
            log_panel._update_profile()
            log_panel._refresh_graph_combo()
            log_panel._redraw()
            fuel_estimate_gauges = all(signal in log_panel.reading_strip.cells
                                      for signal in (FUEL_RATE_SIGNAL, FUEL_CONSUMPTION_SIGNAL))
            from bmw_bin_editor.serial_self_check import run_serial_self_check
            serial_process_check = run_serial_self_check()
            dialog.profile_box.setCurrentText('economy')
            economy_profile = dialog.profile()
            economy_preset = (economy_profile == dialog.presets.get('economy')
                and economy_profile['parameters'] == ['INMOT', 'IMEIA', 'IPLAD', 'SPLAD', 'IALDS',
                    'IPRDR', 'ILMMG', 'IWHE1', 'SMOMO', 'IVKMH', 'ITKUM', 'IDHE1', 'ITANS', 'SPRDR']
                and economy_profile['interval_ms'] == 0
                and sum(dialog.profile_box.itemText(i) == 'economy' for i in range(dialog.profile_box.count())) == 1)
            from bmw_bin_editor.logdata import LiveTestOLog
            held_log = LiveTestOLog(log_panel.catalog)
            held_log.append_sample(0., {SIG_RPM: 1200., SIG_IQ: 20.})
            held_log.append_sample(.49, {SIG_IQ: 20.})
            held_log.append_sample(.5, {SIG_IQ: 20.})
            log_panel.log = held_log
            gauge_age_threshold = (log_panel._display_log_value(SIG_RPM, 1) == (1200., '')
                and log_panel._display_log_value(SIG_RPM, 2) == (1200., ' (0.5s old)'))
            log_panel.log = fuel_log
            report = dict(version=APP_NAME, application_version=__version__, author=APP_AUTHOR,
                          parameters=len(catalog), selected=len(dialog.selected()),
                          logging_action=window.act_live_log.text(), websocket_import=True,
                          bundled_definition=gui.bundled_definition_path().is_file(),
                          empty_startup=empty_startup,
                          tables=len(window.xdf.tables) if window.xdf else 0,
                          xdf_editor=xdf_saved, potential_maps=len(potential_maps),
                          family_suggestions=sum(bool(c.suggestions) for c in family_candidates),
                          definition_library=bool(discover_entries()), definition_matches=len(definition_matches),
                          scaling_evidence=bool(learned_soi), scaling_profiles=len(scaling_profiles),
                          soi_suggested_equation=learned_soi.z_equation if learned_soi else '',
                          soi_reference_profiles=learned_soi.scaling_profile_count if learned_soi else 0,
                          retained_n75_scaling=retained_n75_scaling,
                          native_ols_import=native_ols_import, column_layout=column_layout,
                          fuel_estimates=fuel_estimates, fuel_estimate_gauges=fuel_estimate_gauges,
                          serial_process_check=serial_process_check, gauge_age_threshold=gauge_age_threshold,
                          economy_preset=economy_preset, economy_profile=economy_profile,
                          estimated_fuel_l_h=fuel_l_h, estimated_consumption_l_100km=fuel_l_100km,
                          maximized=window.isMaximized(), frozen=bool(getattr(sys, 'frozen', False)))
            definition_dialog.reject(); dialog.close(); window.close()
        Path(sys.argv[2]).write_text(json.dumps(report, indent=2), encoding='utf-8')
        raise SystemExit(0)
    raise SystemExit(run())
