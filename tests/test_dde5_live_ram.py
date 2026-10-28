"""Persistent live-RAM qualification and editing on an in-memory UART only."""
import json
import unittest

from bmw_bin_editor.core import AxisDefinition, CalibrationDefinition
from bmw_bin_editor.dde5_live_ram import LiveRamController, editable_spans
from bmw_bin_editor.dde5_ram_calibration import BOUNDARIES, EXTERNAL_BASE
from tests.test_dde5_ram_calibration import BIN, Fixture, hardware


def table(address=0x8EA80C, *, rows=1, cols=4, bits=16, axes=False):
    return CalibrationDefinition('Live fixture table', '', 'Tests', address-0x800000,
        rows, cols, z_element_bits=bits, profile_id='361830',
        x_axis=AxisDefinition(0xEA7D0, cols) if axes else None,
        y_axis=AxisDefinition(0xEC6D0, rows) if axes else None)


class LiveFixture(Fixture):
    """Same native manager fixture, extended to managed multi-byte requests."""
    def __init__(self, **options):
        super().__init__(**options)
        self.controllers = []

    def response(self, payload):
        if payload[0] != 0x3D:
            return super().response(payload)
        assert 1 <= payload[4] <= 250 and len(payload) == 5+payload[4]
        start = int.from_bytes(payload[1:4], 'big')
        for target, value in zip(range(start, start+payload[4]), payload[5:]):
            index = next(i for i in range(279) if BOUNDARIES[i] <= target < BOUNDARIES[i+1])
            if index not in self.shadow:
                low, high = BOUNDARIES[index:index+2]
                self.shadow[index] = bytearray(self.load(low, high-low))
                internal = self.get(0x3FE554)
                if internal+high-low <= 0x3FA178:
                    self.put(0x3FE554, internal+high-low)
                else:
                    self.put(0x3FE558, self.get(0x3FE558)+high-low)
            self.shadow[index][target-BOUNDARIES[index]] = value
        return b'\x7D'

    def controller(self, *, original=BIN):
        result = LiveRamController('COM6', original, hardware(), echo=self.echo,
            cancelled=lambda: self.cancel, serial_factory=self.factory,
            clock=self.clock, sleep=self.clock.sleep)
        self.controllers.append(result)
        return result

    def shadow_bytes(self, address, count):
        result = bytearray()
        for target in range(address, address+count):
            index = next(i for i in range(279) if BOUNDARIES[i] <= target < BOUNDARIES[i+1])
            result.append(self.shadow[index][target-BOUNDARIES[index]])
        return bytes(result)


