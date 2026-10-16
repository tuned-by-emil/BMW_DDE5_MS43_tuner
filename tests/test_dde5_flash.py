"""Target-backed offline regression tests; no ECU or vendor-code execution."""
import hashlib
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bmw_bin_editor.dde5_flash import review_loaded_image, correct_loaded_image
from bmw_bin_editor.dde5_checks import ImageMemory, additive_checks
from bmw_bin_editor.dde5_sector_plan import sector_payloads
from bmw_bin_editor.dde5_auth import calculate_installed_response, verify_response, prg_authentication_argument
from bmw_bin_editor.core import CalibrationDocument

SOURCE = Path(__file__).resolve().parents[1]/'local_resources'/'dde5'


@unittest.skipUnless((SOURCE/'read kessv3.MPC').is_file() and (SOURCE/'read kessv3.FLS').is_file(),
                     'Preserved target fixtures are not installed on this computer')
class Dde5IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mpc = (SOURCE/'read kessv3.MPC').read_bytes()
        cls.fls = (SOURCE/'read kessv3.FLS').read_bytes()
        assert hashlib.sha256(cls.mpc).hexdigest() == '40ca5ca2abbf77675ca1db64a13662e9d8a41e40ee31ffd8077948f1267b354c'
        assert hashlib.sha256(cls.fls).hexdigest() == '8862fb53f9f102d2ca2717338a527546a4da03659b2d8f871856d485cf696e4f'

    def test_can_plan_includes_checksum_sector_and_preserves_every_other_byte(self):
        edited = bytearray(self.mpc)
        edited[0x208ee] = 6
        snapshot = bytes(edited)
        report = review_loaded_image(self.mpc, edited, 'MPC', self.fls)
        self.assertFalse(report['current']['selected_space_additive_valid'])
        self.assertTrue(report['corrected_preview']['selected_space_additive_valid'])
        self.assertFalse(report['corrected_preview']['program_signature']['valid'])
        self.assertFalse(report['flash_ready'])
        self.assertFalse(report['sector_plan']['native_partial_supported'])
        self.assertEqual(report['sector_plan']['planned_sector_payload_bytes'], 65536)
        self.assertEqual([s['file_offset'] for s in report['sector_plan']['sectors']], [0x20000, 0x68000])
        corrected, changes = correct_loaded_image(edited, 'MPC', self.fls)
        self.assertEqual(changes[0]['file_offset'], 0x6fffc)
        rebuilt = bytearray(self.mpc)
        for offset, data in sector_payloads(self.mpc, corrected, 'MPC'):
            rebuilt[offset:offset+len(data)] = data
        self.assertEqual(bytes(rebuilt), corrected)
        allowed = {0x208ee, 0x6fffc, 0x6fffd, 0x6fffe, 0x6ffff}
        self.assertTrue(all(a == b or i in allowed for i, (a, b) in enumerate(zip(self.mpc, corrected))))
        self.assertEqual(bytes(edited), snapshot)

    def test_companion_invalid_sum_is_not_silently_repaired(self):
        companion = bytearray(self.fls)
        companion[0x38e03] ^= 1
        edited = bytearray(self.mpc)
        edited[0x208ee] = 6
        corrected, _ = correct_loaded_image(edited, 'MPC', companion)
        rows = additive_checks(ImageMemory(corrected, companion))
        self.assertTrue(all(r['valid'] for r in rows if int(r['last_word'], 16) < 0x800000))
        self.assertFalse(all(r['valid'] for r in rows))
        self.assertEqual(companion[0x38e03], self.fls[0x38e03]^1)

    def test_fls_calibration_correction_and_actual_external_sector_size(self):
        edited = bytearray(self.fls)
        edited[0xee940] ^= 1
        report = review_loaded_image(self.fls, edited, 'FLS', self.mpc)
        self.assertTrue(report['corrected_preview']['selected_space_additive_valid'])
        self.assertEqual(report['sector_plan']['selected_sector_count'], 1)
        self.assertEqual(report['sector_plan']['planned_sector_payload_bytes'], 0x40000)
        self.assertEqual(report['sector_plan']['sectors'][0]['runtime_address'], 0x8c0000)
        self.assertFalse(report['corrected_preview']['calibration_signature']['valid'])

    def test_checksum_correction_is_idempotent_and_original_stays_unchanged(self):
        corrected, changes = correct_loaded_image(self.mpc, 'MPC', self.fls)
        self.assertEqual(corrected, self.mpc)
        self.assertEqual(changes, [])
        with self.assertRaises(ValueError):
            correct_loaded_image(self.mpc[:-4], 'MPC', self.fls)
        unsupported = bytearray(self.mpc)
        unsupported[0x2003c] ^= 1
        with self.assertRaises(ValueError):
            correct_loaded_image(unsupported, 'MPC', self.fls)

    def test_protected_mpc_edits_cannot_become_application_plan(self):
        edited = bytearray(self.mpc)
        edited[0x120] ^= 1
        with self.assertRaisesRegex(ValueError, 'application planning'):
            review_loaded_image(self.mpc, edited, 'MPC', self.fls)

    def test_correction_applies_as_one_undoable_edit_and_saves_exact_bytes(self):
        import tempfile
        doc = CalibrationDocument(None, SOURCE/'read kessv3.MPC')
        doc.apply_bytes('CAN ID edit', [(0x208ee, b'\x06')])
        before = bytes(doc.data)
        corrected, _ = correct_loaded_image(doc.data, 'MPC', self.fls)
        updates = [(i, bytes([b])) for i, (a, b) in enumerate(zip(doc.data, corrected)) if a != b]
        self.assertTrue(doc.apply_bytes('Correct 361830 arithmetic checksums', updates))
        self.assertEqual(len(doc.undo_stack), 2)
        self.assertEqual(doc.original, self.mpc)
        self.assertEqual(doc.saved, self.mpc)
        doc.undo()
        self.assertEqual(bytes(doc.data), before)
        doc.redo()
        self.assertEqual(bytes(doc.data), corrected)
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder)/'checksum_test.MPC'
            doc.save_as(out)
            self.assertEqual(out.read_bytes(), corrected)
            self.assertFalse(doc.dirty)
        self.assertEqual(doc.original, self.mpc)
        self.assertEqual((SOURCE/'read kessv3.MPC').read_bytes(), self.mpc)

    @unittest.skipUnless(Path(r'C:\EC-APPS\NFS\BIN\winkfpt.exe').is_file(), 'Installed pinned WinKFP provider unavailable')
    def test_ported_provider_response_matches_target_key_and_prg_wrapper(self):
        caller, nonce = b'LAB1', bytes.fromhex('0011223344556677')
        response = calculate_installed_response(caller, nonce, self.fls)
        self.assertTrue(verify_response(caller, nonce, response, self.fls)['valid'])
        self.assertFalse(verify_response(b'LAB2', nonce, response, self.fls)['valid'])
        self.assertEqual(len(response), 68)
        self.assertEqual(len(prg_authentication_argument(response)), 90)


if __name__ == '__main__':
    unittest.main()
