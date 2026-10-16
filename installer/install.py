"""Install the editor after native setup acquires a verified private Python."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import subprocess
import sys
import uuid
import zipfile


def verify_payload(payload):
    with zipfile.ZipFile(payload) as archive:
        manifest = json.loads(archive.read('payload-manifest.json'))
        names = archive.namelist()
        expected = [record['path'] for record in manifest['files']]
        for name in names + expected:
            if name.startswith('/') or '..' in Path(name).parts or ':' in name or '\\' in name:
                raise ValueError('Unsafe payload path: ' + name)
        if (len(names) != len(set(names)) or len(expected) != len(set(expected))
                or set(names) != set(expected) | {'payload-manifest.json'}):
            raise ValueError('Payload file list does not match manifest')
        for entry in archive.infolist():
            path = Path(entry.filename)
            if path.is_absolute() or '..' in path.parts or ':' in entry.filename or '\\' in entry.filename:
                raise ValueError('Unsafe payload path: ' + entry.filename)
        for record in manifest['files']:
            if hashlib.sha256(archive.read(record['path'])).hexdigest() != record['sha256']:
                raise ValueError('Payload hash mismatch: ' + record['path'])
    return manifest


def install(payload, root):
    manifest = verify_payload(payload)
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    environment = root / 'environment'
    python = environment / 'Scripts/python.exe'
    if not python.is_file():
        print('Creating private editor environment...', flush=True)
        subprocess.run([sys.executable, '-m', 'venv', str(environment)], check=True)
    app = root / ('app-' + manifest['version'] + '-' + uuid.uuid4().hex[:8])
    with zipfile.ZipFile(payload) as archive: archive.extractall(app)
    print('Downloading verified library wheels from PyPI...', flush=True)
    subprocess.run([str(python), '-m', 'pip', 'install', '--disable-pip-version-check',
        '--index-url', 'https://pypi.org/simple', '--only-binary=:all:', '--require-hashes',
        '-r', str(app / 'requirements-runtime-windows.txt')], check=True)
    subprocess.run([str(python), '-m', 'pip', 'check'], check=True)
    print('Checking synthetic editor startup and simulated serial traffic...', flush=True)
    env = os.environ.copy(); env['QT_QPA_PLATFORM'] = 'offscreen'
    subprocess.run([str(python), str(app / 'run_editor.py'), '--self-check-public',
                    str(app / 'setup-self-check.json')], env=env, check=True)
    pointer = root / 'current-app.new'
    pointer.write_text(str(app), encoding='utf-8')
    os.replace(pointer, root / 'current-app.txt')
    print('Editor files and private environment are ready.', flush=True)
    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--payload', required=True, type=Path)
    parser.add_argument('--install-root', type=Path)
    parser.add_argument('--validate-only', action='store_true')
    args = parser.parse_args()
    if args.validate_only:
        manifest = verify_payload(args.payload)
        print(f"Verified {len(manifest['files'])} files; nothing installed or downloaded.")
    else:
        if args.install_root is None: parser.error('--install-root is required')
        install(args.payload, args.install_root)
