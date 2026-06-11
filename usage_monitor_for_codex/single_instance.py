"""
Single-Instance Guard
======================

Prevents multiple instances from running simultaneously using a named
Win32 mutex.  The holder's PID and version are stored in page-file-backed
shared memory so that a new instance can identify and terminate it
regardless of executable name.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import os
import struct

from . import __version__
from .i18n import T

__all__ = ['ensure_single_instance', 'release_instance_lock']

_MUTEX_NAME = 'CodexUsageMonitor_SingleInstance'
_PID_MAPPING_NAME = 'CodexUsageMonitor_HolderPID'
_ERROR_ALREADY_EXISTS = 0xB7
_INVALID_HANDLE = ctypes.c_void_p(-1).value
_PAGE_READWRITE = 0x04
_FILE_MAP_READ = 0x0004
_FILE_MAP_WRITE = 0x0002

# Shared memory layout: 4-byte PID + null-terminated UTF-8 version string.
# 64 bytes is plenty for a PID and a version like "1.10.0".
_SHARED_MEM_SIZE = 64

# use_last_error=True captures GetLastError() immediately after each
# FFI call into a ctypes-private thread-local, before Python can run
# any intervening code that might reset it.
_kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

_kernel32.CreateMutexW.argtypes = [ctypes.wintypes.LPCVOID, ctypes.wintypes.BOOL, ctypes.wintypes.LPCWSTR]
_kernel32.CreateMutexW.restype = ctypes.wintypes.HANDLE

_kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
_kernel32.CloseHandle.restype = ctypes.wintypes.BOOL

_kernel32.CreateFileMappingW.argtypes = [
    ctypes.wintypes.HANDLE, ctypes.wintypes.LPCVOID, ctypes.wintypes.DWORD,
    ctypes.wintypes.DWORD, ctypes.wintypes.DWORD, ctypes.wintypes.LPCWSTR,
]
_kernel32.CreateFileMappingW.restype = ctypes.wintypes.HANDLE

_kernel32.OpenFileMappingW.argtypes = [ctypes.wintypes.DWORD, ctypes.wintypes.BOOL, ctypes.wintypes.LPCWSTR]
_kernel32.OpenFileMappingW.restype = ctypes.wintypes.HANDLE

_kernel32.MapViewOfFile.argtypes = [
    ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD, ctypes.wintypes.DWORD, ctypes.wintypes.DWORD, ctypes.c_size_t,
]
_kernel32.MapViewOfFile.restype = ctypes.c_void_p

_kernel32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
_kernel32.UnmapViewOfFile.restype = ctypes.wintypes.BOOL

_kernel32.OpenProcess.argtypes = [ctypes.wintypes.DWORD, ctypes.wintypes.BOOL, ctypes.wintypes.DWORD]
_kernel32.OpenProcess.restype = ctypes.wintypes.HANDLE

_kernel32.TerminateProcess.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.UINT]
_kernel32.TerminateProcess.restype = ctypes.wintypes.BOOL

_kernel32.WaitForSingleObject.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD]
_kernel32.WaitForSingleObject.restype = ctypes.wintypes.DWORD

# Handles kept alive for the process lifetime; released on exit or
# explicitly via release_instance_lock().
_mutex_handle: int | None = None
_pid_mapping_handle: int | None = None


def _store_holder_info() -> None:
    """Store our PID and version in named shared memory.

    The shared memory is backed by the page file (no disk I/O) and is
    automatically released when this process terminates.
    """
    global _pid_mapping_handle
    _pid_mapping_handle = _kernel32.CreateFileMappingW(
        _INVALID_HANDLE, None, _PAGE_READWRITE, 0, _SHARED_MEM_SIZE, _PID_MAPPING_NAME,
    )
    if not _pid_mapping_handle:
        return

    view = _kernel32.MapViewOfFile(_pid_mapping_handle, _FILE_MAP_WRITE, 0, 0, _SHARED_MEM_SIZE)
    if not view:
        return

    version_bytes = __version__.encode('utf-8')[:_SHARED_MEM_SIZE - 5]
    payload = struct.pack(f'<I{len(version_bytes) + 1}s', os.getpid(), version_bytes + b'\x00')
    ctypes.memmove(view, payload, len(payload))
    _kernel32.UnmapViewOfFile(view)


def _read_holder_info() -> tuple[int | None, str | None]:
    """Read PID and version of the mutex-holding instance from shared memory.

    Returns
    -------
    tuple[int | None, str | None]
        ``(pid, version)`` of the holder, or ``(None, None)`` if the
        shared memory does not exist.
    """
    mapping = _kernel32.OpenFileMappingW(_FILE_MAP_READ, False, _PID_MAPPING_NAME)
    if not mapping:
        return None, None

    view = _kernel32.MapViewOfFile(mapping, _FILE_MAP_READ, 0, 0, _SHARED_MEM_SIZE)
    if not view:
        _kernel32.CloseHandle(mapping)
        return None, None

    raw = ctypes.string_at(view, _SHARED_MEM_SIZE)
    _kernel32.UnmapViewOfFile(view)
    _kernel32.CloseHandle(mapping)

    if len(raw) < 5:
        return None, None

    pid = struct.unpack('<I', raw[:4])[0]
    version = raw[4:].split(b'\x00', 1)[0].decode('utf-8', errors='replace') or None
    return pid if pid else None, version


def _open_pid_for_terminate(pid: int) -> int | None:
    """Open a terminate-capable handle to *pid*, or return ``None``.

    Opened BEFORE the replace-confirmation dialog: the dialog can sit
    unanswered indefinitely while the old instance exits on its own, and
    Windows recycles PIDs - terminating by bare PID afterwards could kill
    an unrelated process. A held handle pins the kernel process object,
    so the PID cannot be reused while we wait.
    """
    PROCESS_TERMINATE = 0x0001
    PROCESS_SYNCHRONIZE = 0x00100000

    handle = _kernel32.OpenProcess(PROCESS_TERMINATE | PROCESS_SYNCHRONIZE, False, pid)
    return handle or None


def _terminate_process_handle(handle: int) -> bool:
    """Terminate a process by handle and wait until it is fully dead.

    Uses TerminateProcess + WaitForSingleObject so the process has
    released all kernel objects (mutexes, handles) before this function
    returns. The caller owns *handle* and closes it afterwards.

    Returns
    -------
    bool
        True only if termination was requested successfully; False if
        the process could not be terminated (e.g. it already exited).
    """
    if not _kernel32.TerminateProcess(handle, 1):
        return False

    _kernel32.WaitForSingleObject(handle, 5000)
    return True


def ensure_single_instance() -> bool:
    """Ensure only one instance of the application is running.

    If another instance holds the mutex, shows a dialog asking the user
    whether to replace it.  The dialog title includes the running
    instance's version when available.

    Returns
    -------
    bool
        True if this instance may proceed, False if it should exit.
    """
    global _mutex_handle
    MB_YESNO = 0x04
    MB_ICONQUESTION = 0x20
    MB_TOPMOST = 0x40000
    IDYES = 6

    _mutex_handle = _kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    already_exists = ctypes.get_last_error() == _ERROR_ALREADY_EXISTS
    if not _mutex_handle:
        # CreateMutexW failed outright - typically ERROR_ACCESS_DENIED because
        # the mutex was created by an ELEVATED instance whose DACL this
        # non-elevated process cannot open with MUTEX_ALL_ACCESS. The mutex
        # exists, so another instance IS running; it can be neither joined nor
        # replaced (OpenProcess on an elevated holder fails the same way).
        # Fail closed: starting anyway would double-poll and double-run any
        # configured reset/threshold commands.
        _mutex_handle = None
        ctypes.windll.user32.MessageBoxW(
            None, T['replace_failed'], T['popup_title'], MB_ICONQUESTION | MB_TOPMOST,
        )
        return False
    if not already_exists:
        _store_holder_info()
        return True

    # Another instance is running - ask the user.
    holder_pid, running_version = _read_holder_info()
    holder_handle = _open_pid_for_terminate(holder_pid) if holder_pid else None

    title = T['popup_title']
    if running_version:
        title += f' v{running_version}'

    message = T['already_running'].format(
        running_version=running_version or '?',
    )

    answer = ctypes.windll.user32.MessageBoxW(
        None, message, title,
        MB_YESNO | MB_ICONQUESTION | MB_TOPMOST,
    )
    if answer != IDYES:
        if holder_handle:
            _kernel32.CloseHandle(holder_handle)
        _kernel32.CloseHandle(_mutex_handle)
        _mutex_handle = None
        return False

    if holder_handle:
        _terminate_process_handle(holder_handle)
        _kernel32.CloseHandle(holder_handle)
    _kernel32.CloseHandle(_mutex_handle)

    # Re-acquire the mutex. If it STILL exists (or cannot even be created), the
    # previous instance is alive (its PID was unknown, termination failed, or
    # this was a startup race), so do NOT start a duplicate - two instances
    # would double-poll and, worse, double-run any configured reset/threshold
    # commands.
    _mutex_handle = _kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    if not _mutex_handle or ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
        if _mutex_handle:
            _kernel32.CloseHandle(_mutex_handle)
        _mutex_handle = None
        ctypes.windll.user32.MessageBoxW(None, T['replace_failed'], title, MB_ICONQUESTION | MB_TOPMOST)
        return False

    _store_holder_info()
    return True


def release_instance_lock() -> None:
    """Release the mutex and shared memory so a new instance can start."""
    global _mutex_handle, _pid_mapping_handle

    if _mutex_handle:
        _kernel32.CloseHandle(_mutex_handle)
        _mutex_handle = None

    if _pid_mapping_handle:
        _kernel32.CloseHandle(_pid_mapping_handle)
        _pid_mapping_handle = None
