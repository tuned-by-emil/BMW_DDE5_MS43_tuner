"""Offline session/state-machine tests: fake transport, preserved target fixtures."""
from pathlib import Path
import json
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bmw_bin_editor import dde5_programming as session
from bmw_bin_editor.dde5_auth import calculate_response
from bmw_bin_editor.dde5_key_provider import recover_authentication_key
from bmw_bin_editor.ecu_programming import ProgrammingError

SOURCE = Path(__file__).resolve().parents[1]/'local_resources'/'dde5'


def frame(payload, *, reply=False, checksum=True, extended=False):
    addresses = b'\xf1\x12' if reply else b'\x12\xf1'
    if extended or len(payload) > 63:
        value = b'\x80' + addresses + bytes((len(payload),)) + payload
    else:
        value = bytes((0x80 | len(payload),)) + addresses + payload
    return value + bytes((sum(value) & 0xff,)) if checksum else value


def result(request, response, **binary):
    row = {'JOB_STATUS': 'OKAY', '_TEL_AUFTRAG': frame(request).hex().upper(),
           '_TEL_ANTWORT': frame(response, reply=True).hex().upper()}
    row.update({name: value.hex().upper() for name, value in binary.items()})
    return {'result_sets': [row]}


class FakeApi:
    def __init__(self, *, mutate=None, failure=None, prg_hash=None, end_failure=False):
        self.calls = []
        self.mutate = mutate
        self.failure = failure
        self.prg_hash = prg_hash or session.TRUSTED_PRG_SHA256
        self.closed = False
        self.last_authentication_dispatch = None
        self.end_failure = end_failure

    def __enter__(self):
        self.calls.append(('open',))
        return self

    def __exit__(self, *_):
        self.closed = True
        self.calls.append(('close',))
        if self.end_failure:
            raise ProgrammingError('Handle close error; private-buffer-is-not-exportable')

    def authentication_prg(self):
        self.calls.append(('pin',))
        return {'path': 'fixture/28dde504.prg', 'sha256': self.prg_hash,
                'configuration_source': 'fixture/EDIABAS.INI'}

    def read_job(self, ecu, job, **kwargs):
        self.calls.append((job,))
        return {'result_sets': [{'JOB_STATUS': 'OKAY', 'ID_HW_NR': '7792024'}]}

    def authentication_job(self, ecu, job, *, parameters='', binary=None, **kwargs):
        self.last_authentication_dispatch = None
        self.calls.append((job, parameters, binary))
        if self.failure and self.failure[0] == job:
            if self.failure[2]:
                self.last_authentication_dispatch = (ecu, job)
            raise self.failure[1]
        self.last_authentication_dispatch = (ecu, job)
        if job == 'TEL_ROH':
            identifier = int(binary[-2:], 16)
            digits = session.TARGET_HARDWARE if identifier == 0x92 else session.TARGET_SOFTWARE
            response = bytes((0x5a, identifier)) + digits.encode('ascii')
            value = result(bytes((0x1a, identifier)), response, RESPONSE=response)
        elif job == 'AUTHENTISIERUNG_ZUFALLSZAHL_LESEN':
            caller = int(parameters.split(';')[1]).to_bytes(4, 'big')
            nonce = b'\x12\x03\x00\xf1\xff\x05\x06\x07'
            value = result(b'\x31\x07\x03' + caller, b'\x71\x07' + nonce, ZUFALLSZAHL=nonce)
        elif job == 'AUTHENTISIERUNG_START':
            value = result(b'\x31\x08' + binary[21:89], b'\x71\x08\x01')
        else:
            raise AssertionError('A job outside identification/authentication was attempted: '+job)
        if self.mutate:
            self.mutate(job, value)
        return value


