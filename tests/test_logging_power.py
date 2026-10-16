"""Power policy tests use a fake Windows backend; host settings never change."""
from pathlib import Path
import tempfile
import unittest
from bmw_bin_editor.logging_power import LoggingPowerGuard

A='11111111-1111-1111-1111-111111111111'
B='22222222-2222-2222-2222-222222222222'


class FakePower:
    def __init__(self):
        self.plan=A; self.values={A:{'AC':1,'DC':2},B:{'AC':3,'DC':1}}
        self.held=False; self.idle=False; self.applied=[]; self.fail_write=None
        self.fail_idle=False; self.restores=[]
    def prevent_idle(self):
        if self.fail_idle: raise OSError('blocked')
        self.idle=True; return 0x80000000
    def restore_idle(self, previous): self.idle=False; self.restores.append(previous)
    def acquire(self):
        if self.held: raise OSError('another logger owns lid policy')
        self.held=True; return 1
    def release(self, handle): self.held=False
    def active_scheme(self): return self.plan
    def read_lid(self, scheme, supply): return self.values[scheme][supply]
    def write_lid(self, scheme, supply, value):
        if self.fail_write==(supply,value): raise OSError('denied')
        self.values[scheme][supply]=value
    def apply_if_active(self, scheme):
        if self.plan==scheme:self.applied.append(scheme)


class PowerTests(unittest.TestCase):
    def setUp(self):
        self.files=tempfile.TemporaryDirectory();self.addCleanup(self.files.cleanup)
        self.path=Path(self.files.name)/'power.json';self.api=FakePower()
        self.guard=LoggingPowerGuard(self.path,self.api)
        self.addCleanup(self.guard.stop)

    def test_idle_and_both_lid_supplies_restore_exactly_and_stop_is_idempotent(self):
        self.assertIn('lid may stay closed',self.guard.start())
        self.assertTrue(self.api.idle);self.assertEqual(self.api.values[A],{'AC':0,'DC':0})
        self.assertTrue(self.path.exists())
        self.guard.stop();self.guard.stop()
        self.assertEqual(self.api.values[A],{'AC':1,'DC':2})
        self.assertFalse(self.api.idle);self.assertFalse(self.api.held);self.assertFalse(self.path.exists())
        self.assertEqual(self.api.restores,[0x80000000])

    def test_disabled_options_never_call_native_backend(self):
        guard=LoggingPowerGuard(self.path)
        from unittest.mock import patch
        with patch('bmw_bin_editor.logging_power.WindowsPowerBackend',side_effect=AssertionError('No native calls')):
            guard.start(keep_awake=False,lid_closed=False);guard.refresh();guard.stop();guard.recover()
        self.assertFalse(self.path.exists())

    def test_lid_option_also_prevents_idle_sleep(self):
        self.guard.start(keep_awake=False,lid_closed=True)
        self.assertTrue(self.api.idle)

    def test_idle_only_does_not_change_lid(self):
        self.guard.start(lid_closed=False)
        self.assertTrue(self.api.idle);self.assertFalse(self.api.held)
        self.assertEqual(self.api.values[A],{'AC':1,'DC':2});self.assertFalse(self.path.exists())

    def test_crash_journal_is_recovered_before_new_capture(self):
        self.guard.start()
        self.api.held=False  # OS releases the mutex after process death.
        recovered=LoggingPowerGuard(self.path,self.api)
        self.assertIn('Restored',recovered.recover())
        self.assertEqual(self.api.values[A],{'AC':1,'DC':2});self.assertFalse(self.path.exists())
        self.guard.lock=None

    def test_second_editor_cannot_restore_an_active_capture(self):
        self.guard.start()
        other=LoggingPowerGuard(self.path,self.api)
        self.assertIn('pending',other.recover())
        self.assertEqual(self.api.values[A],{'AC':0,'DC':0});self.assertTrue(self.path.exists())
        self.assertIn('keep the lid open',other.start())
        self.assertIsNone(other.lock)
        other.stop()
        self.assertTrue(self.api.held)

    def test_partial_write_is_rolled_back_and_idle_stays_active(self):
        self.api.fail_write=('DC',0)
        self.assertIn('unavailable',self.guard.start())
        self.assertEqual(self.api.values[A],{'AC':1,'DC':2})
        self.assertTrue(self.api.idle);self.assertFalse(self.api.held);self.assertFalse(self.path.exists())

    def test_restore_failure_retains_journal_for_next_startup(self):
        self.guard.start();self.api.fail_write=('AC',1)
        self.assertIn('restoration failed',self.guard.stop())
        self.assertTrue(self.path.exists());self.assertFalse(self.api.idle)
        self.api.fail_write=None
        self.assertIn('Restored',self.guard.recover());self.assertFalse(self.path.exists())
        self.assertEqual(self.api.values[A],{'AC':1,'DC':2})

    def test_plan_changes_are_protected_and_all_originals_restore(self):
        self.guard.start();self.api.plan=B;self.guard.refresh()
        self.assertEqual(self.api.values[B],{'AC':0,'DC':0})
        self.guard.stop()
        self.assertEqual(self.api.plan,B)
        self.assertEqual(self.api.values[A],{'AC':1,'DC':2})
        self.assertEqual(self.api.values[B],{'AC':3,'DC':1})

    def test_manual_lid_change_is_respected_and_no_longer_claims_protection(self):
        self.guard.start();self.api.values[A]['AC']=3
        self.assertIn('keep the lid open',self.guard.refresh())
        self.guard.stop()
        self.assertEqual(self.api.values[A],{'AC':3,'DC':2})

    def test_corrupt_recovery_data_cannot_change_policy(self):
        self.path.write_text('{"bad-guid":{"AC":1,"DC":2}}',encoding='utf-8')
        self.assertIn('pending',self.guard.recover())
        self.assertEqual(self.api.values[A],{'AC':1,'DC':2});self.assertTrue(self.path.exists())

    def test_idle_failure_does_not_claim_lid_protection(self):
        self.api.fail_idle=True
        text=self.guard.start()
        self.assertIn('Keep-awake failed',text);self.assertIn('keep the lid open',text)
        self.assertEqual(self.api.values[A],{'AC':1,'DC':2})
        self.assertFalse(self.api.idle)
