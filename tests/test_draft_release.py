"""Release commands retain the checked source, verified assets and changelog."""
from pathlib import Path
import hashlib
import tempfile
from types import SimpleNamespace
import unittest

from tools.create_draft_release import release_command, verify_existing_tag


class DraftReleaseTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        (self.root / 'bmw_bin_editor').mkdir()
        (self.root / 'bmw_bin_editor/__init__.py').write_text('__version__ = "1.34.0"\n')
        (self.root / 'RELEASE_NOTES.md').write_text('# Version 1.34.0\n\nGauge and tracing fixes.\n')
        (self.root / 'CHANGELOG.md').write_text('# Changelog\n\n## 1.34.0\n\nChanges.\n')
        (self.root / 'dist').mkdir()
        self.installer = self.root / 'dist/BMW_DDE5_MS43_Tuner_1.34.0_Setup.exe'
        self.installer.write_bytes(b'synthetic installer')
        self.checksum = self.installer.with_suffix('.exe.sha256')
        self.checksum.write_text(hashlib.sha256(self.installer.read_bytes()).hexdigest() +
                                 '  ' + self.installer.name + '\n')
        self.sha = 'a' * 40

    def command(self, ref='refs/heads/release/1.34.0', sha=None):
        return release_command(self.root, ref, self.sha if sha is None else sha)

    def test_manual_draft_targets_exact_commit_and_uses_written_notes(self):
        command = self.command()
        self.assertEqual(command[:4], ['gh', 'release', 'create', 'v1.34.0'])
        self.assertEqual(command[command.index('--target') + 1], self.sha)
        self.assertEqual(command[command.index('--notes-file') + 1], str(self.root / 'RELEASE_NOTES.md'))
        self.assertIn('--draft', command)
        self.assertIn(str(self.installer), command)
        self.assertIn(str(self.checksum), command)
        self.assertNotIn('--clobber', command)
        self.assertNotIn('--generate-notes', command)

    def test_matching_tag_works_but_other_branches_and_versions_fail(self):
        self.command('refs/tags/v1.34.0')
        for ref in ('refs/heads/main', 'refs/heads/release/1.33.6', 'refs/tags/v1.33.6'):
            with self.subTest(ref=ref), self.assertRaisesRegex(ValueError, 'matching'):
                self.command(ref)

    def test_mutable_or_missing_commit_ref_is_rejected(self):
        for sha in ('main', '', 'a' * 7):
            with self.subTest(sha=sha), self.assertRaisesRegex(ValueError, 'exact source commit'):
                self.command(sha=sha)

    def test_notes_and_matching_changelog_entry_are_required(self):
        notes = self.root / 'RELEASE_NOTES.md'
        original = notes.read_text()
        notes.write_text('')
        with self.assertRaisesRegex(ValueError, 'Release notes'):
            self.command()
        notes.write_text('# Version 1.33.6\n')
        with self.assertRaisesRegex(ValueError, 'identify this application version'):
            self.command()
        notes.write_text(original)
        (self.root / 'CHANGELOG.md').write_text('## 1.33.6\n')
        with self.assertRaisesRegex(ValueError, 'changelog entry'):
            self.command()

    def test_missing_or_modified_installer_cannot_be_released(self):
        self.installer.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'checksum does not match'):
            self.command()
        self.installer.unlink()
        with self.assertRaisesRegex(ValueError, 'installer and its checksum'):
            self.command()

    def test_only_matching_installer_assets_are_selected(self):
        other = self.root / 'dist/other_Setup.exe'
        other.write_bytes(b'other build')
        self.assertNotIn(str(other), self.command())


    def tag_response(self, status, body, returncode=0):
        return lambda *_a, **_k: SimpleNamespace(
            stdout=f'HTTP/2.0 {status} Result\ncontent-type: application/json\n\n{body}',
            returncode=returncode)

    def test_existing_tag_must_resolve_to_the_built_commit(self):
        verify_existing_tag('owner/repo', 'v1.34.0', self.sha,
                            run=self.tag_response(200, '{"sha": "' + self.sha + '"}'))
        with self.assertRaisesRegex(ValueError, 'different commit'):
            verify_existing_tag('owner/repo', 'v1.34.0', self.sha,
                                run=self.tag_response(200, '{"sha": "other"}'))

    def test_only_a_confirmed_missing_tag_allows_creation(self):
        verify_existing_tag('owner/repo', 'v1.34.0', self.sha,
                            run=self.tag_response(404, '{"message":"Not Found"}', 1))
        for status in (401, 403, 429, 500):
            with self.subTest(status=status), self.assertRaisesRegex(RuntimeError, 'no release was created'):
                verify_existing_tag('owner/repo', 'v1.34.0', self.sha,
                                    run=self.tag_response(status, '{}', 1))


if __name__ == '__main__':
    unittest.main()
