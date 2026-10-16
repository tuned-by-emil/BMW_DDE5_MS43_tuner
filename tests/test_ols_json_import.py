"""WinOLS export metadata survives conversion; bad endian/axes stay excluded."""
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
from bmw_bin_editor.ols_json_import import convert_ols_json


def example(**changes):
    record = {'Name': 'Main SOI', 'IdName': 'InjCrv_Bas1', 'Comment': 'Source comment',
              'FolderName': 'Injection timing', 'Type': 'eZweidim', 'Rows': '2', 'Columns': '3',
              'DataOrg': 'eHiLo', 'Precision': '2', 'SkipBytes': '0', 'LineSkipBytes': '0',
              'Fieldvalues.StartAddr': '$002E', 'Fieldvalues.Factor': '0,023436',
              'Fieldvalues.Offset': '0,000000', 'Fieldvalues.Unit': 'deg BTDC',
              'bSigned': '1', 'bReciprocal': '0', 'bOriginal': '0', 'bOriginalValues': '0',
              'bDelta': '0', 'bPercent': '0'}
    for axis, address, factor, unit in (('X', '$0028', '0,01', 'mm3/Stk'), ('Y', '$0024', '1', 'RPM')):
        record.update({f'Axis{axis}.DataSrc': 'eRom', f'Axis{axis}.DataOrg': 'eHiLo',
                       f'Axis{axis}.DataAddr': address, f'Axis{axis}.DataHeader': '0',
                       f'Axis{axis}.Factor': factor, f'Axis{axis}.Offset': '0', f'Axis{axis}.Unit': unit,
                       f'Axis{axis}.Precision': '2', f'Axis{axis}.bSigned': '0',
                       f'Axis{axis}.bReciprocal': '0', f'Axis{axis}.bBackwards': '0',
                       f'Axis{axis}.SkipBytes': '0'})
    record.update(changes)
    return record


def example_image(little=False):
    image = bytearray(256)
    struct.pack_into(('<' if little else '>')+'HHHHHHHhhhhhh', image, 0x20,
                     2, 3, 500, 700, 0, 500, 1000, -77, -26, 64, 384, 100, 1225)
    return bytes(image)


class OLSJSONTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def convert(self, records, image=None, **kwargs):
        path = self.folder/'pack.json'
        path.write_text(json.dumps({'maps': records}), encoding='utf8')
        before = path.read_bytes()
        image = example_image() if image is None else image
        result = convert_ols_json(path, image=image, **kwargs)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(result.report['source_sha256'], hashlib.sha256(before).hexdigest())
        self.assertEqual(result.report['reference_image_sha256'], hashlib.sha256(image).hexdigest())
        json.dumps(result.report)
        return result

    def document(self, result, image=None):
        path = self.folder/'read.bin'
        path.write_bytes(example_image() if image is None else image)
        return CalibrationDocument(result.database, path)

    def test_signed_soi_decimal_comma_factor_negative_roundtrip_and_addresses(self):
        result = self.convert([example()])
        self.assertEqual(result.report['converted'], 1)
        definition = result.definitions[0]
        self.assertEqual(definition.z_address, 0x2E)
        self.assertEqual((definition.rows, definition.cols), (2, 3))
        self.assertEqual((definition.x_axis.address, definition.y_axis.address), (0x28, 0x24))
        self.assertTrue(definition.z_signed)
        self.assertIn('0.023436', definition.z_equation)
        self.assertEqual(definition.category, 'Injection timing')
        self.assertEqual(definition.z_units, 'deg BTDC')
        doc = self.document(result)
        self.assertAlmostEqual(doc.map_values(definition)[0][0], -1.804572)
        self.assertEqual(doc.axis_values(definition.x_axis), [0, 5, 10])
        self.assertEqual(doc.axis_values(definition.y_axis), [500, 700])
        self.assertEqual(result.report['dimension_checks_passed'], 1)
        result.workspace.save_as(self.folder/'generated.xdf')
        reloaded = XDFDatabase(self.folder/'generated.xdf')
        self.assertTrue(reloaded.definitions[0].z_signed)
        doc.set_cells(reloaded.definitions[0], {(0,0): -2.3436}, 'Negative SOI')
        self.assertEqual(struct.unpack_from('>h', doc.data, 0x2E)[0], -100)
        self.assertEqual(struct.unpack_from('>h', doc.original, 0x2E)[0], -77)

    def test_matching_little_endian_is_supported_without_swapping_metadata(self):
        record = example(**{'DataOrg':'eLoHi', 'AxisX.DataOrg':'eLoHi', 'AxisY.DataOrg':'eLoHi'})
        result = self.convert([record], example_image(little=True))
        self.assertEqual(result.report['converted'], 1)
        self.assertTrue(result.database.default_lsb_first)
        self.assertEqual(result.report['byte_order'], 'little')
        self.assertAlmostEqual(self.document(result, example_image(True)).map_values(result.definitions[0])[0][0], -1.804572)

    def test_wrong_endian_is_rejected_by_rom_axes_instead_of_repaired(self):
        record = example(**{'DataOrg':'eLoHi', 'AxisX.DataOrg':'eLoHi', 'AxisY.DataOrg':'eLoHi'})
        result = self.convert([record])
        self.assertFalse(result.definitions)
        self.assertFalse(result.report['recognition_eligible'])
        self.assertIn('not strictly monotonic', result.report['skipped'][0]['reason'])

    def test_repeated_symbol_retains_distinct_addresses_and_unique_xml_ids(self):
        first = example()
        second = example(**{'Fieldvalues.StartAddr':'$0080'})
        result = self.convert([first,second])
        self.assertEqual([d.z_address for d in result.definitions], [0x2E,0x80])
        self.assertEqual(len(set(d.unique_id for d in result.definitions)), 2)
        self.assertTrue(all('InjCrv_Bas1' in d.description for d in result.definitions))

    def test_vertical_curve_ignores_inactive_singleton_rom_axis(self):
        record = example(**{'Type':'eZweiInv','Rows':'2','Columns':'1',
                            'Fieldvalues.StartAddr':'$0032', 'AxisX.DataAddr':'$0032',
                            'AxisX.DataOrg':'eLoHi'})
        result = self.convert([record])
        definition = result.definitions[0]
        self.assertEqual((definition.rows,definition.cols),(2,1))
        self.assertIsNone(definition.x_axis)
        self.assertEqual(definition.y_axis.address,0x24)
        self.assertFalse(result.report['skipped'])

    def test_user_axis_explicit_values_and_unknown_index_placeholder(self):
        record = example(**{'AxisX.DataSrc':'eUserdef','AxisX.DataOrg':'eLoHi',
                            'AxisX.Values':'0 500 1000'})
        result = self.convert([record])
        axis = result.definitions[0].x_axis
        self.assertIsNone(axis.address)
        self.assertEqual(axis.labels,['0','5','10'])
        self.assertEqual(axis.units,'mm3/Stk')
        record.pop('AxisX.Values')
        result = self.convert([record])
        axis = result.definitions[0].x_axis
        self.assertEqual(axis.labels,['0','1','2'])
        self.assertIn('unavailable',axis.units)
        self.assertTrue(result.report['warnings'])
        self.assertEqual(result.report['warnings'][0]['source_unit'],'mm3/Stk')

    def test_broken_excessive_dimensions_nonmonotonic_overlap_and_bounds_skip(self):
        records = [example(**{'Name':'BROKEN source map'}), example(**{'Rows':'65535'}),
                   example(**{'Fieldvalues.StartAddr':'$00FE'}),
                   example(**{'AxisX.DataAddr':'$002E'}),
                   example(**{'AxisY.DataAddr':'$0040'})]
        result = self.convert(records)
        self.assertFalse(result.definitions)
        self.assertEqual(result.report['skipped_count'],5)
        for key in ('BROKEN','dimensions','outside BIN','overlaps','monotonic'):
            self.assertTrue(any(key in item['reason'] for item in result.report['skipped']),key)

    def test_unsupported_strides_reverse_reciprocal_type_and_missing_fields_skip(self):
        changes = [{'SkipBytes':'2'},{'AxisX.bBackwards':'1'},{'bReciprocal':'1'},
                   {'Type':'eZweiInv'},{'DataOrg':'eFloatHiLo'}, {'AxisX.DataSrc':'eRomAdd'}]
        records = [example(**change) for change in changes]
        missing = example()
        del missing['bSigned']
        records.append(missing)
        result = self.convert(records)
        self.assertFalse(result.definitions)
        self.assertEqual(result.report['skipped_count'],len(records))
        self.assertIn('bSigned',result.report['skipped'][-1]['reason'])

    def test_scalar_byte_address_base_and_unverified_32bit_enum_omitted(self):
        image = bytearray(256)
        image[0x80] = 255
        struct.pack_into('>i',image,0x84,-123456)
        records = [example(**{'Type':'eEinzel','Rows':'1','Columns':'1','DataOrg':'eByte',
                              'Fieldvalues.StartAddr':'$1080','Fieldvalues.Factor':'1','bSigned':'0'}),
                   example(**{'Type':'eEinzel','Rows':'1','Columns':'1','DataOrg':'eHiLoHiLo',
                              'Fieldvalues.StartAddr':'$1084','Fieldvalues.Factor':'1','bSigned':'1'})]
        result = self.convert(records,bytes(image),address_offset=0x1000)
        self.assertEqual([d.z_element_bits for d in result.definitions],[8])
        self.assertEqual([d.z_address for d in result.definitions],[0x80])
        doc = self.document(result,bytes(image))
        self.assertEqual([doc.map_values(d)[0][0] for d in result.definitions],[255])
        self.assertIn('eHiLoHiLo',result.report['skipped'][0]['reason'])

    def test_malformed_empty_and_mixed_endian_fail_cleanly(self):
        path = self.folder/'bad.json'
        for payload in ('[1,2]','{"maps":[]}','{"maps":null}','invalid'):
            path.write_text(payload)
            with self.assertRaises(EditorError):
                convert_ols_json(path,example_image())
        with self.assertRaises(EditorError):
            self.convert([example(),example(**{'DataOrg':'eLoHi'})])


if __name__ == '__main__':
    unittest.main()
