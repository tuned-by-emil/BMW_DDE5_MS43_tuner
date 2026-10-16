"""Temporary Windows idle/lid protection, with crash-recovery journal.

No power settings change at import. Native calls are isolated for offline tests.
The named mutex prevents two editors from restoring each other's lid settings.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import sys
import uuid


class GUID(ctypes.Structure):
    _fields_ = [('data1', wintypes.DWORD), ('data2', wintypes.WORD),
                ('data3', wintypes.WORD), ('data4', ctypes.c_ubyte * 8)]


def _guid(value):
    return GUID.from_buffer_copy(uuid.UUID(value).bytes_le)


class WindowsPowerBackend:
    """Win32 APIs; never shells out or requests elevation."""
    def __init__(self):
        if sys.platform != 'win32': raise OSError('Logging power protection requires Windows.')
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.power = ctypes.WinDLL('powrprof', use_last_error=True)
        self.kernel.SetThreadExecutionState.argtypes = [wintypes.DWORD]
        self.kernel.SetThreadExecutionState.restype = wintypes.DWORD
        self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.CreateMutexW.restype = wintypes.HANDLE
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
        self.kernel.ReleaseMutex.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.kernel.LocalFree.argtypes = [ctypes.c_void_p]
        self.kernel.LocalFree.restype = ctypes.c_void_p
        pg = ctypes.POINTER(GUID)
        self.power.PowerGetActiveScheme.argtypes = [ctypes.c_void_p, ctypes.POINTER(pg)]
        self.power.PowerSetActiveScheme.argtypes = [ctypes.c_void_p, pg]
        for supply in ('AC', 'DC'):
            read = getattr(self.power, 'PowerRead'+supply+'ValueIndex')
            read.argtypes = [ctypes.c_void_p, pg, pg, pg, ctypes.POINTER(wintypes.DWORD)]
            read.restype = wintypes.DWORD
            write = getattr(self.power, 'PowerWrite'+supply+'ValueIndex')
            write.argtypes = [ctypes.c_void_p, pg, pg, pg, wintypes.DWORD]
            write.restype = wintypes.DWORD
        self.power.PowerGetActiveScheme.restype = wintypes.DWORD
        self.power.PowerSetActiveScheme.restype = wintypes.DWORD
        self.subgroup = _guid('4f971e89-eebd-4455-a8de-9e59040e7347')
        self.lid = _guid('5ca83367-6e45-459f-a27b-476b1d01c936')

    @staticmethod
    def check(code):
        if code: raise OSError(code, 'Windows power policy operation failed')

    def prevent_idle(self):
        previous = self.kernel.SetThreadExecutionState(0x80000001)
        if not previous: raise ctypes.WinError(ctypes.get_last_error())
        return previous

    def restore_idle(self, previous):
        if not self.kernel.SetThreadExecutionState(previous | 0x80000000):
            raise ctypes.WinError(ctypes.get_last_error())

    def acquire(self):
        handle = self.kernel.CreateMutexW(None, False, 'Local\\BMW_Bin_Editor_Logging_Lid_Policy')
        if not handle: raise ctypes.WinError(ctypes.get_last_error())
        code = self.kernel.WaitForSingleObject(handle, 0)
        if code in (0, 0x80): return handle
        self.kernel.CloseHandle(handle)
        if code == 0x102: raise OSError('Another editor owns the temporary lid setting.')
        raise OSError('Could not lock the temporary lid setting.')

    def release(self, handle):
        try:
            if not self.kernel.ReleaseMutex(handle): raise ctypes.WinError(ctypes.get_last_error())
        finally: self.kernel.CloseHandle(handle)

    def active_scheme(self):
        pointer = ctypes.POINTER(GUID)()
        self.check(self.power.PowerGetActiveScheme(None, ctypes.byref(pointer)))
        try: return str(uuid.UUID(bytes_le=ctypes.string_at(pointer, 16)))
        finally: self.kernel.LocalFree(pointer)

    def read_lid(self, scheme, supply):
        value = wintypes.DWORD()
        self.check(getattr(self.power, 'PowerRead'+supply+'ValueIndex')(
            None, ctypes.byref(_guid(scheme)), ctypes.byref(self.subgroup), ctypes.byref(self.lid), ctypes.byref(value)))
        return value.value

    def write_lid(self, scheme, supply, value):
        self.check(getattr(self.power, 'PowerWrite'+supply+'ValueIndex')(
            None, ctypes.byref(_guid(scheme)), ctypes.byref(self.subgroup), ctypes.byref(self.lid), value))

    def apply_if_active(self, scheme):
        # Never switch the user back to a power plan they have since left.
        if self.active_scheme() == scheme:
            self.check(self.power.PowerSetActiveScheme(None, ctypes.byref(_guid(scheme))))


class LoggingPowerGuard:
    def __init__(self, journal: Path, backend=None):
        self.journal = Path(journal)
        self.backend = backend
        self.idle_previous = None
        self.lock = None
        self.running = False
        self.lid_enabled = False
        self.message = ''

    def _native(self):
        if self.backend is None: self.backend = WindowsPowerBackend()
        return self.backend

    def _read(self):
        if not self.journal.exists(): return {}
        if self.journal.stat().st_size > 65536: raise ValueError('Invalid power recovery file.')
        data = json.loads(self.journal.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or len(data) > 64: raise ValueError('Invalid power recovery file.')
        for scheme, values in data.items():
            uuid.UUID(scheme)
            if not isinstance(values, dict) or set(values) != {'AC', 'DC'} or any(
                type(value) is not int or value not in (0, 1, 2, 3) for value in values.values()):
                raise ValueError('Invalid saved lid values.')
        return data

    def _save(self, data):
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        temp = self.journal.with_suffix('.tmp')
        with temp.open('w', encoding='utf-8') as file:
            json.dump(data, file)
            file.flush(); os.fsync(file.fileno())
        temp.replace(self.journal)

    def _restore_lid(self):
        api = self._native()
        for scheme, values in self._read().items():
            for supply, original in values.items():
                # A different nonzero lid action was chosen outside this editor.
                # Preserve that choice instead of overwriting it.
                if api.read_lid(scheme, supply) == 0:
                    api.write_lid(scheme, supply, original)
            api.apply_if_active(scheme)
        self.journal.unlink(missing_ok=True)

    def recover(self):
        """Startup recovery only when a previous process left a journal."""
        if not self.journal.exists(): return ''
        handle = None
        try:
            handle = self._native().acquire()
            self._restore_lid()
            return 'Restored lid settings from the interrupted logging session.'
        except (OSError, ValueError):
            return 'Lid recovery pending: another editor is logging or Windows blocked restoration.'
        finally:
            if handle is not None: self.backend.release(handle)

    def _protect_current_plan(self):
        api = self._native()
        scheme = api.active_scheme()
        data = self._read()
        if scheme in data:
            if any(api.read_lid(scheme, supply) != 0 for supply in ('AC', 'DC')):
                raise OSError('Lid policy changed during logging.')
            return
        data[scheme] = {supply: api.read_lid(scheme, supply) for supply in ('AC', 'DC')}
        if any(value not in (0, 1, 2, 3) for value in data[scheme].values()):
            raise OSError('Unsupported original lid action.')
        self._save(data)  # Durable before the first policy write, including partial failure.
        for supply in ('AC', 'DC'): api.write_lid(scheme, supply, 0)
        api.apply_if_active(scheme)
        if any(api.read_lid(scheme, supply) != 0 for supply in ('AC', 'DC')):
            raise OSError('Windows did not retain the requested lid policy.')

    def start(self, *, keep_awake=True, lid_closed=True):
        self.stop()
        self.running = True
        notices = []
        if keep_awake or lid_closed:
            try:
                self.idle_previous = self._native().prevent_idle()
                notices.append('Idle sleep blocked')
            except OSError: notices.append('Keep-awake failed: Windows rejected the request')
        if lid_closed:
            try:
                if self.idle_previous is None: raise OSError('Idle protection is required for closed-lid logging.')
                self.lock = self._native().acquire()
                self._restore_lid()
                self._protect_current_plan()
                self.lid_enabled = True
                notices.append('lid may stay closed (battery + mains)')
            except (OSError, ValueError):
                notices.append('Lid protection unavailable — keep the lid open')
                if self.lock is not None:
                    try: self._restore_lid()
                    except (OSError, ValueError): notices.append('lid restoration pending; reopen editor to retry')
                    self.backend.release(self.lock); self.lock = None
        self.message = 'Power: ' + '; '.join(notices) if notices else 'Power: Windows sleep settings unchanged'
        return self.message

    def refresh(self):
        if self.running and self.lid_enabled:
            try: self._protect_current_plan()
            except (OSError, ValueError):
                self.lid_enabled = False
                self.message = 'Power: lid policy changed or blocked — keep the lid open; idle protection remains active' if self.idle_previous is not None else 'Power: protection unavailable — keep the lid open'
        return self.message

    def stop(self):
        notices = []
        self.running = False; self.lid_enabled = False
        if self.lock is not None:
            try:
                self._restore_lid()
                notices.append('lid settings restored')
            except (OSError, ValueError): notices.append('Lid restoration failed — reopen editor to retry')
            finally:
                self.backend.release(self.lock); self.lock = None
        if self.idle_previous is not None:
            try:
                self.backend.restore_idle(self.idle_previous)
                self.idle_previous = None
                notices.append('normal idle sleep restored')
            except OSError: notices.append('Idle sleep restoration failed — close the editor')
        if notices: self.message = 'Power: ' + '; '.join(notices)
        return self.message
