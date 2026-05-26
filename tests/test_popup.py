"""
Popup Tests
=============

Unit tests for popup data helpers: _usage_entries, _snapshot_to_dict,
and _init_config.
"""
from __future__ import annotations

import ctypes
import unittest
from unittest.mock import MagicMock, patch

from usage_monitor_for_codex.cache import CacheSnapshot
from usage_monitor_for_codex.popup import SettingsWindow, UsagePopup, _BASELINE_DPI, _MONITORINFO, _field_config_changed, _init_config, _snapshot_to_dict, _usage_entries, resolve_block_order, resolve_field_order


def _snap(
    usage=None, profile=None, last_success_time=None,
    refreshing=False, last_error=None, version=1,
) -> CacheSnapshot:
    """Build a CacheSnapshot with convenient defaults."""
    return CacheSnapshot(
        usage=usage or {},
        profile=profile,
        last_success_time=last_success_time,
        refreshing=refreshing,
        last_error=last_error,
        version=version,
    )


# ---------------------------------------------------------------------------
# _usage_entries
# ---------------------------------------------------------------------------

class TestUsageEntries(unittest.TestCase):
    """Tests for _usage_entries - extracts (key, label, entry, period, state) tuples."""

    def test_returns_entries_for_active_fields(self):
        """Returns entries only for non-null fields with utilization."""
        usage = {
            'five_hour': {'utilization': 42, 'resets_at': '2026-01-01T00:00:00Z'},
            'seven_day': {'utilization': 10, 'resets_at': '2026-01-07T00:00:00Z'},
            'seven_day_sonnet': None,
        }
        entries = _usage_entries(usage)
        self.assertEqual(len(entries), 2)

    def test_labels_use_popup_label(self):
        """Each entry's label is generated via popup_label."""
        from usage_monitor_for_codex.formatting import popup_label

        usage = {
            'five_hour': {'utilization': 42, 'resets_at': '2026-01-01T00:00:00Z'},
            'seven_day': {'utilization': 10, 'resets_at': '2026-01-07T00:00:00Z'},
        }
        entries = _usage_entries(usage)
        labels = [e[1] for e in entries]
        self.assertEqual(labels, [popup_label('five_hour'), popup_label('seven_day')])

    def test_periods_derived_from_field_name(self):
        """Period is derived from the field name via field_period."""
        usage = {
            'five_hour': {'utilization': 42, 'resets_at': '2026-01-01T00:00:00Z'},
            'seven_day': {'utilization': 10, 'resets_at': '2026-01-07T00:00:00Z'},
        }
        entries = _usage_entries(usage)
        periods = [e[3] for e in entries]
        self.assertEqual(periods, [5 * 3600, 7 * 24 * 3600])

    def test_data_extraction(self):
        """Entry data is pulled from the correct usage dict keys."""
        five_hour = {'utilization': 42, 'resets_at': '2026-01-01T00:00:00Z'}
        seven_day = {'utilization': 10, 'resets_at': '2026-01-07T00:00:00Z'}
        usage = {'five_hour': five_hour, 'seven_day': seven_day}

        entries = _usage_entries(usage)
        self.assertEqual(len(entries), 2)
        self.assertIs(entries[0][2], five_hour)
        self.assertIs(entries[1][2], seven_day)

    def test_empty_usage_returns_empty(self):
        """Empty usage dict returns no entries."""
        self.assertEqual(_usage_entries({}), [])

    def test_all_null_fields_returns_empty(self):
        """All-null fields return no entries."""
        usage = {'five_hour': None, 'seven_day': None, 'seven_day_sonnet': None}
        self.assertEqual(_usage_entries(usage), [])

    def test_null_utilization_skipped(self):
        """Fields with utilization None are skipped."""
        usage = {
            'five_hour': {'utilization': None, 'resets_at': '2026-01-01T05:00:00Z'},
            'seven_day': {'utilization': 20, 'resets_at': '2026-01-07T00:00:00Z'},
        }
        entries = _usage_entries(usage)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0][2]['utilization'], 20)

    @patch('usage_monitor_for_codex.popup.POPUP_FIELDS', ['fve_hour', 'seven_day'])
    def test_misspelled_popup_field_skipped(self):
        """Misspelled popup_fields entry is skipped, valid one shown."""
        usage = {
            'five_hour': {'utilization': 42, 'resets_at': '2026-01-01T05:00:00Z'},
            'seven_day': {'utilization': 20, 'resets_at': '2026-01-07T00:00:00Z'},
        }
        entries = _usage_entries(usage)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0][2]['utilization'], 20)

    @patch('usage_monitor_for_codex.popup.POPUP_FIELDS', ['seven_day_sonnet'])
    def test_popup_field_pointing_to_null_skipped(self):
        """popup_fields entry pointing to a null field produces no entries."""
        usage = {'seven_day_sonnet': None, 'five_hour': {'utilization': 42, 'resets_at': ''}}
        entries = _usage_entries(usage)
        self.assertEqual(entries, [])

    def test_non_dict_values_in_usage_ignored(self):
        """Non-dict values (like error strings) in usage are ignored."""
        usage = {
            'error': 'server down',
            'five_hour': {'utilization': 42, 'resets_at': '2026-01-01T05:00:00Z'},
        }
        entries = _usage_entries(usage)
        self.assertEqual(len(entries), 1)

    def test_extra_usage_not_shown_as_bar(self):
        """extra_usage is excluded from dynamic bars (different structure)."""
        usage = {
            'five_hour': {'utilization': 42, 'resets_at': '2026-01-01T05:00:00Z'},
            'extra_usage': {'is_enabled': True, 'monthly_limit': 1000, 'used_credits': 500, 'utilization': 50},
        }
        entries = _usage_entries(usage)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0][2]['utilization'], 42)

    def test_keys_are_field_names(self):
        """Each entry's first element is the API field key, in order."""
        usage = {
            'five_hour': {'utilization': 42, 'resets_at': '2026-01-01T00:00:00Z'},
            'seven_day': {'utilization': 10, 'resets_at': '2026-01-07T00:00:00Z'},
        }
        self.assertEqual([e[0] for e in _usage_entries(usage)], ['five_hour', 'seven_day'])

    def test_state_defaults_to_visible(self):
        """Without saved field_states, every entry is visible."""
        usage = {
            'five_hour': {'utilization': 42, 'resets_at': '2026-01-01T00:00:00Z'},
            'seven_day': {'utilization': 10, 'resets_at': '2026-01-07T00:00:00Z'},
        }
        self.assertEqual([e[4] for e in _usage_entries(usage)], ['visible', 'visible'])