class LiveRamTests(unittest.TestCase):
    def setUp(self):
        self.fixtures = []

    def fixture(self, **options):
        result = LiveFixture(**options)
        self.fixtures.append(result)
        return result

    def tearDown(self):
        for fixture in self.fixtures:
            for controller in fixture.controllers:
                controller.disconnect()

    def qualify(self, fixture, selected=None):
        controller = fixture.controller()
        report = controller.qualify(selected or table())
        self.assertEqual(report['status'], 'passed', report)
        self.assertTrue(controller.qualified)
        self.assertEqual(len(fixture.created), 1)
        self.assertTrue(fixture.uart.is_open)
        return controller

    def test_no_open_or_gate_without_actual_same_connection_qualification(self):
        fixture = self.fixture()
        controller = fixture.controller()
        self.assertFalse(controller.qualified)
        self.assertEqual(fixture.created, [])
        for operation in (lambda: controller.register_table(table()),
                          lambda: controller.apply(table(), BIN),
                          lambda: controller.read(table()),
                          lambda: controller.restore(table()),
                          lambda: controller.select_page('working')):
            report = operation()
            self.assertNotEqual(report['status'], 'passed', report)
            self.assertFalse(controller.qualified)
        self.assertEqual(fixture.created, [])

    def test_qualification_proves_changed_byte_restore_and_keeps_only_one_uart(self):
        fixture = self.fixture()
        controller = self.qualify(fixture)
        writes = [payload for payload in fixture.events if payload[0] == 0x3D]
        original = BIN[0xEA80C:0xEA80D]
        self.assertEqual([payload[5:] for payload in writes],
                         [original, bytes([original[0] ^ 1]), original])
        self.assertEqual(fixture.shadow_bytes(0x8EA80C, 2), BIN[0xEA80C:0xEA80E])
        self.assertEqual(fixture.get(0x3FE544), 0)
        self.assertEqual(controller.select_page('working')['status'], 'passed')
        self.assertEqual(fixture.get(0x3FE544), EXTERNAL_BASE-0x27250)
        self.assertEqual(controller.select_page('reference')['status'], 'passed')
        self.assertEqual(fixture.get(0x3FE544), 0)
        self.assertEqual(len(fixture.created), 1)

    def test_prior_report_json_does_not_qualify_another_controller(self):
        first = self.fixture()
        controller = self.qualify(first)
        report = controller.read(table())
        json.loads(json.dumps(report))
        other = self.fixture()
        other_controller = other.controller()
        self.assertFalse(other_controller.qualified)
        self.assertNotEqual(other_controller.apply(table(), BIN)['status'], 'passed')
        self.assertEqual(other.created, [])

    def test_failed_reversible_probe_keeps_gate_locked_even_if_cleanup_succeeds(self):
        def corrupt_changed(request, normal, fixture):
            if request[0] == 0x3D and request[5:] == bytes([BIN[0xEA80C] ^ 1]):
                fixture.shadow[141][0x44] ^= 2
            return normal
        fixture = self.fixture(response_hook=corrupt_changed)
        controller = fixture.controller()
        report = controller.qualify(table())
        self.assertNotEqual(report['status'], 'passed', report)
        self.assertFalse(controller.qualified)
        self.assertTrue(fixture.uart.closed)
        self.assertEqual(fixture.get(0x3FE544), 0)
        self.assertEqual(fixture.shadow_bytes(0x8EA80C, 2), BIN[0xEA80C:0xEA80E])

    def test_apply_and_read_exact_bytes_only_inside_selected_values(self):
        fixture = self.fixture()
        controller = self.qualify(fixture)
        edited = bytearray(BIN)
        edited[0xEA80C:0xEA80E] = b'\x95\x26'
        edited[0xEA820] ^= 1  # Same allocated block, outside selected table.
        edited[0xE0580] ^= 1  # Completely different block.
        report = controller.apply(table(), bytes(edited))
        self.assertEqual(report['status'], 'passed', report)
        self.assertTrue(controller.qualified)
        self.assertEqual(fixture.shadow_bytes(0x8EA80C, 8), bytes(edited[0xEA80C:0xEA814]))
        self.assertEqual(fixture.shadow_bytes(0x8EA820, 1), BIN[0xEA820:0xEA821])
        self.assertEqual(set(fixture.shadow), {141})
        self.assertEqual(fixture.load(0x8EA80C, 8), BIN[0xEA80C:0xEA814])
        self.assertEqual(fixture.get(0x3FE544), EXTERNAL_BASE-0x27250)
        report = controller.read(table())
        self.assertEqual(report['status'], 'passed', report)
        updates = {row['file_offset']+i: value for row in report['updates']
                   for i, value in enumerate(bytes.fromhex(row['hex']))}
        self.assertEqual(bytes(updates[0xEA80C+i] for i in range(8)), bytes(edited[0xEA80C:0xEA814]))
        self.assertEqual(len(fixture.created), 1)

    def test_editable_axes_are_opt_in_and_can_allocate_another_pool(self):
        fixture = self.fixture()
        controller = self.qualify(fixture)
        selected = table(rows=2, cols=4, axes=True)
        edited = bytearray(BIN)
        edited[0xEA80C] ^= 1
        edited[0xEA7D0] ^= 2
        edited[0xEC6D0] ^= 4
        report = controller.apply(selected, bytes(edited), include_axes=False)
        self.assertEqual(report['status'], 'passed', report)
        self.assertEqual(fixture.shadow_bytes(0x8EA7D0, 1), BIN[0xEA7D0:0xEA7D1])
        self.assertNotIn(144, fixture.shadow)
        report = controller.apply(selected, bytes(edited), include_axes=True)
        self.assertEqual(report['status'], 'passed', report)
        self.assertEqual(fixture.shadow_bytes(0x8EA7D0, 1), bytes(edited[0xEA7D0:0xEA7D1]))
        self.assertEqual(fixture.shadow_bytes(0x8EC6D0, 1), bytes(edited[0xEC6D0:0xEC6D1]))
        self.assertGreater(fixture.get(0x3FE554), 0x3F9FD4)

    def test_registering_another_table_in_same_block_preserves_prior_ram_changes(self):
        fixture = self.fixture()
        controller = self.qualify(fixture)
        edited = bytearray(BIN)
        edited[0xEA80C] ^= 0x80
        self.assertEqual(controller.apply(table(), bytes(edited))['status'], 'passed')
        before = fixture.get(0x3FE558)
        report = controller.register_table(table(0x8EA840))
        self.assertEqual(report['status'], 'passed', report)
        self.assertEqual(fixture.get(0x3FE558), before)
        self.assertEqual(fixture.shadow_bytes(0x8EA80C, 1), bytes(edited[0xEA80C:0xEA80D]))

    def test_restore_selected_values_does_not_erase_neighbor_table(self):
        fixture = self.fixture()
        controller = self.qualify(fixture)
        edited = bytearray(BIN)
        edited[0xEA80C] ^= 0x40
        edited[0xEA840] ^= 0x80
        self.assertEqual(controller.apply(table(), bytes(edited))['status'], 'passed')
        neighbor = table(0x8EA840)
        self.assertEqual(controller.apply(neighbor, bytes(edited))['status'], 'passed')
        report = controller.restore(table())
        self.assertEqual(report['status'], 'passed', report)
        self.assertEqual(fixture.shadow_bytes(0x8EA80C, 8), BIN[0xEA80C:0xEA814])
        self.assertEqual(fixture.shadow_bytes(0x8EA840, 1), bytes(edited[0xEA840:0xEA841]))
        self.assertTrue(controller.qualified)

    def test_registration_bad_live_flash_sends_no_managed_copy_and_locks(self):
        fixture = self.fixture()
        controller = self.qualify(fixture)
        fixture.store(0x8ED000, b'\xEE')
        before = len(fixture.events)
        report = controller.register_table(table(0x8ED010))
        self.assertNotEqual(report['status'], 'passed', report)
        self.assertFalse(any(p[0] == 0x3D for p in fixture.events[before:]))
        self.assertFalse(controller.qualified)
        self.assertTrue(fixture.uart.closed)

    def test_total_budget_is_checked_before_any_additional_block_copy(self):
        fixture = self.fixture()
        controller = self.qualify(fixture)
        before = len(fixture.events)
        report = controller.register_table(table(0x8E057C, cols=40000, bits=8))
        self.assertNotEqual(report['status'], 'passed', report)
        self.assertFalse(any(p[0] == 0x3D for p in fixture.events[before:]))
        self.assertLessEqual(fixture.get(0x3FE558), EXTERNAL_BASE+32768)

    def test_protocol_fault_after_changed_write_locks_and_sends_no_more_traffic(self):
        fixture = self.fixture()
        controller = self.qualify(fixture)
        edited = bytearray(BIN)
        edited[0xEA80C] ^= 0x80
        fixture.fault = lambda payload: 'crc' if payload[0] == 0x3D else None
        report = controller.apply(table(), bytes(edited))
        self.assertNotEqual(report['status'], 'passed', report)
        self.assertFalse(controller.qualified)
        self.assertTrue(fixture.uart.closed)
        self.assertEqual(fixture.events[-1][0], 0x3D)
        count = len(fixture.events)
        self.assertNotEqual(controller.read(table())['status'], 'passed')
        self.assertEqual(len(fixture.events), count)
        self.assertEqual(len(fixture.created), 1)

    def test_semantic_changed_readback_failure_rolls_back_then_locks_and_closes(self):
        fixture = self.fixture()
        controller = self.qualify(fixture)
        edited = bytearray(BIN)
        edited[0xEA80C] ^= 0x80
        def corrupt_upload(request, normal, fixture):
            if request[0] == 0x3D and request[5:] == bytes(edited[0xEA80C:0xEA80D]):
                fixture.shadow[141][0x44] ^= 2
            return normal
        fixture.response_hook = corrupt_upload
        report = controller.apply(table(), bytes(edited))
        self.assertNotEqual(report['status'], 'passed', report)
        self.assertFalse(controller.qualified)
        self.assertTrue(fixture.uart.closed)
        self.assertEqual(fixture.shadow_bytes(0x8EA80C, 8), BIN[0xEA80C:0xEA814])
        self.assertEqual(fixture.get(0x3FE544), 0)

    def test_disconnect_selects_reference_closes_and_invalidates_gate(self):
        fixture = self.fixture()
        controller = self.qualify(fixture)
        self.assertEqual(controller.select_page('working')['status'], 'passed')
        report = controller.disconnect()
        self.assertEqual(report['status'], 'passed', report)
        self.assertFalse(controller.qualified)
        self.assertEqual(fixture.get(0x3FE544), 0)
        self.assertTrue(fixture.uart.closed)
        count = len(fixture.events)
        controller.disconnect()
        self.assertEqual(len(fixture.events), count)

    def test_qualification_requires_verified_reference_restoration_before_gate(self):
        fixture = self.fixture(fault=lambda payload: 'partial' if payload == b'\x80\x90\x00' else None)
        controller = fixture.controller()
        report = controller.qualify(table())
        self.assertNotEqual(report['status'], 'passed', report)
        self.assertFalse(controller.qualified)
        self.assertTrue(fixture.uart.closed)
        self.assertEqual(fixture.events[-1], b'\x80\x90\x00')

    def test_cancel_before_qualification_never_creates_uart(self):
        fixture = self.fixture()
        fixture.cancel = True
        controller = fixture.controller()
        report = controller.qualify(table())
        self.assertEqual(report['status'], 'cancelled', report)
        self.assertFalse(controller.qualified)
        self.assertEqual(fixture.created, [])

    def test_partial_computed_result_read_after_upload_stops_without_cleanup_traffic(self):
        fixture = self.fixture()
        controller = self.qualify(fixture)
        edited = bytearray(BIN)
        edited[0xEA80C] ^= 0x80
        wrote = False
        def after_write(payload):
            nonlocal wrote
            if payload[0] == 0x3D:
                wrote = True
        fixture.on_request = after_write
        fixture.fault = lambda payload: 'partial' if wrote and payload == b'\x23\x3F\xE5\x62\x02' else None
        report = controller.apply(table(), bytes(edited))
        self.assertNotEqual(report['status'], 'passed', report)
        self.assertFalse(controller.qualified)
        self.assertEqual(fixture.events[-1], b'\x23\x3F\xE5\x62\x02')
        self.assertTrue(fixture.uart.closed)

    def test_cursor_drift_after_qualification_prevents_another_write(self):
        fixture = self.fixture()
        controller = self.qualify(fixture)
        fixture.put(0x3FE558, fixture.get(0x3FE558)+4)
        edited = bytearray(BIN)
        edited[0xEA80C] ^= 0x80
        before = len(fixture.events)
        report = controller.apply(table(), bytes(edited))
        self.assertNotEqual(report['status'], 'passed', report)
        self.assertFalse(controller.qualified)
        self.assertFalse(any(payload[0] == 0x3D for payload in fixture.events[before:]))

    def test_cancel_during_second_edit_restores_previous_ram_value_then_locks(self):
        fixture = self.fixture()
        controller = self.qualify(fixture)
        first = bytearray(BIN)
        first[0xEA80C] ^= 0x40
        self.assertEqual(controller.apply(table(), bytes(first))['status'], 'passed')
        second = bytearray(first)
        second[0xEA80C] ^= 0x80
        fixture.on_request = lambda payload: setattr(fixture, 'cancel', True) if payload[0] == 0x3D else None
        report = controller.apply(table(), bytes(second))
        self.assertEqual(report['status'], 'cancelled', report)
        self.assertFalse(controller.qualified)
        self.assertTrue(fixture.uart.closed)
        self.assertEqual(fixture.shadow_bytes(0x8EA80C, 8), bytes(first[0xEA80C:0xEA814]))
        self.assertEqual(fixture.get(0x3FE544), 0)

    def test_non_bytes_upload_is_rejected_before_any_live_write(self):
        for edited in (None, 0x100000, 'not a captured BIN'):
            with self.subTest(edited=edited):
                fixture = self.fixture()
                controller = self.qualify(fixture)
                before = len(fixture.events)
                report = controller.apply(table(), edited)
                self.assertNotEqual(report['status'], 'passed',
                    {key: report.get(key) for key in ('status', 'error', 'restore')})
                self.assertFalse(any(payload[0] == 0x3D for payload in fixture.events[before:]))

    def test_cancel_at_final_upload_step_rolls_back_before_locking(self):
        fixture = self.fixture()
        controller = self.qualify(fixture)
        edited = bytearray(BIN)
        edited[0xEA80C] ^= 0x80
        controller.progress = lambda message: setattr(fixture, 'cancel', True) if message.startswith('Uploaded and exactly verified') else None
        report = controller.apply(table(), bytes(edited))
        self.assertEqual(report['status'], 'cancelled', report)
        self.assertFalse(controller.qualified)
        self.assertEqual(report['restore']['write'], 'verified')
        self.assertEqual(fixture.shadow_bytes(0x8EA80C, 8), BIN[0xEA80C:0xEA814])
        self.assertEqual(fixture.get(0x3FE544), 0)

    def test_multi_cell_upload_splits_at_transport_and_block_limits(self):
        for selected, expected_counts in ((table(cols=300, bits=8), [250, 50]),
                                           (table(0x8EB868, cols=4, bits=8), [2, 2])):
            with self.subTest(address=selected.z_address, count=selected.size):
                fixture = self.fixture()
                controller = self.qualify(fixture, selected)
                edited = bytearray(BIN)
                low, high = selected.z_address, selected.z_address+selected.size
                edited[low:high] = bytes(value ^ 0x80 for value in edited[low:high])
                before = len(fixture.events)
                report = controller.apply(selected, bytes(edited))
                self.assertEqual(report['status'], 'passed', report)
                writes = [payload for payload in fixture.events[before:] if payload[0] == 0x3D]
                self.assertEqual([payload[4] for payload in writes], expected_counts)
                for payload in writes:
                    address = int.from_bytes(payload[1:4], 'big')
                    block = next(i for i in range(279) if BOUNDARIES[i] <= address < BOUNDARIES[i+1])
                    self.assertLessEqual(address+payload[4], BOUNDARIES[block+1])
                self.assertEqual(fixture.shadow_bytes(low+0x800000, high-low), bytes(edited[low:high]))

    def test_second_chunk_semantic_failure_rolls_back_all_attempted_chunks(self):
        fixture = self.fixture()
        selected = table(cols=260, bits=8)
        controller = self.qualify(fixture, selected)
        edited = bytearray(BIN)
        low, high = selected.z_address, selected.z_address+selected.size
        edited[low:high] = bytes(value ^ 0x80 for value in edited[low:high])
        second = 0x8EA80C+250
        def corrupt_second(request, normal, fixture):
            if (request[0] == 0x3D and int.from_bytes(request[1:4], 'big') == second
                    and request[5:] == bytes(edited[low+250:high])):
                fixture.shadow[141][second-BOUNDARIES[141]] ^= 2
            return normal
        fixture.response_hook = corrupt_second
        before = len(fixture.events)
        report = controller.apply(selected, bytes(edited))
        self.assertNotEqual(report['status'], 'passed', report)
        self.assertFalse(controller.qualified)
        self.assertEqual(report['restore']['write'], 'verified')
        writes = [payload for payload in fixture.events[before:] if payload[0] == 0x3D]
        self.assertEqual([int.from_bytes(payload[1:4], 'big') for payload in writes],
                         [0x8EA80C, second, second, 0x8EA80C])
        self.assertEqual(fixture.shadow_bytes(0x8EA80C, 260), BIN[low:high])
        self.assertEqual(fixture.get(0x3FE544), 0)

    def test_port_close_failure_invalidates_gate_and_returns_failed_report(self):
        fixture = self.fixture()
        controller = self.qualify(fixture)
        fixture.close_error = True
        report = controller.disconnect()
        self.assertEqual(report['status'], 'failed', report)
        self.assertFalse(controller.qualified)
        self.assertEqual(fixture.get(0x3FE544), 0)
        self.assertTrue(report['details']['port_cleanup_error'])
        later = controller.disconnect()
        self.assertEqual(later['status'], 'failed', later)
        self.assertIs(later['details']['port_closed'], False)

    def test_repeated_disconnect_preserves_uncertain_write_and_page_recovery_flags(self):
        fixture = self.fixture()
        controller = self.qualify(fixture)
        edited = bytearray(BIN)
        edited[0xEA80C] ^= 0x80
        fixture.fault = lambda payload: 'crc' if payload[0] == 0x3D else None
        failed = controller.apply(table(), bytes(edited))
        self.assertEqual(failed['restore']['write'], 'unknown')
        self.assertTrue(failed['restore']['power_cycle_required'])
        count = len(fixture.events)
        for _ in range(2):
            report = controller.disconnect()
            self.assertNotEqual(report['status'], 'passed', report)
            self.assertTrue(report['restore']['power_cycle_required'])
            self.assertTrue(report['restore']['restoration_required'])
            self.assertEqual(report['restore']['write'], 'unknown')
            self.assertEqual(report['restore']['reference_page'], 'unknown')
        self.assertEqual(len(fixture.events), count)
        self.assertEqual(len(fixture.created), 1)

    def test_failed_qualification_recovery_flags_survive_later_disconnect(self):
        fixture = self.fixture(fault=lambda payload: 'partial' if payload == b'\x80\x90\x00' else None)
        controller = fixture.controller()
        failed = controller.qualify(table())
        self.assertNotEqual(failed['status'], 'passed', failed)
        self.assertTrue(failed['restore']['power_cycle_required'])
        count = len(fixture.events)
        report = controller.disconnect()
        self.assertNotEqual(report['status'], 'passed', report)
        self.assertTrue(report['restore']['power_cycle_required'])
        self.assertEqual(report['restore']['reference_page'], 'unknown')
        self.assertEqual(len(fixture.events), count)

    def test_failed_rollback_recovery_flags_survive_later_disconnect(self):
        fixture = self.fixture()
        controller = self.qualify(fixture)
        edited = bytearray(BIN)
        edited[0xEA80C] ^= 0x80
        changed = False
        def corrupt_then_fault_rollback(payload, normal, fixture):
            nonlocal changed
            if payload[0] == 0x3D:
                if payload[5:] == bytes(edited[0xEA80C:0xEA80D]):
                    changed = True
                    fixture.shadow[141][0x44] ^= 2
                elif changed:
                    fixture.fault = lambda request: 'crc' if request[0] == 0x3D else None
            return normal
        fixture.response_hook = corrupt_then_fault_rollback
        failed = controller.apply(table(), bytes(edited))
        self.assertEqual(failed['restore']['write'], 'unknown', failed)
        self.assertTrue(failed['restore']['power_cycle_required'])
        count = len(fixture.events)
        report = controller.disconnect()
        self.assertTrue(report['restore']['power_cycle_required'], report)
        self.assertEqual(report['restore']['write'], 'unknown')
        self.assertEqual(len(fixture.events), count)

    def test_manager_ownership_drift_prevents_unverified_rollback_writes(self):
        fixture = self.fixture()
        selected = table(cols=260, bits=8)
        controller = self.qualify(fixture, selected)
        edited = bytearray(BIN)
        low, high = selected.z_address, selected.z_address+selected.size
        edited[low:high] = bytes(value ^ 0x80 for value in edited[low:high])
        drift_at = None
        def change_owner(message):
            nonlocal drift_at
            if message.startswith('Uploaded and exactly verified') and drift_at is None:
                drift_at = len(fixture.events)
                fixture.put(0x3FE541, 6, 1)
        controller.progress = change_owner
        report = controller.apply(selected, bytes(edited))
        self.assertNotEqual(report['status'], 'passed', report)
        self.assertIsNotNone(drift_at)
        self.assertFalse(any(payload[0] == 0x3D for payload in fixture.events[drift_at:]))
        self.assertEqual(report['restore']['write'], 'unknown')
        self.assertTrue(report['restore']['restoration_required'])
        self.assertFalse(controller.qualified)


