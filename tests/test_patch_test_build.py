"""Stable release identity and optional test-channel settings isolation."""
from pathlib import Path
import tempfile
import unittest

from PySide6.QtCore import QSettings
from bmw_bin_editor import APP_NAME, BUILD_CHANNEL, __version__
from bmw_bin_editor.settings import editor_settings
from tools.build_windows_installer import assembly_version, payload_files, ROOT


class BuildIdentityTests(unittest.TestCase):
    def test_windows_version_has_exactly_four_components(self):
        self.assertEqual(assembly_version('1.34.0'), '1.34.0.0')
        self.assertEqual(assembly_version('1.34.0.1'), '1.34.0.1')
        for invalid in ('1.34', '1.34.0.1.0', '1.34.0-test', '1.65535.0'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                assembly_version(invalid)
        self.assertEqual(BUILD_CHANNEL, 'stable')
        self.assertEqual(__version__, '1.35.0')
        self.assertEqual(APP_NAME, 'BMW DDE5 MS43 Tuner v1.35.0')

    def test_test_preferences_do_not_read_or_modify_public_preferences(self):
        with tempfile.TemporaryDirectory() as temp:
            calls = []
            def factory(org, name):
                calls.append(name)
                return QSettings(str(Path(temp) / (name + '.ini')), QSettings.IniFormat)
            release = factory('EmilTools', 'BMWBinEditor')
            release.setValue('favorite_maps', 'public favorites')
            release.sync()
            public_path = Path(release.fileName())
            before = public_path.read_bytes()
            calls.clear()
            test = editor_settings(factory, channel='patch-test')
            self.assertEqual(calls, ['BMWBinEditorPatchTest'])
            self.assertFalse(test.contains('favorite_maps'))
            test.setValue('favorite_maps', 'test favorites')
            test.sync()
            self.assertEqual(public_path.read_bytes(), before)

    def test_default_settings_retain_the_normal_release_namespace(self):
        with tempfile.TemporaryDirectory() as temp:
            calls = []
            def factory(org, name):
                calls.append((org, name))
                return QSettings(str(Path(temp) / (name + '.ini')), QSettings.IniFormat)
            existing = factory('EmilTools', 'BMWBinEditor')
            existing.setValue('settings/migrated', True)
            existing.setValue('favorite_maps', 'retained release favorites')
            existing.sync()
            calls.clear()
            settings = editor_settings(factory)
            self.assertEqual(calls, [('EmilTools', 'BMWBinEditor')])
            self.assertEqual(settings.value('favorite_maps'), 'retained release favorites')

    def test_payload_includes_patch_modules_and_instructions(self):
        names = {p.relative_to(ROOT).as_posix() for p in payload_files()}
        for name in ('bmw_bin_editor/dde5_patch_test.py',
                     'bmw_bin_editor/dde5_patch_test_gui.py', 'docs/DDE5_PATCH_TEST.txt',
                     'bmw_bin_editor/dde5_ram_calibration.py',
                     'bmw_bin_editor/dde5_ram_calibration_gui.py',
                     'bmw_bin_editor/dde5_ram_self_check.py', 'docs/DDE5_RAM_CALIBRATION_TEST.txt',
                     'bmw_bin_editor/dde5_live_ram.py', 'bmw_bin_editor/dde5_live_ram_gui.py',
                     'bmw_bin_editor/dde5_live_ram_self_check.py', 'docs/DDE5_LIVE_RAM.txt'):
            self.assertIn(name, names)
