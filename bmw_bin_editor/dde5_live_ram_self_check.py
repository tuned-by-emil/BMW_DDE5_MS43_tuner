"""Packaged live RAM smoke check: actual controller, generated ECU/UART model."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

from .core import AxisDefinition, CalibrationDefinition, CalibrationDocument
from .dde5_live_ram import LiveRamController
from .dde5_ram_calibration import BOUNDARIES
from .dde5_ram_self_check import _SyntheticEcu


class _LiveSyntheticEcu(_SyntheticEcu):
    """Extend the packaged bench model to native multi-byte managed writes."""

    def response(self, payload):
        if payload[0] == 0x3D:
            count = payload[4]
            assert 1 <= count <= 250 and len(payload) == 5 + count
            address = int.from_bytes(payload[1:4], 'big')
            # Copy-on-first-write and the two allocator pools are supplied by
            # the original model. Each byte then updates the same shadow block.
            for offset, value in enumerate(payload[5:]):
                super().response(b'\x3D' + (address + offset).to_bytes(3, 'big')
                                  + b'\x01' + bytes([value]))
            return b'\x7D'
        return super().response(payload)


def _run_generated_checks():
    from PySide6.QtWidgets import QApplication
    from . import dde5_live_ram_gui as live_gui
    from .gui import MainWindow

    baseline = bytes(range(256)) * 4096
    definition = CalibrationDefinition('Synthetic live Main SOI', 'Generated smoke fixture',
        'Self-check', 0xEA80C, 1, 2, x_axis=AxisDefinition(0xE0584, 2), profile_id='361830')
    ecu = _LiveSyntheticEcu(baseline)
    hardware = dict(capacity_bytes=32768, confirmed=True,
        reference='Generated simulation only: wiring, bank and usable capacity are not physically verified')
    controller = LiveRamController('COM8', baseline, hardware, echo=True,
        serial_factory=ecu.factory, clock=ecu.clock, sleep=ecu.clock.sleep)
    reports = {}
    checks = {}
    edited = bytearray(baseline)
    edited[definition.z_address:definition.z_address + 4] = bytes.fromhex('44556677')
    axis_address = definition.x_axis.address
    edited[axis_address:axis_address + 4] = bytes.fromhex('AABBCCDD')
    try:
        reports['locked_before'] = controller.apply(definition, bytes(edited))
        checks['locked_before_qualification'] = (reports['locked_before']['status'] == 'failed'
                                                  and not controller.qualified and not ecu.ports)
        reports['qualify'] = controller.qualify(definition)
        qualification = reports['qualify']
        checks['actual_reversible_qualification_passed'] = (
            qualification['status'] == 'passed' and controller.qualified
            and qualification['details'].get('changed_probe', {}).get('exact_readback_verified') is True
            and qualification['restore']['write'] == 'verified'
            and qualification['restore']['reference_page'] == 'verified')
        checks['same_port_retained_after_qualification'] = (
            len(ecu.ports) == 1 and ecu.ports[0].is_open
            and qualification['details'].get('connection_retained') is True)

        # Verify the modeless GUI opens without constructing a controller. Even
        # the actual passing report from the separate synthetic link cannot
        # unlock its controls without its own qualified worker connection.
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory(prefix='dde5-live-smoke-') as folder:
            binary = Path(folder) / 'generated.FLS'
            binary.write_bytes(baseline)
            document = CalibrationDocument(SimpleNamespace(default_lsb_first=False,
                profile_id='361830'), binary)
            gui_calls = 0
            prior_controller = live_gui.LiveRamController
            def reject_gui_controller(*_args, **_kwargs):
                nonlocal gui_calls
                gui_calls += 1
                raise AssertionError('Opening the live RAM dialog must not connect.')
            context = lambda: dict(document=document, original=document.original,
                edited=bytes(document.data), definition=definition, profile_id='361830')
            dialog = None
            try:
                live_gui.LiveRamController = reject_gui_controller
                dialog = live_gui.Dde5LiveRamDialog(context, lambda *_args: None)
                dialog.show()
                app.processEvents()
                checks['gui_opens_without_connection'] = (gui_calls == 0 and dialog.worker is None
                    and not dialog._qualified and not dialog.isModal()
                    and not dialog.upload_button.isEnabled())
                dialog.reports.append(dict(action='qualify', report=deepcopy(qualification)))
                dialog.refresh_controls()
                checks['passing_report_cannot_unlock_gui'] = (
                    not dialog._qualified and not dialog.upload_button.isEnabled()
                    and dialog.worker is None and gui_calls == 0)
            finally:
                if dialog is not None:
                    dialog.close()
                    app.processEvents()
                live_gui.LiveRamController = prior_controller

            reports['register'] = controller.register_table(definition, include_axes=True)
            checks['table_and_axis_blocks_registered'] = (
                reports['register']['status'] == 'passed' and {1, 141}.issubset(ecu.shadow))
            reports['upload'] = controller.apply(definition, bytes(edited), include_axes=True)
            checks['table_and_axis_upload_exact'] = (reports['upload']['status'] == 'passed'
                and ecu.mapped(0x800000 + definition.z_address, 4)
                    == bytes(edited[definition.z_address:definition.z_address + 4])
                and ecu.mapped(0x800000 + axis_address, 4) == bytes(edited[axis_address:axis_address + 4])
                and reports['upload']['details']['page'] == 'working'
                and reports['upload']['restore']['write'] == 'retained_verified')
            reports['read'] = controller.read(definition, include_axes=True)
            updates = live_gui.checked_updates(reports['read'], definition, True, bytes(document.data))
            refreshes = []
            window_context = SimpleNamespace(document=document,
                editor=SimpleNamespace(refresh=lambda: refreshes.append('refresh')),
                _document_changed=lambda: refreshes.append('changed'))
            captured = bytes(document.data)
            changed = MainWindow._apply_dde5_ram_readback(window_context, document, captured, updates)
            checks['gui_readback_is_one_undoable_edit'] = (
                reports['read']['status'] == 'passed' and changed and len(document.undo_stack) == 1
                and bytes(document.data) == bytes(edited) and refreshes == ['refresh', 'changed'])
            document.undo()
            checks['readback_undo_preserves_source_file'] = (
                bytes(document.data) == baseline and binary.read_bytes() == baseline)
            try:
                MainWindow._apply_dde5_ram_readback(window_context, document, b'stale snapshot', updates)
                checks['stale_editor_context_rejected'] = False
            except ValueError:
                checks['stale_editor_context_rejected'] = bytes(document.data) == baseline

        reports['restore'] = controller.restore(definition, include_axes=True)
        checks['selected_table_and_axes_restored_exactly'] = (reports['restore']['status'] == 'passed'
            and ecu.mapped(0x800000 + definition.z_address, 4)
                == baseline[definition.z_address:definition.z_address + 4]
            and ecu.mapped(0x800000 + axis_address, 4) == baseline[axis_address:axis_address + 4])
        reports['reference_page'] = controller.select_page('reference')
        checks['reference_page_button_verified'] = (
            reports['reference_page']['status'] == 'passed' and ecu.get(0x3FE544) == 0)
        reports['working_page'] = controller.select_page('working')
        checks['working_page_button_verified'] = (
            reports['working_page']['status'] == 'passed' and ecu.get(0x3FE544) != 0)
    finally:
        reports['disconnect'] = controller.disconnect()

    checks['reference_restored_and_same_port_closed'] = (
        reports['disconnect']['status'] == 'passed'
        and reports['disconnect']['restore']['reference_page'] == 'verified'
        and not controller.qualified and ecu.get(0x3FE544) == 0
        and len(ecu.ports) == 1 and ecu.ports[0].closed and not ecu.ports[0].is_open)
    before = len(ecu.payloads)
    reports['locked_after'] = controller.apply(definition, bytes(edited), include_axes=True)
    checks['locked_after_disconnect_no_reconnect'] = (
        reports['locked_after']['status'] == 'failed' and not controller.qualified
        and len(ecu.payloads) == before and len(ecu.ports) == 1)
    writes = [payload for payload in ecu.payloads if payload[0] == 0x3D]
    checks['multi_byte_chunks_exercised'] = any(payload[4] > 1 for payload in writes)
    spans = [(0x800000 + definition.z_address, 4), (0x800000 + axis_address, 4)]
    checks['all_writes_bounded_to_table_and_axes'] = all(
        all(any(start <= target < start + count for start, count in spans)
            for target in range(int.from_bytes(payload[1:4], 'big'),
                                int.from_bytes(payload[1:4], 'big') + payload[4])) for payload in writes)
    checks['all_shadow_bytes_restored'] = all(
        bytes(shadow) == ecu.load(BOUNDARIES[index], len(shadow))
        for index, shadow in ecu.shadow.items())
    checks['no_reset_flash_or_auth_commands'] = not any(
        payload[0] in (0x11, 0x27, 0x34, 0x36, 0x37) for payload in ecu.payloads)
    checks['only_9600_baud'] = all(frame['host_baud'] == 9600
        for item in reports.values() for frame in item.get('frames', []))
    return {'ok': all(checks.values()), 'checks': checks, 'reports': reports}


def run_check(path):
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from . import __version__
    output = dict(ok=False, version=__version__, frozen=bool(getattr(sys, 'frozen', False)),
        simulation='generated 1 MiB BIN and high-level RAM/UART model; not ECU instruction emulation',
        physical_port_opened=False, network_used=False)
    attempts = 0
    serial = previous = None
    try:
        import serial
        previous = serial.Serial
        def reject_os_port(*_args, **_kwargs):
            nonlocal attempts
            attempts += 1
            raise AssertionError('The live RAM smoke check must not open an OS serial port.')
        serial.Serial = reject_os_port
        output.update(_run_generated_checks())
        output['ok'] = output['ok'] and attempts == 0
    except Exception as exc:
        output.update(ok=False, error=f'{type(exc).__name__}: {exc}')
    finally:
        if serial is not None and previous is not None:
            serial.Serial = previous
    output['os_port_attempts'] = attempts
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(output, indent=2) + '\n', encoding='utf-8')
    return output['ok']
