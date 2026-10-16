"""A2L provenance, signed conversion and actual byte layouts survive import."""
from pathlib import Path
import hashlib
import json
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bmw_bin_editor.a2l_import import convert_a2l
from bmw_bin_editor.core import CalibrationDocument, EditorError, safe_math


METHODS = '''
/begin COMPU_METHOD RAW "Identity" IDENTICAL "%6.0" "raw" /end COMPU_METHOD
/begin COMPU_METHOD DEG "Angle" RAT_FUNC "%6.2" "deg" COEFFS 0 100 0 0 0 1 /end COMPU_METHOD
/begin COMPU_METHOD TEMP "Temperature" RAT_FUNC "%6.1" "C" COEFFS 0 2 80 0 0 1 /end COMPU_METHOD
/begin COMPU_METHOD LIN "Linear" LINEAR "%6.1" "mg" COEFFS_LINEAR 0.5 -2 /end COMPU_METHOD
'''


def characteristic(name='Test', kind='VALUE', address='0x20', layout='Scalar', conversion='DEG', axes=''):
    return (f'/begin CHARACTERISTIC {name} "A "/begin" literal" {kind} {address} {layout} 0 '
            f'{conversion} -327.68 327.67 {axes} /end CHARACTERISTIC ').replace('"A "/begin" literal"',
                                                                           '"A ""/begin"" literal"')


def axis(kind='STD_AXIS', count=3, conversion='RAW', extra=''):
    return f'/begin AXIS_DESCR {kind} Input {conversion} {count} -100 100 {extra} /end AXIS_DESCR '


