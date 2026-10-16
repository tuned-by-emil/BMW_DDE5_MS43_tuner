
from optional_resources import requires_resource
import struct
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bmw_bin_editor.core import AxisDefinition, CalibrationDefinition, XDFDatabase
from bmw_bin_editor.map_finder import MapCandidate, scan_maps


ROOT = Path(__file__).resolve().parents[1]


def table_blob(rows=3, cols=4, *, endian='>', sequential=False, descending=False,
               grid=None, first_axis=None, second_axis=None):
    y = list(first_axis) if first_axis is not None else [100 + 100 * r for r in range(rows)]
    x = list(second_axis) if second_axis is not None else [1000 + 500 * c for c in range(cols)]
    if descending:
        y.reverse()
        x.reverse()
    z = list(grid) if grid is not None else [1000 + 30 * r + 10 * c for r in range(rows) for c in range(cols)]
    words = [rows] + y + [cols] + x + z if sequential else [rows, cols] + y + x + z
    return struct.pack(endian + 'H' * len(words), *words)


def find_target(data, address, **kwargs):
    return [candidate for candidate in scan_maps(data, **kwargs) if candidate.address == address]


class MapFinderTests(unittest.TestCase):
    def test_both_layouts_and_both_byte_orders(self):
        for endian in ('>', '<'):
            for sequential in (False, True):
                with self.subTest(endian=endian, sequential=sequential):
                    header = 128
                    blob = table_blob(endian=endian, sequential=sequential)
                    data = b'\xff' * header + blob + b'\xff' * 96
                    z_address = header + (2 + 3 + 4) * 2
                    found = find_target(data, z_address, endian=endian)
                    self.assertEqual(len(found), 1)
                    candidate = found[0]
                    self.assertEqual((candidate.rows, candidate.cols), (3, 4))
                    self.assertEqual(candidate.x_address, header + (2 + 3) * 2)
                    self.assertEqual(candidate.y_address, header + (1 if sequential else 2) * 2)
                    self.assertIn('Y axis, cols' if sequential else 'rows, cols, Y axis', candidate.layout)
                    self.assertGreater(candidate.score, 50)
                    self.assertLessEqual(candidate.score, 100)
                    self.assertFalse(candidate.known)

    def test_ranges_include_complete_structure_and_odd_alignment(self):
        header = 129
        blob = table_blob()
        data = b'\xff' * header + blob + b'\xff' * 100
        address = header + (2 + 3 + 4) * 2
        self.assertEqual(len(find_target(data, address, start=128, end=header + len(blob))), 1)
        self.assertEqual(len(find_target(data, address, start=129, end=header + len(blob))), 1)
        self.assertEqual(find_target(data, address, start=header + 1), [])
        self.assertEqual(find_target(data, address, end=header + len(blob) - 1), [])
        self.assertEqual(scan_maps(data, start=header, end=header), [])

    def test_descending_and_nonconstant_plateau_axes(self):
        for blob in (table_blob(descending=True), table_blob(first_axis=[100, 100, 200])):
            found = find_target(blob, 18)
            self.assertEqual(len(found), 1)
            self.assertTrue(any('axis' in reason for reason in found[0].reasons))

    def test_flat_erased_and_nonmonotonic_patterns_are_rejected(self):
        patterns = [
            table_blob(grid=[0] * 12),
            table_blob(grid=[0xFFFF] * 12),
            table_blob(grid=[1234] * 12),
            table_blob(grid=[0] * 10 + [500, 600]),
            table_blob(first_axis=[100, 300, 200]),
            table_blob(first_axis=[100, 100, 100]),
        ]
        for blob in patterns:
            self.assertEqual(find_target(blob, 18), [])
        self.assertEqual(scan_maps(b'\x00' * 8192), [])
        self.assertEqual(scan_maps(b'\xff' * 8192), [])
        random_data = np.random.default_rng(173).integers(0, 256, size=1024 * 1024, dtype=np.uint8).tobytes()
        self.assertEqual(scan_maps(random_data), [])

    def test_equivalent_layouts_are_deduplicated(self):
        # Both [2,3,Y(2),X(3),Z] and [2,Y(2),3,X(3),Z] match,
        # with the same Z interval and different hypotheses for the first axis.
        blob = table_blob(2, 3, first_axis=[2, 3])
        found = find_target(blob, 14)
        self.assertEqual(len(found), 1)

    def test_swapped_orientation_is_considered_without_claiming_proof(self):
        # A broad row peak with a consistent change between its three rows is
        # more coherent at 3x7 than the header's conventional 7x3 interpretation.
        peak = [0, 20, 40, 70, 40, 20, 0]
        values = [5000 + 50 * r + value for r in range(3) for value in peak]
        blob = table_blob(7, 3, grid=values)
        found = find_target(blob, 24)
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0].rows, found[0].cols), (3, 7))
        self.assertIn('cols, rows, X axis, Y axis', found[0].layout)
        self.assertTrue(any('remains unverified' in reason for reason in found[0].reasons))

    def test_known_z_and_axis_overlaps_are_hidden_or_flagged(self):
        header = 128
        blob = table_blob()
        data = b'\xff' * header + blob + b'\xff' * 128
        address = header + 18
        detected = find_target(data, address)[0]
        existing = detected.to_definition()
        for known in ([existing], [(address + 2, address + 4)],
                      [CalibrationDefinition('Other', '', 'Other', 0, 1, 1,
                                             x_axis=AxisDefinition(detected.x_address, 4))]):
            self.assertEqual(find_target(data, address, known=known), [])
            found = find_target(data, address, known=known, include_known=True)
            self.assertEqual(len(found), 1)
            self.assertTrue(found[0].known)
            self.assertTrue(any('overlaps' in reason for reason in found[0].reasons))
        # Touching half-open spans do not overlap.
        found = find_target(data, address, known=[(header + len(blob), header + len(blob) + 2)])
        self.assertEqual(len(found), 1)
        self.assertFalse(found[0].known)

    def test_raw_definition_preserves_only_observed_structure(self):
        candidate = find_target(table_blob(), 18)[0]
        definition = candidate.to_definition()
        self.assertIsInstance(definition, CalibrationDefinition)
        self.assertEqual(definition.category, 'Suspected maps')
        self.assertEqual((definition.z_address, definition.rows, definition.cols), (18, 3, 4))
        self.assertEqual((definition.z_equation, definition.x_axis.equation, definition.y_axis.equation), ('X', 'X', 'X'))
        self.assertEqual((definition.z_units, definition.x_axis.units, definition.y_axis.units), ('', '', ''))
        self.assertEqual((definition.x_axis.count, definition.y_axis.count), (4, 3))
        self.assertFalse(definition.z_signed)
        self.assertEqual(definition.z_element_bits, 16)
        self.assertIn('UNVERIFIED', definition.description)
        self.assertIn('not a probability', definition.description)
        self.assertIn('signedness', definition.description)
        self.assertEqual(MapCandidate(10, 2, 2, 2, 6).bits, 16)

    def test_cancel_progress_result_limits_and_input_are_stable(self):
        block = table_blob() + b'\xff' * 80
        data = bytearray(block * 200)
        before = bytes(data)
        progress = []
        stopped = scan_maps(data, progress=progress.append, cancelled=lambda: len(progress) >= 2)
        self.assertLess(len(stopped), 200)
        self.assertLess(progress[-1], 100)
        self.assertEqual(bytes(data), before)
        self.assertEqual(scan_maps(data, cancelled=lambda: True), [])
        progress.clear()
        limited = scan_maps(data, max_results=3, progress=progress.append)
        self.assertEqual(len(limited), 3)
        self.assertEqual([item.score for item in limited], sorted((item.score for item in limited), reverse=True))
        self.assertEqual(progress[0], 0)
        self.assertEqual(progress[-1], 100)
        self.assertEqual(progress, sorted(set(progress)))
        self.assertEqual(scan_maps(data, max_results=0), [])
        self.assertEqual(bytes(data), before)

    @requires_resource('definitions/EDC16C31 361830.xdf')
    def test_bundled_adjacent_axes_with_realistic_16x16_dimensions(self):
        xdf = XDFDatabase(ROOT / 'definitions' / 'EDC16C31 361830.xdf')
        definition = next(d for d in xdf.tables if d.z_address == 0xE80EA)
        self.assertEqual(definition.x_axis.address, definition.y_axis.address + definition.rows * 2)
        self.assertEqual(definition.z_address, definition.x_axis.address + definition.cols * 2)
        header = definition.y_axis.address - 4
        blob = table_blob(definition.rows, definition.cols)
        data = b'\xff' * header + blob
        candidates = find_target(data, definition.z_address)
        self.assertEqual(len(candidates), 1)
        self.assertEqual((candidates[0].rows, candidates[0].cols), (16, 16))
        self.assertEqual(candidates[0].x_address, definition.x_axis.address)
        self.assertEqual(candidates[0].y_address, definition.y_axis.address)
        self.assertEqual(find_target(data, definition.z_address, known=xdf.definitions), [])

    def test_invalid_arguments_fail_explicitly(self):
        data = table_blob()
        for kwargs in ({'endian': 'big'}, {'start': -1}, {'end': len(data) + 1},
                       {'start': 10, 'end': 5}, {'max_axis': 1}, {'max_axis': 33},
                       {'max_results': -1}, {'known': [(20, 10)]}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    scan_maps(data, **kwargs)
        with self.assertRaises(TypeError):
            scan_maps('not bytes')
        with self.assertRaises(TypeError):
            scan_maps(data, known=['not a span or definition'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
