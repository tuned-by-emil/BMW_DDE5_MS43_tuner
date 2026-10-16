"""Explicit public package data; never glob local definition packs."""
PUBLIC_DEFINITIONS = ('direct_serial_parameters.json', 'live_logging_presets.json',
                      'live_parameters.json', 'testo_signals.tsv',
                      'ms43x/Siemens_MS43_MS43X001_Logging.adx', 'ms43x/NOTICE.md')
PUBLIC_ASSETS = ('editor.ico', 'editor.png', 'editor.svg', 'ols/ols_dump.exe')
PUBLIC_GENERATED = ('EDC16C31 361830.xdf', 'library/EDC16 369821.xdf',
    'library/EDC16 372066.xdf', 'library/EDC16C31 361888.xdf',
    'library/EDC16C35 3618XX.xdf', 'library/EDC16C35 370888.xdf',
    'library/EDC16C35 371268.xdf', 'library/EDC16CP35 372781.xdf',
    'library/EDC16CP35 387658.xdf', 'library/manifest.json')


def keep_qt_binary(entry):
    """Exclude unused auto-collected plugins, including GPL-only Virtual Keyboard."""
    name = entry[0].replace('\\', '/').rsplit('/', 1)[-1].lower()
    return name not in {'qt6virtualkeyboard.dll', 'qtvirtualkeyboardplugin.dll',
                        'qt6pdf.dll', 'qpdf.dll', 'opengl32sw.dll',
                        'qt6quick.dll', 'qt6qml.dll', 'qt6qmlmeta.dll',
                        'qt6qmlmodels.dll', 'qt6qmlworkerscript.dll'}


def public_data(root):
    entries = [(str(root / 'definitions' / name), (root / 'definitions' / name).parent.relative_to(root).as_posix()) for name in PUBLIC_DEFINITIONS]
    for name in PUBLIC_GENERATED:
        path = root / 'definitions' / name
        entries.append((str(path), path.parent.relative_to(root).as_posix()))
    for name in PUBLIC_ASSETS:
        path = root / 'assets' / name
        entries.append((str(path), 'assets/' + str(path.relative_to(root / 'assets').parent).replace('\\', '/')))
    return entries
