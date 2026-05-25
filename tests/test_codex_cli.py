"""
Codex CLI Tests
================

Unit tests for _discover_cli_path(), find_installations(), cli_version(),
and refresh_token() (which is always a no-op for Codex).
"""
from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from usage_monitor_for_codex import codex_cli
from usage_monitor_for_codex.codex_cli import (
    CodexInstallation,
    RefreshResult,
    cli_version,
    find_installations,
    refresh_token,
)


# ---------------------------------------------------------------------------
# _discover_cli_path
# ---------------------------------------------------------------------------

class TestDiscoverCliPath(unittest.TestCase):
    """Tests for _discover_cli_path()."""

    @patch('usage_monitor_for_codex.codex_cli.shutil.which')
    def test_uses_which_when_found(self, mock_which):
        """shutil.which hit returns the discovered path directly."""
        with TemporaryDirectory() as tmp:
            fake = Path(tmp) / 'codex.cmd'
            fake.touch()
            mock_which.return_value = str(fake)
            self.assertEqual(codex_cli._discover_cli_path(), fake)

    @patch('usage_monitor_for_codex.codex_cli.shutil.which')
    def test_substitutes_cmd_for_ps1(self, mock_which):
        """When which returns a .ps1 shim, sibling .cmd is preferred."""
        with TemporaryDirectory() as tmp:
            ps1 = Path(tmp) / 'codex.ps1'
            cmd = Path(tmp) / 'codex.cmd'
            ps1.touch()
            cmd.touch()
            mock_which.return_value = str(ps1)
            self.assertEqual(codex_cli._discover_cli_path(), cmd)

    @patch('usage_monitor_for_codex.codex_cli.shutil.which')
    def test_substitutes_exe_for_ps1_when_no_cmd(self, mock_which):
        """When which returns .ps1 with no .cmd sibling, .exe is preferred."""
        with TemporaryDirectory() as tmp:
            ps1 = Path(tmp) / 'codex.ps1'
            exe = Path(tmp) / 'codex.exe'
            ps1.touch()
            exe.touch()
            mock_which.return_value = str(ps1)
            self.assertEqual(codex_cli._discover_cli_path(), exe)

    @patch('usage_monitor_for_codex.codex_cli.shutil.which')
    def test_returns_ps1_if_no_sibling_exists(self, mock_which):
        """Without a .cmd/.exe sibling, the .ps1 is returned (caller's is_file check handles it)."""
        with TemporaryDirectory() as tmp:
            ps1 = Path(tmp) / 'codex.ps1'
            ps1.touch()
            mock_which.return_value = str(ps1)
            self.assertEqual(codex_cli._discover_cli_path(), ps1)

    @patch('usage_monitor_for_codex.codex_cli.shutil.which', return_value=None)
    def test_falls_back_to_appdata_npm(self, _mock_which):
        """When which finds nothing, use the standard npm location."""
        with TemporaryDirectory() as tmp:
            npm_dir = Path(tmp) / 'npm'
            npm_dir.mkdir()
            cmd = npm_dir / 'codex.cmd'
            cmd.touch()
            with patch.dict(os.environ, {'APPDATA': str(tmp)}, clear=False):
                self.assertEqual(codex_cli._discover_cli_path(), cmd)

    @patch('usage_monitor_for_codex.codex_cli.shutil.which', return_value=None)
    def test_appdata_npm_prefers_cmd_over_exe(self, _mock_which):
        """When both .cmd and .exe exist in npm dir, .cmd is preferred (typical npm shim)."""
        with TemporaryDirectory() as tmp:
            npm_dir = Path(tmp) / 'npm'
            npm_dir.mkdir()
            cmd = npm_dir / 'codex.cmd'
            exe = npm_dir / 'codex.exe'
            cmd.touch()
            exe.touch()
            with patch.dict(os.environ, {'APPDATA': str(tmp)}, clear=False):
                self.assertEqual(codex_cli._discover_cli_path(), cmd)

    @patch('usage_monitor_for_codex.codex_cli.shutil.which', return_value=None)
    def test_falls_back_to_dot_codex_bin(self, _mock_which):
        """When npm has nothing, the Rust install location (~/.codex/bin) is used."""
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            codex_bin = home / '.codex' / 'bin'
            codex_bin.mkdir(parents=True)
            exe = codex_bin / 'codex.exe'
            exe.touch()
            # APPDATA points somewhere with no npm/codex.* so that branch is skipped.
            with patch('usage_monitor_for_codex.codex_cli.Path.home', return_value=home), \
                 patch.dict(os.environ, {'APPDATA': str(home / 'nope')}, clear=False):
                self.assertEqual(codex_cli._discover_cli_path(), exe)

    @patch('usage_monitor_for_codex.codex_cli.shutil.which', return_value=None)
    def test_last_resort_returns_default_when_nothing_found(self, _mock_which):
        """No CLI anywhere -> return the default path so callers fail gracefully."""
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            with patch('usage_monitor_for_codex.codex_cli.Path.home', return_value=home), \
                 patch.dict(os.environ, {'APPDATA': str(tmp)}, clear=False):
                result = codex_cli._discover_cli_path()
            self.assertEqual(result, home / '.local' / 'bin' / 'codex.exe')
            self.assertFalse(result.is_file())


