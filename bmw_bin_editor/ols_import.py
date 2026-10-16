"""Read validated legacy WinOLS projects using the bundled, read-only parser.

The native parser decodes map records. Version slots, metadata, directory
counts, folders, and every returned raw-record span are checked independently
against an immutable Python snapshot. Unsupported wire layouts fail explicitly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile

from .core import EditorError


_HEADER = struct.pack('<I', 11) + b'WinOLS File\0'
_VERSION_FENCE = struct.pack('<I', 0x42007899)
_VERSION_DIRECTORY = struct.pack('<I', 0x98728833)
_MAP_DIRECTORY = struct.pack('<I', 0x11883377)
_FOLDER_DIRECTORY = struct.pack('<III', 0x98638811, 0, 0x98638811)
_SLOT_SIZE = 2 * 1024 * 1024
_MAX_SOURCE_SIZE = 128 * 1024 * 1024
_MAX_OUTPUT_SIZE = 256 * 1024 * 1024
_PARSER_TIMEOUT = 45


@dataclass
class OLSVersion:
    index: int
    name: str
    data: bytes
    sha256: str


@dataclass
class OLSProject:
    source_path: Path
    source_sha256: str
    format_version: int
    title: str
    metadata: dict
    maps: list[dict]
    folders: list[dict]
    versions: list[OLSVersion]
    warnings: list[str]
    source_data: bytes = field(default=b'', repr=False)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _u32(source: bytes, offset: int) -> int:
    if offset < 0 or offset > len(source) - 4:
        raise EditorError('Truncated OLS directory or record.')
    return struct.unpack_from('<I', source, offset)[0]


def _cstring(source: bytes, offset: int, limit: int) -> tuple[str, int]:
    length = _u32(source, offset)
    end = offset + 4 + length + bool(length)
    if length > 8192 or end > limit:
        raise EditorError('Truncated or unsupported legacy OLS string.')
    raw = source[offset + 4:offset + 4 + length]
    if b'\0' in raw or (length and source[end - 1] != 0):
        raise EditorError('Invalid legacy OLS string terminator.')
    return raw.decode('cp1252', errors='replace'), end


def _positions(source: bytes, marker: bytes, start: int, end: int):
    position = source.find(marker, start, end)
    while position >= 0:
        yield position
        position = source.find(marker, position + 1, end)


def _metadata(source: bytes) -> tuple[dict, int]:
    offset = 20
    fields = []
    for _ in range(16):
        start = offset
        value, offset = _cstring(source, offset, len(source))
        fields.append({'source_offset': start, 'source_end': offset, 'value': value})
    values = [item['value'] for item in fields]
    metadata = dict(zip(('make', 'model', 'type', 'year', 'output', 'legacy_reserved_5',
                         'power', 'transmission', 'memory', 'manufacturer', 'ecu_name',
                         'legacy_reserved_11', 'hw_number', 'sw_number', 'engine_code',
                         'project_comment'), values))
    metadata['raw_project_fields'] = fields
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}', metadata['ecu_name']):
        raise EditorError('OLS project has no validated ECU name.')
    if not re.fullmatch(r'\d{6}', metadata['sw_number']):
        raise EditorError('OLS project has no validated six-digit software number.')
    return metadata, offset


def _directories(source: bytes, metadata_end: int) -> dict:
    # A validated directory must describe exact 2 MiB slots ending at EOF.
    # Searching a sentinel alone is insufficient: all directory relations and
    # both sentinels must agree, with precisely one structural candidate.
    candidates = []
    for fence in _positions(source, _VERSION_FENCE, metadata_end, min(len(source), 65536)):
        if fence < 12:
            continue
        count = _u32(source, fence - 12) + 1
        directory = _u32(source, fence - 8)
        stride = _u32(source, fence - 4)
        if not 1 <= count <= 32 or stride != _SLOT_SIZE:
            continue
        payload = directory + 4
        if payload + count * stride != len(source) or directory <= fence:
            continue
        if source[directory:payload] != _VERSION_DIRECTORY:
            continue
        dispatch = list(_positions(source, _MAP_DIRECTORY, fence + 4, min(directory, fence + 65536)))
        if len(dispatch) != 1:
            continue
        map_count = _u32(source, dispatch[0] + 4)
        if not 2 <= map_count <= 50000:
            continue
        folder_candidates = list(_positions(source, _FOLDER_DIRECTORY, dispatch[0] + 8, directory))
        if len(folder_candidates) != 1:
            continue
        folders = folder_candidates[0]
        if not 1 <= _u32(source, folders + 12) <= 10000:
            continue
        candidates.append({'version_count_minus_one_offset': fence - 12,
                           'version_count': count, 'version_data_directory_offset': directory,
                           'payload_offset': payload, 'version_stride': stride,
                           'map_dispatch_offset': dispatch[0], 'declared_map_records': map_count,
                           'folder_table_offset': folders})
    if len(candidates) != 1:
        raise EditorError('Unsupported or truncated OLS format-113 version directory; '
                          'exact 2 MiB version slots and matching sentinels are required.')
    return candidates[0]


def _folders(source: bytes, directory: dict) -> list[dict]:
    offset = directory['folder_table_offset'] + 16
    limit = directory['version_data_directory_offset']
    folders = []
    identifiers = set()
    for index in range(_u32(source, directory['folder_table_offset'] + 12)):
        start = offset
        identifier = _u32(source, offset)
        link = _u32(source, offset + 4)
        name, offset = _cstring(source, offset + 8, limit)
        if not name or offset + 6 > limit or identifier in identifiers:
            raise EditorError('Invalid or truncated legacy OLS folder table.')
        flags = list(source[offset:offset + 6])
        if any(flag not in (0, 1) for flag in flags):
            raise EditorError('Unsupported legacy OLS folder flags.')
        offset += 6
        identifiers.add(identifier)
        folders.append({'index': index, 'id': identifier, 'raw_link_field': link, 'name': name,
                        'raw_flags': flags, 'source_record_offset': start,
                        'source_record_end': offset, 'source_record_hex': source[start:offset].hex()})
    directory['folder_table_end'] = offset
    return folders


def _helper_path() -> Path:
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]))
    return root / 'assets' / 'ols' / 'ols_dump.exe'


def _run_parser(source: bytes) -> dict:
    helper = _helper_path()
    if not helper.is_file():
        raise EditorError('The bundled OLS parser is missing. Reinstall or rebuild the editor '
                          'with assets/ols/ols_dump.exe included.')
    # Relative ASCII names also let the narrow-character native CLI work when
    # the original project filename or Windows temporary folder uses Unicode.
    with tempfile.TemporaryDirectory(prefix='bmw_ols_') as temporary:
        folder = Path(temporary)
        (folder / 'project.ols').write_bytes(source)
        output_path = folder / 'definitions.json'
        options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
        try:
            with output_path.open('wb') as output:
                result = subprocess.run([str(helper), '--json', 'project.ols'], cwd=folder,
                                        stdout=output, stderr=subprocess.PIPE,
                                        timeout=_PARSER_TIMEOUT, check=False, **options)
        except subprocess.TimeoutExpired as exc:
            raise EditorError(f'OLS parser exceeded its {_PARSER_TIMEOUT}-second limit.') from exc
        except OSError as exc:
            raise EditorError(f'Unable to start the bundled OLS parser: {exc}') from exc
        if result.returncode:
            message = result.stderr.decode('utf-8', errors='replace').strip()[:1000]
            raise EditorError(f'OLS parser rejected this project: {message or result.returncode}')
        if output_path.stat().st_size > _MAX_OUTPUT_SIZE:
            raise EditorError('OLS parser output exceeds the supported size limit.')
        try:
            decoded = json.loads(output_path.read_bytes())
        except (ValueError, UnicodeError) as exc:
            raise EditorError('The bundled OLS parser returned invalid JSON.') from exc
        if not isinstance(decoded, dict):
            raise EditorError('The bundled OLS parser returned an invalid project.')
        return decoded


def _verify_record(source: bytes, record: dict, prefix: str, lower: int, upper: int) -> None:
    start, end, raw = (record.get(prefix + suffix) for suffix in ('_offset', '_end', '_hex'))
    if (type(start) is not int or type(end) is not int or not lower <= start < end <= upper
            or not isinstance(raw, str) or raw != source[start:end].hex()):
        raise EditorError(f'OLS parser returned an invalid {prefix.replace("_", " ")} span.')


def _axis_values(axis: dict, image: bytes) -> None:
    # Recompute breakpoints from the exact first slot, ignoring the upstream
    # old-format ROM assembly/base-address guess completely.
    count = axis.get('pts_count', 0)
    width = axis.get('pts_data_size', 0)
    if not axis.get('has_pts_address') or not count:
        return
    address = axis.get('raw_pts_address')
    if type(address) is not int or type(count) is not int or width not in (1, 2, 4, 8):
        raise EditorError('Unsupported OLS ROM axis storage.')
    if not 0 <= address <= len(image) - count * width:
        raise EditorError('OLS ROM axis extends outside the embedded BIN.')
    axis['pts_address'] = address
    axis['pts_address_hex'] = f'0x{address:08x}'
    order = 'big' if axis.get('pts_big_endian') else 'little'
    signed = bool(axis.get('pts_signed'))
    dtype = axis.get('pts_data_type')
    code = {6: '>f', 7: '<f', 12: '>d', 13: '<d'}.get(dtype)
    axis['pts_values'] = [struct.unpack(code, image[address + i * width:address + (i + 1) * width])[0]
                          if code else int.from_bytes(image[address + i * width:address + (i + 1) * width],
                                                      order, signed=signed) for i in range(count)]


def _native_text(value: str) -> str:
    # The C helper writes each legacy byte as a Latin-1 JSON escape. The wire
    # strings use Windows-1252, including punctuation in the 0x80..0x9F range.
    return value.encode('latin-1').decode('cp1252', errors='replace')


def _maps(source: bytes, native: dict, directory: dict, folders: list[dict], image: bytes) -> list[dict]:
    maps = native.get('maps')
    if not isinstance(maps, list) or len(maps) != directory['declared_map_records'] - 1:
        raise EditorError('Incomplete OLS map decoding: serialized record count does not match '
                          'the project directory (excluding its Hexdump view).')
    lower = directory['map_dispatch_offset'] + 9
    upper = directory['folder_table_offset']
    folder_index = {item['id']: item for item in folders}
    # This validated legacy layout has one initial Hexdump view record.
    name, _ = _cstring(source, lower, upper)
    if name != 'Hexdump':
        raise EditorError('Unsupported OLS map-directory prefix; expected the Hexdump view.')
    previous = None
    for record in maps:
        if not isinstance(record, dict):
            raise EditorError('OLS parser returned an invalid map record.')
        _verify_record(source, record, 'source_record', lower, upper)
        start, end = record['source_record_offset'], record['source_record_end']
        if previous is not None and (start != previous + 1 or source[previous] != 0):
            raise EditorError('Incomplete OLS map decoding: unparsed bytes between map records.')
        if source[end - 3:end] != b'\x01\x01\x01':
            raise EditorError('Unsupported OLS map-record trailer.')
        previous = end
        headers = record.get('raw_header_fields')
        if not isinstance(headers, list) or len(headers) != 6 or headers[5] not in folder_index:
            raise EditorError('OLS map references an unknown folder.')
        record['folder_id'] = headers[5]
        record['folder_name'] = folder_index[headers[5]]['name']
        for key in ('name', 'description', 'unit'):
            if isinstance(record.get(key), str):
                record[key] = _native_text(record[key])
        count_x, count_y, width = (record.get(key) for key in ('nx', 'ny', 'data_size'))
        address, rom_end = record.get('raw_address'), record.get('rom_end')
        if (any(type(value) is not int for value in (count_x, count_y, width, address, rom_end))
                or count_x <= 0 or count_y <= 0 or width not in (1, 2, 4, 8)
                or rom_end - address != count_x * count_y * width
                or not 0 <= address < rom_end <= len(image)):
            raise EditorError('OLS map value extent does not match its dimensions or embedded BIN.')
        record['address'] = address
        record['address_hex'] = f'0x{address:08x}'
        for key in ('x_axis', 'y_axis', 'raw_rows_axis', 'raw_cols_axis'):
            axis = record.get(key)
            if not isinstance(axis, dict):
                raise EditorError('OLS parser returned an invalid axis record.')
            if axis.get('source_axis_end', 0) > axis.get('source_axis_offset', 0):
                _verify_record(source, axis, 'source_axis', start, end)
            for label in ('input_name', 'unit'):
                if isinstance(axis.get(label), str):
                    axis[label] = _native_text(axis[label])
            _axis_values(axis, image)
        record['validation'] = {'value_span_matches_dimensions': True,
                                'value_span_inside_bin': True, 'raw_source_record_verified': True}
    if previous != upper:
        raise EditorError('Incomplete OLS map decoding: trailing serialized records were omitted.')
    first = maps[0]['source_record_offset']
    directory['raw_map_prefix_offset'] = lower
    directory['raw_map_prefix_end'] = first
    directory['raw_map_prefix_hex'] = source[lower:first].hex()
    return maps


def load_ols(path: str | Path) -> OLSProject:
    """Recover settings and exact BIN slots from a supported format-113 project."""
    path = Path(path).resolve()
    try:
        if path.stat().st_size > _MAX_SOURCE_SIZE:
            raise EditorError('OLS project exceeds the supported 128 MiB size limit.')
        source = path.read_bytes()
    except OSError as exc:
        raise EditorError(f'Unable to read OLS project: {exc}') from exc
    if len(source) < 24 or not source.startswith(_HEADER):
        raise EditorError('Invalid or truncated WinOLS project header.')
    version = _u32(source, 16)
    if version != 113:
        raise EditorError(f'Unsupported OLS format {version}. Native import currently validates '
                          'legacy format 113 with exact 2 MiB BIN slots.')
    metadata, metadata_end = _metadata(source)
    directory = _directories(source, metadata_end)
    folders = _folders(source, directory)
    versions = []
    for index in range(directory['version_count']):
        start = directory['payload_offset'] + index * directory['version_stride']
        image = source[start:start + directory['version_stride']]
        versions.append(OLSVersion(index, 'Original' if index == 0 else f'Version {index}', image, _sha(image)))
    native = _run_parser(source)
    # Detect replacement/modification even while the helper is reading its own
    # snapshot. Nothing may be exported under a stale source identity.
    try:
        if path.read_bytes() != source:
            raise EditorError('OLS source changed during import; reopen the project and try again.')
    except OSError as exc:
        raise EditorError('OLS source became unavailable during import.') from exc
    header = native.get('header', {})
    if (not isinstance(header, dict) or header.get('format_version') != version
            or header.get('file_size') != len(source) or not header.get('valid')):
        raise EditorError('OLS parser header disagrees with the source snapshot.')
    maps = _maps(source, native, directory, folders, versions[0].data)
    metadata['directory_evidence'] = directory
    metadata['bin_slots'] = [{'index': item.index, 'name': item.name,
                              'ols_file_offset': directory['payload_offset'] + item.index * directory['version_stride'],
                              'byte_length': len(item.data), 'sha256': item.sha256} for item in versions]
    metadata['settings_prefix_length'] = directory['payload_offset']
    native_metadata = native.get('metadata', {})
    for key in ('original_file_name', 'winols_version_string', 'revision_tag', 'base_address_hex'):
        if isinstance(native_metadata, dict) and isinstance(native_metadata.get(key), str):
            metadata[key] = _native_text(native_metadata[key])
    warnings = [str(value) for value in native.get('warnings', [])]
    warnings.append('Native OLS import validates legacy format 113 with exact 2 MiB slots; '
                    'other format versions and storage layouts are unsupported.')
    warnings.append('Decoded map records retain raw fields and exact source bytes. '
                    'Unresolved axis flags and table storage interpretations require conversion review.')
    return OLSProject(path, _sha(source), version, f'{metadata["ecu_name"]} {metadata["sw_number"]}',
                      metadata, maps, folders, versions, warnings, source)
