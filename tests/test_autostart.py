"""
Autostart Tests
================

Unit tests for Windows autostart management via a Startup-folder shortcut.
The registry is never touched (project policy); persistence is a ``.lnk``
file placed in the user's Startup folder.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import usage_monitor_for_codex.autostart as autostart_mod


class TestShortcutPath(unittest.TestCase):
    """Tests for _shortcut_path()."""

    @patch.object(autostart_mod, '_known_folder_startup', return_value=Path(r'D:\Redirected\Startup'))
    def test_known_folder_wins(self, _mock_kf):
        """The SHGetKnownFolderPath result is authoritative: with GPO Folder
        Redirection the real Startup folder is NOT under %APPDATA%, and a
        shortcut written there would never run at logon."""
        self.assertEqual(
            autostart_mod._shortcut_path(),
            Path(r'D:\Redirected\Startup') / 'CodexUsageMonitor.lnk',
        )

    @patch.object(autostart_mod, '_known_folder_startup', return_value=None)
    @patch.dict('os.environ', {'APPDATA': r'C:\Users\me\AppData\Roaming'})
    def test_falls_back_to_appdata_when_api_unavailable(self, _mock_kf):
        """Without the known-folder API the legacy %APPDATA% composition is used."""
        path = autostart_mod._shortcut_path()

        self.assertEqual(
            path,
            Path(r'C:\Users\me\AppData\Roaming')
            / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs' / 'Startup'
            / 'CodexUsageMonitor.lnk',
        )

    @patch.object(autostart_mod, '_known_folder_startup', return_value=None)
    @patch.dict('os.environ', {'APPDATA': r'C:\Users\me\AppData\Roaming'})
    def test_uses_lnk_filename(self, _mock_kf):
        """Shortcut file name ends in .lnk."""
        self.assertEqual(autostart_mod._shortcut_path().name, 'CodexUsageMonitor.lnk')

    @unittest.skipUnless(sys.platform == 'win32', 'Windows-only API')
    def test_known_folder_resolves_on_windows(self):
        """The real SHGetKnownFolderPath call returns the Startup folder."""
        folder = autostart_mod._known_folder_startup()
        self.assertIsNotNone(folder)
        assert folder is not None
        self.assertEqual(folder.name.lower(), 'startup')


class TestPsLiteral(unittest.TestCase):
    """Tests for _ps_literal() PowerShell single-quoted escaping."""

    def test_wraps_in_single_quotes(self):
        """Plain string is wrapped in single quotes."""
        self.assertEqual(autostart_mod._ps_literal(r'C:\App\app.exe'), r"'C:\App\app.exe'")

    def test_doubles_embedded_single_quotes(self):
        """Embedded single quotes are doubled (PowerShell escaping)."""
        self.assertEqual(autostart_mod._ps_literal("a'b"), "'a''b'")


class TestIsAutostartEnabled(unittest.TestCase):
    """Tests for is_autostart_enabled()."""

    @patch.object(Path, 'is_file', return_value=True)
    def test_returns_true_when_shortcut_exists(self, mock_is_file):
        """Existing Startup shortcut returns True."""
        self.assertTrue(autostart_mod.is_autostart_enabled())
        mock_is_file.assert_called_once()

    @patch.object(Path, 'is_file', return_value=False)
    def test_returns_false_when_shortcut_missing(self, mock_is_file):
        """Missing Startup shortcut returns False."""
        self.assertFalse(autostart_mod.is_autostart_enabled())
        mock_is_file.assert_called_once()

    @patch.dict('os.environ', {'APPDATA': r'C:\Users\me\AppData\Roaming'})
    def test_checks_the_startup_shortcut_path(self):
        """Existence is checked on the Startup-folder shortcut path."""
        with patch.object(autostart_mod, '_shortcut_path') as mock_path:
            mock_path.return_value = MagicMock(spec=Path)
            mock_path.return_value.is_file.return_value = True

            self.assertTrue(autostart_mod.is_autostart_enabled())

            mock_path.assert_called_once_with()
            mock_path.return_value.is_file.assert_called_once_with()


class TestSetAutostart(unittest.TestCase):
    """Tests for set_autostart()."""

    @patch.object(autostart_mod, 'subprocess')
    def test_enable_invokes_powershell(self, mock_subprocess):
        """Enabling autostart runs PowerShell to create the shortcut."""
        autostart_mod.set_autostart(True)

        mock_subprocess.run.assert_called_once()
        argv = mock_subprocess.run.call_args[0][0]
        self.assertEqual(argv[0], 'powershell')
        self.assertIn('-Command', argv)

    @patch.object(autostart_mod, 'subprocess')
    def test_enable_builds_create_shortcut_script(self, mock_subprocess):
        """The PowerShell script creates a WScript.Shell shortcut and saves it."""
        with patch.object(sys, 'executable', r'C:\Program Files\MyApp\app.exe'):
            autostart_mod.set_autostart(True)

        argv = mock_subprocess.run.call_args[0][0]
        script = argv[argv.index('-Command') + 1]
        self.assertIn('CreateShortcut', script)
        self.assertIn('$s.Save()', script)
        # The target executable is embedded as a PowerShell literal.
        self.assertIn(autostart_mod._ps_literal(r'C:\Program Files\MyApp\app.exe'), script)

    @patch.object(autostart_mod, 'subprocess')
    def test_enable_targets_current_executable(self, mock_subprocess):
        """The shortcut target is sys.executable."""
        with patch.object(sys, 'executable', r'C:\Python\pythonw.exe'):
            autostart_mod.set_autostart(True)

        script = mock_subprocess.run.call_args[0][0][-1]
        self.assertIn(r'C:\Python\pythonw.exe', script)

    @patch.object(autostart_mod, 'subprocess')
    def test_enable_writes_to_startup_shortcut_path(self, mock_subprocess):
        """The shortcut is created at the Startup-folder path."""
        with patch.object(autostart_mod, '_shortcut_path', return_value=Path(r'C:\Startup\Widget.lnk')):
            autostart_mod.set_autostart(True)

        script = mock_subprocess.run.call_args[0][0][-1]
        self.assertIn(autostart_mod._ps_literal(r'C:\Startup\Widget.lnk'), script)

    @patch.object(autostart_mod, 'subprocess')
    @patch.object(Path, 'unlink')
    def test_disable_removes_shortcut(self, mock_unlink, mock_subprocess):
        """Disabling autostart deletes the shortcut and does not run PowerShell."""
        autostart_mod.set_autostart(False)

        mock_unlink.assert_called_once()
        mock_subprocess.run.assert_not_called()

    @patch.object(autostart_mod, 'subprocess')
    def test_disable_unlinks_startup_shortcut_path(self, mock_subprocess):
        """Disable unlinks exactly the Startup-folder shortcut path."""
        fake_path = MagicMock(spec=Path)
        with patch.object(autostart_mod, '_shortcut_path', return_value=fake_path):
            autostart_mod.set_autostart(False)

        fake_path.unlink.assert_called_once_with()
        mock_subprocess.run.assert_not_called()

    @patch.object(autostart_mod, 'subprocess')
    @patch.object(Path, 'unlink', side_effect=FileNotFoundError)
    def test_disable_ignores_missing_shortcut(self, mock_unlink, mock_subprocess):
        """Disabling when the shortcut is already absent does not raise."""
        autostart_mod.set_autostart(False)  # should not raise

        mock_unlink.assert_called_once()


class TestSyncAutostartPath(unittest.TestCase):
    """Tests for sync_autostart_path()."""

    @patch.object(autostart_mod, 'set_autostart')
    @patch.object(autostart_mod, 'is_autostart_enabled', return_value=False)
    def test_no_op_when_disabled(self, mock_enabled, mock_set):
        """No update attempted when autostart is not enabled."""
        autostart_mod.sync_autostart_path()  # should not raise

        mock_set.assert_not_called()

    @patch.object(autostart_mod, 'set_autostart')
    @patch.object(autostart_mod, 'is_autostart_enabled', return_value=True)
    def test_recreates_shortcut_when_enabled(self, mock_enabled, mock_set):
        """When enabled, the shortcut is recreated for the current executable."""
        autostart_mod.sync_autostart_path()

        mock_set.assert_called_once_with(True)


if __name__ == '__main__':
    unittest.main()