@unittest.skipUnless((SOURCE/'read kessv3.FLS').is_file(), 'Preserved target FLS fixture unavailable')
class AuthenticationSessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fls = (SOURCE/'read kessv3.FLS').read_bytes()
        cls.mpc = (SOURCE/'read kessv3.MPC').read_bytes()
        _, cls.private = recover_authentication_key(cls.fls)

    def calculator(self, caller, nonce, fls):
        return calculate_response(caller, nonce, fls, self.private)

    def run_auth(self, api=None, **kwargs):
        api = api or FakeApi()
        self.api = api
        return session.authenticate_ecu('fixture/BIN', self.fls,
                                        api_factory=lambda _: api,
                                        response_calculator=self.calculator, **kwargs)

    def assert_no_write(self, report):
        for flag in ('erase_sent', 'write_sent', 'reset_sent', 'programming_mode_requested',
                     'programming_enabled', 'flash_ready'):
            self.assertFalse(report[flag], flag)
        allowed = {'open', 'pin', 'IDENT', 'TEL_ROH', 'AUTHENTISIERUNG_ZUFALLSZAHL_LESEN',
                   'AUTHENTISIERUNG_START', 'close'}
        self.assertTrue(all(call[0] in allowed for call in self.api.calls))

    def test_success_same_handle_signed_caller_id_no_secret_export(self):
        messages = []
        report = self.run_auth(progress=messages.append)
        self.assertTrue(report['authenticated'])
        self.assertEqual(report['identity'], {'hardware': '0281010565', 'software': '1037361830', 'matched': True})
        self.assertEqual([call[0] for call in self.api.calls],
                         ['open', 'pin', 'IDENT', 'TEL_ROH', 'TEL_ROH',
                          'AUTHENTISIERUNG_ZUFALLSZAHL_LESEN', 'AUTHENTISIERUNG_START', 'close'])
        challenge = self.api.calls[-3]
        self.assertLessEqual(int(challenge[1][2:]), 0x7fffffff)
        self.assertGreater(int(challenge[1][2:]), 0)
        wrapper = self.api.calls[-2][2]
        self.assertEqual(len(wrapper), 90)
        encoded = json.dumps(report)
        self.assertNotIn(wrapper.hex(), encoded.lower())
        self.assertNotIn(wrapper[21:89].hex(), encoded.lower())
        self.assertNotIn('120300f1ff050607', encoded.lower())
        self.assertNotIn('ZUFALLSZAHL', encoded)
        self.assertNotIn('_TEL_', encoded)
        self.assertTrue(report['volatile_authentication_may_remain'])
        self.assertTrue(messages)
        self.assertTrue(self.api.closed)
        self.assert_no_write(report)

    def test_wrong_trusted_baseline_before_open(self):
        changed = bytearray(self.fls)
        changed[-1] ^= 1
        api = FakeApi()
        report = session.authenticate_ecu('fixture/BIN', changed, api_factory=lambda _: api,
                                          response_calculator=self.calculator)
        self.assertEqual(report['error_code'], 'trusted_image')
        self.assertFalse(report['authenticated'])
        self.assertEqual(api.calls, [])

    def test_provider_failure_before_open_is_actionable_and_sanitised(self):
        api = FakeApi()
        def missing(*_):
            raise FileNotFoundError('sensitive-key-material-path')
        report = session.authenticate_ecu('fixture/BIN', self.fls, api_factory=lambda _: api,
                                          response_calculator=missing)
        self.assertEqual(report['error_code'], 'provider_missing')
        self.assertNotIn('sensitive', json.dumps(report))
        self.assertEqual(api.calls, [])

    def test_prg_mismatch_closes_before_first_job(self):
        report = self.run_auth(FakeApi(prg_hash='0'*64))
        self.assertEqual(report['error_code'], 'prg_source')
        self.assertEqual([call[0] for call in self.api.calls], ['open', 'pin', 'close'])
        self.assert_no_write(report)

    def test_hardware_mismatch_stops_before_nonce(self):
        def wrong(job, value):
            if job == 'TEL_ROH':
                row = value['result_sets'][0]
                response = b'\x5a\x92' + b'0281019999'
                if bytes.fromhex(row['RESPONSE'])[:2] == b'\x5a\x92':
                    row['RESPONSE'] = response.hex()
                    row['_TEL_ANTWORT'] = frame(response, reply=True).hex()
        report = self.run_auth(FakeApi(mutate=wrong))
        self.assertEqual(report['error_code'], 'identity')
        self.assertEqual(report['identity']['hardware'], '0281019999')
        self.assertFalse(report['nonce_request_attempted'])
        self.assert_no_write(report)

    def test_nonce_result_must_equal_positive_telegram(self):
        def wrong(job, value):
            if job == 'AUTHENTISIERUNG_ZUFALLSZAHL_LESEN':
                value['result_sets'][0]['ZUFALLSZAHL'] = '0102030405060708'
        report = self.run_auth(FakeApi(mutate=wrong))
        self.assertEqual(report['error_code'], 'positive_response')
        self.assertFalse(report['authentication_dispatch_attempted'])
        self.assert_no_write(report)

    def test_negative_authentication_even_when_job_claims_okay(self):
        def wrong(job, value):
            if job == 'AUTHENTISIERUNG_START':
                value['result_sets'][0]['_TEL_ANTWORT'] = frame(b'\x7f\x31\x33', reply=True).hex()
        report = self.run_auth(FakeApi(mutate=wrong))
        self.assertEqual(report['error_code'], 'negative_response')
        self.assertFalse(report['authenticated'])
        self.assertTrue(report['authentication_sent'])
        self.assertTrue(self.api.closed)
        self.assert_no_write(report)

    def test_missing_binary_export_before_auth_dispatch_is_not_reported_sent(self):
        api = FakeApi(failure=('AUTHENTISIERUNG_START', ProgrammingError('EDIABAS DLL lacks __apiJobData.'), False))
        report = self.run_auth(api)
        self.assertEqual(report['error_code'], 'api_exports')
        self.assertTrue(report['authentication_dispatch_attempted'])
        self.assertFalse(report['authentication_sent'])
        self.assertFalse(report['volatile_authentication_may_remain'])
        self.assert_no_write(report)

    def test_timeout_after_dispatch_records_unknown_state_without_retry(self):
        api = FakeApi(failure=('AUTHENTISIERUNG_START', ProgrammingError('AUTHENTISIERUNG_START timed out; response-secret'), True))
        report = self.run_auth(api)
        self.assertEqual(report['error_code'], 'timeout')
        self.assertEqual(report['ecu_authentication_state'], 'unknown')
        self.assertTrue(report['authentication_sent'])
        self.assertTrue(report['volatile_authentication_may_remain'])
        self.assertNotIn('response-secret', json.dumps(report))
        self.assertEqual(sum(call[0] == 'AUTHENTISIERUNG_START' for call in api.calls), 1)
        self.assert_no_write(report)

    def test_cancel_after_challenge_never_sends_response(self):
        stop = {'value': False}
        def cancel_after(job, _):
            if job == 'AUTHENTISIERUNG_ZUFALLSZAHL_LESEN':
                stop['value'] = True
        report = self.run_auth(FakeApi(mutate=cancel_after), cancelled=lambda: stop['value'])
        self.assertTrue(report['cancelled'])
        self.assertTrue(report['nonce_requested'])
        self.assertFalse(report['authentication_dispatch_attempted'])
        self.assert_no_write(report)

    def test_cancel_before_preflight_never_opens(self):
        report = self.run_auth(cancelled=lambda: True)
        self.assertTrue(report['cancelled'])
        self.assertEqual(self.api.calls, [])
        self.assert_no_write(report)

    def test_accepted_but_cleanup_failed_preserves_historical_acceptance(self):
        report = self.run_auth(FakeApi(end_failure=True))
        self.assertTrue(report['authenticated'])
        self.assertEqual(report['ecu_authentication_state'], 'accepted_at_response')
        self.assertEqual(report['stages'][-1]['stage'], 'cleanup')
        self.assertIsNotNone(report['error'])
        self.assertNotIn('private-buffer', json.dumps(report))
        self.assert_no_write(report)

    def test_dry_run_reports_real_erase_size_and_invalid_saved_calibration(self):
        before = (self.mpc, self.fls)
        report = session.dry_run_programming_sequence(*before)
        self.assertEqual(report['factory_program_erased_bytes'], 832*1024)
        self.assertTrue(report['image_checks']['program_signature']['valid'])
        self.assertFalse(report['image_checks']['calibration_signature']['valid'])
        self.assertFalse(report['native_partial_supported'])
        self.assertFalse(report['erase_sent'])
        self.assertFalse(report['write_sent'])
        self.assertFalse(report['flash_ready'])
        self.assertEqual((self.mpc, self.fls), before)
        json.dumps(report)


