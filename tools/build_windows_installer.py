"""Build a small native Windows setup EXE; Python/library binaries are not bundled."""
from pathlib import Path
import hashlib
import json
import os
import re
import runpy
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from public_build_resources import PUBLIC_ASSETS, PUBLIC_DEFINITIONS, PUBLIC_GENERATED


def assembly_version(version):
    """Windows accepts four numeric parts; local builds may supply the fourth."""
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:\.\d+)?', version):
        raise ValueError('Windows installer version must have three or four numeric parts.')
    parts = [int(part) for part in version.split('.')]
    if any(part > 65534 for part in parts):
        raise ValueError('Windows installer version component is too large.')
    return '.'.join(str(part) for part in parts + [0] * (4-len(parts)))


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
    windows_version = assembly_version(version)
    patch_test = metadata.get('BUILD_CHANNEL') == 'patch-test'
    product = 'BMW DDE5 MS43 Tuner' + (' Patch Test' if patch_test else '')
    folder = ROOT / 'build/windows-installer'
    folder.mkdir(parents=True, exist_ok=True)
    dist = ROOT / 'dist'
    dist.mkdir(exist_ok=True)
    files = payload_files()
    records = [{'path': p.relative_to(ROOT).as_posix(), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for p in files]
    manifest = {'version': version, 'product': product,
                'channel': metadata.get('BUILD_CHANNEL', 'stable'), 'files': records}
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
        f'[assembly: AssemblyTitle("{product} Setup")]\n'
        f'[assembly: AssemblyProduct("{product}")]\n'
        '[assembly: AssemblyCompany("Emil Larsson")]\n'
        f'[assembly: AssemblyVersion("{windows_version}")]\n'
        f'[assembly: AssemblyFileVersion("{windows_version}")]\n', encoding='utf-8')
    compiler = Path(os.environ['WINDIR']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    if not compiler.is_file(): raise RuntimeError('The Windows .NET Framework C# compiler was not found.')
    suffix = '_PatchTest' if patch_test else ''
    output = dist / f'BMW_DDE5_MS43_Tuner_{version}{suffix}_Setup.exe'
    compiler_temp = folder / 'compiler-temp'
    compiler_temp.mkdir(exist_ok=True)
    compiler_env = os.environ.copy()
    compiler_env['TEMP'] = compiler_env['TMP'] = str(compiler_temp)
    subprocess.run([str(compiler), '/nologo', '/target:winexe', '/platform:x64', '/optimize+',
        *(['/define:PATCH_TEST'] if patch_test else []),
        '/reference:System.Windows.Forms.dll', '/reference:System.Drawing.dll',
        '/reference:System.IO.Compression.dll', '/reference:System.IO.Compression.FileSystem.dll',
        '/reference:System.Web.Extensions.dll', '/reference:Microsoft.CSharp.dll',
        '/win32icon:' + str(ROOT / 'assets/editor.ico'), '/out:' + str(output),
        '/resource:' + str(payload) + ',payload.zip',
        str(ROOT / 'installer/Setup.cs'), str(assembly)], check=True, env=compiler_env)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix('.exe.sha256').write_text(digest + '  ' + output.name + '\n', encoding='ascii')
    report = dict(version=version, channel=metadata.get('BUILD_CHANNEL', 'stable'),
                  executable=output.name, size=output.stat().st_size,
                  sha256=digest, payload_files=len(files), bundled_python=False,
                  bundled_library_dlls=False, runtime=json.loads((ROOT / 'installer/runtime.json').read_text()))
    (folder / 'build-info.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(output)
    print(f'{output.stat().st_size:,} bytes; no Python or third-party library wheels/DLLs embedded.')
    return output


if __name__ == '__main__': build()