# ---------------------------------------------------------------------------
# cli_version
# ---------------------------------------------------------------------------

class TestCliVersion(unittest.TestCase):
    """Tests for cli_version()."""

    def setUp(self):
        codex_cli._version_cache.clear()

    @patch('usage_monitor_for_codex.codex_cli.subprocess.run')
    @patch('pathlib.Path.stat', return_value=MagicMock(st_mtime=1000.0))
    def test_parses_version_string(self, _mock_stat, mock_run):
        """Extracts version from 'codex-cli 0.5.0' output."""
        mock_run.return_value = MagicMock(stdout='codex-cli 0.5.0\n', stderr='', returncode=0)
        self.assertEqual(cli_version(Path('/fake/codex.exe')), '0.5.0')

    @patch('usage_monitor_for_codex.codex_cli.subprocess.run')
    @patch('pathlib.Path.stat', return_value=MagicMock(st_mtime=1000.0))
    def test_parses_codex_prefix(self, _mock_stat, mock_run):
        """Extracts version from 'codex 0.5.0' output."""
        mock_run.return_value = MagicMock(stdout='codex 0.5.0\n', stderr='', returncode=0)
        self.assertEqual(cli_version(Path('/fake/codex.exe')), '0.5.0')

    @patch('usage_monitor_for_codex.codex_cli.subprocess.run')
    @patch('pathlib.Path.stat', return_value=MagicMock(st_mtime=1000.0))
    def test_version_only(self, _mock_stat, mock_run):
        """Handles bare version string without suffix."""
        mock_run.return_value = MagicMock(stdout='0.12.0\n', stderr='', returncode=0)
        self.assertEqual(cli_version(Path('/fake/codex.exe')), '0.12.0')

    @patch('usage_monitor_for_codex.codex_cli.subprocess.run')
    @patch('pathlib.Path.stat', return_value=MagicMock(st_mtime=1000.0))
    def test_empty_output(self, _mock_stat, mock_run):
        """Returns empty string when output is empty."""
        mock_run.return_value = MagicMock(stdout='', stderr='', returncode=0)
        self.assertEqual(cli_version(Path('/fake/codex.exe')), '')

    @patch('usage_monitor_for_codex.codex_cli.subprocess.run')
    @patch('pathlib.Path.stat', return_value=MagicMock(st_mtime=1000.0))
    def test_non_version_output(self, _mock_stat, mock_run):
        """Returns empty string for non-version output."""
        mock_run.return_value = MagicMock(stdout='error: something wrong', stderr='', returncode=1)
        self.assertEqual(cli_version(Path('/fake/codex.exe')), '')

    @patch('usage_monitor_for_codex.codex_cli.subprocess.run')
    @patch('pathlib.Path.stat', return_value=MagicMock(st_mtime=1000.0))
    def test_reads_version_from_stderr(self, _mock_stat, mock_run):
        """Version is parsed even when printed to stderr."""
        mock_run.return_value = MagicMock(stdout='', stderr='codex-cli 0.7.1\n', returncode=0)
        self.assertEqual(cli_version(Path('/fake/codex.exe')), '0.7.1')

    @patch('usage_monitor_for_codex.codex_cli.subprocess.run')
    @patch('pathlib.Path.stat', return_value=MagicMock(st_mtime=1000.0))
    def test_timeout_returns_empty(self, _mock_stat, mock_run):
        """Returns empty string on timeout."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd='codex', timeout=10)
        self.assertEqual(cli_version(Path('/fake/codex.exe')), '')

    @patch('usage_monitor_for_codex.codex_cli.subprocess.run')
    @patch('pathlib.Path.stat', return_value=MagicMock(st_mtime=1000.0))
    def test_os_error_returns_empty(self, _mock_stat, mock_run):
        """Returns empty string on OSError (binary not found)."""
        mock_run.side_effect = OSError('not found')
        self.assertEqual(cli_version(Path('/fake/codex.exe')), '')

    @patch('usage_monitor_for_codex.codex_cli.subprocess.run')
    @patch('pathlib.Path.stat', return_value=MagicMock(st_mtime=1000.0))
    def test_passes_correct_args(self, _mock_stat, mock_run):
        """Calls subprocess with correct arguments."""
        mock_run.return_value = MagicMock(stdout='codex-cli 0.5.0\n', stderr='', returncode=0)
        path = Path('/fake/codex.exe')
        cli_version(path)
        mock_run.assert_called_once_with(
            [str(path), '--version'],
            capture_output=True, text=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW,
        )

    @patch('usage_monitor_for_codex.codex_cli.subprocess.run')
    @patch('pathlib.Path.stat', return_value=MagicMock(st_mtime=1000.0))
    def test_cache_hit_skips_subprocess(self, _mock_stat, mock_run):
        """Second call with same mtime returns cached version without subprocess."""
        mock_run.return_value = MagicMock(stdout='codex-cli 0.5.0\n', stderr='', returncode=0)
        path = Path('/fake/codex.exe')
        self.assertEqual(cli_version(path), '0.5.0')
        self.assertEqual(cli_version(path), '0.5.0')
        mock_run.assert_called_once()

    @patch('usage_monitor_for_codex.codex_cli.subprocess.run')
    def test_cache_invalidated_on_mtime_change(self, mock_run):
        """Changed mtime triggers a new subprocess call."""
        mock_run.return_value = MagicMock(stdout='codex-cli 0.5.0\n', stderr='', returncode=0)
        path = Path('/fake/codex.exe')
        with patch('pathlib.Path.stat', return_value=MagicMock(st_mtime=1000.0)):
            self.assertEqual(cli_version(path), '0.5.0')
        mock_run.return_value = MagicMock(stdout='codex-cli 0.6.0\n', stderr='', returncode=0)
        with patch('pathlib.Path.stat', return_value=MagicMock(st_mtime=2000.0)):
            self.assertEqual(cli_version(path), '0.6.0')
        self.assertEqual(mock_run.call_count, 2)

    def test_stat_failure_returns_empty(self):
        """Returns empty string when stat() fails (file deleted)."""
        with patch('pathlib.Path.stat', side_effect=OSError('not found')):
            self.assertEqual(cli_version(Path('/fake/codex.exe')), '')


# ---------------------------------------------------------------------------
# find_installations
# ---------------------------------------------------------------------------

class TestFindInstallations(unittest.TestCase):
    """Tests for find_installations()."""

    @patch('usage_monitor_for_codex.codex_cli.cli_version', return_value='')
    @patch('usage_monitor_for_codex.codex_cli.CODEX_CLI_PATH')
    @patch('usage_monitor_for_codex.codex_cli._EXTENSION_DIRS', [])
    def test_no_installations_found(self, mock_cli_path, _mock_version):
        """Returns empty list when nothing is installed."""
        mock_cli_path.is_file.return_value = False
        result = find_installations()
        self.assertEqual(result, [])

    @patch('usage_monitor_for_codex.codex_cli.cli_version', return_value='0.5.0')
    @patch('usage_monitor_for_codex.codex_cli.CODEX_CLI_PATH')
    @patch('usage_monitor_for_codex.codex_cli._EXTENSION_DIRS', [])
    def test_cli_only(self, mock_cli_path, _mock_version):
        """Returns CLI installation when binary exists."""
        mock_cli_path.is_file.return_value = True
        result = find_installations()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].name, 'CLI')
        self.assertEqual(result[0].version, '0.5.0')

    @patch('usage_monitor_for_codex.codex_cli.cli_version', return_value='0.5.0')
    @patch('usage_monitor_for_codex.codex_cli.CODEX_CLI_PATH')
    @patch('usage_monitor_for_codex.codex_cli._EXTENSION_DIRS', [])
    def test_cli_installation_is_codex_installation(self, mock_cli_path, _mock_version):
        """Discovered entries are CodexInstallation instances."""
        mock_cli_path.is_file.return_value = True
        result = find_installations()
        self.assertIsInstance(result[0], CodexInstallation)

    @patch('usage_monitor_for_codex.codex_cli.cli_version', return_value='')
    @patch('usage_monitor_for_codex.codex_cli.CODEX_CLI_PATH')
    @patch('usage_monitor_for_codex.codex_cli._EXTENSION_DIRS', [])
    def test_cli_exists_but_version_fails(self, mock_cli_path, _mock_version):
        """CLI binary exists but version command fails - not included."""
        mock_cli_path.is_file.return_value = True
        result = find_installations()
        self.assertEqual(result, [])

    @patch('usage_monitor_for_codex.codex_cli.cli_version', return_value='')
    @patch('usage_monitor_for_codex.codex_cli.CODEX_CLI_PATH')
    def test_vscode_extension(self, mock_cli_path, _mock_version):
        """Finds VS Code extension and extracts version from directory name."""
        mock_cli_path.is_file.return_value = False
        with TemporaryDirectory() as tmp:
            ext_dir = Path(tmp)
            (ext_dir / 'openai.codex-0.5.0-win32-x64').mkdir()
            with patch('usage_monitor_for_codex.codex_cli._EXTENSION_DIRS', [('VS Code', ext_dir)]):
                result = find_installations()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].name, 'VS Code')
        self.assertEqual(result[0].version, '0.5.0')

    @patch('usage_monitor_for_codex.codex_cli.cli_version', return_value='')
    @patch('usage_monitor_for_codex.codex_cli.CODEX_CLI_PATH')
    def test_picks_highest_version(self, mock_cli_path, _mock_version):
        """When multiple extension versions exist, picks the highest."""
        mock_cli_path.is_file.return_value = False
        with TemporaryDirectory() as tmp:
            ext_dir = Path(tmp)
            (ext_dir / 'openai.codex-0.4.3-win32-x64').mkdir()
            (ext_dir / 'openai.codex-0.5.0-win32-x64').mkdir()
            (ext_dir / 'openai.codex-0.4.6-win32-x64').mkdir()
            with patch('usage_monitor_for_codex.codex_cli._EXTENSION_DIRS', [('VS Code', ext_dir)]):
                result = find_installations()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].version, '0.5.0')

    @patch('usage_monitor_for_codex.codex_cli.cli_version', return_value='')
    @patch('usage_monitor_for_codex.codex_cli.CODEX_CLI_PATH')
    def test_ignores_non_codex_extensions(self, mock_cli_path, _mock_version):
        """Ignores directories that don't match the Codex extension prefix."""
        mock_cli_path.is_file.return_value = False
        with TemporaryDirectory() as tmp:
            ext_dir = Path(tmp)
            (ext_dir / 'some-other-extension-1.0.0').mkdir()
            (ext_dir / 'anthropic.claude-code-2.1.69-win32-x64').mkdir()
            (ext_dir / 'openai.codex-0.5.0-win32-x64').mkdir()
            with patch('usage_monitor_for_codex.codex_cli._EXTENSION_DIRS', [('VS Code', ext_dir)]):
                result = find_installations()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].version, '0.5.0')

    @patch('usage_monitor_for_codex.codex_cli.cli_version', return_value='')
    @patch('usage_monitor_for_codex.codex_cli.CODEX_CLI_PATH')
    def test_nonexistent_extension_dir_skipped(self, mock_cli_path, _mock_version):
        """Extension directories that don't exist are silently skipped."""
        mock_cli_path.is_file.return_value = False
        with patch('usage_monitor_for_codex.codex_cli._EXTENSION_DIRS', [('VS Code', Path('/nonexistent'))]):
            result = find_installations()
        self.assertEqual(result, [])

    @patch('usage_monitor_for_codex.codex_cli.cli_version', return_value='0.5.0')
    @patch('usage_monitor_for_codex.codex_cli.CODEX_CLI_PATH')
    def test_cli_and_extensions_combined(self, mock_cli_path, _mock_version):
        """Returns both CLI and extension installations, CLI first."""
        mock_cli_path.is_file.return_value = True
        with TemporaryDirectory() as tmp:
            ext_dir = Path(tmp)
            (ext_dir / 'openai.codex-0.4.8-win32-x64').mkdir()
            with patch('usage_monitor_for_codex.codex_cli._EXTENSION_DIRS', [('VS Code', ext_dir)]):
                result = find_installations()
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].name, 'CLI')
        self.assertEqual(result[1].name, 'VS Code')


