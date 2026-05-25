"""
Widget State Tests
==================

Unit tests for the resident widget's INI persistence: window position,
the always-on-top preference, and per-field display states.  Each test
points ``ini_path()`` at a temporary file so the real state file is
never touched.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import usage_monitor_for_codex.widget_state as ws


class _TempIni(unittest.TestCase):
    """Base class that redirects ini_path() to a temp file."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ini = Path(tmp.name) / 'CodexUsageMonitor.ini'
        patcher = patch.object(ws, 'ini_path', return_value=self.ini)
        patcher.start()
        self.addCleanup(patcher.stop)


class TestAlwaysOnTop(_TempIni):
    """Tests for save_always_on_top() / load_widget_state().always_on_top."""

    def test_missing_returns_none(self):
        """No INI file means the preference is unset (None)."""
        self.assertIsNone(ws.load_widget_state().always_on_top)

    def test_roundtrip_true(self):
        """Saving True reads back as True."""
        ws.save_always_on_top(True)
        self.assertIs(ws.load_widget_state().always_on_top, True)

    def test_roundtrip_false(self):
        """Saving False reads back as False (not None)."""
        ws.save_always_on_top(False)
        self.assertIs(ws.load_widget_state().always_on_top, False)

    def test_invalid_value_returns_none(self):
        """A non-boolean value falls back to None instead of raising."""
        self.ini.write_text('[widget]\nalways_on_top = maybe\n', encoding='utf-8')
        self.assertIsNone(ws.load_widget_state().always_on_top)

    def test_overwrite(self):
        """The latest saved value wins."""
        ws.save_always_on_top(True)
        ws.save_always_on_top(False)
        self.assertIs(ws.load_widget_state().always_on_top, False)


class TestCrossPreservation(_TempIni):
    """Each save must preserve the other sections of the INI."""

    def test_saving_aot_preserves_position(self):
        """Saving the preference does not wipe the window position."""
        ws.save_window_position(100, 200)
        ws.save_always_on_top(False)
        state = ws.load_widget_state()
        self.assertEqual((state.window_x, state.window_y), (100, 200))
        self.assertIs(state.always_on_top, False)

    def test_saving_position_preserves_aot(self):
        """Saving the position does not wipe the preference."""
        ws.save_always_on_top(True)
        ws.save_window_position(5, 6)
        state = ws.load_widget_state()
        self.assertIs(state.always_on_top, True)
        self.assertEqual((state.window_x, state.window_y), (5, 6))

    def test_saving_aot_preserves_fields(self):
        """Saving the preference does not wipe field display states."""
        ws.save_field_config([('five_hour', ws.FIELD_VISIBLE), ('seven_day', ws.FIELD_HIDDEN)])
        ws.save_always_on_top(True)
        state = ws.load_widget_state()
        self.assertEqual(state.field_states, {'five_hour': 'visible', 'seven_day': 'hidden'})
        self.assertIs(state.always_on_top, True)


class TestWidgetStateDefaults(unittest.TestCase):
    """The always_on_top field is optional for backward compatibility."""

    def test_always_on_top_defaults_to_none(self):
        """Constructing with three positional args leaves the preference unset."""
        state = ws.WidgetState(None, None, {})
        self.assertIsNone(state.always_on_top)


class TestExpanded(_TempIni):
    """Tests for save_expanded() / load_widget_state().expanded."""

    def test_missing_returns_none(self):
        """No INI file means the view state is unset (None)."""
        self.assertIsNone(ws.load_widget_state().expanded)

    def test_roundtrip_true(self):
        """Saving True (expanded) reads back as True."""
        ws.save_expanded(True)
        self.assertIs(ws.load_widget_state().expanded, True)

    def test_roundtrip_false(self):
        """Saving False (compact) reads back as False (not None)."""
        ws.save_expanded(False)
        self.assertIs(ws.load_widget_state().expanded, False)

    def test_preserves_other_widget_settings(self):
        """Saving the view state keeps the always-on-top preference intact."""
        ws.save_always_on_top(True)
        ws.save_expanded(True)
        state = ws.load_widget_state()
        self.assertIs(state.expanded, True)
        self.assertIs(state.always_on_top, True)


class TestLanguage(_TempIni):
    """Tests for save_language() / load_language()."""

    def test_missing_returns_empty(self):
        """No saved language means follow the system/JSON setting (empty string)."""
        self.assertEqual(ws.load_language(), '')

    def test_roundtrip(self):
        """A saved language code reads back unchanged."""
        ws.save_language('ja')
        self.assertEqual(ws.load_language(), 'ja')

    def test_empty_code_clears_the_setting(self):
        """Saving an empty code removes the override (back to system default)."""
        ws.save_language('de')
        ws.save_language('')
        self.assertEqual(ws.load_language(), '')

    def test_preserves_other_widget_settings(self):
        """Saving the language keeps the always-on-top preference intact."""
        ws.save_always_on_top(False)
        ws.save_language('pt-BR')
        self.assertEqual(ws.load_language(), 'pt-BR')
        self.assertIs(ws.load_widget_state().always_on_top, False)


if __name__ == '__main__':
    unittest.main()
