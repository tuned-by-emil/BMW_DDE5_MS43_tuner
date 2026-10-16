"""Import explicit flat-key WinOLS JSON map-list exports without BIN writes.

Storage types, endian, addresses and equations come from the export. They are
never repaired from a guessed surface. Associated image bytes validate bounds
and active ROM-axis ordering; uncertain/broken entries are reported and omitted.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re
import struct
import xml.etree.ElementTree as ET

from .a2l_import import ConversionResult
from .core import AxisDefinition, CalibrationDefinition, EditorError, XDFDatabase, safe_math
from .xdf_edit import XDFWorkspace

_ORGANIZATION = {'eByte': (8, None), 'eHiLo': (16, 'big'), 'eLoHi': (16, 'little')}


def _required(record: dict, key: str) -> str:
    if key not in record or not isinstance(record[key], (str, int, float)) or isinstance(record[key], bool):
        raise EditorError(f'Missing/invalid required WinOLS property {key}')
    return str(record[key]).strip()


def _integer(value: str, label: str) -> int:
    try:
        if value.startswith('$'):
            return int(value[1:], 16)
        return int(value, 16) if value.lower().startswith('0x') else int(value, 10)
    except ValueError as exc:
        raise EditorError(f'Invalid {label}: {value!r}') from exc


def _flag(record: dict, key: str) -> bool:
    value = _required(record, key)
    if value not in ('0', '1'):
        raise EditorError(f'{key} must be 0 or 1')
    return value == '1'


def _number(value: str, label: str) -> str:
    value = value.strip().replace(',', '.')
    if not re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?', value):
        raise EditorError(f'Invalid {label}: {value!r}')
    if not math.isfinite(float(value)):
        raise EditorError(f'Nonfinite {label}')
    return value


def _equation(record: dict, prefix: str):
    factor = _number(_required(record, prefix + 'Factor'), prefix + 'Factor')
    offset = _number(_required(record, prefix + 'Offset'), prefix + 'Offset')
    if float(factor) == 0:
        raise EditorError(f'Noninvertible zero {prefix}Factor')
    return f'X*({factor})+({offset})'


def _precision(record: dict, key: str) -> int:
    value = _integer(_required(record, key), key)
    if not 0 <= value <= 8:
        raise EditorError(f'{key} must be between 0 and 8')
    return value


def _organization(value: str):
    if value not in _ORGANIZATION:
        raise EditorError(f'Unsupported WinOLS data organization {value}')
    return _ORGANIZATION[value]


def _monotonic(values) -> bool:
    return (len(values) > 1 and (all(a < b for a, b in zip(values, values[1:])) or
                              all(a > b for a, b in zip(values, values[1:]))))


def convert_ols_json(path: Path | str, image: bytes, *, address_offset: int = 0) -> ConversionResult:
    """Return an unsaved workspace/report; require an explicit reference image.

    Supported types are eEinzel scalars, eZweidim contiguous row-major maps,
    and eEindim/eZweiInv curves with a singleton dimension. Empty active user
    axes are marked as index placeholders, retaining no invented physical unit.
    Original dimensions are preserved (including vertical, one-column curves).
    """
    path = Path(path)
    if not isinstance(image, (bytes, bytearray)) or not image or type(address_offset) is not int:
        raise EditorError('WinOLS JSON conversion requires reference image bytes and an integer address base')
    raw = path.read_bytes()
    try:
        payload = json.loads(raw.decode('utf-8-sig'))
    except (UnicodeError, ValueError) as exc:
        raise EditorError('WinOLS export is not valid UTF-8 JSON') from exc
    if not isinstance(payload, dict) or not isinstance(payload.get('maps'), list):
        raise EditorError('Expected WinOLS JSON object containing a maps list')
    maps = payload['maps']
    if not maps:
        raise EditorError('WinOLS JSON map list is empty')
    orders = set()
    for record in maps:
        if isinstance(record, dict) and record.get('DataOrg') in _ORGANIZATION:
            _, order = _organization(record['DataOrg'])
            if order:
                orders.add(order)
    if len(orders) > 1:
        raise EditorError('Mixed map byte orders are unsupported by this editor')
    if not orders:
        # Byte-only maps do not depend on endianness; use the editor's documented
        # big-endian header default without changing their one-byte storage.
        order = 'big'
    else:
        order = next(iter(orders))
    report = {'source_name': path.name, 'source_sha256': hashlib.sha256(raw).hexdigest(),
              'source_format': 'WinOLS flat-key JSON', 'bin_size': len(image),
              'address_offset': address_offset, 'byte_order': order,
              'reference_image_sha256': hashlib.sha256(image).hexdigest(),
              'characteristics': len(maps), 'converted': 0, 'skipped': [], 'warnings': [],
              'records': [], 'rom_axis_checks': [], 'dimension_checks': []}
    workspace = XDFWorkspace(bin_size=len(image))
    workspace.set_metadata(f'{path.stem} — imported WinOLS JSON', 'Emil Larsson',
                           f'Imported from {path.name}. Source SHA-256: {report["source_sha256"]}.\n'
                           f'Reference image SHA-256: {report["reference_image_sha256"]}. '
                           'Only supported, bounded definitions with valid active axes are included; see import report.',
                           order == 'little')
    root = workspace.database.root

    def checked(address, count, bits, label):
        offset = address - address_offset
        length = count * bits // 8
        if offset < 0 or count <= 0 or offset + length > len(image):
            raise EditorError(f'{label} address range outside BIN: 0x{address:X}, {count} × {bits} bits')
        return offset, length

    def axis(record, name, count, z_offset, z_length, entry_index, label):
        prefix = f'Axis{name}.'
        if count == 1:
            return None, None, None
        source = _required(record, prefix + 'DataSrc')
        if source not in ('eRom', 'eUserdef', 'eDataSrcNone'):
            raise EditorError(f'Unsupported {prefix}DataSrc {source}')
        equation = _equation(record, prefix)
        precision = _precision(record, prefix + 'Precision')
        unit = _required(record, prefix + 'Unit')
        if _flag(record, prefix + 'bReciprocal') or _flag(record, prefix + 'bBackwards'):
            raise EditorError(f'Unsupported {name} reciprocal/backward axis')
        if _integer(_required(record, prefix + 'SkipBytes'), prefix + 'SkipBytes') != 0:
            raise EditorError(f'Noncontiguous {name} axis storage unsupported')
        if _integer(_required(record, prefix + 'DataHeader'), prefix + 'DataHeader') != 0:
            raise EditorError(f'Nonzero {name} axis DataHeader unsupported')
        if source == 'eRom':
            bits, axis_order = _organization(_required(record, prefix + 'DataOrg'))
            if axis_order and axis_order != order:
                raise EditorError(f'Mixed {name} ROM axis byte order unsupported')
            signed = _flag(record, prefix + 'bSigned')
            address = _integer(_required(record, prefix + 'DataAddr'), prefix + 'DataAddr')
            offset, length = checked(address, count, bits, name + ' axis')
            if max(z_offset, offset) < min(z_offset + z_length, offset + length):
                raise EditorError(f'{name} axis overlaps map values')
            code = {(8, False): 'B', (8, True): 'b', (16, False): 'H', (16, True): 'h',
                    (32, False): 'I', (32, True): 'i'}[(bits, signed)]
            values = list(struct.unpack_from(('>' if order == 'big' else '<') + code * count, image, offset))
            monotonic = _monotonic(values)
            evidence = {'map_index': entry_index, 'name': label, 'axis': name, 'offset': offset, 'count': count,
                        'bits': bits, 'signed': signed, 'byte_order': order, 'monotonic': monotonic,
                        'first': values[0], 'last': values[-1]}
            report['rom_axis_checks'].append(evidence)
            if not monotonic:
                raise EditorError(f'{name} ROM axis is not strictly monotonic in declared {order}-endian storage')
            return (AxisDefinition(offset, count, bits, signed, equation, unit, precision),
                    {'offset': offset, 'length': length, 'kind': name + ' axis'}, evidence)
        raw_values = str(record.get(prefix + 'Values', '')).strip()
        if raw_values:
            values = [_number(value, prefix + 'Values') for value in raw_values.split()]
            if len(values) != count:
                raise EditorError(f'{name} user-defined axis value count {len(values)} does not match {count}')
            labels = [f'{safe_math(equation, float(value)):.12g}' for value in values]
            if not _monotonic(list(map(float, labels))):
                raise EditorError(f'{name} user-defined numeric axis is not strictly monotonic')
            return AxisDefinition(None, count, equation='X', units=unit, decimal_places=precision, labels=labels), None, None
        report['warnings'].append({'map_index': entry_index, 'name': label,
                                   'reason': f'{name} source axis values unavailable; explicit index placeholder',
                                   'source_unit': unit})
        return AxisDefinition(None, count, equation='X', units='Index (source values unavailable)',
                              decimal_places=0, labels=[str(i) for i in range(count)]), None, None

    for index, record in enumerate(maps):
        label = str(record.get('Name', '<unnamed>')) if isinstance(record, dict) else '<invalid>'
        try:
            if not isinstance(record, dict):
                raise EditorError('Map entry is not an object')
            label = _required(record, 'Name')
            identifier = _required(record, 'IdName')
            if 'BROKEN' in label.upper():
                raise EditorError('Source export explicitly marks this entry BROKEN')
            if not label:
                raise EditorError('Map name is empty')
            kind = _required(record, 'Type')
            if kind not in ('eEinzel', 'eZweidim', 'eEindim', 'eZweiInv'):
                raise EditorError(f'Unsupported WinOLS Type {kind}')
            rows = _integer(_required(record, 'Rows'), 'Rows')
            cols = _integer(_required(record, 'Columns'), 'Columns')
            if not 0 < rows <= 512 or not 0 < cols <= 512 or rows * cols > 65536:
                raise EditorError(f'Invalid/excessive dimensions {rows} × {cols}')
            if kind == 'eEinzel' and (rows, cols) != (1, 1):
                raise EditorError('eEinzel scalar dimensions must be 1 × 1')
            if kind in ('eEindim', 'eZweiInv') and min(rows, cols) != 1:
                raise EditorError(f'{kind} with two active axes has unsupported storage orientation')
            for name in ('SkipBytes', 'LineSkipBytes'):
                if _integer(_required(record, name), name) != 0:
                    raise EditorError(f'Noncontiguous {name} storage unsupported')
            for name in ('bReciprocal', 'bOriginal', 'bDelta', 'bPercent'):
                if _flag(record, name):
                    raise EditorError(f'Unsupported display interpretation {name}')
            bits, _ = _organization(_required(record, 'DataOrg'))
            signed = _flag(record, 'bSigned')
            address = _integer(_required(record, 'Fieldvalues.StartAddr'), 'Fieldvalues.StartAddr')
            offset, length = checked(address, rows * cols, bits, 'Map values')
            equation = _equation(record, 'Fieldvalues.')
            precision = _precision(record, 'Precision')
            unit = _required(record, 'Fieldvalues.Unit')
            axes = {}
            spans = [{'offset': offset, 'length': length, 'kind': 'values'}]
            evidence = []
            for name, count in (('X', cols), ('Y', rows)):
                axes[name], span, check = axis(record, name, count, offset, length, index, label)
                if span:
                    spans.append(span)
                if check:
                    evidence.append(check)
            # Equal-dimension/header evidence is useful for catalog selection;
            # absent Bosch-style headers are not fabricated or repaired.
            dimension_fields = []
            if len(evidence) == 2 and all(check['bits'] == bits for check in evidence) and bits == 16:
                first = min(check['offset'] for check in evidence)
                if first >= 4:
                    words = struct.unpack_from(('>' if order == 'big' else '<') + 'HH', image, first - 4)
                    if sorted(words) == sorted((rows, cols)):
                        dimension_fields = [{'offset': first - 4 + i * 2, 'bits': 16, 'count': value,
                                             'axis': 'source header'} for i, value in enumerate(words)]
                        spans.append({'offset': first - 4, 'length': 4, 'kind': 'dimension header'})
                report['dimension_checks'].append({'map_index': index, 'name': label, 'matched': bool(dimension_fields),
                                                    'rows': rows, 'cols': cols})
            description = (f'{str(record.get("Comment", ""))}\nImported WinOLS JSON definition: {path.name}, entry {index}. '
                           f'IdName: {identifier}. Value address 0x{address:X}; {bits}-bit '
                           f'{"signed" if signed else "unsigned"}; {order} endian; original dimensions {rows} × {cols}. '
                           f'Type {kind}; contiguous rows, with original axis assignments.\n'
                           'Export scaling is retained exactly; map function labels originate from the source pack.')
            title = f'0x{offset:X} {label}'
            definition = CalibrationDefinition(title, description, str(record.get('FolderName', '')).strip() or 'Imported maps',
                                                offset, rows, cols, bits, signed, equation, unit, precision,
                                                axes['X'], axes['Y'], kind='constant' if kind == 'eEinzel' else 'table')
            workspace._validate(definition)
            item = ET.SubElement(root, 'XDFCONSTANT' if definition.kind == 'constant' else 'XDFTABLE',
                                 uniqueid=f'0x{len(report["records"]) + 1:X}', flags='0x0')
            workspace._write_definition(root, item, definition)
            report['records'].append({'name': title, 'source_name': label, 'source_id_name': identifier,
                                      'source_index': index, 'ecu_address': address, 'z_offset': offset,
                                      'rows': rows, 'cols': cols, 'bits': bits, 'signed': signed,
                                      'storage': 'ROW_DIR', 'source_type': kind, 'axes_exchanged': False,
                                      'dimension_fields': dimension_fields, 'spans': spans})
        except (EditorError, ValueError, OverflowError, ArithmeticError) as exc:
            report['skipped'].append({'map_index': index, 'name': label, 'reason': str(exc)})
    workspace.database = XDFDatabase.from_tree(workspace.database.tree, path.with_suffix('.xdf'))
    report['converted'] = len(workspace.database.definitions)
    report['skipped_count'] = len(report['skipped'])
    report['skip_reasons'] = dict(Counter(item['reason'] for item in report['skipped']))
    checks = report['rom_axis_checks']
    report['rom_axis_checks_total'] = len(checks)
    report['rom_axis_checks_passed'] = sum(check['monotonic'] for check in checks)
    report['monotonic_axis_ratio'] = report['rom_axis_checks_passed'] / len(checks) if checks else 0.0
    report['dimension_checks_total'] = len(report['dimension_checks'])
    report['dimension_checks_passed'] = sum(check['matched'] for check in report['dimension_checks'])
    report['recognition_eligible'] = (report['rom_axis_checks_passed'] >= 8 and
                                     report['monotonic_axis_ratio'] >= 0.9 and
                                     report['dimension_checks_passed'] >= 8)
    return ConversionResult(workspace, report)
