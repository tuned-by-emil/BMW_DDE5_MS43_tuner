"""Nonblocking cable monitoring owned by the editor's event loop."""
import ctypes
import os
import re
import sys
import time
from pathlib import Path
from PySide6.QtCore import QObject, QTimer, QProcess, Signal, QLockFile, QDir

DEVICE = re.compile(r'USB\\VID_1C43&PID_0500(?:[&\\\s]|$)', re.I)
EXECUTABLE = Path(r'C:\mpps\mpps.exe')


def mpps_running():
    from ctypes import wintypes as W
    class ProcessEntry(ctypes.Structure):
        _fields_ = [('dwSize', W.DWORD), ('cntUsage', W.DWORD),
            ('th32ProcessID', W.DWORD), ('th32DefaultHeapID', ctypes.c_size_t),
            ('th32ModuleID', W.DWORD), ('cntThreads', W.DWORD),
            ('th32ParentProcessID', W.DWORD), ('pcPriClassBase', W.LONG),
            ('dwFlags', W.DWORD), ('szExeFile', W.WCHAR * 260)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    snapshot = kernel.CreateToolhelp32Snapshot
    snapshot.argtypes = [W.DWORD, W.DWORD]; snapshot.restype = W.HANDLE
    first = kernel.Process32FirstW; next_process = kernel.Process32NextW
    for func in (first, next_process):
        func.argtypes = [W.HANDLE, ctypes.POINTER(ProcessEntry)]; func.restype = W.BOOL
    kernel.CloseHandle.argtypes = [W.HANDLE]; kernel.CloseHandle.restype = W.BOOL
    handle = snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = ProcessEntry(); entry.dwSize = ctypes.sizeof(entry)
        ok = first(handle, ctypes.byref(entry))
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        while ok:
            if entry.szExeFile.casefold() == 'mpps.exe':
                return True
            ok = next_process(handle, ctypes.byref(entry))
        error = ctypes.get_last_error()
        if error != 18:  # ERROR_NO_MORE_FILES
            raise ctypes.WinError(error)
        return False
    finally:
        kernel.CloseHandle(handle)


class CableEdge:
    def __init__(self):
        self.present = False
        self.misses = 0

    def update(self, present):
        if present:
            self.misses = 0
            inserted = not self.present
            self.present = True
            return inserted
        self.misses += 1
        if self.misses >= 2:
            self.present = False
        return False


class MppsAutoStart(QObject):
    status = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.enabled = False
        self.edge = CableEdge()
        self.stage = ''
        self.owns_lock = False
        self.launch_after = None
        self.lock = QLockFile(QDir.tempPath() + '/BMWBinEditor-MPPS.lock')
        self.process = QProcess(self)
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._error)
        self.timer = QTimer(self); self.timer.setInterval(2000)
        self.timer.timeout.connect(self.poll)
        self.timeout = QTimer(self); self.timeout.setSingleShot(True)
        self.timeout.timeout.connect(self._timeout)

    def set_enabled(self, enabled):
        self.stop()
        self.enabled = bool(enabled) and sys.platform == 'win32'
        self.edge = CableEdge()
        if self.enabled:
            self.timer.start()

    def stop(self):
        self.enabled = False
        self.launch_after = None
        self.timer.stop(); self.timeout.stop()
        self.stage = ''
        if self.process.state() != QProcess.NotRunning:
            self.process.kill()
            self.process.waitForFinished(1000)
        if self.owns_lock:
            self.lock.unlock(); self.owns_lock = False

    def poll(self):
        if not self.enabled or self.process.state() != QProcess.NotRunning:
            return
        if not self.owns_lock:
            self.owns_lock = self.lock.tryLock(0)
        if not self.owns_lock:
            return
        self._run('devices', 'pnputil.exe', ['/enum-devices', '/connected', '/ids'])

    def _run(self, stage, program, arguments):
        self.stage = stage
        self.process.setProgram(str(Path(os.environ.get('SystemRoot', r'C:\Windows'))/'System32'/program))
        self.process.setArguments(arguments)
        self.process.start()
        self.timeout.start(8000)

    def _timeout(self):
        self.stage = ''
        self.process.kill()
        self.status.emit('MPPS cable check timed out; will retry.')

    def _error(self, error):
        if self.enabled and error == QProcess.FailedToStart:
            self.timeout.stop(); self.stage = ''
            self.status.emit('Could not run Windows MPPS cable detection.')

    def _finished(self, code, exit_status):
        self.timeout.stop()
        stage, self.stage = self.stage, ''
        output = bytes(self.process.readAllStandardOutput()).decode('utf-8', errors='replace')
        if not self.enabled or not stage:
            return
        if code != 0 or exit_status != QProcess.NormalExit:
            self.status.emit('Windows MPPS cable check failed; will retry.')
            return
        if stage == 'devices':
            present = bool(DEVICE.search(output))
            inserted = self.edge.update(present)
            if not present:
                self.launch_after = None
            elif inserted:
                self.launch_after = time.monotonic() + 4.0
            if present and self.launch_after is not None and time.monotonic() >= self.launch_after:
                self.launch_after = None
                try:
                    if mpps_running():
                        self.status.emit('MPPS cable connected; MPPS is already running.')
                    else:
                        self._launch()
                except OSError as exc:
                    self.edge.present = False
                    self.status.emit('MPPS process check failed: ' + str(exc))

    def _launch(self):
        if not EXECUTABLE.is_file():
            self.status.emit(r'MPPS cable connected, but C:\mpps\mpps.exe was not found.')
            return
        # ShellExecute preserves the application's usual Windows elevation prompt.
        shell_execute = ctypes.windll.shell32.ShellExecuteW
        shell_execute.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
                                  ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int]
        shell_execute.restype = ctypes.c_void_p
        # PyInstaller's DLL directory is inherited by child processes. MPPS must
        # resolve its own libraries, while the editor needs its directory restored.
        kernel = ctypes.windll.kernel32
        get_dir = kernel.GetDllDirectoryW
        get_dir.argtypes = [ctypes.c_uint32, ctypes.c_wchar_p]
        get_dir.restype = ctypes.c_uint32
        set_dir = kernel.SetDllDirectoryW
        set_dir.argtypes = [ctypes.c_wchar_p]
        set_dir.restype = ctypes.c_int
        size = get_dir(0, None)
        buffer = ctypes.create_unicode_buffer(size + 1)
        get_dir(len(buffer), buffer)
        previous = buffer.value or None
        if not set_dir(None):
            self.status.emit('MPPS could not start: could not reset DLL search directory.')
            return
        try:
            result = shell_execute(None, 'open', str(EXECUTABLE), None, str(EXECUTABLE.parent), 1)
        finally:
            set_dir(previous)
        if result and result > 32:
            self.status.emit('MPPS cable connected; launched MPPS.')
        else:
            self.status.emit(f'MPPS could not start (Windows error {result or 0}).')
