"""Release commands retain the checked source, verified assets and changelog."""
from pathlib import Path
from contextlib import redirect_stdout
import hashlib
import io
import json
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools import create_draft_release
from tools.create_draft_release import release_command, release_exists, verify_existing_tag


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

    def test_exact_existing_release_is_read_only_and_does_not_inspect_assets(self):
        calls = []
        def run(command, **options):
            calls.append((command, options))
            return self.tag_response(200, '{"id":123,"tag_name":"v1.34.0"}')()
        self.assertTrue(release_exists('owner/repo', 'v1.34.0', run=run))
        self.assertEqual(calls, [(['gh', 'api', '--include',
                                  'repos/owner/repo/releases/tags/v1.34.0'],
                                 dict(capture_output=True, text=True, check=False))])

    def test_only_confirmed_404_means_release_is_absent(self):
        self.assertFalse(release_exists('owner/repo', 'v1.34.0',
            run=self.tag_response(404, '{"message":"Not Found"}', 1)))
        cases = ((404, '{"message":"Not Found"}', 0),
                 (404, '{"message":"Not Found"}', 2),
                 (404, '{}', 1), (404, '<html>Not Found</html>', 1),
                 (200, '{"id":123,"tag_name":"v1.34.0"}', 1),
                 (200, '{"id":123,"tag_name":"v1.33.6"}', 0),
                 (200, '{"tag_name":"v1.34.0"}', 0),
                 (200, '{"id":true,"tag_name":"v1.34.0"}', 0),
                 (200, '[]', 0), (200, 'not json', 0),
                 (201, '{}', 0), (401, '{}', 1), (403, '{}', 1),
                 (429, '{}', 1), (500, '{}', 1))
        for status, body, returncode in cases:
            with self.subTest(status=status, body=body, returncode=returncode), self.assertRaisesRegex(RuntimeError, 'no release was created'):
                release_exists('owner/repo', 'v1.34.0', run=self.tag_response(status, body, returncode))
        for raw in ('', 'network failure', 'HTTP/2.0 200\n{"id":123,"tag_name":"v1.34.0"}',
                    'warning\nHTTP/2.0 404\n\n{"message":"Not Found"}',
                    'HTTP/2.0 404garbage\n\n{"message":"Not Found"}'):
            with self.subTest(raw=raw), self.assertRaisesRegex(RuntimeError, 'no release was created'):
                release_exists('owner/repo', 'v1.34.0',
                    run=lambda *_args, raw=raw, **_kwargs: SimpleNamespace(stdout=raw, returncode=1))

    def test_invalid_lookup_arguments_do_not_run_gh(self):
        def forbidden(*_args, **_kwargs):
            self.fail('Invalid lookup arguments reached GitHub CLI.')
        for repository, tag in (('owner/repo/extra', 'v1.34.0'),
                                ('owner/repo', 'v1.34.0/other'),
                                ('owner/repo', 'main')):
            with self.subTest(repository=repository, tag=tag), self.assertRaises(ValueError):
                release_exists(repository, tag, run=forbidden)

    def invoke_main(self, responses):
        calls = []
        def run(command, **options):
            calls.append((command, options))
            if command[:3] == ['gh', 'release', 'create']:
                return SimpleNamespace(returncode=0)
            status, body, code = responses.pop(0)
            return self.tag_response(status, json.dumps(body), code)()
        output = io.StringIO()
        with patch.object(create_draft_release, 'ROOT', self.root), patch.dict('os.environ', {
                'GITHUB_REF': 'refs/tags/v1.34.0', 'RELEASE_SHA': self.sha, 'GH_REPO': 'owner/repo'}), redirect_stdout(output):
            create_draft_release.main(run=run)
        self.assertFalse(responses)
        return calls, output.getvalue()

    def test_main_leaves_existing_draft_or_published_release_unchanged(self):
        for draft in (True, False):
            with self.subTest(draft=draft):
                calls, output = self.invoke_main([(200, {'sha': self.sha}, 0),
                    (200, {'id': 123, 'tag_name': 'v1.34.0', 'draft': draft}, 0)])
                self.assertEqual(len(calls), 2)
                self.assertTrue(all(command[:3] == ['gh', 'api', '--include'] for command, _ in calls))
                self.assertIn('left unchanged', output)
                self.assertIn('assets were not validated', output)

    def test_main_creates_only_after_tag_verification_and_confirmed_missing_release(self):
        calls, output = self.invoke_main([(200, {'sha': self.sha}, 0),
                                         (404, {'message': 'Not Found'}, 1)])
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0][0][-1], 'repos/owner/repo/commits/v1.34.0')
        self.assertEqual(calls[1][0][-1], 'repos/owner/repo/releases/tags/v1.34.0')
        self.assertEqual(calls[2], (self.command('refs/tags/v1.34.0'), dict(check=True, cwd=self.root)))
        self.assertEqual(output, '')

    def test_wrong_commit_is_rejected_before_release_lookup(self):
        calls = []
        def run(command, **options):
            calls.append(command)
            return self.tag_response(200, '{"sha":"' + 'b'*40 + '"}')()
        with patch.object(create_draft_release, 'ROOT', self.root), patch.dict('os.environ', {
                'GITHUB_REF': 'refs/tags/v1.34.0', 'RELEASE_SHA': self.sha, 'GH_REPO': 'owner/repo'}):
            with self.assertRaisesRegex(ValueError, 'different commit'):
                create_draft_release.main(run=run)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][-1], 'repos/owner/repo/commits/v1.34.0')

    def test_lookup_network_or_status_error_never_runs_release_creation(self):
        for response in (SimpleNamespace(stdout='', returncode=1),
                         SimpleNamespace(stdout='HTTP/2.0 500 Error\n\n{}', returncode=1)):
            calls = []
            def run(command, **options):
                calls.append(command)
                if len(calls) == 1:
                    return self.tag_response(200, '{"sha":"' + self.sha + '"}')()
                return response
            with self.subTest(response=response), patch.object(create_draft_release, 'ROOT', self.root), patch.dict('os.environ', {
                    'GITHUB_REF': 'refs/tags/v1.34.0', 'RELEASE_SHA': self.sha, 'GH_REPO': 'owner/repo'}):
                with self.assertRaisesRegex(RuntimeError, 'no release was created'):
                    create_draft_release.main(run=run)
            self.assertEqual(len(calls), 2)

    def test_existing_release_does_not_bypass_local_asset_validation(self):
        self.checksum.write_text('invalid checksum\n')
        def forbidden(*_args, **_kwargs):
            self.fail('Local release validation must finish before any network lookup.')
        with patch.object(create_draft_release, 'ROOT', self.root), patch.dict('os.environ', {
                'GITHUB_REF': 'refs/tags/v1.34.0', 'RELEASE_SHA': self.sha, 'GH_REPO': 'owner/repo'}):
            with self.assertRaisesRegex(ValueError, 'checksum does not match'):
                create_draft_release.main(run=forbidden)


if __name__ == '__main__':
    unittest.main()
