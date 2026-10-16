"""Conservative conversion of decoded legacy WinOLS definitions into XDF.

BIN bytes and native settings remain immutable. Integer types, signedness and
linear conversions must be explicitly declared. Active ROM axes and Bosch
record headers are checked against the selected embedded version. Unsupported
axis interpretations and storage layouts are reported rather than repaired.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import xml.etree.ElementTree as ET

from .core import AxisDefinition, CalibrationDefinition, EditorError, XDFDatabase
from .ols_import import OLSProject
from .xdf_edit import XDFWorkspace


# Native DataOrg integer enums. Floating point and 64-bit storage are omitted.
_INTEGER_TYPES = {1: (1, None), 2: (2, 'big'), 3: (2, 'little'),
                  4: (4, 'big'), 5: (4, 'little')}
_DISPLAY_DECIMALS = 6
_DISPLAY_REFERENCE_SOURCE = '9470b1482cb6f4147ececa92511535909110a1a31c446f46b98543d85a4df743'
_DISPLAY_REFERENCE_BIN = '26d24996e5f420d99f835acd6112ad3f068e25f95eb5910a68e59030b37df05e'


@dataclass
class OLSConversion:
    workspace: XDFWorkspace
    report: dict

    @property
    def database(self):
        return self.workspace.database

    @property
    def definitions(self):
        return self.database.definitions


def _integer(value, label: str) -> int:
    if type(value) is not int:
        raise EditorError(f'{label} must be an explicit integer')
    return value


def _boolean(value, label: str) -> bool:
    if type(value) is not bool:
        raise EditorError(f'{label} must be an explicit boolean')
    return value


def _storage(dtype, width, endian, label: str):
    if type(dtype) is not int or dtype not in _INTEGER_TYPES:
        raise EditorError(f'Unsupported {label} native DataOrg {dtype!r}')
    declared_width, order = _INTEGER_TYPES[dtype]
    if type(width) is not int or width != declared_width:
        raise EditorError(f'{label} native type and element width disagree')
    if order and _boolean(endian, label + ' endian') != (order == 'big'):
        raise EditorError(f'{label} native type and byte order disagree')
    return declared_width, order


def _equation(record: dict, label: str) -> str:
    scale, offset = record.get('scale'), record.get('offset')
    if (type(scale) not in (float, int) or type(offset) not in (float, int)
            or not math.isfinite(scale) or not math.isfinite(offset) or scale == 0):
        raise EditorError(f'{label} requires finite, nonzero linear source scaling')
    return f'X*({scale!r})+({offset!r})'


def _monotonic(values) -> bool:
    return len(values) > 1 and (all(a < b for a, b in zip(values, values[1:])) or
                                all(a > b for a, b in zip(values, values[1:])))


def convert_ols(project: OLSProject, version_index: int = 0) -> OLSConversion:
    """Return an unsaved XDF workspace and a JSON-serializable audit report."""
    if not isinstance(project, OLSProject) or project.format_version != 113:
        raise EditorError('Native XDF conversion requires a validated format-113 OLS project')
    if type(version_index) is not int:
        raise EditorError('OLS version index must be an integer')
    versions = [item for item in project.versions if item.index == version_index]
    if len(versions) != 1 or not isinstance(versions[0].data, bytes) or not versions[0].data:
        raise EditorError('The requested embedded OLS version was not found')
    version = versions[0]
    image = version.data
    image_hash = hashlib.sha256(image).hexdigest()
    if image_hash != version.sha256:
        raise EditorError('Embedded OLS version bytes disagree with their recorded SHA-256')
    orders = {order for item in project.maps for _, order in
              [_INTEGER_TYPES.get(item.get('cell_data_type'), (0, None))] if order}
    if len(orders) > 1:
        raise EditorError('Mixed native map byte orders are unsupported by this editor')
    order = next(iter(orders), 'big')
    report = {'source_name': project.source_path.name, 'source_sha256': project.source_sha256,
              'source_format': 'WinOLS native project', 'source_type': 'Native OLS',
              'format_version': project.format_version, 'title': project.title,
              'version_index': version.index, 'version_name': version.name,
              'reference_image_sha256': image_hash, 'bin_sha256': image_hash,
              'bin_size': len(image), 'address_offset': 0,
              'byte_order': order, 'characteristics': len(project.maps), 'converted': 0,
              'records': [], 'skipped': [], 'rom_axis_checks': [], 'dimension_checks': [],
              'warnings': list(project.warnings), 'display_decimal_places': _DISPLAY_DECIMALS,
              'display_precision_is_default': True,
              'storage_interpretation': 'Native parser COLUMN_DIR, with source X/Y assignments retained; '
                                        'format-113 layout interpretation, not a decoded explicit direction flag.'}
    report['warnings'].append('XDF uses six decimal places for display; native display settings remain '
                              'preserved in the complete settings export and are not claimed to be decoded.')
    report['warnings'].append('Table COLUMN_DIR is the format-113 parser interpretation; '
                              'a stored dimension header verifies the dimensions, not traversal direction. '
                              'Source map labels remain pack descriptions.')
    if project.source_sha256 == _DISPLAY_REFERENCE_SOURCE and image_hash == _DISPLAY_REFERENCE_BIN:
        first_row = [int.from_bytes(image[0xCF358 + col * 32:0xCF35A + col * 32], 'big', signed=True) * 0.001
                     for col in range(4)]
        if first_row == [0.8, 1.0, 1.06, 1.24]:
            report['column_storage_display_evidence'] = {
                'source_sha256': _DISPLAY_REFERENCE_SOURCE, 'bin_sha256': _DISPLAY_REFERENCE_BIN,
                'symbol': 'FlMng_rLmbdSmk_MAP', 'z_offset': 0xCF358,
                'x_offset': 0xCF318, 'y_offset': 0xCF338, 'factor': 0.001,
                'first_visible_row': first_row,
                'method': 'Selected WinOLS display and independently decoded column traversal agree for this map only.'}
    workspace = XDFWorkspace(bin_size=len(image))
    workspace.set_metadata(project.title, 'Emil Larsson',
                           f'Native WinOLS import: {project.source_path.name}.\n'
                           f'OLS SHA-256: {project.source_sha256}\nBIN SHA-256: {image_hash}\n'
                           'Only explicitly typed, bounded definitions with verified active ROM axes '
                           'and supported record layouts are included. See the import report.\n'
                           'Native parser column storage retains source X/Y assignments. Native '
                           'display settings are preserved separately; XDF displays six decimal places.',
                           order == 'little')
    root = workspace.database.root
    profile = f'native_ols:{project.source_sha256}:{version.index}:{image_hash}'
    ET.SubElement(root.find('XDFHEADER'), 'BMWBINEDITORPROFILE').text = profile

    def checked(address, length, label):
        address = _integer(address, label + ' address')
        if address < 0 or length <= 0 or address + length > len(image):
            raise EditorError(f'{label} address range outside embedded BIN')
        return address

    def axis(source, count, z, z_length, axis_name, index, title):
        if count == 1:
            return None, None
        if not isinstance(source, dict) or source.get('axis_type') != 1:
            raise EditorError(f'{axis_name} axis source kind is unresolved or unsupported')
        if source.get('has_pts_address') is not True:
            raise EditorError(f'{axis_name} axis has no explicit ROM address')
        if _integer(source.get('pts_count'), axis_name + ' axis count') != count:
            raise EditorError(f'{axis_name} axis count differs from its table dimension')
        width, axis_order = _storage(source.get('pts_data_type'), source.get('pts_data_size'),
                                     source.get('pts_big_endian'), axis_name + ' axis')
        if axis_order and axis_order != order:
            raise EditorError(f'{axis_name} axis has unsupported mixed byte order')
        flags = source.get('raw_flags')
        if not isinstance(flags, list) or len(flags) != 2 or flags != [0, 0]:
            raise EditorError(f'{axis_name} axis has unresolved display flags')
        signed = _boolean(source.get('pts_signed'), axis_name + ' axis signedness')
        signed_flag = source.get('raw_signed_flag')
        if type(signed_flag) is not int or signed_flag not in (0, 1) or signed != bool(signed_flag):
            raise EditorError(f'{axis_name} axis signedness disagrees with native flags')
        address = checked(source.get('raw_pts_address'), count * width, axis_name + ' axis')
        if source.get('pts_address') != address:
            raise EditorError(f'{axis_name} axis decoded address differs from source address')
        if max(z, address) < min(z + z_length, address + count * width):
            raise EditorError(f'{axis_name} axis overlaps map values')
        values = [int.from_bytes(image[address + i * width:address + (i + 1) * width],
                                 axis_order or order, signed=signed) for i in range(count)]
        equation = _equation(source, axis_name + ' axis')
        monotonic = _monotonic(values)
        report['rom_axis_checks'].append({'map_index': index, 'name': title, 'axis': axis_name,
                                          'offset': address, 'count': count, 'bits': width * 8,
                                          'signed': signed, 'byte_order': axis_order or order,
                                          'monotonic': monotonic, 'first': values[0], 'last': values[-1]})
        if not monotonic:
            raise EditorError(f'{axis_name} ROM axis is not strictly monotonic in its declared storage')
        return (AxisDefinition(address, count, width * 8, signed, equation,
                               str(source.get('unit', '')), _DISPLAY_DECIMALS),
                {'offset': address, 'length': count * width, 'kind': axis_name + ' axis'})

    def stored_header(definition):
        """Require exact local counts and source-axis bytes; shared copies allowed."""
        x, y = definition.x_axis, definition.y_axis
        z = definition.z_address
        if definition.rows == 1 and x:
            length = x.count * x.element_bits // 8
            local = z - length
            if (local >= 2 and int.from_bytes(image[local - 2:local], order) == x.count
                    and image[local:z] == image[x.address:x.address + length]):
                return [{'offset': local - 2, 'bits': 16, 'count': x.count, 'axis': 'X'}], local != x.address
            return [], False
        if not x or not y:
            return [], False
        xl, yl = x.count * x.element_bits // 8, y.count * y.element_bits // 8
        for local_x, local_y, counts in ((z - xl - yl, z - yl, (x.count, y.count)),
                                        (z - xl, z - xl - yl, (y.count, x.count))):
            first = min(local_x, local_y)
            if first < 4:
                continue
            native_counts = tuple(int.from_bytes(image[first - 4 + i * 2:first - 2 + i * 2], order)
                                  for i in range(2))
            if (native_counts == counts
                    and image[local_x:local_x + xl] == image[x.address:x.address + xl]
                    and image[local_y:local_y + yl] == image[y.address:y.address + yl]):
                fields = [{'offset': first - 4 + i * 2, 'bits': 16, 'count': count,
                           'axis': 'X' if ((i == 0) == (local_x < local_y)) else 'Y'}
                          for i, count in enumerate(counts)]
                return fields, (local_x, local_y) != (x.address, y.address)
        return [], False

    for index, source in enumerate(project.maps):
        label = str(source.get('name', '<unnamed>')) if isinstance(source, dict) else '<invalid>'
        try:
            if not isinstance(source, dict) or not label.strip():
                raise EditorError('Native definition has no valid source name')
            kind = source.get('type')
            if kind not in ('VALUE', 'CURVE', 'MAP'):
                raise EditorError(f'Unsupported native definition type {kind!r}')
            cols, rows = _integer(source.get('nx'), 'Columns'), _integer(source.get('ny'), 'Rows')
            if min(rows, cols) <= 0 or max(rows, cols) > 512 or rows * cols > 65536:
                raise EditorError('Native definition dimensions are invalid or excessive')
            if (kind == 'VALUE' and (rows, cols) != (1, 1)) or (kind == 'CURVE' and rows != 1):
                raise EditorError('Native definition type and dimensions disagree')
            if source.get('kennfeld_type') != {'VALUE': 2, 'CURVE': 3, 'MAP': 5}[kind]:
                raise EditorError('Native definition kind disagrees with its raw type enum')
            geometry, headers = source.get('raw_geometry'), source.get('raw_header_fields')
            expected_geometry = [rows, cols, 0, 0] if kind == 'MAP' else [cols, rows, 0, 0]
            if (not isinstance(geometry, list) or len(geometry) != 5 or geometry[:4] != expected_geometry
                    or not isinstance(headers, list) or len(headers) != 6
                    or headers[0] != source.get('kennfeld_type') or headers[2] != source.get('cell_data_type')):
                raise EditorError('Native raw geometry/type does not corroborate its decoded dimensions')
            width, _ = _storage(source.get('cell_data_type'), source.get('data_size'),
                                source.get('cell_big_endian'), 'Map')
            signed = _boolean(source.get('data_signed'), 'Map signedness')
            flags = source.get('raw_flags')
            if (not isinstance(flags, list) or len(flags) != 4 or flags[1] not in (0, 1)
                    or signed != bool(flags[1]) or any(flags[i] != 0 for i in (0, 2, 3))):
                raise EditorError('Native map signedness or display flags are unresolved')
            length = rows * cols * width
            z = checked(source.get('raw_address'), length, 'Map values')
            if source.get('address') != z or source.get('rom_end') != z + length:
                raise EditorError('Native map extent does not exactly match its typed dimensions')
            equation = _equation(source, 'Map')
            axes, spans = {}, [{'offset': z, 'length': length, 'kind': 'values'}]
            for name, count in (('X', cols), ('Y', rows)):
                axes[name], span = axis(source.get(name.lower() + '_axis'), count, z, length, name, index, label)
                if span:
                    spans.append(span)
            storage = 'column_major' if kind != 'VALUE' and source.get('column_major') is True else 'row_major'
            if kind != 'VALUE' and type(source.get('column_major')) is not bool:
                raise EditorError('Native table storage direction is unresolved')
            title = f'0x{z:X} {label}'
            description = (f'{source.get("description", "")}\nNative WinOLS symbol {label}; source record '
                           f'{index}, OLS offset 0x{source.get("source_record_offset", 0):X}. '
                           f'{width * 8}-bit {"signed" if signed else "unsigned"}; {order} endian. '
                           'Source linear scaling and folder are retained. Native parser column storage '
                           'interpretation retains the original X/Y assignments; see import report.')
            definition = CalibrationDefinition(title, description, source.get('folder_name') or 'Imported maps',
                                                z, rows, cols, width * 8, signed, equation,
                                                str(source.get('unit', '')), _DISPLAY_DECIMALS,
                                                axes['X'], axes['Y'], kind='constant' if kind == 'VALUE' else 'table',
                                                storage_order=storage)
            dimension_fields, shared = [], False
            if kind != 'VALUE':
                dimension_fields, shared = stored_header(definition)
                report['dimension_checks'].append({'map_index': index, 'name': label,
                                                    'matched': bool(dimension_fields), 'shared_axes': shared,
                                                    'rows': rows, 'cols': cols})
                if not dimension_fields:
                    raise EditorError('Native table is not corroborated by stored dimension counts and local ROM-axis bytes')
                spans.extend({'offset': field['offset'], 'length': 2, 'kind': 'dimension header'}
                             for field in dimension_fields)
            workspace._validate(definition)
            item = ET.SubElement(root, 'XDFCONSTANT' if kind == 'VALUE' else 'XDFTABLE',
                                 uniqueid=f'0x{len(report["records"]) + 1:X}', flags='0x0')
            workspace._write_definition(root, item, definition)
            report['records'].append({'name': title, 'source_name': label, 'source_index': index,
                                      'source_record_offset': source.get('source_record_offset'),
                                      'source_record_end': source.get('source_record_end'),
                                      'z_offset': z, 'rows': rows, 'cols': cols, 'bits': width * 8,
                                      'signed': signed, 'source_type': kind, 'storage': storage,
                                      'storage_is_parser_interpretation': source.get('column_major_is_assumed', False),
                                      'storage_display_corroborated': (z == 0xCF358 and
                                                                      bool(report.get('column_storage_display_evidence'))),
                                      'axes_exchanged': False, 'folder_id': source.get('folder_id'),
                                      'folder_name': source.get('folder_name'), 'dimension_fields': dimension_fields,
                                      'shared_axes_validated': shared, 'spans': spans})
        except (EditorError, ValueError, TypeError, OverflowError, ArithmeticError) as exc:
            report['skipped'].append({'map_index': index, 'name': label,
                                      'source_record_offset': source.get('source_record_offset') if isinstance(source, dict) else None,
                                      'reason': str(exc)})
    workspace.database = XDFDatabase.from_tree(workspace.database.tree, project.source_path.with_suffix('.xdf'))
    report.update(converted=len(workspace.database.definitions), skipped_count=len(report['skipped']),
                  skip_reasons=dict(Counter(item['reason'] for item in report['skipped'])), profile_id=profile,
                  table_count=sum(item.kind == 'table' for item in workspace.database.definitions),
                  constant_count=sum(item.kind == 'constant' for item in workspace.database.definitions),
                  validated_two_axis_headers=sum(item['matched'] and min(item['rows'], item['cols']) > 1
                                                 for item in report['dimension_checks']))
    report['rom_axis_checks_total'] = len(report['rom_axis_checks'])
    report['rom_axis_checks_passed'] = sum(item['monotonic'] for item in report['rom_axis_checks'])
    report['recognition_eligible'] = report['validated_two_axis_headers'] >= 8
    return OLSConversion(workspace, report)