class TestUsageEntriesFieldStates(unittest.TestCase):
    """Tests for _usage_entries with a saved 3-state field config."""

    @staticmethod
    def _usage():
        return {
            'five_hour': {'utilization': 42, 'resets_at': '2026-01-01T00:00:00Z'},
            'seven_day': {'utilization': 10, 'resets_at': '2026-01-07T00:00:00Z'},
        }

    def test_states_applied(self):
        """Saved states set each entry's state."""
        entries = _usage_entries(self._usage(), {'five_hour': 'collapsed', 'seven_day': 'visible'})
        self.assertEqual({e[0]: e[4] for e in entries}, {'five_hour': 'collapsed', 'seven_day': 'visible'})

    def test_hidden_excluded(self):
        """Fields marked hidden are dropped; unconfigured fields still show."""
        keys = [e[0] for e in _usage_entries(self._usage(), {'five_hour': 'hidden'})]
        self.assertNotIn('five_hour', keys)
        self.assertIn('seven_day', keys)

    def test_saved_order_respected(self):
        """Entry order follows the saved field order."""
        entries = _usage_entries(self._usage(), {'seven_day': 'visible', 'five_hour': 'visible'})
        self.assertEqual([e[0] for e in entries], ['seven_day', 'five_hour'])

    def test_unconfigured_fields_default_visible_and_appended(self):
        """Fields absent from the config default to visible and follow the rest."""
        entries = _usage_entries(self._usage(), {'seven_day': 'collapsed'})
        self.assertEqual([(e[0], e[4]) for e in entries], [('seven_day', 'collapsed'), ('five_hour', 'visible')])

    def test_stale_saved_field_dropped(self):
        """A saved field the API no longer returns is ignored."""
        keys = [e[0] for e in _usage_entries(self._usage(), {'opus': 'visible', 'five_hour': 'visible'})]
        self.assertNotIn('opus', keys)

    def test_include_hidden_keeps_hidden_fields(self):
        """resolve_field_order(include_hidden=True) keeps hidden fields for the settings UI."""
        ordered = resolve_field_order(self._usage(), {'five_hour': 'hidden'}, include_hidden=True)
        self.assertIn(('five_hour', 'hidden'), ordered)
        # The default (rendering) path still drops it.
        self.assertNotIn(('five_hour', 'hidden'), resolve_field_order(self._usage(), {'five_hour': 'hidden'}))


# ---------------------------------------------------------------------------
# _snapshot_to_dict
# ---------------------------------------------------------------------------

