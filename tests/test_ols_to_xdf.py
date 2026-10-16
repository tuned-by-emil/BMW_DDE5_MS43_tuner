"""Native OLS imports retain typed values, source axes and column traversal."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bmw_bin_editor.core import CalibrationDocument, EditorError, XDFDatabase
from bmw_bin_editor.definition_library import propose_matches
from bmw_bin_editor.ols_import import OLSProject, OLSVersion, load_ols
from bmw_bin_editor.ols_to_xdf import convert_ols
from generate_definition_library import generate_library


def digest(data):
    return hashlib.sha256(data).hexdigest()


def image():
    data = bytearray(512)
    struct.pack_into('>HHHHHHHhhhhhh', data, 0x20,
                     3, 2, 500, 700, 1000, 0, 1000, -77, 384, -26, 100, 64, 1225)
    data[0x80] = 255
    struct.pack_into('>HHHHhhh', data, 0xA0, 3, 0, 100, 200, -11, 22, 33)
    return bytes(data)


def axis(address, count, *, signed=False, scale=1, unit='rpm'):
    return {'axis_type': 1, 'has_pts_address': True, 'raw_pts_address': address,
            'pts_address': address, 'pts_count': count, 'pts_data_type': 2,
            'pts_data_size': 2, 'pts_big_endian': True, 'pts_signed': signed,
            'raw_flags': [0, 0], 'raw_signed_flag': int(signed),
            'scale': scale, 'offset': 0, 'unit': unit, 'input_name': unit}


def example(**changes):
    record = {'name': 'Main SOI', 'description': 'Original symbol description',
              'folder_name': 'Injection timing', 'folder_id': 7, 'source_record_offset': 0x100,
              'source_record_end': 0x200, 'type': 'MAP', 'kennfeld_type': 5,
              'raw_header_fields': [5, 2, 2, 2, 10, 7], 'raw_geometry': [2, 3, 0, 0, 3],
              'nx': 3, 'ny': 2, 'cell_data_type': 2, 'data_size': 2,
              'cell_big_endian': True, 'data_signed': True, 'raw_flags': [0, 1, 0, 0],
              'raw_address': 0x2E, 'address': 0x2E, 'rom_end': 0x3A,
              'scale': 0.023436, 'offset': 0, 'unit': 'deg BTDC',
              'column_major': True, 'column_major_is_assumed': True,
              'x_axis': axis(0x24, 3), 'y_axis': axis(0x2A, 2, scale=0.01, unit='mm3/Stk')}
    record.update(changes)
    return record


def scalar(**changes):
    return example(type='VALUE', kennfeld_type=2, nx=1, ny=1,
                   raw_header_fields=[2, 2, 1, 2, 10, 7], raw_geometry=[1, 1, 0, 0, 0],
                   cell_data_type=1, data_size=1, cell_big_endian=False, data_signed=False,
                   raw_flags=[0, 0, 0, 0], raw_address=0x80, address=0x80, rom_end=0x81,
                   scale=1, unit='flag', **changes)


def project(records, data=None):
    data = image() if data is None else data
    source = b'Immutable native project snapshot'
    return OLSProject(Path('example.ols'), digest(source), 113, 'EDC16C31 361888',
                      {'ecu_name': 'EDC16C31', 'sw_number': '361888'}, records, [],
                      [OLSVersion(0, 'Original', data, digest(data))], [], source)


class NativeOLSConversionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def document(self, conversion, data=None):
        path = self.folder / 'original.bin'
        path.write_bytes(image() if data is None else data)
        return CalibrationDocument(conversion.database, path)

    def test_column_layout_preserves_source_axes_scaled_negative_values_and_writes(self):
        source = project([example()])
        before = deepcopy(source.maps)
        result = convert_ols(source)
        definition = result.definitions[0]
        doc = self.document(result)
        self.assertEqual(result.report['converted'], 1)
        self.assertEqual(definition.storage_order, 'column_major')
        self.assertEqual((definition.rows, definition.cols), (2, 3))
        self.assertEqual((definition.x_axis.address, definition.y_axis.address), (0x24, 0x2A))
        self.assertEqual(doc.axis_values(definition.x_axis), [500, 700, 1000])
        self.assertEqual(doc.axis_values(definition.y_axis), [0, 10])
        self.assertAlmostEqual(doc.map_values(definition)[0][1], -26 * 0.023436)
        self.assertAlmostEqual(doc.map_values(definition)[1][0], 384 * 0.023436)
        doc.set_cells(definition, {(1, 2): -100 * 0.023436}, 'Column location')
        self.assertEqual(struct.unpack_from('>h', doc.data, 0x38)[0], -100)
        self.assertEqual(struct.unpack_from('>h', doc.original, 0x38)[0], 1225)
        path = self.folder / 'converted.xdf'
        result.workspace.save_as(path)
        decoded = XDFDatabase(path).definitions[0]
        self.assertEqual(decoded.storage_order, 'column_major')
        self.assertEqual(decoded.x_axis, definition.x_axis)
        self.assertEqual(source.maps, before)
        self.assertEqual(source.versions[0].data, image())
        self.assertEqual(result.report['source_sha256'], source.source_sha256)
        self.assertEqual(result.report['bin_sha256'], digest(image()))
        json.dumps(result.report)

    def test_scalar_byte_signed_flag_and_linear_offset_are_source_declared(self):
        record = scalar()
        result = convert_ols(project([record]))
        definition = result.definitions[0]
        self.assertEqual(definition.kind, 'constant')
        self.assertEqual(definition.storage_order, 'row_major')
        self.assertFalse(definition.z_signed)
        self.assertEqual(self.document(result).map_values(definition), [[255]])
        record.update(data_signed=True, raw_flags=[0, 1, 0, 0], scale=0.5, offset=-10)
        result = convert_ols(project([record]))
        self.assertEqual(self.document(result).map_values(result.definitions[0]), [[-10.5]])
        self.assertEqual(result.report['constant_count'], 1)

    def test_signed_axis_and_physical_scaling_do_not_use_unsigned_guess(self):
        data = bytearray(image())
        struct.pack_into('>hhh', data, 0x24, -400, -200, 0)
        record = example(x_axis=axis(0x24, 3, signed=True, scale=0.1, unit='deg C'))
        result = convert_ols(project([record], bytes(data)))
        self.assertEqual(self.document(result, bytes(data)).axis_values(result.definitions[0].x_axis), [-40, -20, 0])
        self.assertTrue(result.definitions[0].x_axis.signed)

    def test_curve_requires_source_axis_and_stored_count(self):
        record = example(type='CURVE', kennfeld_type=3, nx=3, ny=1,
                         raw_header_fields=[3, 2, 2, 2, 10, 7], raw_geometry=[3, 1, 0, 0, 3],
                         raw_address=0xA8, address=0xA8, rom_end=0xAE, x_axis=axis(0xA2, 3), scale=1)
        result = convert_ols(project([record]))
        self.assertEqual(self.document(result).map_values(result.definitions[0]), [[-11, 22, 33]])
        self.assertIsNone(result.definitions[0].y_axis)
        record['x_axis']['axis_type'] = 3
        result = convert_ols(project([record]))
        self.assertFalse(result.definitions)
        self.assertIn('source kind', result.report['skipped'][0]['reason'])

    def test_invalid_extents_geometry_flags_types_zero_scale_and_bounds_are_omitted(self):
        records = [example(rom_end=0x3B), example(raw_geometry=[3, 2, 0, 0, 3]),
                   example(raw_flags=[0, 0, 0, 0]), example(cell_data_type=6),
                   example(scale=0), example(raw_address=510, address=510, rom_end=522),
                   example(data_size=4), example(kennfeld_type=3)]
        result = convert_ols(project(records))
        self.assertFalse(result.definitions)
        self.assertEqual(result.report['skipped_count'], len(records))
        self.assertFalse(result.report['recognition_eligible'])
        self.assertFalse(result.report.get('column_storage_display_evidence'))

    def test_bad_axis_missing_zero20_reversed_and_header_cannot_be_repaired(self):
        records = []
        for changes in ({'has_pts_address': False}, {'raw_pts_address': 0x20, 'pts_address': 0x20},
                        {'pts_count': 4}, {'axis_type': 3}, {'raw_flags': [0, 1]},
                        {'raw_signed_flag': 1}, {'raw_pts_address': 0x2E, 'pts_address': 0x2E}):
            record = example()
            record['x_axis'].update(changes)
            records.append(record)
        result = convert_ols(project(records))
        self.assertFalse(result.definitions)
        self.assertEqual(result.report['skipped_count'], len(records))
        data = bytearray(image())
        struct.pack_into('>H', data, 0x20, 4)
        self.assertFalse(convert_ols(project([example()], bytes(data))).definitions)

    def test_selected_version_is_independently_decoded_and_report_binds_identity(self):
        source = project([example()])
        changed = bytearray(image())
        struct.pack_into('>h', changed, 0x2E, -100)
        source.versions.append(OLSVersion(1, 'Version 1', bytes(changed), digest(changed)))
        result = convert_ols(source, 1)
        self.assertEqual(result.report['version_index'], 1)
        self.assertEqual(result.report['bin_sha256'], digest(changed))
        self.assertAlmostEqual(self.document(result, bytes(changed)).map_values(result.definitions[0])[0][0], -2.3436)
        for invalid in (2, True):
            with self.assertRaises(EditorError):
                convert_ols(source, invalid)
        source.versions[0].sha256 = 'wrong'
        with self.assertRaises(EditorError):
            convert_ols(source)

    def test_mixed_endian_is_rejected_instead_of_inventing_per_map_override(self):
        with self.assertRaises(EditorError):
            convert_ols(project([example(), example(cell_data_type=3, cell_big_endian=False)]))

    def test_native_library_entry_survives_regeneration_and_edited_file_is_preserved(self):
        source, output = self.folder / 'packs', self.folder / 'library'
        source.mkdir()
        output.mkdir()
        result = convert_ols(project([scalar()]))
        xdf = output / 'EDC16C31 361888.xdf'
        result.workspace.save_as(xdf)
        entry = {'key': 'native-test', 'title': 'EDC16C31 361888', 'xdf': xdf.name,
                 'xdf_sha256': digest(xdf.read_bytes()), 'source_type': 'Native OLS',
                 'source_ols_sha256': result.report['source_sha256'], 'map_count': 1,
                 'byte_length': 512, 'usable': True}
        manifest = output / 'manifest.json'
        manifest.write_text(json.dumps({'format': 1, 'entries': [entry]}))
        regenerated = generate_library(source, output)
        self.assertEqual(regenerated['entries'], [entry])
        self.assertEqual(regenerated['summary']['native_ols_profiles'], 1)
        xdf.write_bytes(xdf.read_bytes() + b'\n<!-- User edit -->\n')
        changed = xdf.read_bytes()
        generate_library(source, output)
        self.assertEqual(xdf.read_bytes(), changed)

    def test_real_pack_lambda_display_values_settings_and_complete_accounting(self):
        root = Path(__file__).resolve().parents[1]
        path = root / 'mappacks' / 'BMW_30D_EDC16_218PS__E60.ols'
        if not path.is_file() or not (root / 'assets' / 'ols' / 'ols_dump.exe').is_file():
            self.skipTest('Optional native project fixture/helper is unavailable')
        before = digest(path.read_bytes())
        source = load_ols(path)
        result = convert_ols(source)
        self.assertEqual(result.report['characteristics'], 9531)
        self.assertEqual(result.report['converted'] + result.report['skipped_count'], 9531)
        self.assertEqual(result.report['converted'], 9414)
        self.assertEqual(result.report['constant_count'], 8720)
        definition = next(item for item in result.definitions if item.z_address == 0xCF358)
        doc = self.document(result, source.versions[0].data)
        self.assertEqual(doc.map_values(definition)[0][:4], [0.8, 1, 1.06, 1.24])
        self.assertEqual(doc.axis_values(definition.x_axis)[:4], [500, 700, 800, 1000])
        self.assertEqual(doc.axis_values(definition.y_axis)[:4], [250, 300, 350, 400])
        self.assertEqual(definition.category, 'flmng: "Gemischbildung"')
        self.assertEqual(digest(path.read_bytes()), before)
        self.assertEqual(source.versions[0].data, source.versions[1].data)
        self.assertEqual(result.report['column_storage_display_evidence']['z_offset'], 0xCF358)
        # Only this exact source/BIN pair receives the observed display evidence.
        source.source_sha256 = 'another format-113 project'
        self.assertNotIn('column_storage_display_evidence', convert_ols(source).report)


if __name__ == '__main__':
    unittest.main()
