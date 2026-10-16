"""Audit the exact Git file set, including staged files, before publication."""
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = {'.bin', '.fls', '.mpc', '.hex', '.ols', '.a2l', '.xdf', '.adx',
             '.csv', '.key', '.pem', '.pfx', '.zip'}
PREFIXES = ('mpps/', 'mappacks/', 'local_resources/', 'definitions/library/',
            'definitions/ms43x/', 'work/', 'outputs/', 'build/', 'dist/')
SECRETS = re.compile(rb'gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|sk-[A-Za-z0-9_-]{30,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')


def check():
    raw = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT)
    paths = [Path(p.decode('utf-8')) for p in raw.split(b'\0') if p]
    issues = []
    total = 0
    for relative in paths:
        name = relative.as_posix()
        lower = name.lower()
        if (lower.startswith(PREFIXES) or relative.suffix.lower() in FORBIDDEN
                or 'disassembly_handoff' in lower
                or (relative.suffix.lower() == '.exe' and lower != 'assets/ols/ols_dump.exe')):
            issues.append('Excluded reference/artifact in Git: ' + name)
        file = ROOT / relative
        if not file.is_file():
            issues.append('Tracked file missing: ' + name)
            continue
        data = file.read_bytes()
        total += len(data)
        if len(data) > 10 * 1024 * 1024:
            issues.append('Unexpected large source file: ' + name)
        if SECRETS.search(data):
            issues.append('Potential credential signature: ' + name)
        if re.search(rb'[A-Za-z]:[/\\]Users[/\\][A-Za-z0-9_.-]+[/\\]', data):
            issues.append('Developer-specific absolute path: ' + name)
    print(f'Audited {len(paths)} Git files, {total / 1048576:.2f} MiB')
    for issue in issues:
        print(issue)
    if not issues: print('Public source file policy passed; manually review provenance before adding new third-party resources.')
    return not issues


if __name__ == '__main__':
    raise SystemExit(0 if check() else 1)