class TestSnapshotToDict(unittest.TestCase):
    """Tests for _snapshot_to_dict - converts CacheSnapshot to popup JSON."""

    # -- profile --

    def test_no_profile(self):
        """Profile is None when snapshot has no profile."""
        result = _snapshot_to_dict(_snap(), installations=[])
        self.assertIsNone(result['profile'])

    def test_profile_extraction(self):
        """Email and plan are extracted from nested account/organization dicts."""
        profile = {
            'account': {'email': 'test@example.com'},
            'organization': {'organization_type': 'pro_team'},
        }
        result = _snapshot_to_dict(_snap(profile=profile), installations=[])
        self.assertEqual(result['profile']['email'], 'test@example.com')
        self.assertEqual(result['profile']['plan'], 'Pro Team')

    def test_empty_profile_hidden(self):
        """Empty profile dict from API is treated as absent (no broken UI)."""
        result = _snapshot_to_dict(_snap(profile={}), installations=[])
        self.assertIsNone(result['profile'])

    def test_profile_missing_nested_keys(self):
        """Present but incomplete profile defaults missing fields to empty strings."""
        result = _snapshot_to_dict(_snap(profile={'account': {}}), installations=[])
        self.assertEqual(result['profile']['email'], '')
        self.assertEqual(result['profile']['plan'], '')

    # -- usage bars --

    def test_no_usage_data(self):
        """Empty usage dict produces empty usage list."""
        result = _snapshot_to_dict(_snap(), installations=[])
        self.assertEqual(result['usage'], [])

    def test_skips_entries_without_utilization(self):
        """Entries with None utilization are omitted."""
        usage = {'five_hour': {'utilization': None}}
        result = _snapshot_to_dict(_snap(usage=usage), installations=[])
        self.assertEqual(result['usage'], [])

    def test_skips_missing_entries(self):
        """Missing usage keys produce no bar entries."""
        usage = {'five_hour': None}
        result = _snapshot_to_dict(_snap(usage=usage), installations=[])
        self.assertEqual(result['usage'], [])

    @patch('usage_monitor_for_codex.popup.elapsed_pct', return_value=None)
    @patch('usage_monitor_for_codex.popup.time_until', return_value='5h 0m')
    @patch('usage_monitor_for_codex.popup.midnight_positions', return_value=[])
    def test_usage_bar_fields(self, _mock_midnights, _mock_time_until, _mock_elapsed):
        """Each usage bar dict has all required fields with correct types."""
        usage = {'five_hour': {'utilization': 42, 'resets_at': '2026-01-01T05:00:00Z'}}
        result = _snapshot_to_dict(_snap(usage=usage), installations=[])

        self.assertEqual(len(result['usage']), 1)
        bar = result['usage'][0]
        self.assertEqual(bar['pct_text'], '42%')
        self.assertAlmostEqual(bar['fill_pct'], 0.42)
        self.assertFalse(bar['warn'])
        self.assertIsNone(bar['marker_rel'])
        self.assertEqual(bar['reset_text'], '5h 0m')
        self.assertEqual(bar['midnights'], [])

    @patch('usage_monitor_for_codex.popup.elapsed_pct', return_value=30.0)
    @patch('usage_monitor_for_codex.popup.time_until', return_value='3h 30m')
    @patch('usage_monitor_for_codex.popup.midnight_positions', return_value=[0.5])
    def test_warn_when_usage_ahead_of_time(self, _mock_midnights, _mock_time_until, _mock_elapsed):
        """Bar is marked warn when utilization exceeds elapsed percentage."""
        usage = {'five_hour': {'utilization': 60, 'resets_at': '2026-01-01T05:00:00Z'}}
        result = _snapshot_to_dict(_snap(usage=usage), installations=[])

        bar = result['usage'][0]
        self.assertTrue(bar['warn'])
        self.assertAlmostEqual(bar['marker_rel'], 0.3)

    @patch('usage_monitor_for_codex.popup.elapsed_pct', return_value=80.0)
    @patch('usage_monitor_for_codex.popup.time_until', return_value='1h 0m')
    @patch('usage_monitor_for_codex.popup.midnight_positions', return_value=[])
    def test_no_warn_when_usage_behind_time(self, _mock_midnights, _mock_time_until, _mock_elapsed):
        """Bar is not warn when utilization is below elapsed percentage."""
        usage = {'five_hour': {'utilization': 40, 'resets_at': '2026-01-01T05:00:00Z'}}
        result = _snapshot_to_dict(_snap(usage=usage), installations=[])

        bar = result['usage'][0]
        self.assertFalse(bar['warn'])

    @patch('usage_monitor_for_codex.popup.elapsed_pct', return_value=50.0)
    @patch('usage_monitor_for_codex.popup.time_until', return_value='2h 30m')
    @patch('usage_monitor_for_codex.popup.midnight_positions', return_value=[])
    def test_no_warn_when_equal(self, _mock_midnights, _mock_time_until, _mock_elapsed):
        """Exactly equal usage and elapsed is not a warning (strictly greater)."""
        usage = {'five_hour': {'utilization': 50, 'resets_at': '2026-01-01T05:00:00Z'}}
        result = _snapshot_to_dict(_snap(usage=usage), installations=[])
        self.assertFalse(result['usage'][0]['warn'])

    @patch('usage_monitor_for_codex.popup.elapsed_pct', return_value=None)
    @patch('usage_monitor_for_codex.popup.time_until', return_value='')
    @patch('usage_monitor_for_codex.popup.midnight_positions', return_value=[])
    def test_warn_at_100_without_time_period(self, _mock_midnights, _mock_time_until, _mock_elapsed):
        """Bar at 100% is warn even when no time period (time_pct is None)."""
        usage = {'five_hour': {'utilization': 100, 'resets_at': ''}}
        result = _snapshot_to_dict(_snap(usage=usage), installations=[])
        self.assertTrue(result['usage'][0]['warn'])

    @patch('usage_monitor_for_codex.popup.elapsed_pct', return_value=100.0)
    @patch('usage_monitor_for_codex.popup.time_until', return_value='')
    @patch('usage_monitor_for_codex.popup.midnight_positions', return_value=[])
    def test_warn_at_100_when_time_also_100(self, _mock_midnights, _mock_time_until, _mock_elapsed):
        """Bar at 100% is warn even when elapsed time is also 100% (strict > would miss this)."""
        usage = {'five_hour': {'utilization': 100, 'resets_at': '2026-01-01T05:00:00Z'}}
        result = _snapshot_to_dict(_snap(usage=usage), installations=[])
        self.assertTrue(result['usage'][0]['warn'])

    @patch('usage_monitor_for_codex.popup.elapsed_pct', return_value=None)
    @patch('usage_monitor_for_codex.popup.time_until', return_value='')
    @patch('usage_monitor_for_codex.popup.midnight_positions', return_value=[])
    def test_fill_pct_clamped_to_0_1(self, _mock_midnights, _mock_time_until, _mock_elapsed):
        """Fill percentage is clamped between 0.0 and 1.0, and over-quota is always warn."""
        usage = {'five_hour': {'utilization': 150, 'resets_at': '2026-01-01T05:00:00Z'}}
        result = _snapshot_to_dict(_snap(usage=usage), installations=[])
        self.assertEqual(result['usage'][0]['fill_pct'], 1.0)
        self.assertTrue(result['usage'][0]['warn'])

    @patch('usage_monitor_for_codex.popup.elapsed_pct', return_value=None)
    @patch('usage_monitor_for_codex.popup.time_until', return_value='')
    @patch('usage_monitor_for_codex.popup.midnight_positions', return_value=[])
    def test_zero_utilization(self, _mock_midnights, _mock_time_until, _mock_elapsed):
        """Zero utilization produces 0% text and 0.0 fill."""
        usage = {'five_hour': {'utilization': 0, 'resets_at': '2026-01-01T05:00:00Z'}}
        result = _snapshot_to_dict(_snap(usage=usage), installations=[])
        # utilization 0 is falsy, so `or 0` kicks in - entry is still shown
        bar = result['usage'][0]
        self.assertEqual(bar['pct_text'], '0%')
        self.assertAlmostEqual(bar['fill_pct'], 0.0)

    @patch('usage_monitor_for_codex.popup.elapsed_pct', return_value=None)
    @patch('usage_monitor_for_codex.popup.time_until', return_value='')
    @patch('usage_monitor_for_codex.popup.midnight_positions', return_value=[])
    def test_multiple_usage_entries(self, _mock_midnights, _mock_time_until, _mock_elapsed):
        """Multiple usage types each produce a bar entry."""
        usage = {
            'five_hour': {'utilization': 10, 'resets_at': '2026-01-01T05:00:00Z'},
            'seven_day': {'utilization': 20, 'resets_at': '2026-01-07T00:00:00Z'},
            'seven_day_sonnet': {'utilization': 30, 'resets_at': '2026-01-07T00:00:00Z'},
        }
        result = _snapshot_to_dict(_snap(usage=usage), installations=[])
        self.assertEqual(len(result['usage']), 3)
        pcts = [b['pct_text'] for b in result['usage']]
        self.assertEqual(pcts, ['10%', '20%', '30%'])

    @patch('usage_monitor_for_codex.popup.POPUP_FIELDS', ['typo_field', 'seven_day'])
    @patch('usage_monitor_for_codex.popup.elapsed_pct', return_value=None)
    @patch('usage_monitor_for_codex.popup.time_until', return_value='')
    @patch('usage_monitor_for_codex.popup.midnight_positions', return_value=[])
    def test_misspelled_popup_field_skipped_in_dict(self, _mock_mid, _mock_tu, _mock_ep):
        """Misspelled popup_fields entry produces no bar, valid one shown."""
        usage = {
            'five_hour': {'utilization': 42, 'resets_at': '2026-01-01T05:00:00Z'},
            'seven_day': {'utilization': 20, 'resets_at': '2026-01-07T00:00:00Z'},
        }
        result = _snapshot_to_dict(_snap(usage=usage), installations=[])
        self.assertEqual(len(result['usage']), 1)
        self.assertEqual(result['usage'][0]['pct_text'], '20%')

    def test_all_null_fields_no_bars(self):
        """All-null quota fields produce no usage bars."""
        usage = {'five_hour': None, 'seven_day': None, 'seven_day_sonnet': None}
        result = _snapshot_to_dict(_snap(usage=usage), installations=[])
        self.assertEqual(result['usage'], [])

    @patch('usage_monitor_for_codex.popup.elapsed_pct', return_value=None)
    @patch('usage_monitor_for_codex.popup.time_until', return_value='')
    @patch('usage_monitor_for_codex.popup.midnight_positions', return_value=[])
    def test_non_dict_values_in_response_ignored(self, _mock_mid, _mock_tu, _mock_ep):
        """Non-dict values in the API response are not shown as bars."""
        usage = {
            'error': 'temporary',
            'rate_limited': True,
            'five_hour': {'utilization': 42, 'resets_at': '2026-01-01T05:00:00Z'},
        }
        result = _snapshot_to_dict(_snap(usage=usage), installations=[])
        self.assertEqual(len(result['usage']), 1)
        self.assertEqual(result['usage'][0]['pct_text'], '42%')

    # -- extra usage --

    def test_no_extra_usage(self):
        """Extra is None when no extra_usage key in usage dict."""
        result = _snapshot_to_dict(_snap(), installations=[])
        self.assertIsNone(result['extra'])

    def test_extra_usage_disabled(self):
        """Extra is None when extra usage is not enabled."""
        usage = {'extra_usage': {'is_enabled': False, 'monthly_limit': 1000, 'used_credits': 500}}
        result = _snapshot_to_dict(_snap(usage=usage), installations=[])
        self.assertIsNone(result['extra'])

    def test_extra_usage_zero_limit(self):
        """Extra is None when monthly limit is zero."""
        usage = {'extra_usage': {'is_enabled': True, 'monthly_limit': 0, 'used_credits': 0}}
        result = _snapshot_to_dict(_snap(usage=usage), installations=[])
        self.assertIsNone(result['extra'])

    @patch('usage_monitor_for_codex.popup.format_credits', side_effect=lambda c: f'${c / 100:.2f}')
    def test_extra_usage_calculation(self, _mock_credits):
        """Extra usage computes percentage and formatted text correctly."""
        usage = {'extra_usage': {'is_enabled': True, 'monthly_limit': 10000, 'used_credits': 2500}}
        result = _snapshot_to_dict(_snap(usage=usage), installations=[])

        extra = result['extra']
        self.assertIsNotNone(extra)
        self.assertEqual(extra['pct_text'], '25%')
        self.assertAlmostEqual(extra['fill_pct'], 0.25)
        self.assertIn('$25.00', extra['spent_text'])
        self.assertIn('$100.00', extra['spent_text'])

    def test_extra_usage_credits_balance(self):
        """A Codex credits balance renders as a text line, not a ratio bar."""
        usage = {'extra_usage': {'is_enabled': True, 'unlimited': False, 'balance': 1250}}
        result = _snapshot_to_dict(_snap(usage=usage), installations=[])
        extra = result['extra']
        self.assertIsNotNone(extra)
        self.assertEqual(extra['mode'], 'text')
        self.assertIn('1,250', extra['text'])

    def test_extra_usage_credits_unlimited(self):
        """Unlimited credits render as a text line."""
        usage = {'extra_usage': {'is_enabled': True, 'unlimited': True, 'balance': None}}
        result = _snapshot_to_dict(_snap(usage=usage), installations=[])
        extra = result['extra']
        self.assertIsNotNone(extra)
        self.assertEqual(extra['mode'], 'text')
        self.assertTrue(extra['text'])

    def test_credits_shown_on_api_source(self):
        """Credits ARE shown from a verified live (api) response."""
        usage = {'five_hour': {'utilization': 42.0, 'resets_at': ''},
                 'extra_usage': {'is_enabled': True, 'unlimited': False, 'balance': 1250}, 'source': 'api'}
        result = _snapshot_to_dict(_snap(usage=usage), installations=[])
        self.assertIsNotNone(result['extra'])
        self.assertEqual(result['extra']['mode'], 'text')

    def test_credits_hidden_on_session_source(self):
        """Credits (billing-sensitive) are hidden while on unverified local session data."""
        usage = {'five_hour': {'utilization': 42.0, 'resets_at': ''},
                 'extra_usage': {'is_enabled': True, 'unlimited': False, 'balance': 1250}, 'source': 'session'}
        result = _snapshot_to_dict(_snap(usage=usage), installations=[])
        self.assertIsNone(result['extra'])

    def test_status_flags_session_source_as_degraded(self):
        """Session-sourced data marks the status degraded (so the UI shows a local/stale indicator)."""
        usage = {'five_hour': {'utilization': 42.0, 'resets_at': ''},
                 'source': 'session', 'snapshot_at': '2026-05-26T08:00:00Z'}
        result = _snapshot_to_dict(_snap(usage=usage), installations=[])
        self.assertEqual(result['status'].get('source'), 'session')
        self.assertEqual(result['status'].get('snapshot_at'), '2026-05-26T08:00:00Z')

    def test_status_not_degraded_on_api_source(self):
        """Live (api) data does not carry the degraded session flag."""
        usage = {'five_hour': {'utilization': 42.0, 'resets_at': ''}, 'source': 'api'}
        result = _snapshot_to_dict(_snap(usage=usage), installations=[])
        self.assertNotIn('source', result['status'])

    @patch('usage_monitor_for_codex.popup.format_credits', side_effect=lambda c: f'${c / 100:.2f}')
    def test_extra_usage_fill_clamped(self, _mock_credits):
        """Extra usage fill is clamped to 1.0 when over limit."""
        usage = {'extra_usage': {'is_enabled': True, 'monthly_limit': 1000, 'used_credits': 2000}}
        result = _snapshot_to_dict(_snap(usage=usage), installations=[])
        self.assertEqual(result['extra']['fill_pct'], 1.0)

    # -- installations --

    def test_installations_passthrough(self):
        """Pre-computed installations list is passed through unchanged."""
        installs = [{'name': 'VS Code', 'version': '1.0.0'}]
        result = _snapshot_to_dict(_snap(), installations=installs)
        self.assertEqual(result['installations'], installs)

    @patch('usage_monitor_for_codex.popup.find_installations')
    def test_installations_auto_detected(self, mock_find):
        """When installations is None, find_installations() is called."""
        inst = MagicMock()
        inst.name = 'Cursor'
        inst.version = '2.0.0'
        mock_find.return_value = [inst]

        result = _snapshot_to_dict(_snap(), installations=None)
        mock_find.assert_called_once()
        self.assertEqual(result['installations'], [{'name': 'Cursor', 'version': '2.0.0'}])

    # -- status --

    def test_status_error_when_no_usage(self):
        """Shows error text when there's no usage data but there's an error."""
        result = _snapshot_to_dict(_snap(usage={}, last_error='Connection failed'), installations=[])
        self.assertEqual(result['status']['text'], 'Connection failed')
        self.assertTrue(result['status']['is_error'])

    def test_status_error_truncated(self):
        """Error messages are truncated to 120 characters."""
        long_error = 'x' * 200
        result = _snapshot_to_dict(_snap(usage={}, last_error=long_error), installations=[])
        self.assertEqual(len(result['status']['text']), 120)

    def test_status_refreshing_when_no_usage_no_error(self):
        """Shows refreshing status when no usage data and no error."""
        from usage_monitor_for_codex.i18n import T

        result = _snapshot_to_dict(_snap(usage={}, last_error=None), installations=[])
        self.assertEqual(result['status']['text'], T['status_refreshing'])
        self.assertFalse(result['status']['is_error'])

    @patch('usage_monitor_for_codex.popup.elapsed_pct', return_value=None)
    @patch('usage_monitor_for_codex.popup.time_until', return_value='')
    @patch('usage_monitor_for_codex.popup.midnight_positions', return_value=[])
    def test_status_live_mode_keys(self, _mock_mid, _mock_tu, _mock_ep):
        """Live mode status contains all required keys for the JS timer."""
        usage = {'five_hour': {'utilization': 50, 'resets_at': '2026-01-01T05:00:00Z'}}
        result = _snapshot_to_dict(
            _snap(usage=usage, last_success_time=1000.0, refreshing=True, last_error='Server down'),
            installations=[], next_poll_time=1180.0,
        )
        self.assertEqual(set(result['status'].keys()), {'last_success_time', 'next_poll_time', 'refreshing', 'error'})

    @patch('usage_monitor_for_codex.popup.elapsed_pct', return_value=None)
    @patch('usage_monitor_for_codex.popup.time_until', return_value='')
    @patch('usage_monitor_for_codex.popup.midnight_positions', return_value=[])
    def test_status_error_truncated_in_live_mode(self, _mock_mid, _mock_tu, _mock_ep):
        """Error messages are truncated to 120 characters in live mode."""
        usage = {'five_hour': {'utilization': 50, 'resets_at': '2026-01-01T05:00:00Z'}}
        long_error = 'x' * 200
        result = _snapshot_to_dict(
            _snap(usage=usage, last_error=long_error),
            installations=[],
        )
        self.assertEqual(len(result['status']['error']), 120)

    # -- top-level dict structure --

    def test_all_top_level_keys_present(self):
        """Result always has profile, usage, extra, installations, status."""
        result = _snapshot_to_dict(_snap(), installations=[])
        self.assertEqual(set(result.keys()), {'profile', 'usage', 'extra', 'installations', 'status', 'layout'})


