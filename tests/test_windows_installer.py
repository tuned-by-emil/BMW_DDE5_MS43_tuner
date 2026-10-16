import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from installer.install import verify_payload
from tools.build_windows_installer import payload_files, ROOT


class PayloadTests(unittest.TestCase):
    def payload(self, name='run_editor.py', data=b'editor', digest=None, extra=False):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / 'payload.zip'
        manifest = {'files': [{'path': name, 'sha256': digest or hashlib.sha256(data).hexdigest()}]}
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr(zipfile.ZipInfo(name), data)
            archive.writestr('payload-manifest.json', json.dumps(manifest))
            if extra:
                archive.writestr('unexpected.py', b'extra')
        return path

    def test_valid_payload(self):
        self.assertEqual(verify_payload(self.payload())['files'][0]['path'], 'run_editor.py')

    def test_modified_payload_rejected(self):
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            verify_payload(self.payload(digest='0' * 64))

    def test_traversal_rejected(self):
        for name in ('../outside.py', '/outside.py', 'C:/outside.py', '..\\outside.py'):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'Unsafe'):
                verify_payload(self.payload(name=name))

    def test_unlisted_file_rejected(self):
        with self.assertRaisesRegex(ValueError, 'file list'):
            verify_payload(self.payload(extra=True))

    def test_public_payload_contains_generated_definitions_without_runtime_binaries(self):
        names = [p.relative_to(ROOT).as_posix() for p in payload_files()]
        self.assertEqual(sum(name.endswith('.xdf') for name in names), 9)
        self.assertIn('definitions/EDC16C31 361830.xdf', names)
        self.assertEqual(sum(name.endswith('.adx') for name in names), 1)
        self.assertFalse(any(name.lower().endswith(('.dll', '.pyd', '.whl', '.bin')) for name in names))
        self.assertFalse(any('mpps' in name.lower() for name in names))
