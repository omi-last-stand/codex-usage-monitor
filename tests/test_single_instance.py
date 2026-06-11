"""
Single-Instance Tests
======================

Unit tests for the single-instance guard: shared memory round-trip,
ensure_single_instance control flow, and release_instance_lock.
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

MODULE = 'usage_monitor_for_codex.single_instance'


def _reset_globals():
    """Reset module-level handles to None between tests."""
    import usage_monitor_for_codex.single_instance as si
    si._mutex_handle = None
    si._pid_mapping_handle = None


# ---------------------------------------------------------------------------
# Shared memory round-trip
# ---------------------------------------------------------------------------

class TestSharedMemoryRoundTrip(unittest.TestCase):
    """Verify _store_holder_info / _read_holder_info write and read correctly."""

    def setUp(self):
        _reset_globals()

    def tearDown(self):
        import usage_monitor_for_codex.single_instance as si
        if si._pid_mapping_handle:
            si._kernel32.CloseHandle(si._pid_mapping_handle)
            si._pid_mapping_handle = None

    @patch(f'{MODULE}.__version__', '2.5.3')
    def test_round_trip_returns_pid_and_version(self):
        from usage_monitor_for_codex.single_instance import _read_holder_info, _store_holder_info

        _store_holder_info()
        pid, version = _read_holder_info()

        self.assertEqual(pid, os.getpid())
        self.assertEqual(version, '2.5.3')

    @patch(f'{MODULE}.__version__', '0.0.1')
    def test_round_trip_short_version(self):
        from usage_monitor_for_codex.single_instance import _read_holder_info, _store_holder_info

        _store_holder_info()
        pid, version = _read_holder_info()

        self.assertEqual(pid, os.getpid())
        self.assertEqual(version, '0.0.1')

    @patch(f'{MODULE}.__version__', 'a' * 100)
    def test_long_version_is_truncated(self):
        """Version strings exceeding shared memory size are truncated, not crashed."""
        from usage_monitor_for_codex.single_instance import _read_holder_info, _store_holder_info

        _store_holder_info()
        pid, version = _read_holder_info()

        self.assertEqual(pid, os.getpid())
        self.assertIsNotNone(version)
        self.assertTrue(len(version) < 100)


# ---------------------------------------------------------------------------
# ensure_single_instance
# ---------------------------------------------------------------------------

class TestEnsureSingleInstance(unittest.TestCase):
    """Tests for ensure_single_instance() control flow."""

    def setUp(self):
        _reset_globals()

    def tearDown(self):
        _reset_globals()

    @patch(f'{MODULE}._store_holder_info')
    @patch(f'{MODULE}.ctypes')
    def test_first_instance_returns_true(self, mock_ctypes, mock_store):
        """First instance (no duplicate) creates mutex and returns True."""
        mock_ctypes.get_last_error.return_value = 0
        mock_ctypes.WinDLL = MagicMock
        mock_kernel32 = MagicMock()
        mock_kernel32.CreateMutexW.return_value = 42

        with patch(f'{MODULE}._kernel32', mock_kernel32):
            from usage_monitor_for_codex.single_instance import ensure_single_instance
            result = ensure_single_instance()

        self.assertTrue(result)
        mock_store.assert_called_once()

    @patch(f'{MODULE}._terminate_process_handle', return_value=True)
    @patch(f'{MODULE}._open_pid_for_terminate', return_value=555)
    @patch(f'{MODULE}._read_holder_info', return_value=(99999, '1.9.0'))
    @patch(f'{MODULE}._store_holder_info')
    @patch(f'{MODULE}.ctypes')
    def test_duplicate_user_accepts_returns_true(self, mock_ctypes, mock_store, mock_read, mock_open, mock_terminate):
        """Duplicate detected, user clicks Yes - terminates old instance and returns True."""
        # 1st call: duplicate detected (ALREADY_EXISTS); 2nd call (after the mutex
        # is re-created): success, so the replacing instance proceeds.
        mock_ctypes.get_last_error.side_effect = [0xB7, 0]
        mock_kernel32 = MagicMock()
        mock_kernel32.CreateMutexW.return_value = 42

        mock_user32 = MagicMock()

        # The holder handle must be opened BEFORE the dialog blocks: an open
        # handle pins the PID, so it cannot be recycled to an unrelated process
        # while the dialog sits unanswered. Asserting from inside the dialog
        # call proves the ordering.
        def _dialog_with_order_check(*args):
            mock_open.assert_called_once_with(99999)
            return 6  # IDYES

        mock_user32.MessageBoxW.side_effect = _dialog_with_order_check

        with patch(f'{MODULE}._kernel32', mock_kernel32), \
             patch(f'{MODULE}.ctypes.windll.user32', mock_user32):
            from usage_monitor_for_codex.single_instance import ensure_single_instance
            result = ensure_single_instance()

        self.assertTrue(result)
        # Termination goes through the pre-opened handle, never the raw PID.
        mock_terminate.assert_called_once_with(555)
        mock_kernel32.CloseHandle.assert_any_call(555)
        self.assertEqual(mock_store.call_count, 1)

    @patch(f'{MODULE}._open_pid_for_terminate')
    @patch(f'{MODULE}._read_holder_info', return_value=(None, None))
    @patch(f'{MODULE}._store_holder_info')
    @patch(f'{MODULE}.ctypes')
    def test_duplicate_replace_fails_when_still_held(self, mock_ctypes, mock_store, mock_read, mock_open):
        """If the mutex is still held after the replace attempt (PID unknown or
        termination failed), do NOT start a duplicate: return False and don't store."""
        mock_ctypes.get_last_error.return_value = 0xB7  # still ALREADY_EXISTS after re-create
        mock_kernel32 = MagicMock()
        mock_kernel32.CreateMutexW.return_value = 42

        mock_user32 = MagicMock()
        mock_user32.MessageBoxW.return_value = 6  # IDYES

        with patch(f'{MODULE}._kernel32', mock_kernel32), \
             patch(f'{MODULE}.ctypes.windll.user32', mock_user32):
            from usage_monitor_for_codex.single_instance import ensure_single_instance
            result = ensure_single_instance()

        self.assertFalse(result)
        mock_store.assert_not_called()
        # No PID was known, so no process handle is ever opened.
        mock_open.assert_not_called()

    @patch(f'{MODULE}._read_holder_info')
    @patch(f'{MODULE}._store_holder_info')
    @patch(f'{MODULE}.ctypes')
    def test_mutex_create_failure_fails_closed(self, mock_ctypes, mock_store, mock_read):
        """CreateMutexW returning NULL (e.g. ERROR_ACCESS_DENIED because the
        mutex belongs to an ELEVATED instance) must NOT be treated as "first
        instance" - that would run two instances side by side and double-fire
        event commands. The guard fails closed: dialog + exit."""
        mock_ctypes.get_last_error.return_value = 5  # ERROR_ACCESS_DENIED
        mock_kernel32 = MagicMock()
        mock_kernel32.CreateMutexW.return_value = 0  # NULL: create/open failed

        mock_user32 = MagicMock()

        with patch(f'{MODULE}._kernel32', mock_kernel32), \
             patch(f'{MODULE}.ctypes.windll.user32', mock_user32):
            import usage_monitor_for_codex.single_instance as si
            result = si.ensure_single_instance()

        self.assertFalse(result)
        mock_store.assert_not_called()
        mock_read.assert_not_called()
        mock_user32.MessageBoxW.assert_called_once()
        self.assertIsNone(si._mutex_handle)

    @patch(f'{MODULE}._terminate_process_handle', return_value=True)
    @patch(f'{MODULE}._open_pid_for_terminate', return_value=555)
    @patch(f'{MODULE}._read_holder_info', return_value=(99999, '1.9.0'))
    @patch(f'{MODULE}._store_holder_info')
    @patch(f'{MODULE}.ctypes')
    def test_reacquire_create_failure_fails_closed(self, mock_ctypes, mock_store, mock_read, mock_open, mock_terminate):
        """If the post-replace re-acquire CreateMutexW fails outright (NULL),
        the old instance may still be alive behind an unopenable mutex - do
        not start a duplicate."""
        mock_ctypes.get_last_error.return_value = 0xB7
        mock_kernel32 = MagicMock()
        mock_kernel32.CreateMutexW.side_effect = [42, 0]  # ask-dialog pass, then NULL

        mock_user32 = MagicMock()
        mock_user32.MessageBoxW.return_value = 6  # IDYES

        with patch(f'{MODULE}._kernel32', mock_kernel32), \
             patch(f'{MODULE}.ctypes.windll.user32', mock_user32):
            import usage_monitor_for_codex.single_instance as si
            result = si.ensure_single_instance()

        self.assertFalse(result)
        mock_store.assert_not_called()
        # Ask dialog + replace_failed notice.
        self.assertEqual(mock_user32.MessageBoxW.call_count, 2)
        self.assertIsNone(si._mutex_handle)

    @patch(f'{MODULE}._open_pid_for_terminate', return_value=555)
    @patch(f'{MODULE}._read_holder_info', return_value=(99999, '1.9.0'))
    @patch(f'{MODULE}.ctypes')
    def test_duplicate_user_declines_returns_false(self, mock_ctypes, mock_read, mock_open):
        """Duplicate detected, user clicks No - returns False without terminating."""
        mock_ctypes.get_last_error.return_value = 0xB7
        mock_kernel32 = MagicMock()
        mock_kernel32.CreateMutexW.return_value = 42

        mock_user32 = MagicMock()
        mock_user32.MessageBoxW.return_value = 7  # IDNO

        with patch(f'{MODULE}._kernel32', mock_kernel32), \
             patch(f'{MODULE}.ctypes.windll.user32', mock_user32):
            from usage_monitor_for_codex.single_instance import ensure_single_instance
            result = ensure_single_instance()

        self.assertFalse(result)
        # The pre-opened holder handle is released on decline.
        mock_kernel32.CloseHandle.assert_any_call(555)

    @patch(f'{MODULE}._read_holder_info', return_value=(99999, '1.9.0'))
    @patch(f'{MODULE}.ctypes')
    def test_duplicate_dialog_title_includes_version(self, mock_ctypes, mock_read):
        """Dialog title includes the running version."""
        mock_ctypes.get_last_error.return_value = 0xB7
        mock_kernel32 = MagicMock()
        mock_kernel32.CreateMutexW.return_value = 42

        mock_user32 = MagicMock()
        mock_user32.MessageBoxW.return_value = 7  # IDNO

        with patch(f'{MODULE}._kernel32', mock_kernel32), \
             patch(f'{MODULE}.ctypes.windll.user32', mock_user32):
            from usage_monitor_for_codex.single_instance import ensure_single_instance
            ensure_single_instance()

        title = mock_user32.MessageBoxW.call_args[0][2]
        self.assertIn('v1.9.0', title)

    @patch(f'{MODULE}._read_holder_info', return_value=(99999, None))
    @patch(f'{MODULE}.ctypes')
    def test_duplicate_unknown_version_shows_question_mark(self, mock_ctypes, mock_read):
        """When version is unknown, message shows '?' as placeholder."""
        mock_ctypes.get_last_error.return_value = 0xB7
        mock_kernel32 = MagicMock()
        mock_kernel32.CreateMutexW.return_value = 42

        mock_user32 = MagicMock()
        mock_user32.MessageBoxW.return_value = 7  # IDNO

        with patch(f'{MODULE}._kernel32', mock_kernel32), \
             patch(f'{MODULE}.ctypes.windll.user32', mock_user32):
            from usage_monitor_for_codex.single_instance import ensure_single_instance
            ensure_single_instance()

        message = mock_user32.MessageBoxW.call_args[0][1]
        self.assertIn('?', message)


# ---------------------------------------------------------------------------
# release_instance_lock
# ---------------------------------------------------------------------------

class TestReleaseInstanceLock(unittest.TestCase):
    """Tests for release_instance_lock()."""

    def setUp(self):
        _reset_globals()

    def tearDown(self):
        _reset_globals()

    def test_release_closes_both_handles(self):
        """Both mutex and mapping handles are closed and set to None."""
        import usage_monitor_for_codex.single_instance as si
        mock_kernel32 = MagicMock()

        si._mutex_handle = 100
        si._pid_mapping_handle = 200

        with patch(f'{MODULE}._kernel32', mock_kernel32):
            si.release_instance_lock()

        self.assertIsNone(si._mutex_handle)
        self.assertIsNone(si._pid_mapping_handle)
        self.assertEqual(mock_kernel32.CloseHandle.call_count, 2)

    def test_release_with_no_handles_is_safe(self):
        """Calling release when no handles are held does not crash."""
        import usage_monitor_for_codex.single_instance as si

        si._mutex_handle = None
        si._pid_mapping_handle = None
        si.release_instance_lock()

        self.assertIsNone(si._mutex_handle)
        self.assertIsNone(si._pid_mapping_handle)


if __name__ == '__main__':
    unittest.main()
