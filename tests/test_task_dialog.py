"""
Task Dialog Tests
=================

Unit tests for the ``TaskDialogIndirect`` wrapper used by the About box.
The native dialog is modal UI, so these tests mock the Win32 calls and
exercise the structure layout, hyperlink stripping, and MessageBox fallback.
"""
from __future__ import annotations

import ctypes
import unittest
from unittest.mock import MagicMock, patch

import usage_monitor_for_codex.task_dialog as td


class TestStripHyperlinks(unittest.TestCase):
    """Tests for _strip_hyperlinks()."""

    def test_replaces_anchor_with_visible_text(self):
        """An <a> tag collapses to its visible text."""
        out = td._strip_hyperlinks('see <a href="https://x.test/a">https://x.test/a</a> end')
        self.assertEqual(out, 'see https://x.test/a end')

    def test_leaves_plain_text_untouched(self):
        """Text without links is returned unchanged."""
        self.assertEqual(td._strip_hyperlinks('no links here'), 'no links here')

    def test_handles_multiple_links(self):
        """Every anchor in the string is collapsed."""
        out = td._strip_hyperlinks('<a href="u1">one</a> and <a href="u2">two</a>')
        self.assertEqual(out, 'one and two')


class TestStructLayout(unittest.TestCase):
    """Tests guarding the TASKDIALOGCONFIG ABI."""

    def test_struct_is_byte_packed(self):
        """commctrl.h packs the struct to 1 byte; natural alignment corrupts it."""
        self.assertEqual(td._TASKDIALOGCONFIG._pack_, 1)

    def test_struct_size_is_computable(self):
        """The struct has a concrete size used for cbSize."""
        self.assertGreater(ctypes.sizeof(td._TASKDIALOGCONFIG), 0)


class TestShowInfoDialog(unittest.TestCase):
    """Tests for show_info_dialog()."""

    def test_uses_task_dialog_when_available(self):
        """A working TaskDialogIndirect is used and MessageBox is not."""
        fake_comctl32 = MagicMock()
        fake_comctl32.TaskDialogIndirect.return_value = 0  # S_OK
        fake_user32 = MagicMock()
        with patch.object(ctypes, 'windll', MagicMock(comctl32=fake_comctl32, user32=fake_user32)):
            result = td.show_info_dialog(123, 'title', 'heading', 'body')

        self.assertTrue(result)
        fake_comctl32.TaskDialogIndirect.assert_called_once()
        fake_user32.MessageBoxW.assert_not_called()

    def test_falls_back_when_api_missing(self):
        """Without TaskDialogIndirect (comctl32 v5), MessageBox is used."""
        fake_comctl32 = MagicMock(spec=[])  # no TaskDialogIndirect attribute
        fake_user32 = MagicMock()
        with patch.object(ctypes, 'windll', MagicMock(comctl32=fake_comctl32, user32=fake_user32)):
            result = td.show_info_dialog(0, 'title', 'heading', 'x <a href="u">u</a>')

        self.assertFalse(result)
        fake_user32.MessageBoxW.assert_called_once()
        text_arg = fake_user32.MessageBoxW.call_args[0][1]
        self.assertIn('u', text_arg)
        self.assertNotIn('<a href', text_arg)

    def test_falls_back_when_task_dialog_errors(self):
        """A failing HRESULT triggers the MessageBox fallback."""
        fake_comctl32 = MagicMock()
        fake_comctl32.TaskDialogIndirect.return_value = -2147467259  # E_FAIL
        fake_user32 = MagicMock()
        with patch.object(ctypes, 'windll', MagicMock(comctl32=fake_comctl32, user32=fake_user32)):
            result = td.show_info_dialog(0, 'title', '', 'body')

        self.assertFalse(result)
        fake_user32.MessageBoxW.assert_called_once()


if __name__ == '__main__':
    unittest.main()