# ---------------------------------------------------------------------------
# _init_config
# ---------------------------------------------------------------------------

class TestInitConfig(unittest.TestCase):
    """Tests for _init_config - builds the JS init() config object."""

    def test_top_level_keys(self):
        """Config has colors, t, app_version, always_on_top, expanded, and data."""
        config = _init_config(_snap())
        self.assertEqual(
            set(config.keys()),
            {'colors', 't', 'app_version', 'always_on_top', 'expanded', 'data'},
        )

    def test_always_on_top_reflects_argument(self):
        """always_on_top defaults to True and mirrors the passed value."""
        self.assertIs(_init_config(_snap())['always_on_top'], True)
        self.assertIs(_init_config(_snap(), always_on_top=False)['always_on_top'], False)

    def test_expanded_reflects_argument(self):
        """expanded defaults to False and mirrors the passed value."""
        self.assertIs(_init_config(_snap())['expanded'], False)
        self.assertIs(_init_config(_snap(), expanded=True)['expanded'], True)

    def test_colors_from_settings(self):
        """Color values come from settings module constants."""
        from usage_monitor_for_codex.settings import BAR_BG, BAR_DIVIDER, BAR_FG, BAR_FG_START, BAR_FG_WARN, BAR_MARKER, BG, FG, FG_DIM, FG_HEADING, FG_LINK

        config = _init_config(_snap())
        colors = config['colors']
        self.assertEqual(colors['bg'], BG)
        self.assertEqual(colors['fg'], FG)
        self.assertEqual(colors['fg_dim'], FG_DIM)
        self.assertEqual(colors['fg_heading'], FG_HEADING)
        self.assertEqual(colors['fg_link'], FG_LINK)
        self.assertEqual(colors['bar_bg'], BAR_BG)
        self.assertEqual(colors['bar_fg'], BAR_FG)
        self.assertEqual(colors['bar_fg_start'], BAR_FG_START)
        self.assertEqual(colors['bar_fg_warn'], BAR_FG_WARN)
        self.assertEqual(colors['bar_divider'], BAR_DIVIDER)
        self.assertEqual(colors['bar_marker'], BAR_MARKER)

    def test_translations_from_i18n(self):
        """Translation values come from the T dict."""
        from usage_monitor_for_codex.i18n import T

        config = _init_config(_snap())
        t = config['t']
        self.assertEqual(t['title'], T['popup_title'])
        self.assertEqual(t['account'], T['account'])
        self.assertEqual(t['email'], T['email'])
        self.assertEqual(t['plan'], T['plan'])
        self.assertEqual(t['usage'], T['usage'])
        self.assertEqual(t['extra_usage'], T['extra_usage'])
        self.assertEqual(t['claude_code'], T['claude_code'])
        self.assertEqual(t['changelog'], T['changelog'])
        self.assertEqual(t['status_updated_s'], T['status_updated_s'])
        self.assertEqual(t['status_updated'], T['status_updated'])
        self.assertEqual(t['status_refreshing'], T['status_refreshing'])
        self.assertEqual(t['status_next_update'], T['status_next_update'])
        self.assertEqual(t['duration_hm'], T['duration_hm'])
        self.assertEqual(t['duration_m'], T['duration_m'])
        self.assertEqual(t['duration_s'], T['duration_s'])

    def test_app_version(self):
        """app_version matches the package version."""
        from usage_monitor_for_codex import __version__

        config = _init_config(_snap())
        self.assertEqual(config['app_version'], __version__)

    def test_data_is_snapshot_to_dict_output(self):
        """The data key contains the output of _snapshot_to_dict."""
        snap = _snap(profile={'account': {'email': 'a@b.com'}, 'organization': {}})
        config = _init_config(snap)
        self.assertEqual(config['data']['profile']['email'], 'a@b.com')
        self.assertEqual(set(config['data'].keys()), {'profile', 'usage', 'extra', 'installations', 'status', 'layout'})


