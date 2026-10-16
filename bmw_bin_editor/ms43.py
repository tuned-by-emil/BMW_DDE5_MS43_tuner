"""MS43X001 address normalization and byte-diff based export selection."""
from dataclasses import dataclass
from pathlib import Path
import copy
import os
import tempfile

from .core import EditorError, parse_int

FULL_SIZE = 0x80000
CAL_START = 0x70000
CAL_SIZE = 0x10000


def is_ms43x(database):
    return database.title.strip() == 'MS43X001'


def normalize_xdf(root):
    base = root.find('./XDFHEADER/BASEOFFSET')
    offset = parse_int(base.get('offset')) if base is not None else 0
    if base is not None and parse_int(base.get('subtract')):
        offset = -offset
    for embedded in root.iter('EMBEDDEDDATA'):
        if embedded.get('mmedaddress') is not None:
            address = parse_int(embedded.get('mmedaddress')) + offset
            if address < 0:
                raise EditorError('MS43X XDF has a negative file address.')
            embedded.set('mmedaddress', hex(address))
    if base is not None:
        base.set('offset', '0')
        base.set('subtract', '0')
    # Editor-facing engineering units; the inverse equation still writes the
    # original raw representation. Guard units to keep repeated parses idempotent.
    for node in root:
        title = node.findtext('title', '')
        target = node if node.tag == 'XDFCONSTANT' else node.find("./XDFAXIS[@id='z']")
        if target is None:
            continue
        units = target.find('units')
        math_node = target.find('MATH')
        if units is None or math_node is None:
            continue
        if title == 'c_eng_disp' and units.text != 'cc':
            math_node.set('equation', '(' + math_node.get('equation', 'X') + ') * 1000')
            units.text = 'cc'
            decimals = target.find('decimalpl')
            if decimals is not None:
                decimals.text = '1'
        elif title.startswith('ip_map_ve_') and units.text == 'VE':
            math_node.set('equation', '(' + math_node.get('equation', 'X') + ') * 100')
            units.text = '%'
            for tag in ('min', 'max'):
                bound = target.find(tag)
                if bound is not None and bound.text:
                    bound.text = str(float(bound.text) * 100)
    by_id = {parse_int(node.get('uniqueid')): node for node in root
             if node.get('uniqueid') is not None}
    for axis in root.findall('./XDFTABLE/XDFAXIS'):
        if axis.get('id') == 'z':
            continue
        link = axis.find('embedinfo')
        if link is None or link.get('type') != '3':
            continue
        target = by_id.get(parse_int(link.get('linkobjid')))
        z = target.find("./XDFAXIS[@id='z']") if target is not None else None
        storage = z.find('EMBEDDEDDATA') if z is not None else None
        count = parse_int(axis.findtext('indexcount'), 1)
        if storage is None or (parse_int(storage.get('mmedrowcount'), 1) *
                              parse_int(storage.get('mmedcolcount'), 1)) != count:
            # The supplied XDF contains an unresolved coolant-controller link.
            # Keep it explicit and use index positions instead of bogus 8-bit data.
            import xml.etree.ElementTree as ET
            axis.set('bmw_unresolved_link', '1')
            embedded = axis.find('EMBEDDEDDATA')
            if embedded is not None:
                embedded.attrib.pop('mmedaddress', None)
            units = axis.find('units')
            if units is None:
                units = ET.SubElement(axis, 'units')
            units.text = 'index (unresolved XDF axis)'
            continue
        for tag in ['EMBEDDEDDATA', 'MATH', 'units', 'decimalpl', 'LABEL', 'embedinfo']:
            for child in list(axis.findall(tag)):
                axis.remove(child)
        for tag in ['EMBEDDEDDATA', 'MATH', 'units', 'decimalpl']:
            node = z.find(tag)
            if node is not None:
                axis.append(copy.deepcopy(node))


def database_for_size(database, size):
    """Use the same MS43X001 definitions for a full image or its calibration slice."""
    if not is_ms43x(database):
        return database
    region = database.root.find('./XDFHEADER/REGION')
    defined_size = parse_int(region.get('size')) if region is not None else 0
    if size not in (FULL_SIZE, CAL_SIZE) or defined_size not in (FULL_SIZE, CAL_SIZE):
        raise EditorError('MS43X001 requires a 512 KB full BIN or 64 KB calibration BIN and matching XDF region.')
    if size == defined_size:
        return database
    tree = copy.deepcopy(database.tree)
    delta = CAL_START if size == FULL_SIZE else -CAL_START
    for embedded in tree.getroot().iter('EMBEDDEDDATA'):
        if embedded.get('mmedaddress') is not None:
            address = parse_int(embedded.get('mmedaddress')) + delta
            if not 0 <= address < size:
                raise EditorError('This MS43X definition contains addresses outside the calibration region.')
            embedded.set('mmedaddress', hex(address))
    tree.getroot().find('./XDFHEADER/REGION').set('size', hex(size))
    from .core import XDFDatabase
    return XDFDatabase.from_tree(tree, database.path)


@dataclass(frozen=True)
class ExportPlan:
    start: int
    size: int
    changed_bytes: int
    code_changed_bytes: int
    reason: str

    @property
    def partial(self):
        return self.size == CAL_SIZE


def export_plan(document):
    size = len(document.data)
    if len(document.original) != size:
        raise EditorError('The image length changed; automatic export is unavailable.')
    changed = [i for i, (a, b) in enumerate(zip(document.original, document.data)) if a != b]
    if not is_ms43x(document.xdf):
        return ExportPlan(0, size, len(changed), 0, 'Full BIN: no partial-export rule for this ECU.')
    if size == CAL_SIZE:
        return ExportPlan(0, CAL_SIZE, len(changed), 0, '64 KB calibration BIN loaded.')
    if size != FULL_SIZE:
        raise EditorError('MS43X001 export requires a 512 KB or 64 KB image.')
    outside = sum(i < CAL_START for i in changed)
    if outside:
        return ExportPlan(0, FULL_SIZE, len(changed), outside,
                          f'Full 512 KB required: {outside} changed bytes outside calibration.')
    if not changed:
        return ExportPlan(0, FULL_SIZE, 0, 0, 'No edits detected; full image retained.')
    return ExportPlan(CAL_START, CAL_SIZE, len(changed), 0,
                      'All changed bytes are inside 0x70000–0x7FFFF; 64 KB contains every edit.')


def export_binary(document, path, *, full=False):
    plan = export_plan(document)
    out = Path(path)
    if out.resolve() == document.path.resolve():
        raise EditorError('Choose a new export filename to preserve the working BIN.')
    if full:
        plan = ExportPlan(0, len(document.data), plan.changed_bytes, plan.code_changed_bytes,
                          'Full working image explicitly selected.')
    payload = bytes(document.data[plan.start:plan.start + plan.size])
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='wb', prefix=f'.{out.name}.', suffix='.tmp',
                                         dir=out.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, out)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    # Export must not replace the full editing document or reset its save baseline.
    return plan
