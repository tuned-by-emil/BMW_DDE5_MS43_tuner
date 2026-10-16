"""Target-backed config/FLS checksum tests; no ECU access or missing-byte padding."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from bmw_bin_editor.dde5_checks import (
    ExternalFlashMemory,CoverageError,additive_checks,inspect_external_flash,
    signature_check,validate_config_layout)
from bmw_bin_editor.dde5_flash import correct_loaded_image,review_loaded_image
from bmw_bin_editor.core import CalibrationDocument

SOURCE=Path(__file__).resolve().parents[1]/'local_resources'/'dde5'
FACTORY=Path(__file__).resolve().parents[1]/'local_resources'/'winkfp'
ANALYSIS=Path(__file__).resolve().parents[3]


@unittest.skipUnless((SOURCE/'read kessv3.FLS').is_file(),'Preserved E46 target fixture unavailable')
class ConfigChecksumTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fls=(SOURCE/'read kessv3.FLS').read_bytes()
        cls.config=cls.fls[0xe0000:]

    def test_complete_fls_checks_four_regions_without_mpc(self):
        result=inspect_external_flash(self.fls,trusted_fls=self.fls)
        self.assertEqual(len(result['additive_checks']),4)
        self.assertTrue(result['all_additive_valid'])
        self.assertEqual(result['all_additive_valid_scope'],'covered_scope_only')
        self.assertIsNone(result['complete_image_additive_valid'])
        self.assertIsNone(result['program_signature']['valid'])
        self.assertEqual(result['program_signature']['status'],'skipped')
        self.assertEqual(result['program_signature']['missing_ranges'][0]['start'],'0x20000')
        self.assertEqual(result['calibration_signature']['status'],'checked')
        self.assertFalse(result['calibration_signature']['valid'])
        self.assertFalse(result['flash_ready'])

    def test_standalone_review_does_not_construct_a_paired_or_dummy_mpc_image(self):
        with patch('bmw_bin_editor.dde5_checks.ImageMemory',side_effect=AssertionError('No MPC supplied')), \
             patch('bmw_bin_editor.dde5_flash.ImageMemory',side_effect=AssertionError('No MPC supplied')):
            report=review_loaded_image(self.fls,self.fls,'FLS')
            self.assertTrue(report['current']['selected_space_additive_valid'])
            config_report=review_loaded_image(self.config,self.config,'CONFIG')
            self.assertTrue(config_report['current']['selected_space_additive_valid'])

    def test_complete_fls_repairs_each_of_four_residues_only(self):
        edited=bytearray(self.fls)
        for offset in (0x9000,0x8030,0x22000,0xee940):edited[offset]^=1
        snapshot=bytes(edited)
        corrected,changes=correct_loaded_image(edited,'FLS')
        offsets={r['file_offset'] for r in changes}
        self.assertEqual(offsets,{0x1fff4,0x803c,0x5fff8,0xfeff8})
        allowed={i for offset in offsets for i in range(offset,offset+4)}
        self.assertTrue(all(a==b or i in allowed for i,(a,b) in enumerate(zip(snapshot,corrected))))
        self.assertEqual(bytes(edited),snapshot)
        self.assertTrue(inspect_external_flash(corrected,trusted_fls=self.fls)['all_additive_valid'])
        self.assertEqual(correct_loaded_image(corrected,'FLS'),(corrected,[]))

    def test_config_scope_leaves_other_fls_checksum_failures_unrepaired(self):
        edited=bytearray(self.fls)
        edited[0x38e03]^=1
        edited[0xee940]^=1
        corrected,changes=correct_loaded_image(edited,'CONFIG')
        self.assertEqual([r['file_offset'] for r in changes],[0xfeff8])
        self.assertEqual(corrected[:0xe0000],bytes(edited[:0xe0000]))
        report=review_loaded_image(self.fls,edited,'CONFIG')
        checks=report['corrected_preview']
        self.assertEqual(len(checks['additive_checks']),1)
        self.assertTrue(checks['selected_space_additive_valid'])
        self.assertTrue(checks['all_additive_valid'])
        self.assertIsNone(checks['complete_image_additive_valid'])
        self.assertIsNone(checks['program_signature']['valid'])
        self.assertFalse(inspect_external_flash(corrected,trusted_fls=self.fls)['all_additive_valid'])
        self.assertTrue(report['sector_plan']['payload_coverage_complete'])
        self.assertFalse(report['flash_ready'])

    def test_raw_124_and_128_kib_config_correction_and_honest_signature_status(self):
        for size in (0x1f000,0x20000):
            with self.subTest(size=size):
                original=self.config[:size]
                edited=bytearray(original);edited[0xe940]^=1
                corrected,changes=correct_loaded_image(edited,'CONFIG')
                self.assertEqual([r['file_offset'] for r in changes],[0x1eff8])
                self.assertEqual(changes[0]['runtime_address'],0x8feff8)
                self.assertEqual(corrected[:0x1eff8],bytes(edited[:0x1eff8]))
                self.assertEqual(corrected[0x1effc:],bytes(edited[0x1effc:]))
                report=review_loaded_image(original,edited,'CONFIG')
                prepared=report['corrected_preview']
                self.assertTrue(prepared['selected_space_additive_valid'])
                self.assertEqual(prepared['calibration_signature']['status'],'not_checked')
                self.assertIsNone(prepared['calibration_signature']['valid'])
                self.assertIsNotNone(prepared['calibration_signature']['actual_md5'])
                self.assertIsNone(prepared['trusted_key_image_sha256'])
                plan=report['sector_plan']
                self.assertFalse(plan['payload_coverage_complete'])
                self.assertEqual(plan['sectors'],[])
                self.assertEqual(plan['planned_sector_payload_bytes'],0)
                self.assertEqual(sum(r['bytes'] for r in plan['missing_sector_ranges']),0x40000-size)
                self.assertFalse(plan['flash_ready'])
                self.assertEqual(correct_loaded_image(corrected,'CONFIG'),(corrected,[]))

    def test_config_hash_scope_is_equal_between_full_and_raw_images(self):
        full=inspect_external_flash(self.fls,trusted_fls=self.fls,config_only=True)
        raw=inspect_external_flash(self.config,trusted_fls=self.config,config_only=True)
        self.assertEqual(full['additive_checks'],raw['additive_checks'])
        self.assertEqual(full['calibration_signature']['actual_md5'],raw['calibration_signature']['actual_md5'])

    def test_full_fls_review_reports_physical_sector_without_claiming_partial_flash(self):
        edited=bytearray(self.fls);edited[0xee940]^=1
        report=review_loaded_image(self.fls,edited,'FLS')
        self.assertFalse(report['current']['selected_space_additive_valid'])
        self.assertTrue(report['corrected_preview']['selected_space_additive_valid'])
        self.assertEqual(report['sector_plan']['selected_sector_count'],1)
        self.assertEqual(report['sector_plan']['sectors'][0]['runtime_address'],0x8c0000)
        self.assertEqual(report['sector_plan']['planned_sector_payload_bytes'],0x40000)
        self.assertFalse(report['sector_plan']['native_partial_supported'])
        self.assertFalse(report['flash_ready'])

    def test_reject_truncation_and_inappropriate_scope(self):
        for raw in (self.config[:-1],self.config[:0x1effc],self.config[:0x1f000-4],self.fls[:-4]):
            with self.subTest(size=len(raw)),self.assertRaises(ValueError):
                correct_loaded_image(raw,'CONFIG')
        with self.assertRaisesRegex(ValueError,'complete 1 MiB'):
            correct_loaded_image(self.config,'FLS')
        with self.assertRaisesRegex(ValueError,'companion FLS'):
            correct_loaded_image(bytes(0x70200),'MPC')

    def test_reject_wrong_descriptor_evaluator_header_and_byte_order_before_changes(self):
        for offset in (0,4,0x14,0x100,0x180,0x188,0x280,0x271,0x278,0x290):
            with self.subTest(offset=offset):
                unsupported=bytearray(self.config);unsupported[offset]^=1
                snapshot=bytes(unsupported)
                with self.assertRaises(ValueError):correct_loaded_image(unsupported,'CONFIG')
                self.assertEqual(bytes(unsupported),snapshot)
        byte_swapped=b''.join(self.config[i:i+4][::-1] for i in range(0,len(self.config),4))
        with self.assertRaisesRegex(ValueError,'byte order'):
            correct_loaded_image(byte_swapped,'CONFIG')

    def test_program_signature_directory_is_not_reinterpreted_to_hide_missing_mpc(self):
        changed=bytearray(self.fls)
        changed[0x8184:0x8188]=(0x820000).to_bytes(4,'big')
        with self.assertRaisesRegex(ValueError,'MD5 ranges'):
            correct_loaded_image(changed,'FLS')
        # The unrelated program directory is explicitly outside CONFIG scope.
        corrected,corrections=correct_loaded_image(changed,'CONFIG')
        self.assertEqual(corrected,bytes(changed))
        self.assertEqual(corrections,[])

    def test_raw_memory_refuses_uncovered_key_and_neighbouring_sector_bytes(self):
        memory=ExternalFlashMemory(self.config,config_only=True)
        for address,size in ((0x808588,68),(0x8c0000,0x40000),(0x900000,1)):
            with self.subTest(address=address),self.assertRaises(CoverageError):memory.read(address,size)

    def test_config_correction_is_one_undoable_edit_and_save_preserves_size(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'raw_config.bin';source.write_bytes(self.config[:0x1f000])
            doc=CalibrationDocument(None,source)
            doc.apply_bytes('Calibration edit',[(0xe940,bytes([doc.data[0xe940]^1]))])
            before=bytes(doc.data)
            corrected,_=correct_loaded_image(doc.data,'CONFIG')
            updates=[(i,bytes([b])) for i,(a,b) in enumerate(zip(doc.data,corrected)) if a!=b]
            self.assertTrue(doc.apply_bytes('Correct config arithmetic checksum',updates))
            self.assertEqual(len(doc.undo_stack),2)
            doc.undo();self.assertEqual(bytes(doc.data),before)
            doc.redo();self.assertEqual(bytes(doc.data),corrected)
            output=Path(folder)/'corrected.bin';doc.save_as(output)
            self.assertEqual(output.read_bytes(),corrected)
            self.assertEqual(len(output.read_bytes()),0x1f000)
            self.assertEqual(source.read_bytes(),self.config[:0x1f000])

    @unittest.skipUnless(FACTORY.is_dir() and (ANALYSIS/'extract_dde504_daten.py').is_file(),
                         'Factory DDE504 records or the offline parser are unavailable')
    def test_factory_e46_calibration_headers_ranges_and_checksums_from_sparse_coverage(self):
        sys.path.insert(0,str(ANALYSIS))
        from extract_dde504_daten import parse

        class SparseFactoryMemory:
            def __init__(self,data):self.data=data
            def read(self,address,size):
                try:return bytes(self.data[a] for a in range(address,address+size))
                except KeyError as exc:raise CoverageError(str(exc)) from exc

        count=0
        for path in sorted(FACTORY.glob('*.0DA')):
            data,_=parse(path)
            memory=SparseFactoryMemory(data)
            if data.get(0x8e0280)!=0xfa:continue
            with self.subTest(path=path.name):
                validate_config_layout(memory)
                self.assertTrue(additive_checks(memory,scope='CONFIG')[0]['valid'])
                sig=signature_check(memory,None,0x8e0000)
                self.assertEqual(sig['status'],'not_checked')
                self.assertIsNone(sig['valid'])
                self.assertIsNotNone(sig['actual_md5'])
                # The factory records are sparse. No invented FF bytes were supplied.
                with self.assertRaises(CoverageError):memory.read(0x8e0040,1)
                count+=1
        self.assertEqual(count,46)


if __name__=='__main__':unittest.main()