# ---------------------------------------------------------------------------
# _resize_and_position
# ---------------------------------------------------------------------------

class TestResizeAndPosition(unittest.TestCase):
    """Tests for UsagePopup._resize_and_position - DPI-aware resize."""

    def _call(self, css_height, dpi):
        """Call _resize_and_position and capture the resize/move arguments."""
        popup = object.__new__(UsagePopup)
        popup.WIDTH = UsagePopup.WIDTH
        popup._popup_hwnd = 12345
        popup._positioned = False
        popup._saved_pos = None

        mock_window = MagicMock()
        popup._window = mock_window

        def fill_mon_info(_hmon, ptr):
            info = ctypes.cast(ptr, ctypes.POINTER(_MONITORINFO)).contents
            info.cbSize = ctypes.sizeof(_MONITORINFO)
            info.rcMonitor.left = 0
            info.rcMonitor.top = 0
            info.rcMonitor.right = 1920
            info.rcMonitor.bottom = 1080
            info.rcWork.left = 0
            info.rcWork.top = 0
            info.rcWork.right = 1920
            info.rcWork.bottom = 1040

        with patch('ctypes.windll.user32.GetDpiForWindow', return_value=dpi), \
             patch('ctypes.windll.user32.FindWindowW', return_value=99999), \
             patch('ctypes.windll.user32.MonitorFromWindow', return_value=11111), \
             patch('ctypes.windll.user32.GetMonitorInfoW', side_effect=fill_mon_info), \
             patch('usage_monitor_for_codex.popup.save_window_position'):
            popup._resize_and_position(css_height)

        return mock_window

    def test_resize_at_100_percent(self):
        """At 100% DPI, resize uses CSS pixels directly (scale=1)."""
        mock = self._call(500, 96)
        mock.resize.assert_called_once_with(340, 500)

    def test_resize_at_125_percent(self):
        """At 125% DPI, resize receives logical pixels; pywebview scales internally."""
        mock = self._call(500, 120)
        mock.resize.assert_called_once_with(340, 500)

    def test_resize_at_150_percent(self):
        """At 150% DPI, resize receives logical pixels; pywebview scales internally."""
        mock = self._call(500, 144)
        mock.resize.assert_called_once_with(340, 500)

    def test_move_receives_logical_coordinates(self):
        """move() receives logical coordinates regardless of DPI."""
        mock = self._call(500, 120)
        x, y = mock.move.call_args[0]
        # Logical coordinates must be smaller than physical work area
        self.assertLess(x, 1920)
        self.assertLess(y, 1040)

    def test_window_fits_within_work_area_at_125_percent(self):
        """After resize + move at 125% DPI, the window stays within the work area."""
        dpi = 120
        scale = dpi / _BASELINE_DPI
        mock = self._call(500, dpi)
        resize_w, resize_h = mock.resize.call_args[0]
        move_x, move_y = mock.move.call_args[0]
        # pywebview 6.x scales both resize() and move() to physical internally
        self.assertLessEqual((move_x + resize_w) * scale, 1920)
        self.assertLessEqual((move_y + resize_h) * scale, 1040)

    def test_falls_back_to_system_dpi_when_window_dpi_unavailable(self):
        """When GetDpiForWindow returns 0, GetDpiForSystem is used as fallback."""
        popup = object.__new__(UsagePopup)
        popup.WIDTH = UsagePopup.WIDTH
        popup._popup_hwnd = 12345
        popup._positioned = False
        popup._saved_pos = None

        mock_window = MagicMock()
        popup._window = mock_window

        def fill_mon_info(_hmon, ptr):
            info = ctypes.cast(ptr, ctypes.POINTER(_MONITORINFO)).contents
            info.cbSize = ctypes.sizeof(_MONITORINFO)
            info.rcMonitor.left = 0
            info.rcMonitor.top = 0
            info.rcMonitor.right = 1920
            info.rcMonitor.bottom = 1080
            info.rcWork.left = 0
            info.rcWork.top = 0
            info.rcWork.right = 1920
            info.rcWork.bottom = 1040

        with patch('ctypes.windll.user32.GetDpiForWindow', return_value=0), \
             patch('ctypes.windll.user32.GetDpiForSystem', return_value=144) as mock_sys_dpi, \
             patch('ctypes.windll.user32.FindWindowW', return_value=99999), \
             patch('ctypes.windll.user32.MonitorFromWindow', return_value=11111), \
             patch('ctypes.windll.user32.GetMonitorInfoW', side_effect=fill_mon_info), \
             patch('usage_monitor_for_codex.popup.save_window_position'):
            popup._resize_and_position(500)

        mock_sys_dpi.assert_called()
        mock_window.resize.assert_called_once_with(340, 500)


