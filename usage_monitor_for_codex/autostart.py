"""
Autostart
==========

Manages Windows autostart by placing a shortcut in the user's Startup
folder.  No registry access - per project policy, the registry is never
used; all persistence is file-based.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from pathlib import Path

__all__ = ['is_autostart_enabled', 'set_autostart', 'sync_autostart_path']

_SHORTCUT_NAME = 'CodexUsageMonitor.lnk'


class _GUID(ctypes.Structure):
    _fields_ = [
        ('Data1', ctypes.c_uint32), ('Data2', ctypes.c_uint16),
        ('Data3', ctypes.c_uint16), ('Data4', ctypes.c_ubyte * 8),
    ]


# FOLDERID_Startup {B97D20BB-F46A-4C97-BA10-5E3608430854}
_FOLDERID_STARTUP = _GUID(
    0xB97D20BB, 0xF46A, 0x4C97,
    (ctypes.c_ubyte * 8)(0xBA, 0x10, 0x5E, 0x36, 0x08, 0x43, 0x08, 0x54),
)


def _known_folder_startup() -> Path | None:
    """Resolve the real Startup folder via ``SHGetKnownFolderPath``, or ``None``.

    The Start Menu can be redirected (GPO Folder Redirection / roaming
    profiles); composing the path from ``%APPDATA%`` then points at a folder
    Explorer never processes at logon - the shortcut would be written but
    never run, while the checkbox still reads as enabled.
    """
    try:
        out = ctypes.c_wchar_p()
        result = ctypes.windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(_FOLDERID_STARTUP), 0, None, ctypes.byref(out),
        )
        try:
            if result == 0 and out.value:
                return Path(out.value)
        finally:
            # Per the API contract the buffer is freed whether the call
            # succeeded or not.
            ctypes.windll.ole32.CoTaskMemFree(out)
    except Exception:
        pass
    return None


def _shortcut_path() -> Path:
    """Return the path of the autostart shortcut in the Startup folder."""
    folder = _known_folder_startup()
    if folder is None:
        appdata = os.environ.get('APPDATA', '')
        folder = Path(appdata) / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs' / 'Startup'
    return folder / _SHORTCUT_NAME


def _ps_literal(value: str) -> str:
    """Quote a string as a PowerShell single-quoted literal (doubling quotes)."""
    return "'" + value.replace("'", "''") + "'"


def is_autostart_enabled() -> bool:
    """Return True if the Startup shortcut exists."""
    return _shortcut_path().is_file()


def set_autostart(enable: bool) -> None:
    """Create or remove the Startup shortcut.

    Parameters
    ----------
    enable : bool
        ``True`` to create the shortcut, ``False`` to remove it.
    """
    path = _shortcut_path()

    if not enable:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return

    target = sys.executable
    working_dir = str(Path(target).parent)
    script = (
        f'$s = (New-Object -ComObject WScript.Shell).CreateShortcut({_ps_literal(str(path))}); '
        f'$s.TargetPath = {_ps_literal(target)}; '
        f'$s.WorkingDirectory = {_ps_literal(working_dir)}; '
        f'$s.Save()'
    )
    subprocess.run(
        ['powershell', '-NoProfile', '-NonInteractive', '-Command', script],
        creationflags=subprocess.CREATE_NO_WINDOW, check=False,
    )


def sync_autostart_path() -> None:
    """Recreate the shortcut for the current executable if autostart is on.

    The Startup shortcut stores an absolute path; if the executable has
    been moved, recreating it keeps autostart pointing at the right file.
    """
    if is_autostart_enabled():
        set_autostart(True)
