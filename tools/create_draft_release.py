"""Create a changelog-backed draft for the exact checked and built commit."""
from pathlib import Path
import hashlib
import json
import os
import re
import runpy
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def release_command(root, ref, commit_sha):
    root = Path(root)
    version = runpy.run_path(str(root / 'bmw_bin_editor/__init__.py'))['__version__']
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('Release version must have three numeric components.')
    tag = f'v{version}'
    if ref not in (f'refs/tags/{tag}', f'refs/heads/release/{version}'):
        raise ValueError('Select the release branch or tag matching the application version.')
    if not re.fullmatch(r'[0-9a-fA-F]{40}', commit_sha):
        raise ValueError('An exact source commit SHA is required.')
    notes = root / 'RELEASE_NOTES.md'
    changelog = root / 'CHANGELOG.md'
    if not notes.is_file() or not notes.read_text(encoding='utf-8').strip():
        raise ValueError('Release notes are required.')
    title = notes.read_text(encoding='utf-8').splitlines()[0]
    if not re.search(rf'(?<![0-9.]){re.escape(version)}(?![0-9.])', title):
        raise ValueError('Release notes must identify this application version.')
    if not changelog.is_file() or f'## {version}' not in changelog.read_text(encoding='utf-8').splitlines():
        raise ValueError('A changelog entry for this version is required.')
    installer = root / 'dist' / f'BMW_DDE5_MS43_Tuner_{version}_Setup.exe'
    checksum = installer.with_suffix('.exe.sha256')
    if not installer.is_file() or not installer.stat().st_size or not checksum.is_file():
        raise ValueError('The built installer and its checksum are required.')
    expected = f'{hashlib.sha256(installer.read_bytes()).hexdigest()}  {installer.name}'
    if checksum.read_text(encoding='ascii').strip() != expected:
        raise ValueError('Installer checksum does not match the built file.')
    # No clobber/edit operation: GitHub refuses if this release already exists.
    return ['gh', 'release', 'create', tag, str(installer), str(checksum),
            '--target', commit_sha, '--draft', '--notes-file', str(notes),
            '--title', f'BMW DDE5 MS43 Tuner {tag}']


def verify_existing_tag(repository, tag, commit_sha, *, run=subprocess.run):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('Expected a GitHub owner/repository name.')
    response = run(['gh', 'api', '--include', f'repos/{repository}/commits/{tag}'],
                   capture_output=True, text=True, check=False)
    headers, separator, body = response.stdout.replace('\r\n', '\n').partition('\n\n')
    status = re.match(r'HTTP/\S+\s+(\d{3})', headers)
    code = int(status[1]) if status else None
    if code == 404 and response.returncode != 0:
        return  # An absent tag may be created for the exact built commit.
    if code != 200 or response.returncode != 0 or not separator:
        raise RuntimeError('Could not verify the release tag; no release was created.')
    if json.loads(body).get('sha') != commit_sha:
        raise ValueError('The release tag points to a different commit; refusing mismatched assets.')


def main():
    command = release_command(ROOT, os.environ['GITHUB_REF'], os.environ['RELEASE_SHA'])
    verify_existing_tag(os.environ['GH_REPO'], command[3], os.environ['RELEASE_SHA'])
    subprocess.run(command, check=True, cwd=ROOT)


if __name__ == '__main__':
    main()