# ---------------------------------------------------------------------------
# SettingsWindow.apply
# ---------------------------------------------------------------------------

class TestSettingsWindowApply(unittest.TestCase):
    """Tests for SettingsWindow.apply - saving fields and the language auto-restart."""

    def _window(self):
        """Build a SettingsWindow without constructing the real webview window."""
        window = object.__new__(SettingsWindow)
        window.app = MagicMock()
        return window

    @patch('usage_monitor_for_codex.popup.save_field_config')
    @patch('usage_monitor_for_codex.popup.save_language')
    @patch('usage_monitor_for_codex.popup.load_language', return_value='')
    def test_language_change_restarts_app(self, _load, mock_save_language, _save_fields):
        """Changing the language saves it and restarts the app to apply it."""
        window = self._window()
        with patch.object(window, 'close') as mock_close:
            window.apply({'fields': [], 'language': 'ja'})

        mock_save_language.assert_called_once_with('ja')
        mock_close.assert_called_once()
        window.app.on_restart.assert_called_once()

    @patch('usage_monitor_for_codex.popup.save_field_config')
    @patch('usage_monitor_for_codex.popup.save_language')
    @patch('usage_monitor_for_codex.popup.load_language', return_value='ja')
    def test_unchanged_language_does_not_restart(self, _load, _save_language, _save_fields):
        """Saving without a language change just closes; the app is not restarted."""
        window = self._window()
        with patch.object(window, 'close') as mock_close:
            window.apply({'fields': [], 'language': 'ja'})

        window.app.on_restart.assert_not_called()
        mock_close.assert_called_once()

    @patch('usage_monitor_for_codex.popup.save_field_config')
    @patch('usage_monitor_for_codex.popup.save_language')
    @patch('usage_monitor_for_codex.popup.load_language', return_value='')
    def test_field_rows_saved_as_pairs(self, _load, _save_language, mock_save_fields):
        """Field rows are persisted as (key, state) pairs."""
        window = self._window()
        with patch.object(window, 'close'):
            window.apply({'fields': [{'key': 'five_hour', 'state': 'collapsed'}], 'language': ''})

        mock_save_fields.assert_called_once_with([('five_hour', 'collapsed')])
        window.app.on_restart.assert_not_called()


