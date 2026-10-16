"""Conservative ASAP2 imports into the editor's contiguous integer XDF model.

``address_offset`` is an ECU address base: file_offset = ECU_address - base.
Providing ``image`` verifies dynamic record lengths against the associated
original image. Without it, AXIS_DESCR/AXIS_PTS maximum lengths are used and
that limitation is recorded. Inputs are read only; callers save the workspace.

RAT_FUNC coefficients describe physical -> internal (ASAM MCD-2 MC); only
unambiguously invertible affine cases are imported. COLUMN_DIR maps are shown
with exchanged axes, preserving the original contiguous byte order.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
import hashlib
import math
from pathlib import Path
import re
import xml.etree.ElementTree as ET

from .core import AxisDefinition, CalibrationDefinition, EditorError, XDFDatabase, safe_math
from .xdf_edit import XDFWorkspace


@dataclass
class ConversionResult:
    workspace: XDFWorkspace
    report: dict

    @property
    def database(self):
        return self.workspace.database

    @property
    def definitions(self):
        return self.database.definitions


@dataclass
class _Block:
    kind: str
    tokens: list[str] = field(default_factory=list)
    children: list['_Block'] = field(default_factory=list)

    def option(self, name: str, count: int = 1) -> list[str] | None:
        try:
            index = self.tokens.index(name)
        except ValueError:
            return None
        values = self.tokens[index + 1:index + 1 + count]
        if len(values) != count:
            raise EditorError(f"Incomplete {name}")
        return values


# Comments must be recognized together with strings: comment markers inside a
# description, or a literal /begin in a quoted string, are never syntax.
_TOKEN = re.compile(r'/\*.*?\*/|//[^\r\n]*|"(?:""|\\.|[^"\\])*"|[^\s]+', re.S)
_WANTED = {'PROJECT', 'HEADER', 'MODULE', 'MOD_COMMON', 'MOD_PAR',
           'CHARACTERISTIC', 'AXIS_PTS', 'COMPU_METHOD', 'RECORD_LAYOUT',
           'FUNCTION', 'GROUP'}
_INTEGER = {'UBYTE': (8, False), 'SBYTE': (8, True),
            'UWORD': (16, False), 'SWORD': (16, True),
            'ULONG': (32, False), 'SLONG': (32, True)}


def _decode_string(token: str) -> str:
    if token.startswith('"'):
        return re.sub(r'\\(["\\])', r'\1', token[1:-1]).replace('""', '"')
    return token


def _parse(text: str) -> dict[str, list[_Block]]:
    blocks = defaultdict(list)
    root = _Block('ROOT')
    stack = [root]
    iterator = iter(_TOKEN.finditer(text))
    for match in iterator:
        token = match.group()
        if token.startswith(('/*', '//')):
            continue
        if token == '/begin':
            try:
                kind = next(iterator).group()
            except StopIteration as exc:
                raise EditorError('Incomplete A2L block start') from exc
            block = _Block(kind)
            stack[-1].children.append(block)
            stack.append(block)
            if kind in _WANTED:
                blocks[kind].append(block)
        elif token == '/end':
            try:
                kind = next(iterator).group()
            except StopIteration as exc:
                raise EditorError('Incomplete A2L block end') from exc
            if len(stack) == 1 or stack[-1].kind != kind:
                raise EditorError(f'Mismatched A2L /end {kind}')
            stack.pop()
        else:
            stack[-1].tokens.append(_decode_string(token))
    if len(stack) != 1:
        raise EditorError(f'Unclosed A2L {stack[-1].kind} block')
    return dict(blocks)


def _int(value: str) -> int:
    try:
        return int(value, 16) if value.lower().startswith('0x') else int(value, 10)
    except (ValueError, TypeError) as exc:
        raise EditorError(f'Invalid integer {value!r}') from exc


def _decimal(fmt: str | None, default: int = 2) -> int:
    match = re.search(r'%\d*\.(\d+)', fmt or '')
    return min(8, int(match.group(1))) if match else default


def _one(block: _Block, name: str, default=None):
    values = block.option(name)
    return values[0] if values is not None else default


def _index(blocks: list[_Block], kind: str) -> dict[str, _Block]:
    indexed = {}
    for block in blocks:
        if not block.tokens:
            raise EditorError(f'{kind} has no name')
        name = block.tokens[0]
        if name in indexed:
            raise EditorError(f'Duplicate {kind} name {name}; ambiguous module scope')
        indexed[name] = block
    return indexed


def _conversion(name: str, methods: dict[str, _Block], owner: _Block | None = None) -> tuple[str, str, int]:
    if name == 'NO_COMPU_METHOD':
        equation, units, decimals = 'X', '', 2
    else:
        method = methods.get(name)
        if method is None or len(method.tokens) < 5:
            raise EditorError(f'Unknown/incomplete conversion {name}')
        _, _, mode, fmt, units = method.tokens[:5]
        decimals = _decimal(fmt)
        if mode == 'IDENTICAL':
            equation = 'X'
        elif mode == 'LINEAR':
            coefficients = method.option('COEFFS_LINEAR', 2)
            if coefficients is None:
                raise EditorError(f'Missing COEFFS_LINEAR for {name}')
            slope, offset = map(float, coefficients)
            if not math.isfinite(slope) or not math.isfinite(offset) or slope == 0:
                raise EditorError(f'Noninvertible/nonfinite LINEAR conversion {name}')
            equation = f'X*({slope:.17g})+({offset:.17g})'
        elif mode == 'RAT_FUNC':
            coefficients = method.option('COEFFS', 6)
            if coefficients is None:
                raise EditorError(f'Missing COEFFS for {name}')
            a, b, c, d, e, f = map(float, coefficients)
            if not all(math.isfinite(v) for v in (a, b, c, d, e, f)):
                raise EditorError(f'Nonfinite RAT_FUNC conversion {name}')
            if a != 0 or d != 0 or e != 0 or b == 0 or f == 0:
                raise EditorError(f'Unsupported/noninvertible nonlinear RAT_FUNC conversion {name}')
            equation = f'X*({f/b:.17g})+({-c/b:.17g})'
        else:
            raise EditorError(f'Unsupported conversion {mode}: {name}')
    if owner is not None:
        units = _one(owner, 'PHYS_UNIT', units)
        decimals = _decimal(_one(owner, 'FORMAT'), decimals)
    return equation, units, decimals


@dataclass
class _Field:
    kind: str
    position: int
    datatype: str
    mode: str = ''


def _fields(layout: _Block) -> list[_Field]:
    result = []
    tokens = layout.tokens[1:]
    index = 0
    while index < len(tokens):
        kind = tokens[index]
        if kind == 'STATIC_RECORD_LAYOUT':
            raise EditorError('STATIC_RECORD_LAYOUT requires reserved maximum dimensions; unsupported')
        if kind.startswith('ALIGNMENT_'):
            index += 2
            continue
        if kind == 'FNC_VALUES' or kind.startswith('AXIS_PTS_'):
            if index + 4 >= len(tokens):
                raise EditorError(f'Incomplete layout {kind}')
            position, datatype, mode, addressing = tokens[index + 1:index + 5]
            if addressing != 'DIRECT':
                raise EditorError(f'Indirect {addressing} addressing is unsupported')
            if kind == 'FNC_VALUES' and mode not in ('ROW_DIR', 'COLUMN_DIR'):
                raise EditorError(f'Unsupported table storage {mode}')
            if kind != 'FNC_VALUES' and mode != 'INDEX_INCR':
                raise EditorError(f'Unsupported axis sequence {mode}')
            result.append(_Field(kind, _int(position), datatype, mode))
            index += 5
        elif kind.startswith('NO_AXIS_PTS_'):
            if index + 2 >= len(tokens):
                raise EditorError(f'Incomplete layout {kind}')
            position, datatype = tokens[index + 1:index + 3]
            result.append(_Field(kind, _int(position), datatype))
            index += 3
        elif kind == 'RESERVED':
            if index + 2 >= len(tokens):
                raise EditorError('Incomplete RESERVED')
            position, datatype = tokens[index + 1:index + 3]
            # ASAP2 RESERVED uses BYTE/WORD/LONG (unsigned representation).
            datatype = {'BYTE': 'UBYTE', 'WORD': 'UWORD', 'LONG': 'ULONG'}.get(datatype, datatype)
            result.append(_Field(kind, _int(position), datatype))
            index += 3
        else:
            raise EditorError(f'Unsupported record layout field {kind}')
    if any(item.datatype not in _INTEGER for item in result):
        unsupported = ', '.join(sorted({item.datatype for item in result if item.datatype not in _INTEGER}))
        raise EditorError(f'Unsupported datatype {unsupported}')
    positions = [item.position for item in result]
    if len(set(positions)) != len(positions) or any(p <= 0 for p in positions):
        raise EditorError('Duplicate/nonpositive record layout positions')
    names = [item.kind for item in result if item.kind != 'RESERVED']
    if len(set(names)) != len(names):
        raise EditorError('Duplicate record layout fields')
    return sorted(result, key=lambda item: item.position)


def _byte_order(block: _Block, expected: bool) -> None:
    value = _one(block, 'BYTE_ORDER')
    if value is None:
        return
    if value not in ('MSB_FIRST', 'MSB_LAST'):
        raise EditorError(f'Unsupported byte order {value}')
    if (value == 'MSB_LAST') != expected:
        raise EditorError('Mixed per-definition byte order is unsupported by this editor')


def convert_a2l(path: Path | str, bin_size: int, address_offset: int = 0,
                default_lsb_first: bool = False, image: bytes | None = None) -> ConversionResult:
    """Return a new workspace and JSON-compatible import report, without writes.

    Supported: integer VALUE, CURVE and MAP, direct adjacent or shared axes,
    fixed axes, ROW_DIR/COLUMN_DIR, IDENTICAL/LINEAR/affine inverse RAT_FUNC.
    Unsupported objects are omitted with an explicit name and reason.
    """
    path = Path(path)
    if type(bin_size) is not int or bin_size <= 0 or type(address_offset) is not int:
        raise EditorError('BIN size must be positive and address base must be an integer')
    if type(default_lsb_first) is not bool:
        raise EditorError('Default byte order must be boolean')
    if image is not None and len(image) != bin_size:
        raise EditorError('Reference image length must match BIN size')
    raw = path.read_bytes()
    try:
        text = raw.decode('utf-8-sig')
        encoding = 'utf-8'
    except UnicodeDecodeError:
        try:
            text = raw.decode('cp1252')
            encoding = 'cp1252'
        except UnicodeDecodeError:
            # Bosch exports contain Windows-1252 descriptions and occasional
            # DOS bytes in vendor comments. Preserve the characteristic text;
            # replacing undefined CP1252 bytes avoids misreading ü/ß as glyphs.
            text = raw.decode('cp1252', errors='replace')
            encoding = 'cp1252 (undefined bytes replaced)'
    blocks = _parse(text)
    if not blocks.get('CHARACTERISTIC'):
        raise EditorError('No A2L CHARACTERISTIC objects found (legacy DAM/OLS are not supported)')
    if len(blocks.get('MODULE', [])) > 1:
        raise EditorError('Multi-module A2L requires scoped address handling; unsupported')
    methods = _index(blocks.get('COMPU_METHOD', []), 'COMPU_METHOD')
    layouts = _index(blocks.get('RECORD_LAYOUT', []), 'RECORD_LAYOUT')
    shared_axes = _index(blocks.get('AXIS_PTS', []), 'AXIS_PTS')
    common = blocks.get('MOD_COMMON', [_Block('MOD_COMMON')])[0]
    byte_order = _one(common, 'BYTE_ORDER')
    if byte_order is not None:
        if byte_order not in ('MSB_FIRST', 'MSB_LAST'):
            raise EditorError(f'Unsupported module byte order {byte_order}')
        default_lsb_first = byte_order == 'MSB_LAST'
    alignments = {}
    for datatype, (bits, _) in _INTEGER.items():
        keyword = {8: 'ALIGNMENT_BYTE', 16: 'ALIGNMENT_WORD', 32: 'ALIGNMENT_LONG'}[bits]
        alignment = _int(_one(common, keyword, '1'))
        if alignment not in (1, 2, 4, 8):
            raise EditorError(f'Unsupported {keyword} {alignment}')
        alignments[datatype] = alignment
    project = blocks.get('PROJECT', [_Block('PROJECT')])[0].tokens
    project_name = project[0] if project else path.stem
    project_description = project[1] if len(project) > 1 else ''
    metadata_text = '\n'.join([' '.join(b.tokens) for kind in ('PROJECT', 'HEADER', 'MOD_PAR') for b in blocks.get(kind, [])])
    explicit_families = sorted(set(re.findall(r'EDC\s*1[56][A-Za-z0-9/+._-]*', metadata_text, re.I)))
    report = {'source_name': path.name, 'source_sha256': hashlib.sha256(raw).hexdigest(), 'encoding': encoding,
              'project': project_name, 'project_description': project_description,
              'ecu_family_evidence': explicit_families, 'bin_size': bin_size,
              'address_offset': address_offset, 'byte_order': 'little' if default_lsb_first else 'big',
              'reference_image_sha256': hashlib.sha256(image).hexdigest() if image is not None else None,
              'characteristics': len(blocks['CHARACTERISTIC']), 'converted': 0,
              'skipped': [], 'warnings': [], 'records': []}
    epk_values = {_one(block, 'EPK') for block in blocks.get('MOD_PAR', []) if _one(block, 'EPK')}
    if len(epk_values) > 1:
        raise EditorError('Multiple different EPK values require scoped address handling; unsupported')
    report['epk'] = next(iter(epk_values), None)
    report['epk_addresses'] = [_int(block.tokens[index + 1])
                               for block in blocks.get('MOD_PAR', [])
                               for index, token in enumerate(block.tokens[:-1]) if token == 'ADDR_EPK']
    report['epk_checks'] = []
    if report['epk']:
        try:
            expected = report['epk'].encode('ascii')
        except UnicodeEncodeError as exc:
            raise EditorError('EPK identity is not an ASCII identifier') from exc
        for address in report['epk_addresses']:
            offset = address - address_offset
            within = 0 <= offset <= bin_size - len(expected)
            matched = image[offset:offset + len(expected)] == expected if image is not None and within else None
            report['epk_checks'].append({'text': report['epk'], 'ecu_address': address, 'offset': offset,
                                         'inside_bin': within, 'matched': matched})
    report['header_versions'] = [_one(block, 'VERSION') for block in blocks.get('HEADER', [])
                                  if _one(block, 'VERSION')]
    if image is None:
        report['warnings'].append('No reference image: A2L maximum axis counts used; dynamic lengths are unverified.')
    if '\ufffd' in text:
        report['warnings'].append(f'{text.count(chr(0xfffd))} undefined source encoding bytes replaced.')
    if any(f.upper().startswith('EDC15') for f in explicit_families):
        report['warnings'].append('Source explicitly identifies EDC15 despite its enclosing directory name.')
    workspace = XDFWorkspace(bin_size=bin_size)
    workspace.set_metadata(f'{project_name} — imported A2L', 'Emil Larsson',
                           f'Imported from {path.name}. {project_description}\nSource SHA-256: {report["source_sha256"]}\n'
                           f'File offset = ECU address - 0x{address_offset:X}. Supported objects only; see import report.',
                           default_lsb_first)
    root = workspace.database.root
    layout_cache = {}
    shared_cache = {}

    def checked(address: int, count: int, datatype: str) -> int:
        bits, _ = _INTEGER[datatype]
        offset = address - address_offset
        if offset < 0 or count <= 0 or offset + count * bits // 8 > bin_size:
            raise EditorError(f'Address range outside BIN: ECU 0x{address:X}, {count} × {bits} bits')
        return offset

    def layout_fields(name):
        if name not in layout_cache:
            if name not in layouts:
                raise EditorError(f'Unknown record layout {name}')
            layout_cache[name] = _fields(layouts[name])
        return layout_cache[name]

    def deposit(base: int, name: str, counts: dict[str, int], *, shared=False):
        fields = layout_fields(name)
        counts = dict(counts)
        addresses = {}
        count_offsets = []
        cursor = base
        for item in fields:
            if item.kind.endswith(('_Z', '_4', '_5')):
                raise EditorError('More than two axes are unsupported')
            width = _INTEGER[item.datatype][0] // 8
            layout = layouts[name]
            alignment_keyword = {1: 'ALIGNMENT_BYTE', 2: 'ALIGNMENT_WORD', 4: 'ALIGNMENT_LONG'}[width]
            alignment = _int(_one(layout, alignment_keyword, str(alignments[item.datatype])))
            if alignment not in (1, 2, 4, 8):
                raise EditorError(f'Unsupported alignment {alignment}')
            cursor = ((cursor + alignment - 1) // alignment) * alignment
            if item.kind.startswith('NO_AXIS_PTS_'):
                axis = item.kind[-1]
                offset = checked(cursor, 1, item.datatype)
                maximum = counts.get(axis)
                if maximum is None:
                    raise EditorError(f'Missing {axis} axis description')
                count = maximum
                if image is not None:
                    count = int.from_bytes(image[offset:offset + width], 'little' if default_lsb_first else 'big',
                                           signed=_INTEGER[item.datatype][1])
                    if not 0 < count <= maximum:
                        raise EditorError(f'Invalid {axis} dimension count {count}; A2L maximum {maximum}')
                counts[axis] = count
                count_offsets.append({'axis': axis, 'offset': offset, 'bits': width * 8, 'count': count})
                size = 1
            elif item.kind.startswith('AXIS_PTS_'):
                axis = item.kind[-1]
                if axis not in counts:
                    raise EditorError(f'Missing {axis} axis description')
                size = counts[axis]
            elif item.kind == 'FNC_VALUES':
                size = counts.get('X', 1) * counts.get('Y', 1)
            else:
                size = 1
            addresses[item.kind] = (checked(cursor, size, item.datatype), item.datatype, item.mode)
            cursor += width * size
        if shared and ('FNC_VALUES' in addresses or 'AXIS_PTS_Y' in addresses):
            raise EditorError('Shared AXIS_PTS layout contains unsupported function/Y fields')
        return addresses, counts, count_offsets

    def shared_axis(name: str):
        if name in shared_cache:
            return shared_cache[name]
        node = shared_axes.get(name)
        if node is None or len(node.tokens) < 10:
            raise EditorError(f'Unknown/incomplete AXIS_PTS {name}')
        _byte_order(node, default_lsb_first)
        if _int(_one(node, 'ECU_ADDRESS_EXTENSION', '0')) != 0 or node.option('BIT_MASK') is not None:
            raise EditorError('Shared axis address extension/bit masking unsupported')
        if _one(node, 'DEPOSIT', 'ABSOLUTE') != 'ABSOLUTE':
            raise EditorError('Relative axis deposit unsupported')
        _, _, address, _, layout_name, _, conversion_name, max_count, _, _ = node.tokens[:10]
        if not 0 < _int(max_count) <= 4096:
            raise EditorError('Invalid/excessive shared axis maximum')
        entries, counts, length_fields = deposit(_int(address), layout_name, {'X': _int(max_count)}, shared=True)
        if 'AXIS_PTS_X' not in entries:
            raise EditorError('Shared axis layout has no AXIS_PTS_X')
        offset, datatype, _ = entries['AXIS_PTS_X']
        equation, units, decimals = _conversion(conversion_name, methods, node)
        result = AxisDefinition(offset, counts['X'], *_INTEGER[datatype], equation, units, decimals)
        shared_cache[name] = (result, length_fields)
        return shared_cache[name]

    categories = {}
    for group_kind in ('FUNCTION', 'GROUP'):
        for group in blocks.get(group_kind, []):
            if not group.tokens:
                continue
            for child in group.children:
                if child.kind in ('DEF_CHARACTERISTIC', 'REF_CHARACTERISTIC'):
                    for name in child.tokens:
                        categories.setdefault(name, group.tokens[0])

    definitions = []
    for characteristic in blocks['CHARACTERISTIC']:
        name = characteristic.tokens[0] if characteristic.tokens else '<unnamed>'
        try:
            if len(characteristic.tokens) < 9:
                raise EditorError('Incomplete CHARACTERISTIC header')
            name, description, kind, address, layout_name, _, conversion_name, _, _ = characteristic.tokens[:9]
            if kind not in ('VALUE', 'CURVE', 'MAP'):
                raise EditorError(f'Unsupported characteristic type {kind}')
            for forbidden in ('BIT_MASK', 'VIRTUAL', 'DEPENDENT_CHARACTERISTIC'):
                if forbidden in characteristic.tokens or any(c.kind == forbidden for c in characteristic.children):
                    raise EditorError(f'Unsupported {forbidden}')
            if _int(_one(characteristic, 'ECU_ADDRESS_EXTENSION', '0')) != 0:
                raise EditorError('Nonzero ECU address extension unsupported')
            _byte_order(characteristic, default_lsb_first)
            axis_nodes = [child for child in characteristic.children if child.kind == 'AXIS_DESCR']
            required = {'VALUE': 0, 'CURVE': 1, 'MAP': 2}[kind]
            if len(axis_nodes) != required:
                raise EditorError(f'{kind} requires {required} axis descriptions')
            counts = {}
            axes = {}
            length_fields = []
            for axis_name, node in zip(('X', 'Y'), axis_nodes):
                if len(node.tokens) < 6:
                    raise EditorError('Incomplete AXIS_DESCR')
                axis_kind, _, axis_conversion, max_count, _, _ = node.tokens[:6]
                maximum = _int(max_count)
                if not 0 < maximum <= 4096:
                    raise EditorError('Invalid/excessive axis maximum')
                _byte_order(node, default_lsb_first)
                if _one(node, 'DEPOSIT', 'ABSOLUTE') != 'ABSOLUTE':
                    raise EditorError('Relative axis deposit unsupported')
                counts[axis_name] = maximum
                if axis_kind == 'COM_AXIS':
                    reference = _one(node, 'AXIS_PTS_REF')
                    if reference is None:
                        raise EditorError('COM_AXIS has no AXIS_PTS_REF')
                    axis, shared_lengths = shared_axis(reference)
                    if axis.count > maximum:
                        raise EditorError('Shared axis exceeds AXIS_DESCR maximum')
                    equation, units, decimals = _conversion(axis_conversion, methods, node)
                    axes[axis_name] = AxisDefinition(axis.address, axis.count, axis.element_bits, axis.signed,
                                                     equation, units, decimals)
                    counts[axis_name] = axis.count
                    length_fields.extend(shared_lengths)
                elif axis_kind == 'FIX_AXIS':
                    equation, units, decimals = _conversion(axis_conversion, methods, node)
                    fixed = node.option('FIX_AXIS_PAR', 3)
                    distributed = node.option('FIX_AXIS_PAR_DIST', 3)
                    listed = [child for child in node.children if child.kind == 'FIX_AXIS_PAR_LIST']
                    if sum(bool(value) for value in (fixed, distributed, listed)) != 1:
                        raise EditorError('FIX_AXIS needs exactly one supported axis parameter form')
                    if fixed or distributed:
                        offset, interval, number = fixed or distributed
                        number = _int(number)
                        if not 0 < number <= maximum:
                            raise EditorError('Fixed axis count exceeds maximum')
                        if fixed and not -64 <= _int(interval) <= 64:
                            raise EditorError('Fixed axis binary exponent outside supported range')
                        increment = 2 ** _int(interval) if fixed else float(interval)
                        values = [float(offset) + i * increment for i in range(number)]
                    else:
                        values = list(map(float, listed[0].tokens))
                    if not 0 < len(values) <= maximum:
                        raise EditorError('Fixed axis count exceeds maximum')
                    labels = [f'{safe_math(equation, value):.12g}' for value in values]
                    axes[axis_name] = AxisDefinition(None, len(values), equation='X', units=units,
                                                     decimal_places=decimals, labels=labels)
                    counts[axis_name] = len(values)
                elif axis_kind != 'STD_AXIS':
                    raise EditorError(f'Unsupported axis attribute {axis_kind}')
            entries, counts, own_lengths = deposit(_int(address), layout_name, counts)
            length_fields.extend(own_lengths)
            for axis_name, node in zip(('X', 'Y'), axis_nodes):
                entry = entries.get('AXIS_PTS_' + axis_name)
                if node.tokens[0] == 'STD_AXIS':
                    if entry is None:
                        raise EditorError(f'STD_AXIS {axis_name} has no axis points in record layout')
                    offset, datatype, _ = entry
                    equation, units, decimals = _conversion(node.tokens[2], methods, node)
                    axes[axis_name] = AxisDefinition(offset, counts[axis_name], *_INTEGER[datatype],
                                                     equation, units, decimals)
                elif entry is not None:
                    raise EditorError('Shared/fixed axis also occupies inline record storage')
                elif axes[axis_name].count != counts[axis_name]:
                    raise EditorError('Stored axis count conflicts with fixed/shared length')
            if 'FNC_VALUES' not in entries:
                raise EditorError('Record layout has no FNC_VALUES')
            offset, datatype, mode = entries['FNC_VALUES']
            equation, units, decimals = _conversion(conversion_name, methods, characteristic)
            x_axis, y_axis = axes.get('X'), axes.get('Y')
            cols, rows = counts.get('X', 1), counts.get('Y', 1)
            transposed = kind == 'MAP' and mode == 'COLUMN_DIR'
            if transposed:
                x_axis, y_axis = y_axis, x_axis
                rows, cols = cols, rows
            provenance = (f'{description}\nImported A2L definition: {path.name} / {name}. '
                          f'ECU record address 0x{_int(address):X}; value offset 0x{offset:X}. '
                          f'Record layout {layout_name}; {datatype}; {mode}.')
            if transposed:
                provenance += '\nCOLUMN_DIR: displayed axes exchanged to preserve contiguous original byte order.'
            definition = CalibrationDefinition(name, provenance, categories.get(name, name.split('_', 1)[0]),
                                                offset, rows, cols, *_INTEGER[datatype], equation, units, decimals,
                                                x_axis, y_axis, kind='constant' if kind == 'VALUE' else 'table')
            workspace._validate(definition)
            # Batch construction avoids reparsing/deep-copying thousands of
            # definitions once per object, which would be quadratic.
            ident = f'0x{len(definitions) + 1:X}'
            item = ET.SubElement(root, 'XDFCONSTANT' if definition.kind == 'constant' else 'XDFTABLE',
                                 uniqueid=ident, flags='0x0')
            workspace._write_definition(root, item, definition)
            definitions.append(definition)
            spans = [{'offset': offset, 'length': definition.size * definition.z_width, 'kind': 'values'}]
            for displayed_name, displayed_axis in (('X', x_axis), ('Y', y_axis)):
                if displayed_axis is not None and displayed_axis.address is not None:
                    spans.append({'offset': displayed_axis.address,
                                  'length': displayed_axis.count * displayed_axis.element_bits // 8,
                                  'kind': f'{displayed_name} axis'})
            for dimension in length_fields:
                spans.append({'offset': dimension['offset'], 'length': dimension['bits'] // 8,
                              'kind': f'{dimension["axis"]} dimension'})
            report['records'].append({'name': name, 'ecu_address': _int(address),
                                      'z_offset': offset, 'rows': rows, 'cols': cols,
                                      'bits': definition.z_element_bits, 'signed': definition.z_signed,
                                      'storage': mode, 'axes_exchanged': transposed,
                                      'dimension_fields': length_fields, 'spans': spans})
        except (EditorError, ValueError, OverflowError, ArithmeticError) as exc:
            report['skipped'].append({'name': name, 'reason': str(exc)})
    workspace.database = XDFDatabase.from_tree(workspace.database.tree, path.with_suffix('.xdf'))
    report['converted'] = len(workspace.database.definitions)
    report['skipped_count'] = len(report['skipped'])
    report['skip_reasons'] = dict(Counter(item['reason'] for item in report['skipped']))
    return ConversionResult(workspace, report)
