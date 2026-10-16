"""Build a small native Windows setup EXE; Python/library binaries are not bundled."""
from pathlib import Path
import hashlib
import json
import os
import runpy
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from public_build_resources import PUBLIC_ASSETS, PUBLIC_DEFINITIONS, PUBLIC_GENERATED


def payload_files():
    files = list((ROOT / 'bmw_bin_editor').glob('*.py'))
    files += [ROOT / 'assets' / name for name in PUBLIC_ASSETS]
    files += [ROOT / 'definitions' / name for name in PUBLIC_DEFINITIONS]
    files += [ROOT / 'definitions' / name for name in PUBLIC_GENERATED]
    files += list((ROOT / 'docs').glob('*'))
    files += list((ROOT / 'third_party/ols_parser').glob('*'))
    files += list((ROOT / 'third_party/license_texts').glob('*.txt'))
    files += [ROOT / name for name in ('run_editor.py', 'README.md', 'LICENSE.txt',
              'THIRD_PARTY_NOTICES.md', 'requirements-runtime-windows.txt',
              'installer/runtime.json', 'installer/install.py')]
    return sorted(p for p in set(files) if p.is_file())


def build():
    if os.name != 'nt': raise RuntimeError('Build the setup EXE on Windows.')
    metadata = runpy.run_path(str(ROOT / 'bmw_bin_editor/__init__.py'))
    version = metadata['__version__']
    folder = ROOT / 'build/windows-installer'
    folder.mkdir(parents=True, exist_ok=True)
    dist = ROOT / 'dist'
    dist.mkdir(exist_ok=True)
    files = payload_files()
    records = [{'path': p.relative_to(ROOT).as_posix(), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for p in files]
    manifest = {'version': version, 'product': 'BMW DDE5 MS43 Tuner', 'files': records}
    payload = folder / 'payload.zip'
    with zipfile.ZipFile(payload, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for p in files: archive.write(p, p.relative_to(ROOT))
        archive.writestr('payload-manifest.json', json.dumps(manifest, indent=2))
    with zipfile.ZipFile(payload) as archive:
        assert archive.testzip() is None
        allowed_xdf = {'definitions/' + name for name in PUBLIC_GENERATED + PUBLIC_DEFINITIONS}
        forbidden = [name for name in archive.namelist() if
            (name.lower().endswith(('.dll', '.pyd', '.bin', '.ols', '.a2l', '.whl'))
             or (name.lower().endswith(('.xdf', '.adx')) and name not in allowed_xdf) or 'mpps' in name.lower())]
        if forbidden: raise RuntimeError('Unexpected library/reference binaries: ' + repr(forbidden))
    assembly = folder / 'AssemblyInfo.cs'
    assembly.write_text('using System.Reflection;\n'
        '[assembly: AssemblyTitle("BMW DDE5 MS43 Tuner Setup")]\n'
        '[assembly: AssemblyProduct("BMW DDE5 MS43 Tuner")]\n'
        '[assembly: AssemblyCompany("Emil Larsson")]\n'
        f'[assembly: AssemblyVersion("{version}.0")]\n'
        f'[assembly: AssemblyFileVersion("{version}.0")]\n', encoding='utf-8')
    compiler = Path(os.environ['WINDIR']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    if not compiler.is_file(): raise RuntimeError('The Windows .NET Framework C# compiler was not found.')
    output = dist / f'BMW_DDE5_MS43_Tuner_{version}_Setup.exe'
    compiler_temp = folder / 'compiler-temp'
    compiler_temp.mkdir(exist_ok=True)
    compiler_env = os.environ.copy()
    compiler_env['TEMP'] = compiler_env['TMP'] = str(compiler_temp)
    subprocess.run([str(compiler), '/nologo', '/target:winexe', '/platform:x64', '/optimize+',
        '/reference:System.Windows.Forms.dll', '/reference:System.Drawing.dll',
        '/reference:System.IO.Compression.dll', '/reference:System.IO.Compression.FileSystem.dll',
        '/reference:System.Web.Extensions.dll', '/reference:Microsoft.CSharp.dll',
        '/win32icon:' + str(ROOT / 'assets/editor.ico'), '/out:' + str(output),
        '/resource:' + str(payload) + ',payload.zip',
        str(ROOT / 'installer/Setup.cs'), str(assembly)], check=True, env=compiler_env)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix('.exe.sha256').write_text(digest + '  ' + output.name + '\n', encoding='ascii')
    report = dict(version=version, executable=output.name, size=output.stat().st_size,
                  sha256=digest, payload_files=len(files), bundled_python=False,
                  bundled_library_dlls=False, runtime=json.loads((ROOT / 'installer/runtime.json').read_text()))
    (folder / 'build-info.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(output)
    print(f'{output.stat().st_size:,} bytes; no Python or third-party library wheels/DLLs embedded.')
    return output


if __name__ == '__main__': build()
