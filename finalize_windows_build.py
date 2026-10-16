"""Check the bundled QtCore ICU imports before releasing a Windows build."""
from pathlib import Path
import os
import sys

import pefile


def exports(path):
    with pefile.PE(str(path), fast_load=True) as executable:
        executable.parse_data_directories(
            directories=[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_EXPORT']]
        )
        table = getattr(executable, 'DIRECTORY_ENTRY_EXPORT', None)
        return {symbol.name for symbol in table.symbols if symbol.name} if table else set()


def fix_icu(folder):
    root = Path(folder).resolve()
    internal = root / '_internal'
    candidate = internal / 'icuuc.dll'
    qt = internal / 'PySide6' / 'Qt6Core.dll'
    if not root.is_dir() or not qt.is_file():
        raise RuntimeError(f'Expected a portable build containing QtCore: {qt}')
    if not qt.resolve().is_relative_to(root):
        raise RuntimeError('QtCore is outside the build directory')
    with pefile.PE(str(qt), fast_load=True) as executable:
        executable.parse_data_directories(
            directories=[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_IMPORT']]
        )
        required = {
            symbol.name
            for entry in getattr(executable, 'DIRECTORY_ENTRY_IMPORT', [])
            if entry.dll.lower() == b'icuuc.dll'
            for symbol in entry.imports
            if symbol.name
        }
    if not required:
        print('QtCore does not import the Windows ICU override.')
        return
    if candidate.exists():
        if not candidate.resolve().is_relative_to(root):
            raise RuntimeError('ICU is outside the build directory')
        if required <= exports(candidate):
            print('Bundled ICU satisfies QtCore imports.')
            return
    native = Path(os.environ['SystemRoot']) / 'System32' / 'icuuc.dll'
    if not native.is_file() or not required <= exports(native):
        raise RuntimeError('Bundled ICU is absent/incompatible and Windows ICU does not satisfy QtCore. Build not releasable.')
    if candidate.exists():
        candidate.unlink()
        print('Removed incompatible bundled ICU override; verified Windows ICU exports satisfy QtCore.')
    else:
        print('No bundled ICU override; verified Windows ICU exports satisfy QtCore.')


if __name__ == '__main__':
    fix_icu(sys.argv[1] if len(sys.argv) > 1 else 'dist/BMW_Bin_Editor_V1_32_0')
