"""
Autostart
==========

Manages Windows autostart by placing a shortcut in the user's Startup
folder.  No registry access - per project policy, the registry is never
used; all persistence is file-based.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

__all__ = ['is_autostart_enabled', 'set_autostart', 'sync_autostart_path']

_SHORTCUT_NAME = 'CodexUsageMonitor.lnk'


def _shortcut_path() -> Path:
    """Return the path of the autostart shortcut in the Startup folder."""
    appdata = os.environ.get('APPDATA', '')
    return Path(appdata) / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs' / 'Startup' / _SHORTCUT_NAME


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