class SpanTests(unittest.TestCase):
    def test_overlapping_and_adjacent_axes_merge_without_including_unrelated_gaps(self):
        selected = table()
        selected.x_axis = AxisDefinition(0xEA808, 3)
        selected.y_axis = AxisDefinition(0xEA814, 2)
        self.assertEqual(editable_spans(selected), [dict(file_offset=0xEA808, bytes=16,
                                                       logical_address=0x8EA808)])
        selected.y_axis.address = 0xEA820
        spans = editable_spans(selected)
        self.assertEqual([(s['file_offset'], s['bytes']) for s in spans],
                         [(0xEA808, 12), (0xEA820, 4)])

    def test_fixed_axes_and_invalid_storage_do_not_expand_memory_permissions(self):
        selected = table()
        selected.x_axis = AxisDefinition(None, 4, labels=['1', '2', '3', '4'])
        self.assertEqual(editable_spans(selected), editable_spans(table()))
        for address, count, bits in ((0x3F9844, 1, 8), (0xE057B, 2, 16),
                                     (0xFE10F, 2, 8), (0xEA7D0, 0, 16),
                                     (0xEA7D0, 2, 24)):
            selected = table()
            selected.x_axis = AxisDefinition(address, count, element_bits=bits)
            with self.subTest(address=address, count=count, bits=bits), self.assertRaises(ValueError):
                editable_spans(selected)
        with self.assertRaises(ValueError):
            editable_spans(table(), include_axes=1)


if __name__ == '__main__':
    unittest.main()