class A2LImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def convert(self, content, image=None, size=256, base=0, order='MSB_FIRST', project='EDC16'):
        path = self.folder / 'test.a2l'
        path.write_text(f'ASAP2_VERSION 1 7 /begin PROJECT P "{project}" /begin MODULE M "" '
                        f'/begin MOD_COMMON "" BYTE_ORDER {order} ALIGNMENT_BYTE 1 ALIGNMENT_WORD 2 '
                        f'ALIGNMENT_LONG 4 /end MOD_COMMON {METHODS} {content} /end MODULE /end PROJECT',
                        encoding='utf-8')
        before = path.read_bytes()
        result = convert_a2l(path, size, base, image=image)
        self.assertEqual(before, path.read_bytes())
        self.assertEqual(hashlib.sha256(before).hexdigest(), result.report['source_sha256'])
        json.dumps(result.report)
        return result

    def document(self, result, image):
        path = self.folder / 'reference.bin'
        path.write_bytes(image)
        return CalibrationDocument(result.database, path)

    def test_signed_soi_inverse_rat_func_and_lossless_readonly_input(self):
        image = bytearray(256)
        struct.pack_into('>h', image, 0x20, -180)
        result = self.convert('/begin RECORD_LAYOUT Scalar FNC_VALUES 1 SWORD COLUMN_DIR DIRECT '
                              '/end RECORD_LAYOUT ' + characteristic(), bytes(image))
        self.assertEqual(result.report['converted'], 1)
        definition = result.definitions[0]
        self.assertTrue(definition.z_signed)
        self.assertEqual(definition.z_address, 0x20)
        self.assertAlmostEqual(self.document(result, image).map_values(definition)[0][0], -1.8)
        self.assertIn('A "/begin" literal', definition.description)
        target = self.folder / 'converted.xdf'
        saved = result.workspace.save_as(target)
        self.assertTrue(saved.definitions[0].z_signed)
        self.assertAlmostEqual(safe_math(saved.definitions[0].z_equation, -180), -1.8)
        doc = self.document(result, image)
        doc.set_cells(saved.definitions[0], {(0, 0): -2.5}, 'Signed calibration edit')
        self.assertEqual(struct.unpack_from('>h', doc.data, 0x20)[0], -250)
        self.assertEqual(struct.unpack_from('>h', doc.original, 0x20)[0], -180)

    def test_address_base_alignment_and_dynamic_inline_counts(self):
        image = bytearray(256)
        # ECU record starts at 0x1021, UBYTE count then word-aligned axes/Z.
        image[0x21] = 2
        struct.pack_into('>hhhh', image, 0x22, 100, 200, -123, 234)
        layout = ('/begin RECORD_LAYOUT Curve NO_AXIS_PTS_X 1 UBYTE AXIS_PTS_X 2 SWORD INDEX_INCR DIRECT '
                  'FNC_VALUES 3 SWORD COLUMN_DIR DIRECT /end RECORD_LAYOUT ')
        result = self.convert(layout + characteristic(kind='CURVE', address='0x1021', layout='Curve',
                              axes=axis(count=5)), bytes(image), base=0x1000)
        definition = result.definitions[0]
        self.assertEqual((definition.rows, definition.cols), (1, 2))
        self.assertEqual(definition.x_axis.address, 0x22)
        self.assertEqual(definition.z_address, 0x26)
        doc = self.document(result, image)
        self.assertEqual(doc.axis_values(definition.x_axis), [100, 200])
        self.assertEqual(doc.map_values(definition), [[-1.23, 2.34]])
        self.assertEqual(result.report['records'][0]['dimension_fields'][0]['offset'], 0x21)
        self.assertEqual(result.report['records'][0]['spans'][0]['length'], 4)

    def test_column_dir_exchanges_axes_preserving_values_and_row_dir_does_not(self):
        image = bytearray(256)
        struct.pack_into('>HH', image, 0x20, 2, 3)
        struct.pack_into('>hhhhh', image, 0x24, 10, 20, 100, 200, 300)
        # COLUMN_DIR: first complete Y column then second Y column.
        struct.pack_into('>hhhhhh', image, 0x2E, 1, 2, 3, 4, 5, 6)
        layout = ('/begin RECORD_LAYOUT Map NO_AXIS_PTS_X 1 UWORD NO_AXIS_PTS_Y 2 UWORD '
                  'AXIS_PTS_X 3 SWORD INDEX_INCR DIRECT AXIS_PTS_Y 4 SWORD INDEX_INCR DIRECT '
                  'FNC_VALUES 5 SWORD {mode} DIRECT /end RECORD_LAYOUT ')
        content = characteristic(kind='MAP', layout='Map', conversion='RAW', axes=axis(count=2)+axis(count=3))
        result = self.convert(layout.format(mode='COLUMN_DIR') + content, bytes(image))
        definition = result.definitions[0]
        self.assertEqual((definition.rows, definition.cols), (2, 3))
        self.assertEqual((definition.x_axis.address, definition.y_axis.address), (0x28, 0x24))
        self.assertEqual(self.document(result, image).map_values(definition), [[1, 2, 3], [4, 5, 6]])
        self.assertTrue(result.report['records'][0]['axes_exchanged'])
        result = self.convert(layout.format(mode='ROW_DIR') + content, bytes(image))
        definition = result.definitions[0]
        self.assertEqual((definition.rows, definition.cols), (3, 2))
        self.assertEqual((definition.x_axis.address, definition.y_axis.address), (0x24, 0x28))
        self.assertEqual(self.document(result, image).map_values(definition), [[1, 2], [3, 4], [5, 6]])

    def test_shared_axes_counts_conversions_and_little_endian(self):
        image = bytearray(256)
        struct.pack_into('<Hhhh', image, 0x40, 3, 100, 200, 300)
        struct.pack_into('<hhh', image, 0x20, 4, 6, 8)
        content = ('/begin RECORD_LAYOUT Shared NO_AXIS_PTS_X 1 UWORD AXIS_PTS_X 2 SWORD INDEX_INCR DIRECT '
                   '/end RECORD_LAYOUT /begin RECORD_LAYOUT Curve FNC_VALUES 1 SWORD COLUMN_DIR DIRECT '
                   '/end RECORD_LAYOUT /begin AXIS_PTS SharedAxis "Shared" 0x40 Input Shared 0 LIN 5 0 100 '
                   '/end AXIS_PTS ' + characteristic(kind='CURVE', layout='Curve', conversion='TEMP',
                                                    axes=axis('COM_AXIS', 5, 'LIN', 'AXIS_PTS_REF SharedAxis')))
        result = self.convert(content, bytes(image), order='MSB_LAST')
        definition = result.definitions[0]
        self.assertEqual(definition.x_axis.address, 0x42)
        self.assertEqual(definition.cols, 3)
        self.assertTrue(result.database.default_lsb_first)
        doc = self.document(result, image)
        self.assertEqual(doc.axis_values(definition.x_axis), [48, 98, 148])
        self.assertEqual(doc.map_values(definition), [[-38, -37, -36]])
        self.assertEqual(result.report['records'][0]['dimension_fields'][0]['offset'], 0x40)

    def test_fixed_axes_units_override_and_categories(self):
        content = ('/begin RECORD_LAYOUT Curve FNC_VALUES 1 SWORD ROW_DIR DIRECT /end RECORD_LAYOUT '
                   '/begin FUNCTION Injection "Injection" /begin DEF_CHARACTERISTIC Fixed /end DEF_CHARACTERISTIC '
                   '/end FUNCTION ' + characteristic('Fixed', 'CURVE', layout='Curve', conversion='RAW',
                       axes=axis('FIX_AXIS', 3, 'LIN', 'FIX_AXIS_PAR_DIST 10 2 3 PHYS_UNIT "mg/stroke"')))
        result = self.convert(content)
        definition = result.definitions[0]
        self.assertEqual(definition.category, 'Injection')
        self.assertIsNone(definition.x_axis.address)
        self.assertEqual(definition.x_axis.labels, ['3', '4', '5'])
        self.assertEqual(definition.x_axis.units, 'mg/stroke')
        self.assertTrue(result.report['warnings'])

    def test_unsupported_objects_skipped_instead_of_silent_raw_conversion(self):
        content = '''
/begin RECORD_LAYOUT Scalar FNC_VALUES 1 SWORD COLUMN_DIR DIRECT /end RECORD_LAYOUT
/begin RECORD_LAYOUT Indirect FNC_VALUES 1 SWORD COLUMN_DIR PWORD /end RECORD_LAYOUT
/begin RECORD_LAYOUT Floating FNC_VALUES 1 FLOAT32_IEEE COLUMN_DIR DIRECT /end RECORD_LAYOUT
/begin COMPU_METHOD NONLINEAR "Nonlinear" RAT_FUNC "%6.2" "deg" COEFFS 1 2 0 0 0 1 /end COMPU_METHOD
/begin COMPU_METHOD ENUM "Enumeration" TAB_VERB "%6.0" "state" COMPU_TAB_REF States /end COMPU_METHOD
'''
        content += characteristic('Good')
        content += characteristic('Nonlinear', conversion='NONLINEAR')
        content += characteristic('Enum', conversion='ENUM')
        content += characteristic('Indirect', layout='Indirect')
        content += characteristic('Float', layout='Floating')
        content += characteristic('Outside', address='0xFF')
        content += characteristic('Masked').replace('/end CHARACTERISTIC', 'BIT_MASK 0xFF /end CHARACTERISTIC')
        result = self.convert(content)
        self.assertEqual([definition.title for definition in result.definitions], ['Good'])
        self.assertEqual(result.report['skipped_count'], 6)
        reasons = ' '.join(item['reason'] for item in result.report['skipped'])
        for word in ('nonlinear', 'TAB_VERB', 'Indirect', 'FLOAT32', 'outside BIN', 'BIT_MASK'):
            self.assertIn(word, reasons)

    def test_invalid_dimensions_mixed_byte_order_and_conflicting_shared_length_skip(self):
        image = bytearray(256)
        struct.pack_into('>H', image, 0x20, 0)
        content = ('/begin RECORD_LAYOUT Curve NO_AXIS_PTS_X 1 UWORD AXIS_PTS_X 2 SWORD INDEX_INCR DIRECT '
                   'FNC_VALUES 3 SWORD ROW_DIR DIRECT /end RECORD_LAYOUT ' +
                   characteristic(kind='CURVE', layout='Curve', axes=axis(count=3)))
        result = self.convert(content, bytes(image))
        self.assertFalse(result.definitions)
        self.assertIn('Invalid X dimension', result.report['skipped'][0]['reason'])
        content = '/begin RECORD_LAYOUT Scalar FNC_VALUES 1 SWORD ROW_DIR DIRECT /end RECORD_LAYOUT '
        content += characteristic().replace('/end CHARACTERISTIC', 'BYTE_ORDER MSB_LAST /end CHARACTERISTIC')
        result = self.convert(content)
        self.assertFalse(result.definitions)
        self.assertIn('Mixed', result.report['skipped'][0]['reason'])

    def test_folder_names_do_not_override_explicit_edc15_source_metadata(self):
        result = self.convert('/begin RECORD_LAYOUT Scalar FNC_VALUES 1 SWORD ROW_DIR DIRECT '
                              '/end RECORD_LAYOUT ' + characteristic(), project='EDC15C')
        self.assertEqual(result.report['ecu_family_evidence'], ['EDC15C'])
        self.assertTrue(any('EDC15' in warning for warning in result.report['warnings']))

    def test_epk_identity_has_explicit_addresses_and_reports_mismatch(self):
        image = bytearray(256)
        image[0x60:0x65] = b'SW123'
        content = ('/begin MOD_PAR "Description" EPK "SW123" ADDR_EPK 0x60 ADDR_EPK 0x70 /end MOD_PAR '
                   '/begin RECORD_LAYOUT Scalar FNC_VALUES 1 SWORD ROW_DIR DIRECT /end RECORD_LAYOUT ' +
                   characteristic())
        result = self.convert(content, bytes(image))
        self.assertEqual(result.report['epk'], 'SW123')
        self.assertEqual(result.report['epk_addresses'], [0x60, 0x70])
        self.assertEqual([check['matched'] for check in result.report['epk_checks']], [True, False])
        # The importer does not decide recognition; its explicit failed check
        # lets the catalog builder reject the neighboring software version.
        self.assertEqual(result.report['epk_checks'][0]['ecu_address'], 0x60)

    def test_malformed_legacy_and_ambiguous_multi_module_fail_cleanly(self):
        path = self.folder / 'invalid.a2l'
        for content in ('/begin PROJECT P "" /end MODULE', '/PRO,{EDC15C}',
                        '/begin PROJECT P "" /begin MODULE A "" /end MODULE '
                        '/begin MODULE B "" '+characteristic()+' /end MODULE /end PROJECT'):
            path.write_text(content)
            with self.assertRaises(EditorError):
                convert_a2l(path, 256)


if __name__ == '__main__':
    unittest.main()
