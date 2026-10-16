"""Read ADX logging metadata without executing formulas or connection commands."""
from pathlib import Path
import xml.etree.ElementTree as ET
from .logdata import SignalCatalog, SignalDefinition


def load_adx_catalog(path, *, include_states=False):
    root = ET.parse(path).getroot()
    if root.tag != 'ADXFORMAT':
        raise ValueError('Expected a TunerPro ADX logging definition.')
    definitions = []
    styles = {}
    for node in root.findall('ADXVALUE'):
        ident = node.get('id', '')
        if not ident:
            continue
        title = node.get('title', ident)
        units = node.findtext('units', '')
        # This ADX includes the display label in the units, e.g. Engine Speed (rpm).
        if '(' in units and units.endswith(')'):
            units = units.rsplit('(', 1)[1][:-1]
        definitions.append(SignalDefinition(ident, ident, ident, node.findtext('sizeinbits', ''),
                                           units, ident, title, node.findtext('desc', title)))
        limits = node.find('range')
        if limits is not None:
            try:
                low, high = float(limits.get('low')), float(limits.get('high'))
                if high > low:
                    styles[ident] = {'lower': low, 'upper': high}
            except (TypeError, ValueError):
                pass
    if include_states:
        for node in root.findall('ADXBITMASK'):
            ident = node.get('id', '')
            definitions.append(SignalDefinition(ident, ident, ident, 'bool', '', ident,
                node.get('title', ident), node.findtext('desc', node.get('title', ident))))
            styles[ident] = {'lower': 0., 'upper': 1.}
    return SignalCatalog(definitions), styles


def bundled_ms43_adx_path():
    import sys
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]))
    return root / 'definitions' / 'ms43x' / 'Siemens_MS43_MS43X001_Logging.adx'