# ---------------------------------------------------------------------------
# _field_config_changed
# ---------------------------------------------------------------------------

class TestFieldConfigChanged(unittest.TestCase):
    """Tests for _field_config_changed - the widget's live-refresh trigger."""

    def test_identical_config_is_unchanged(self):
        """Same keys, states, and order is not a change."""
        self.assertFalse(_field_config_changed(
            {'five_hour': 'visible', 'seven_day': 'hidden'},
            {'five_hour': 'visible', 'seven_day': 'hidden'},
        ))

    def test_reorder_is_a_change(self):
        """A pure reorder (same keys and states) counts as a change."""
        self.assertTrue(_field_config_changed(
            {'seven_day': 'hidden', 'five_hour': 'visible'},
            {'five_hour': 'visible', 'seven_day': 'hidden'},
        ))

    def test_state_change_is_a_change(self):
        """Changing a field's display state counts as a change."""
        self.assertTrue(_field_config_changed({'five_hour': 'collapsed'}, {'five_hour': 'visible'}))

    def test_added_or_removed_field_is_a_change(self):
        """Adding or removing a field counts as a change."""
        self.assertTrue(_field_config_changed(
            {'five_hour': 'visible', 'seven_day': 'visible'},
            {'five_hour': 'visible'},
        ))