# ---------------------------------------------------------------------------
# refresh_token
# ---------------------------------------------------------------------------

class TestRefreshToken(unittest.TestCase):
    """Tests for refresh_token().

    Unlike the Claude CLI, the Codex CLI has no non-interactive command that
    refreshes the OAuth token (``codex login`` is interactive), so
    ``refresh_token()`` is a deliberate no-op that always reports failure.
    """

    def test_always_reports_failure(self):
        """refresh_token() never succeeds for Codex."""
        result = refresh_token()
        self.assertFalse(result.success)

    def test_returns_refresh_result(self):
        """The return value is a RefreshResult."""
        self.assertIsInstance(refresh_token(), RefreshResult)

    def test_not_updated(self):
        """No CLI/token update is ever reported."""
        result = refresh_token()
        self.assertFalse(result.updated)
        self.assertEqual(result.old_version, '')
        self.assertEqual(result.new_version, '')

    def test_error_mentions_relogin(self):
        """The error explains that an interactive re-login is required."""
        result = refresh_token()
        self.assertTrue(result.error)
        self.assertIn('codex login', result.error)

    @patch('usage_monitor_for_codex.codex_cli.subprocess.run')
    def test_does_not_spawn_subprocess(self, mock_run):
        """refresh_token() never shells out (no headless refresh command exists)."""
        refresh_token()
        mock_run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
