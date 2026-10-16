# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
from PyInstaller.utils.hooks import collect_all
import sys

project_root = Path(SPECPATH)
sys.path.insert(0, str(project_root))
from public_build_resources import public_data
matplotlib_data, matplotlib_binaries, matplotlib_imports = collect_all(
    "matplotlib",
    include_py_files=False,
    filter_submodules=lambda name: not name.startswith("matplotlib.tests"),
    exclude_datas=["**/tests/**"],
)

a = Analysis(
    [str(project_root / "run_editor.py")],
    pathex=[str(project_root)],
    binaries=matplotlib_binaries,
    datas=public_data(project_root) + matplotlib_data,
    hiddenimports=matplotlib_imports + ["mpl_toolkits.mplot3d"],
    hookspath=[],
    hooksconfig={"matplotlib": {"backends": ["QtAgg"]}},
    runtime_hooks=[],
    excludes=["PyQt5", "PyQt6", "PySide2", "tkinter", "matplotlib.tests"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="BMW_Bin_Editor_V1_33_1",
    icon=str(project_root / "assets" / "editor.ico"),
    version=str(project_root / "windows_version_info_V1_33_1.txt"),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="BMW_Bin_Editor_V1_33_1",
)