# ---------------------------------------------------------------------------
# resolve_block_order
# ---------------------------------------------------------------------------

class TestResolveBlockOrder(unittest.TestCase):
    """Tests for resolve_block_order - the unified block list (account, bars, extra, versions, status)."""

    PSEUDO = {'account', 'extra_usage', 'installations', 'status'}

    def test_default_order_and_states(self):
        """No saved config: account, usage bars, extra, versions, status - bars visible, the rest collapsed."""
        ordered = resolve_block_order(['five_hour', 'seven_day'], self.PSEUDO, {})
        self.assertEqual(ordered, [
            ('account', 'collapsed'),
            ('five_hour', 'visible'),
            ('seven_day', 'visible'),
            ('extra_usage', 'collapsed'),
            ('installations', 'collapsed'),
            ('status', 'collapsed'),
        ])

    def test_only_available_pseudo_blocks_included(self):
        """Pseudo blocks absent from pseudo_available are omitted."""
        ordered = resolve_block_order(['five_hour'], {'status'}, {})
        self.assertEqual([key for key, _ in ordered], ['five_hour', 'status'])

    def test_saved_order_and_state_respected(self):
        """Saved entries set order and state; unconfigured blocks follow in default order."""
        saved = {'status': 'visible', 'account': 'hidden', 'five_hour': 'collapsed'}
        ordered = resolve_block_order(['five_hour', 'seven_day'], self.PSEUDO, saved, include_hidden=True)
        self.assertEqual(ordered, [
            ('status', 'visible'),
            ('account', 'hidden'),
            ('five_hour', 'collapsed'),
            ('seven_day', 'visible'),
            ('extra_usage', 'collapsed'),
            ('installations', 'collapsed'),
        ])

    def test_hidden_excluded_by_default(self):
        """Hidden blocks are dropped unless include_hidden is set."""
        saved = {'account': 'hidden'}
        keys = [key for key, _ in resolve_block_order(['five_hour'], self.PSEUDO, saved)]
        self.assertNotIn('account', keys)
        keys_incl = [key for key, _ in resolve_block_order(['five_hour'], self.PSEUDO, saved, include_hidden=True)]
        self.assertIn('account', keys_incl)

    def test_stale_saved_keys_ignored(self):
        """Saved entries for blocks that are not available are dropped."""
        keys = [key for key, _ in resolve_block_order(['five_hour'], {'account', 'status'}, {'extra_usage': 'visible'})]
        self.assertNotIn('extra_usage', keys)

    def test_snapshot_layout_lists_available_blocks_in_order(self):
        """_snapshot_to_dict emits an ordered layout of the blocks that have data."""
        usage = {'five_hour': {'utilization': 42, 'resets_at': '2026-01-01T00:00:00Z'}}
        result = _snapshot_to_dict(_snap(usage=usage, profile={'account': {'email': 'a@b.com'}}), installations=[])
        self.assertEqual([block['key'] for block in result['layout']], ['account', 'five_hour', 'status'])

    def test_layout_promotes_status_when_nothing_visible(self):
        """With no usage data, the status line is promoted to visible so compact is not blank."""
        result = _snapshot_to_dict(_snap(usage={}, last_error='boom'), installations=[])
        self.assertTrue(any(block['state'] == 'visible' for block in result['layout']))
        status = next(block for block in result['layout'] if block['key'] == 'status')
        self.assertEqual(status['state'], 'visible')

    def test_layout_keeps_status_collapsed_when_a_bar_is_visible(self):
        """When a usage bar is visible, the status block keeps its default collapsed state."""
        usage = {'five_hour': {'utilization': 42, 'resets_at': '2026-01-01T00:00:00Z'}}
        result = _snapshot_to_dict(_snap(usage=usage), installations=[])
        status = next(block for block in result['layout'] if block['key'] == 'status')
        self.assertEqual(status['state'], 'collapsed')


if __name__ == '__main__':
    unittest.main()
