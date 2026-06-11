"""
Codex CLI
=========

Discovers OpenAI Codex CLI installations on the system and reports their
versions.  Unlike the Claude CLI, the Codex CLI has no non-interactive
command that refreshes the OAuth token, so :func:`refresh_token` is a
deliberate no-op that signals "re-login required" - the app then keeps
showing the most recent local session snapshot until Codex itself
refreshes ``auth.json`` in the background.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


def _discover_cli_path() -> Path:
    """Discover the Codex CLI binary path.

    Strategy
    --------
    1. ``shutil.which('codex')`` - respects PATH and PATHEXT.  A typical
       npm install resolves to ``codex.cmd`` in ``%APPDATA%\\npm``.
    2. If the result is a ``.ps1`` shim, substitute the sibling ``.cmd``
       or ``.exe``; subprocess cannot execute PowerShell scripts directly.
    3. Fall back to the npm location and the Rust install location
       (``~/.codex/bin``), then a last-resort path so callers'
       ``is_file()`` checks fail gracefully.
    """
    found = shutil.which('codex')
    if found:
        path = Path(found)
        if path.suffix.lower() == '.ps1':
            for ext in ('.cmd', '.exe'):
                alt = path.with_suffix(ext)
                if alt.is_file():
                    return alt
        return path

    appdata = os.environ.get('APPDATA')
    if appdata:
        for name in ('codex.cmd', 'codex.exe'):
            candidate = Path(appdata) / 'npm' / name
            if candidate.is_file():
                return candidate

    for candidate in (
        Path.home() / '.codex' / 'bin' / 'codex.exe',
        Path.home() / '.local' / 'bin' / 'codex.exe',
    ):
        if candidate.is_file():
            return candidate

    return Path.home() / '.local' / 'bin' / 'codex.exe'


# Resolved at import time. The CLI path doesn't move during runtime.
CODEX_CLI_PATH = _discover_cli_path()

# Common VS Code-family extension dirs and the Codex extension prefix.
_EXTENSION_DIRS: list[tuple[str, Path]] = [
    ('VS Code', Path.home() / '.vscode' / 'extensions'),
    ('VS Code Insiders', Path.home() / '.vscode-insiders' / 'extensions'),
    ('Cursor', Path.home() / '.cursor' / 'extensions'),
    ('Windsurf', Path.home() / '.windsurf' / 'extensions'),
]
_EXTENSION_PREFIX = 'openai.codex-'

CHANGELOG_URL = 'https://github.com/openai/codex/blob/main/CHANGELOG.md'
PROJECT_URL = 'https://github.com/omi-last-stand/codex-usage-monitor'

__all__ = [
    'CODEX_CLI_PATH', 'CHANGELOG_URL', 'PROJECT_URL',
    'CodexInstallation', 'RefreshResult', 'cli_version', 'find_installations', 'refresh_token',
]

# Cache: path -> (mtime, version) - avoids re-running subprocess unchanged.
_version_cache: dict[Path, tuple[float, str]] = {}


@dataclass
class CodexInstallation:
    """A discovered Codex CLI / extension installation."""

    name: str
    version: str
    path: Path


@dataclass
class RefreshResult:
    """Result of a token-refresh attempt (always a no-op for Codex)."""

    success: bool
    updated: bool
    old_version: str
    new_version: str
    error: str


def find_installations() -> list[CodexInstallation]:
    """Discover Codex installations on the system.

    Checks the native CLI path and common IDE extension directories.
    The CLI version is read via ``codex --version``; extension versions
    come from the directory name.

    Returns
    -------
    list[CodexInstallation]
        Found installations, CLI first.
    """
    results: list[CodexInstallation] = []

    if CODEX_CLI_PATH.is_file():
        version = cli_version(CODEX_CLI_PATH)
        if version:
            results.append(CodexInstallation('CLI', version, CODEX_CLI_PATH))

    for ide_name, ext_dir in _EXTENSION_DIRS:
        if not ext_dir.is_dir():
            continue

        best_version = ''
        best_parts: tuple[int, ...] = ()
        best_path = None
        try:
            entries = list(ext_dir.iterdir())
        except OSError:
            # Enumeration can fail even after is_dir() (ACL denial, cloud
            # placeholder, dir deleted in between); one bad extension dir
            # must not take down the caller's update loop.
            continue
        for entry in entries:
            if not entry.name.startswith(_EXTENSION_PREFIX):
                continue
            remainder = entry.name[len(_EXTENSION_PREFIX):]
            match = re.match(r'(\d+\.\d+\.\d+)', remainder)
            if match:
                version = match.group(1)
                parts = tuple(int(x) for x in version.split('.'))
                if parts > best_parts:
                    best_version = version
                    best_parts = parts
                    best_path = entry

        if best_version and best_path:
            results.append(CodexInstallation(ide_name, best_version, best_path))

    return results


def refresh_token() -> RefreshResult:
    """Token refresh is not available non-interactively for Codex.

    The Codex CLI refreshes ``auth.json`` on its own while it runs; there
    is no safe headless command to force it (``codex login`` is
    interactive).  This returns a failure so the cache falls back to the
    most recent local session snapshot and waits for the token to change.
    """
    return RefreshResult(
        success=False, updated=False, old_version='', new_version='',
        error='Codex token refresh requires re-login (run "codex login")',
    )


def cli_version(path: Path) -> str:
    """Run ``codex --version`` and return the version string, or ``''``.

    Results are cached by file modification time so the subprocess is
    only spawned once per binary change.
    """
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return ''

    cached = _version_cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]

    version = ''
    try:
        proc = subprocess.run(
            [str(path), '--version'],
            capture_output=True, text=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        # Output examples: "codex-cli 0.5.0", "codex 0.5.0", "0.5.0"
        output = (proc.stdout or '') + (proc.stderr or '')
        match = re.search(r'(\d+\.\d+\.\d+)', output)
        version = match.group(1) if match else ''
    except Exception:
        # Fall through to cache the failure: this is called on EVERY usage
        # fetch (via the User-Agent) and popup refresh, so a persistently
        # hanging `codex --version` (AV sandboxing, broken node shim) must
        # not re-block for the full 10 s timeout each time. Like the
        # versionless-output case, probe once per binary change.
        pass
    _version_cache[path] = (mtime, version)
    return version
