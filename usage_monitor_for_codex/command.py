"""
Command
========

Execute user-configured shell commands on usage events.

Commands run as fire-and-forget subprocesses.  Event details are passed
via environment variables so the user's script can inspect them without
any string interpolation in the command itself.
"""
from __future__ import annotations

import os
import subprocess
import sys
import traceback
from pathlib import Path

from . import __version__

__all__ = ['run_event_command']


def run_event_command(commands: list[str], env_vars: dict[str, str]) -> None:
    """Launch shell commands with event-specific environment variables.

    Each command runs asynchronously (fire-and-forget).  Exceptions from
    ``subprocess.Popen`` are caught per command so one failure does not
    prevent the remaining commands from running.

    Parameters
    ----------
    commands : list[str]
        Shell command strings to execute.
    env_vars : dict[str, str]
        Mapping of ``USAGE_MONITOR_*`` environment variable names to
        their values.  Merged into the current process environment.
    """
    if not commands:
        return

    env = {**os.environ, 'USAGE_MONITOR_VERSION': __version__, **env_vars}

    # Pin working directory to the executable's folder so that relative paths
    # in commands resolve predictably - even when Windows autostart sets the
    # CWD to C:\Windows\System32.
    if getattr(sys, 'frozen', False):
        working_dir = Path(sys.executable).parent
    else:
        working_dir = Path(__file__).resolve().parent.parent

    for command in commands:
        try:
            subprocess.Popen(
                command, shell=True, env=env, cwd=working_dir,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception:
            traceback.print_exc()