class ProgrammingContractTests(unittest.TestCase):
    def test_factory_erase_length_cannot_select_one_mpc_sector(self):
        regions = session.factory_erase_regions(0x208ee)
        self.assertEqual(sum(region.byte_count for region in regions), 832*1024)
        self.assertEqual(regions[0].start, 0x20000)
        self.assertEqual(regions[0].end_exclusive, 0x70000)
        with self.assertRaises(ValueError):
            session.factory_erase_regions(0x8000)

    def test_download_and_exit_payload_are_eight_bytes(self):
        self.assertEqual(session.factory_download_payload(0x20000, 0x50000),
                         bytes.fromhex('0200000600050000'))
        for start, size in ((0x1ffff, 1), (0x6ffff, 2), (0x8fefff, 2), (0x808000, 0), (-1, 5)):
            with self.assertRaises(ValueError):
                session.factory_download_payload(start, size)

    def test_transfer_chunks_preserve_all_bytes_without_extra_sequence_byte(self):
        payload = bytes(range(256))*3
        chunks = session.transfer_data_payloads(payload)
        self.assertEqual(list(map(len, chunks)), [245, 245, 245, 33])
        self.assertEqual(b''.join(chunks), payload)
        with self.assertRaises(ValueError):
            session.transfer_data_payloads(payload, maximum_data_bytes=246)

    def test_typed_plan_is_ordered_and_writes_unavailable(self):
        steps = session.programming_sequence_plan()
        self.assertEqual([step.phase for step in steps], list(session.ProgrammingPhase))
        self.assertTrue(next(step for step in steps if step.phase == session.ProgrammingPhase.AUTHENTICATION).live_available)
        self.assertFalse(any(step.live_available for step in steps[3:]))
        api = FakeApi()
        with self.assertRaisesRegex(ProgrammingError, 'disabled'):
            session.execute_programming_sequence(api, steps)
        self.assertEqual(api.calls, [])

    def test_telegram_parser_accepts_checksums_and_b8_but_rejects_bad_addresses(self):
        expected = b'\x71\x08\x01'
        self.assertEqual(session._kwp_payload(frame(expected, reply=True), reply=True), expected)
        short = frame(expected, reply=True, checksum=False)
        self.assertEqual(session._kwp_payload(short, reply=True), expected)
        b8 = b'\xb8\xf1\x12\x03' + expected
        b8 += bytes((sum(b8) & 0xff,))
        self.assertEqual(session._kwp_payload(b8, reply=True), expected)
        invalid = bytearray(frame(expected, reply=True))
        invalid[-1] ^= 1
        with self.assertRaises(session.AuthenticationFailure):
            session._kwp_payload(invalid, reply=True)
        invalid = frame(expected, reply=False)
        with self.assertRaises(session.AuthenticationFailure):
            session._kwp_payload(invalid, reply=True)


if __name__ == '__main__':
    unittest.main()
