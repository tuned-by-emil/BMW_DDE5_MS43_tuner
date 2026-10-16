"""Explicit public package data; never glob local definition packs."""
PUBLIC_DEFINITIONS = ('direct_serial_parameters.json', 'live_logging_presets.json',
                      'live_parameters.json', 'testo_signals.tsv')
PUBLIC_ASSETS = ('editor.ico', 'editor.png', 'editor.svg', 'ols/ols_dump.exe')


def public_data(root):
    entries = [(str(root / 'definitions' / name), 'definitions') for name in PUBLIC_DEFINITIONS]
    for name in PUBLIC_ASSETS:
        path = root / 'assets' / name
        entries.append((str(path), 'assets/' + str(path.relative_to(root / 'assets').parent).replace('\\', '/')))
    return entries
