"""
Application Tests
===================

Unit tests for the application module: threshold alerts, update orchestration,
tray rendering, polling interval, and reset notifications.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from usage_monitor_for_codex.app import UsageMonitorForCodex
from usage_monitor_for_codex.cache import UpdateResult
from usage_monitor_for_codex.codex_cli import RefreshResult


def _make_app(thresholds: list[float] | None = None) -> UsageMonitorForCodex:
    """Create a UsageMonitorForCodex with mocked icon and configurable thresholds.

    Parameters
    ----------
    thresholds : list[float] or None
        Alert thresholds to use for all variants.  Defaults to ``[80, 95]``.
    """
    if thresholds is None:
        thresholds = [80, 95]
    with patch('usage_monitor_for_codex.app.pystray'), \
         patch('usage_monitor_for_codex.app.load_tray_icon'):
        app = UsageMonitorForCodex()
    app.icon = MagicMock()
    app._thresholds_patch = patch('usage_monitor_for_codex.app.get_alert_thresholds', return_value=thresholds)
    app._thresholds_patch.start()
    # Notifications are deferred while the user is away, so mock an active
    # machine by default. This keeps the notification tests deterministic
    # instead of depending on the real idle/lock state of the test host;
    # tests that exercise the away path override these with their own patches.
    app._active_patches = [
        patch('usage_monitor_for_codex.app.is_workstation_locked', return_value=False),
        patch('usage_monitor_for_codex.app.get_idle_seconds', return_value=0),
    ]
    for active_patch in app._active_patches:
        active_patch.start()
    return app


def _cleanup(app: UsageMonitorForCodex) -> None:
    """Stop patches started by _make_app."""
    app._thresholds_patch.stop()
    for active_patch in app._active_patches:
        active_patch.stop()


# ---------------------------------------------------------------------------
# _check_threshold_alerts
# ---------------------------------------------------------------------------

class TestCheckThresholdAlerts(unittest.TestCase):
    """Tests for _check_threshold_alerts() notification logic."""

    def setUp(self):
        self.app = _make_app()
        self._cmd_patch = patch('usage_monitor_for_codex.app.run_event_command')
        self._cmd_patch.start()

    def tearDown(self):
        self._cmd_patch.stop()
        _cleanup(self.app)

    def test_notification_on_first_crossing(self):
        """Notification fires when usage crosses a threshold for the first time."""
        self.app._check_threshold_alerts({'five_hour': {'utilization': 82}})

        self.app.icon.notify.assert_called_once()
        args = self.app.icon.notify.call_args
        self.assertIn('82%', args[0][0])

    def test_no_duplicate_notification(self):
        """No notification if threshold was already notified."""
        self.app._check_threshold_alerts({'five_hour': {'utilization': 82}})
        self.app.icon.notify.reset_mock()

        self.app._check_threshold_alerts({'five_hour': {'utilization': 85}})

        self.app.icon.notify.assert_not_called()

    def test_higher_threshold_triggers_new_notification(self):
        """Crossing a higher threshold triggers a new notification."""
        self.app._check_threshold_alerts({'five_hour': {'utilization': 82}})
        self.app.icon.notify.reset_mock()

        self.app._check_threshold_alerts({'five_hour': {'utilization': 97}})

        self.app.icon.notify.assert_called_once()
        args = self.app.icon.notify.call_args
        self.assertIn('97%', args[0][0])

    def test_jump_past_multiple_thresholds_single_notification(self):
        """Jumping from below all thresholds to above multiple shows only one notification."""
        self.app._check_threshold_alerts({'five_hour': {'utilization': 97}})

        self.app.icon.notify.assert_called_once()
        self.assertEqual(self.app._notified_thresholds.get('five_hour'), 95)

    def test_notification_shows_current_pct_not_threshold(self):
        """Notification message contains the actual usage %, not the threshold value."""
        self.app._check_threshold_alerts({'five_hour': {'utilization': 83.7}})

        args = self.app.icon.notify.call_args
        self.assertIn('84%', args[0][0])

    def test_re_notification_after_usage_drops(self):
        """After usage drops below a threshold, it can re-trigger."""
        self.app._check_threshold_alerts({'five_hour': {'utilization': 82}})
        self.app.icon.notify.reset_mock()

        # Usage drops below 80 (e.g. after reset)
        self.app._check_threshold_alerts({'five_hour': {'utilization': 30}})
        self.app.icon.notify.assert_not_called()

        # Usage rises above 80 again
        self.app._check_threshold_alerts({'five_hour': {'utilization': 81}})
        self.app.icon.notify.assert_called_once()

    def test_no_notification_when_thresholds_empty(self):
        """No notification when thresholds list is empty."""
        _cleanup(self.app)
        self.app = _make_app(thresholds=[])

        self.app._check_threshold_alerts({'five_hour': {'utilization': 99}})

        self.app.icon.notify.assert_not_called()

    def test_on_startup_above_threshold(self):
        """On startup (no prior state), notification fires if already above threshold."""
        self.app._check_threshold_alerts({'five_hour': {'utilization': 90}})

        self.app.icon.notify.assert_called_once()

    def test_each_variant_tracked_independently(self):
        """Different variants are tracked independently."""
        self.app._check_threshold_alerts({
            'five_hour': {'utilization': 82},
            'seven_day': {'utilization': 50},
        })

        self.app.icon.notify.assert_called_once()
        self.assertEqual(self.app._notified_thresholds.get('five_hour'), 80)
        self.assertEqual(self.app._notified_thresholds.get('seven_day', 0), 0)

    def test_multiple_variants_crossing_simultaneously(self):
        """Multiple variants crossing thresholds each get their own notification."""
        self.app._check_threshold_alerts({
            'five_hour': {'utilization': 82},
            'seven_day': {'utilization': 96},
        })

        self.assertEqual(self.app.icon.notify.call_count, 2)

    def test_variant_with_no_utilization_skipped(self):
        """Variants with None utilization are skipped."""
        self.app._check_threshold_alerts({'five_hour': {'utilization': None}})

        self.app.icon.notify.assert_not_called()

    def test_missing_variant_skipped(self):
        """Missing variants in data are skipped."""
        self.app._check_threshold_alerts({})

        self.app.icon.notify.assert_not_called()

    def test_usage_exactly_at_threshold(self):
        """Usage exactly at threshold value triggers notification."""
        self.app._check_threshold_alerts({'five_hour': {'utilization': 80}})

        self.app.icon.notify.assert_called_once()

    def test_usage_just_below_threshold(self):
        """Usage just below threshold does not trigger notification."""
        self.app._check_threshold_alerts({'five_hour': {'utilization': 79.9}})

        self.app.icon.notify.assert_not_called()

    def test_non_dict_entries_skipped(self):
        """Non-dict entries in response (strings, booleans) are silently skipped."""
        self.app._check_threshold_alerts({
            'error': 'server down',
            'rate_limited': True,
            'five_hour': {'utilization': 82},
        })

        self.app.icon.notify.assert_called_once()

    def test_extra_usage_excluded_from_regular_alerts(self):
        """extra_usage is handled separately, not by the regular threshold loop."""
        _cleanup(self.app)
        self.app = _make_app(thresholds=[50])

        with patch.object(self.app, '_check_extra_usage_alerts'):
            self.app._check_threshold_alerts({
                'extra_usage': {'is_enabled': True, 'monthly_limit': 1000, 'used_credits': 800, 'utilization': 80},
            })

        self.app.icon.notify.assert_not_called()

    def test_null_entry_skipped(self):
        """Null entry value is silently skipped."""
        self.app._check_threshold_alerts({'five_hour': None, 'seven_day': {'utilization': 82}})

        self.app.icon.notify.assert_called_once()

    def test_entry_without_utilization_key_skipped(self):
        """Entry dict without utilization key is silently skipped."""
        self.app._check_threshold_alerts({'five_hour': {'resets_at': '2026-01-01T05:00:00Z'}})

        self.app.icon.notify.assert_not_called()

    def test_unknown_dynamic_variant_uses_fallback_thresholds(self):
        """Dynamically discovered variant uses base period fallback thresholds."""
        _cleanup(self.app)
        self.app = _make_app()

        # seven_day_cowork is not in the hardcoded thresholds but falls back to seven_day
        self.app._check_threshold_alerts({'seven_day_cowork': {'utilization': 96}})

        self.app.icon.notify.assert_called_once()

    def test_field_without_resets_at_alerts_normally(self):
        """Field missing resets_at still triggers threshold alert (time-aware falls back gracefully)."""
        self.app._check_threshold_alerts({'five_hour': {'utilization': 82}})

        self.app.icon.notify.assert_called_once()


# ---------------------------------------------------------------------------
# Time-aware alerts
# ---------------------------------------------------------------------------

class TestTimeAwareAlerts(unittest.TestCase):
    """Tests for time-aware threshold alert suppression."""

    def setUp(self):
        self.app = _make_app()
        self._cmd_patch = patch('usage_monitor_for_codex.app.run_event_command')
        self._time_aware_patch = patch('usage_monitor_for_codex.app.ALERT_TIME_AWARE', True)
        self._below_patch = patch('usage_monitor_for_codex.app.ALERT_TIME_AWARE_BELOW', 100)
        self._cmd_patch.start()
        self._time_aware_patch.start()
        self._below_patch.start()

    def tearDown(self):
        self._below_patch.stop()
        self._time_aware_patch.stop()
        self._cmd_patch.stop()
        _cleanup(self.app)

    def test_alert_suppressed_when_usage_behind_time(self):
        """No notification when usage (82%) <= elapsed time (90%)."""
        with patch('usage_monitor_for_codex.app.elapsed_pct', return_value=90.0):
            self.app._check_threshold_alerts({'five_hour': {'utilization': 82, 'resets_at': '2025-01-15T14:30:00+00:00'}})

        self.app.icon.notify.assert_not_called()

    def test_alert_shown_when_usage_ahead_of_time(self):
        """Notification fires when usage (82%) > elapsed time (50%)."""
        with patch('usage_monitor_for_codex.app.elapsed_pct', return_value=50.0):
            self.app._check_threshold_alerts({'five_hour': {'utilization': 82, 'resets_at': '2025-01-15T14:30:00+00:00'}})

        self.app.icon.notify.assert_called_once()

    def test_fallback_when_elapsed_pct_none(self):
        """Notification fires normally when elapsed_pct returns None (no resets_at)."""
        with patch('usage_monitor_for_codex.app.elapsed_pct', return_value=None):
            self.app._check_threshold_alerts({'five_hour': {'utilization': 82}})

        self.app.icon.notify.assert_called_once()

    def test_tracking_updated_when_suppressed(self):
        """Notified threshold tracking is updated even when alert is suppressed."""
        with patch('usage_monitor_for_codex.app.elapsed_pct', return_value=90.0):
            self.app._check_threshold_alerts({'five_hour': {'utilization': 82, 'resets_at': '2025-01-15T14:30:00+00:00'}})

        self.assertEqual(self.app._notified_thresholds.get('five_hour'), 80)

    def test_no_re_notification_after_suppression(self):
        """After suppression, the same threshold does not re-trigger."""
        with patch('usage_monitor_for_codex.app.elapsed_pct', return_value=90.0):
            self.app._check_threshold_alerts({'five_hour': {'utilization': 82, 'resets_at': '2025-01-15T14:30:00+00:00'}})

        # Now time catches up less - usage is ahead, but threshold already tracked
        with patch('usage_monitor_for_codex.app.elapsed_pct', return_value=50.0):
            self.app._check_threshold_alerts({'five_hour': {'utilization': 84, 'resets_at': '2025-01-15T14:30:00+00:00'}})

        self.app.icon.notify.assert_not_called()

    def test_disabled_when_false(self):
        """With ALERT_TIME_AWARE=False, alerts fire regardless of time."""
        self._time_aware_patch.stop()
        with patch('usage_monitor_for_codex.app.ALERT_TIME_AWARE', False):
            with patch('usage_monitor_for_codex.app.elapsed_pct', return_value=90.0):
                self.app._check_threshold_alerts({'five_hour': {'utilization': 82, 'resets_at': '2025-01-15T14:30:00+00:00'}})
        self._time_aware_patch.start()

        self.app.icon.notify.assert_called_once()

    def test_usage_equal_to_time_suppressed(self):
        """Notification suppressed when usage exactly equals elapsed time."""
        with patch('usage_monitor_for_codex.app.elapsed_pct', return_value=82.0):
            self.app._check_threshold_alerts({'five_hour': {'utilization': 82, 'resets_at': '2025-01-15T14:30:00+00:00'}})

        self.app.icon.notify.assert_not_called()

    def test_threshold_at_or_above_below_cutoff_always_fires(self):
        """Threshold >= alert_time_aware_below fires even when usage <= time."""
        self._below_patch.stop()
        with patch('usage_monitor_for_codex.app.ALERT_TIME_AWARE_BELOW', 90):
            # Thresholds are [80, 95]. Usage crosses 95 which is >= 90 cutoff.
            with patch('usage_monitor_for_codex.app.elapsed_pct', return_value=98.0):
                self.app._check_threshold_alerts({'five_hour': {'utilization': 97, 'resets_at': '2025-01-15T14:30:00+00:00'}})
        self._below_patch.start()

        self.app.icon.notify.assert_called_once()

    def test_threshold_below_cutoff_suppressed(self):
        """Threshold < alert_time_aware_below is suppressed when usage <= time."""
        self._below_patch.stop()
        with patch('usage_monitor_for_codex.app.ALERT_TIME_AWARE_BELOW', 90):
            # Thresholds are [80, 95]. Usage crosses 80 which is < 90 cutoff.
            with patch('usage_monitor_for_codex.app.elapsed_pct', return_value=90.0):
                self.app._check_threshold_alerts({'five_hour': {'utilization': 82, 'resets_at': '2025-01-15T14:30:00+00:00'}})
        self._below_patch.start()

        self.app.icon.notify.assert_not_called()

    def test_below_cutoff_exact_boundary_fires(self):
        """Threshold exactly at alert_time_aware_below fires regardless of time."""
        self._below_patch.stop()
        with patch('usage_monitor_for_codex.app.ALERT_TIME_AWARE_BELOW', 80):
            with patch('usage_monitor_for_codex.app.elapsed_pct', return_value=90.0):
                self.app._check_threshold_alerts({'five_hour': {'utilization': 82, 'resets_at': '2025-01-15T14:30:00+00:00'}})
        self._below_patch.start()

        self.app.icon.notify.assert_called_once()


# ---------------------------------------------------------------------------
# Extra usage alerts
# ---------------------------------------------------------------------------

class TestExtraUsageAlerts(unittest.TestCase):
    """Tests for _check_extra_usage_alerts() notification logic."""

    def setUp(self):
        self.app = _make_app()
        self._cmd_patch = patch('usage_monitor_for_codex.app.run_event_command')
        self._cmd_patch.start()

    def tearDown(self):
        self._cmd_patch.stop()
        _cleanup(self.app)

    def _extra_data(self, used: float = 0.0, limit: float = 1000, enabled: bool = True) -> dict:
        return {'extra_usage': {'is_enabled': enabled, 'monthly_limit': limit, 'used_credits': used, 'utilization': None}}

    def test_notification_at_threshold(self):
        """Notification fires when extra usage crosses a threshold."""
        self.app._check_extra_usage_alerts(self._extra_data(used=820, limit=1000))

        self.app.icon.notify.assert_called_once()
        args = self.app.icon.notify.call_args[0]
        self.assertIn('82%', args[0])

    def test_no_notification_below_threshold(self):
        """No notification when usage is below all thresholds."""
        self.app._check_extra_usage_alerts(self._extra_data(used=100, limit=1000))

        self.app.icon.notify.assert_not_called()

    def test_no_duplicate_notification(self):
        """No notification if threshold was already notified."""
        self.app._check_extra_usage_alerts(self._extra_data(used=820, limit=1000))
        self.app.icon.notify.reset_mock()

        self.app._check_extra_usage_alerts(self._extra_data(used=850, limit=1000))

        self.app.icon.notify.assert_not_called()

    def test_higher_threshold_triggers_new_notification(self):
        """Crossing a higher threshold triggers a new notification."""
        self.app._check_extra_usage_alerts(self._extra_data(used=820, limit=1000))
        self.app.icon.notify.reset_mock()

        self.app._check_extra_usage_alerts(self._extra_data(used=960, limit=1000))

        self.app.icon.notify.assert_called_once()
        args = self.app.icon.notify.call_args[0]
        self.assertIn('96%', args[0])

    def test_re_notification_after_usage_drops(self):
        """After usage drops (e.g. new billing cycle), thresholds re-trigger."""
        self.app._check_extra_usage_alerts(self._extra_data(used=820, limit=1000))
        self.app.icon.notify.reset_mock()

        self.app._check_extra_usage_alerts(self._extra_data(used=100, limit=1000))
        self.app.icon.notify.assert_not_called()

        self.app._check_extra_usage_alerts(self._extra_data(used=820, limit=1000))
        self.app.icon.notify.assert_called_once()

    def test_disabled_extra_usage_skipped(self):
        """No notification when extra usage is disabled."""
        self.app._check_extra_usage_alerts(self._extra_data(used=950, limit=1000, enabled=False))

        self.app.icon.notify.assert_not_called()

    def test_missing_extra_usage_skipped(self):
        """No notification when extra_usage is missing from data."""
        self.app._check_extra_usage_alerts({})

        self.app.icon.notify.assert_not_called()

    def test_zero_limit_skipped(self):
        """No notification when monthly limit is zero."""
        self.app._check_extra_usage_alerts(self._extra_data(used=0, limit=0))

        self.app.icon.notify.assert_not_called()

    def test_no_notification_when_thresholds_empty(self):
        """No notification when thresholds list is empty."""
        _cleanup(self.app)
        self.app = _make_app(thresholds=[])

        self.app._check_extra_usage_alerts(self._extra_data(used=950, limit=1000))

        self.app.icon.notify.assert_not_called()

    def test_notification_includes_credit_amounts(self):
        """Notification message includes formatted credit amounts."""
        with patch('usage_monitor_for_codex.app.format_credits', side_effect=lambda c: f'${c / 100:.2f}'):
            self.app._check_extra_usage_alerts(self._extra_data(used=820, limit=1000))

        args = self.app.icon.notify.call_args[0]
        self.assertIn('$8.20', args[0])
        self.assertIn('$10.00', args[0])

    def test_called_from_check_threshold_alerts(self):
        """_check_threshold_alerts delegates to _check_extra_usage_alerts."""
        data = self._extra_data(used=820, limit=1000)
        self.app._check_threshold_alerts(data)

        self.app.icon.notify.assert_called_once()

    def test_no_time_aware_logic(self):
        """Extra usage alerts are not affected by time-aware settings."""
        with patch('usage_monitor_for_codex.app.ALERT_TIME_AWARE', True):
            self.app._check_extra_usage_alerts(self._extra_data(used=820, limit=1000))

        self.app.icon.notify.assert_called_once()


# ---------------------------------------------------------------------------
# update() orchestration
# ---------------------------------------------------------------------------

class TestUpdateOrchestration(unittest.TestCase):
    """Tests for update() delegating to cache and processing results."""

    def setUp(self):
        self.app = _make_app()
        self._cmd_patch = patch('usage_monitor_for_codex.app.run_event_command')
        self._cmd_patch.start()

    def tearDown(self):
        self._cmd_patch.stop()
        _cleanup(self.app)

    def test_skipped_update_does_nothing(self):
        """When cache.update() returns None data, update() returns early."""
        self.app.cache = MagicMock()
        self.app.cache.update.return_value = UpdateResult(data=None)

        self.app.update()

        self.assertEqual(self.app._last_response, {})

    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_success_updates_last_response(self, _icon, _tooltip):
        """Successful update stores response in _last_response."""
        data = {'five_hour': {'utilization': 42.0}}
        self.app.cache = MagicMock()
        self.app.cache.update.return_value = UpdateResult(data=data)

        self.app.update()

        self.assertEqual(self.app._last_response, data)

    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_error_updates_last_response(self, _status, _tooltip):
        """Error update stores error response in _last_response."""
        data = {'error': 'server down'}
        self.app.cache = MagicMock()
        self.app.cache.update.return_value = UpdateResult(data=data)

        self.app.update()

        self.assertEqual(self.app._last_response, data)

    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_token_refresh_notification(self, _icon, _tooltip):
        """Shows notification when token refresh updated CLI version."""
        data = {'five_hour': {'utilization': 10.0}}
        refresh = RefreshResult(success=True, updated=True, old_version='2.1.38', new_version='2.1.69', error='')
        self.app.cache = MagicMock()
        self.app.cache.update.return_value = UpdateResult(data=data, token_refresh=refresh)

        self.app.update()

        self.app.icon.notify.assert_called_once()
        args = self.app.icon.notify.call_args[0]
        self.assertIn('2.1.38', args[0])
        self.assertIn('2.1.69', args[0])

    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_no_notification_when_no_cli_update(self, _icon, _tooltip):
        """No notification when token refreshed but no CLI update."""
        data = {'five_hour': {'utilization': 10.0}}
        refresh = RefreshResult(success=True, updated=False, old_version='2.1.69', new_version='2.1.69', error='')
        self.app.cache = MagicMock()
        self.app.cache.update.return_value = UpdateResult(data=data, token_refresh=refresh)

        self.app.update()

        self.app.icon.notify.assert_not_called()

    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_error_returns_before_threshold_checks(self, _status, _tooltip):
        """Error response returns early without threshold checks."""
        data = {'error': 'fail'}
        self.app.cache = MagicMock()
        self.app.cache.update.return_value = UpdateResult(data=data)

        with patch.object(self.app, '_check_threshold_alerts') as mock_check:
            self.app.update()
            mock_check.assert_not_called()

    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_update_tracks_previous_values(self, _icon, _tooltip):
        """update() stores current pct values for next comparison."""
        data = {'five_hour': {'utilization': 42.0}, 'seven_day': {'utilization': 15.0}}
        self.app.cache = MagicMock()
        self.app.cache.update.return_value = UpdateResult(data=data)

        self.app.update()

        self.assertEqual(self.app._prev_utilization.get('five_hour'), 42.0)
        self.assertEqual(self.app._prev_utilization.get('seven_day'), 15.0)

    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_error_does_not_update_previous_values(self, _status, _tooltip):
        """Error response does not change tracked previous values."""
        self.app._prev_utilization = {'five_hour': 50.0, 'seven_day': 20.0}
        data = {'error': 'fail'}
        self.app.cache = MagicMock()
        self.app.cache.update.return_value = UpdateResult(data=data)

        self.app.update()

        self.assertEqual(self.app._prev_utilization.get('five_hour'), 50.0)
        self.assertEqual(self.app._prev_utilization.get('seven_day'), 20.0)


# ---------------------------------------------------------------------------
# Reset notifications
# ---------------------------------------------------------------------------

class TestResetNotifications(unittest.TestCase):
    """Tests for quota reset notifications in update()."""

    def setUp(self):
        self.app = _make_app()
        self._cmd_patch = patch('usage_monitor_for_codex.app.run_event_command')
        self._cmd_patch.start()

    def tearDown(self):
        self._cmd_patch.stop()
        _cleanup(self.app)

    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_5h_reset_notification(self, _icon, _tooltip):
        """Notification fires when 5h usage drops from >95% with 7d not blocking."""
        self.app._prev_utilization = {'five_hour': 97.0, 'seven_day': 50.0}
        data = {'five_hour': {'utilization': 10.0}, 'seven_day': {'utilization': 50.0}}
        self.app.cache = MagicMock()
        self.app.cache.update.return_value = UpdateResult(data=data)

        self.app.update()

        self.app.icon.notify.assert_called_once()

    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_5h_reset_suppressed_when_7d_blocking(self, _icon, _tooltip):
        """No 5h reset notification when 7d is at 99%+."""
        self.app._prev_utilization = {'five_hour': 97.0, 'seven_day': 50.0}
        data = {'five_hour': {'utilization': 10.0}, 'seven_day': {'utilization': 99.5}}
        self.app.cache = MagicMock()
        self.app.cache.update.return_value = UpdateResult(data=data)

        with patch.object(self.app, '_check_threshold_alerts'):
            self.app.update()

        self.app.icon.notify.assert_not_called()

    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_7d_reset_notification(self, _icon, _tooltip):
        """Notification fires when 7d usage drops from >98% with 5h not blocking."""
        self.app._prev_utilization = {'five_hour': 50.0, 'seven_day': 99.0}
        data = {'five_hour': {'utilization': 50.0}, 'seven_day': {'utilization': 10.0}}
        self.app.cache = MagicMock()
        self.app.cache.update.return_value = UpdateResult(data=data)

        self.app.update()

        self.app.icon.notify.assert_called_once()

    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_no_reset_notification_on_first_update(self, _icon, _tooltip):
        """No reset notification on first update (no previous values)."""
        data = {'five_hour': {'utilization': 10.0}, 'seven_day': {'utilization': 10.0}}
        self.app.cache = MagicMock()
        self.app.cache.update.return_value = UpdateResult(data=data)

        self.app.update()

        self.app.icon.notify.assert_not_called()

    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_update_ignores_non_dict_entries(self, _icon, _tooltip):
        """Non-dict entries in API response don't affect quota tracking."""
        self.app._prev_utilization = {'five_hour': 50.0}
        data = {
            'error_code': 'temporary',
            'rate_limited': False,
            'five_hour': {'utilization': 55.0},
        }
        self.app.cache = MagicMock()
        self.app.cache.update.return_value = UpdateResult(data=data)

        self.app.update()

        self.assertEqual(self.app._prev_utilization.get('five_hour'), 55.0)
        self.assertNotIn('error_code', self.app._prev_utilization)

    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_update_excludes_extra_usage_from_quota_tracking(self, _icon, _tooltip):
        """extra_usage is not tracked as a quota field for resets or fast polling."""
        data = {
            'five_hour': {'utilization': 42.0},
            'extra_usage': {'is_enabled': True, 'monthly_limit': 1000, 'used_credits': 500, 'utilization': 50.0},
        }
        self.app.cache = MagicMock()
        self.app.cache.update.return_value = UpdateResult(data=data)

        self.app.update()

        self.assertIn('five_hour', self.app._prev_utilization)
        self.assertNotIn('extra_usage', self.app._prev_utilization)

    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_update_handles_all_null_fields(self, _icon, _tooltip):
        """All-null quota fields produce empty tracking state."""
        data = {'five_hour': None, 'seven_day': None}
        self.app.cache = MagicMock()
        self.app.cache.update.return_value = UpdateResult(data=data)

        self.app.update()

        self.assertEqual(self.app._prev_utilization, {})

    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_7d_reset_suppressed_when_5h_blocking(self, _icon, _tooltip):
        """No 7d reset notification when 5h is at 99%+."""
        self.app._prev_utilization = {'five_hour': 50.0, 'seven_day': 99.0}
        data = {'five_hour': {'utilization': 99.5}, 'seven_day': {'utilization': 10.0}}
        self.app.cache = MagicMock()
        self.app.cache.update.return_value = UpdateResult(data=data)

        with patch.object(self.app, '_check_threshold_alerts'):
            self.app.update()

        self.app.icon.notify.assert_not_called()

    @patch('usage_monitor_for_codex.app.is_workstation_locked', return_value=True)
    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_5h_reset_notification_deferred_while_idle(self, _icon, _tooltip, _locked):
        """Reset notification is deferred (not shown) while user is away."""
        self.app._prev_utilization = {'five_hour': 97.0, 'seven_day': 50.0}
        data = {'five_hour': {'utilization': 10.0}, 'seven_day': {'utilization': 50.0}}
        self.app.cache = MagicMock()
        self.app.cache.update.return_value = UpdateResult(data=data)

        self.app.update()

        self.app.icon.notify.assert_not_called()
        self.assertEqual(len(self.app._deferred_notifications), 1)

    @patch('usage_monitor_for_codex.app.is_workstation_locked', return_value=True)
    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_deferred_notification_shown_on_flush(self, _icon, _tooltip, _locked):
        """Deferred notifications are shown when flushed."""
        self.app._prev_utilization = {'five_hour': 97.0, 'seven_day': 50.0}
        data = {'five_hour': {'utilization': 10.0}, 'seven_day': {'utilization': 50.0}}
        self.app.cache = MagicMock()
        self.app.cache.update.return_value = UpdateResult(data=data)

        self.app.update()
        self.app.icon.notify.assert_not_called()

        self.app._flush_deferred_notifications()

        self.app.icon.notify.assert_called_once()
        self.assertEqual(len(self.app._deferred_notifications), 0)

    @patch('usage_monitor_for_codex.app.is_workstation_locked', return_value=True)
    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_repeated_resets_while_idle_deduplicated(self, _icon, _tooltip, _locked):
        """Multiple reset drops while idle produce only one deferred notification."""
        self.app.cache = MagicMock()

        # First reset cycle: 97% -> 10%
        self.app._prev_utilization = {'five_hour': 97.0, 'seven_day': 50.0}
        data = {'five_hour': {'utilization': 10.0}, 'seven_day': {'utilization': 50.0}}
        self.app.cache.update.return_value = UpdateResult(data=data)
        self.app.update()

        # Second reset cycle: usage went back up on another device, then reset again
        self.app._prev_utilization['five_hour'] = 96.0
        data = {'five_hour': {'utilization': 5.0}, 'seven_day': {'utilization': 50.0}}
        self.app.cache.update.return_value = UpdateResult(data=data)
        self.app.update()

        # Only one deferred notification (same 'reset' category, latest wins)
        self.assertEqual(len(self.app._deferred_notifications), 1)

        self.app._flush_deferred_notifications()
        self.app.icon.notify.assert_called_once()

    @patch('usage_monitor_for_codex.app.ALERT_TIME_AWARE', False)
    @patch('usage_monitor_for_codex.app.is_workstation_locked', return_value=True)
    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_threshold_notifications_deferred_and_deduplicated(self, _icon, _tooltip, _locked):
        """Successive threshold crossings while idle keep only the latest notification per variant."""
        self.app._prev_utilization = {'five_hour': 50.0, 'seven_day': 10.0}
        self.app.cache = MagicMock()

        # Cross 80% threshold
        data = {'five_hour': {'utilization': 82.0, 'resets_at': '2025-01-15T18:00:00Z'}, 'seven_day': {'utilization': 10.0}}
        self.app.cache.update.return_value = UpdateResult(data=data)
        self.app.update()

        # Cross 95% threshold
        self.app._prev_utilization['five_hour'] = 82.0
        data = {'five_hour': {'utilization': 96.0, 'resets_at': '2025-01-15T18:00:00Z'}, 'seven_day': {'utilization': 10.0}}
        self.app.cache.update.return_value = UpdateResult(data=data)
        self.app.update()

        self.app.icon.notify.assert_not_called()
        # Only one deferred notification for threshold_five_hour (the 96% one)
        self.assertIn('threshold_five_hour', self.app._deferred_notifications)
        self.assertIn('96', self.app._deferred_notifications['threshold_five_hour'][0])

        self.app._flush_deferred_notifications()
        self.app.icon.notify.assert_called_once()


# ---------------------------------------------------------------------------
# Fast polling (adaptive)
# ---------------------------------------------------------------------------

class TestFastPolling(unittest.TestCase):
    """Tests for adaptive fast polling when session usage is increasing."""

    def setUp(self):
        self.app = _make_app()
        self._cmd_patch = patch('usage_monitor_for_codex.app.run_event_command')
        self._cmd_patch.start()

    def tearDown(self):
        self._cmd_patch.stop()
        _cleanup(self.app)

    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_fast_polling_starts_on_usage_increase(self, _icon, _tooltip):
        """Fast polls start when 5h usage is increasing."""
        self.app._prev_utilization = {'five_hour': 40.0, 'seven_day': 10.0}
        data = {'five_hour': {'utilization': 45.0}, 'seven_day': {'utilization': 10.0}}
        self.app.cache = MagicMock()
        self.app.cache.update.return_value = UpdateResult(data=data)

        self.app.update()

        self.assertGreater(self.app._fast_polls_remaining, 0)

    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_fast_polling_decrements(self, _icon, _tooltip):
        """Fast poll counter decrements when usage is stable."""
        self.app._prev_utilization = {'five_hour': 40.0, 'seven_day': 10.0}
        self.app._fast_polls_remaining = 2
        data = {'five_hour': {'utilization': 40.0}, 'seven_day': {'utilization': 10.0}}
        self.app.cache = MagicMock()
        self.app.cache.update.return_value = UpdateResult(data=data)

        self.app.update()

        self.assertEqual(self.app._fast_polls_remaining, 1)

    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_fast_polling_not_below_zero(self, _icon, _tooltip):
        """Fast poll counter does not go below zero."""
        self.app._prev_utilization = {'five_hour': 40.0, 'seven_day': 10.0}
        self.app._fast_polls_remaining = 0
        data = {'five_hour': {'utilization': 40.0}, 'seven_day': {'utilization': 10.0}}
        self.app.cache = MagicMock()
        self.app.cache.update.return_value = UpdateResult(data=data)

        self.app.update()

        self.assertEqual(self.app._fast_polls_remaining, 0)


# ---------------------------------------------------------------------------
# _render_tray
# ---------------------------------------------------------------------------

class TestRenderTray(unittest.TestCase):
    """Tests for _render_tray() icon and tooltip rendering."""

    def setUp(self):
        self.app = _make_app()

    def tearDown(self):
        _cleanup(self.app)

    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='Usage: 42%')
    def test_success_sets_tooltip(self, _tooltip):
        """Successful data sets the tray tooltip from format_tooltip."""
        self.app._last_response = {'five_hour': {'utilization': 42.0}, 'seven_day': {'utilization': 10.0}}
        self.app._render_tray()

        self.assertEqual(self.app.icon.title, 'Usage: 42%')

    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='Error')
    def test_error_sets_tooltip(self, _tooltip):
        """Error data still sets the tray tooltip (the icon stays the static brand mark)."""
        self.app._last_response = {'error': 'server down'}
        self.app._render_tray()

        self.assertEqual(self.app.icon.title, 'Error')

    def test_formats_current_last_response(self):
        """_render_tray formats whatever is in _last_response."""
        data = {'five_hour': {'utilization': 42.0}}
        self.app._last_response = data
        with patch('usage_monitor_for_codex.app.format_tooltip', return_value='t') as mock_fmt:
            self.app._render_tray()

        mock_fmt.assert_called_once_with(data)


# ---------------------------------------------------------------------------
# _calculate_poll_interval
# ---------------------------------------------------------------------------

class TestCalculatePollInterval(unittest.TestCase):
    """Tests for _calculate_poll_interval() adaptive interval logic."""

    def setUp(self):
        self.app = _make_app()

    def tearDown(self):
        _cleanup(self.app)

    def test_normal_interval(self):
        """Normal state returns POLL_INTERVAL."""
        self.app._last_response = {'five_hour': {'utilization': 50.0}}
        interval = self.app._calculate_poll_interval()
        self.assertEqual(interval, 180)

    def test_fast_polling_interval(self):
        """When fast polling is active, returns POLL_FAST."""
        self.app._last_response = {'five_hour': {'utilization': 50.0}}
        self.app._fast_polls_remaining = 3
        interval = self.app._calculate_poll_interval()
        self.assertEqual(interval, 120)

    def test_error_interval(self):
        """Transient error returns POLL_ERROR."""
        self.app._last_response = {'error': 'server down'}
        interval = self.app._calculate_poll_interval()
        self.assertEqual(interval, 30)

    def test_rate_limited_with_high_remaining(self):
        """Rate-limited uses cache.rate_limit_remaining for the interval."""
        self.app._last_response = {'error': 'rate limited', 'rate_limited': True}
        self.app.cache = MagicMock()
        self.app.cache.rate_limit_remaining = 300.0
        interval = self.app._calculate_poll_interval()
        self.assertEqual(interval, 300)

    def test_rate_limited_with_low_remaining(self):
        """Rate-limited with low remaining uses POLL_INTERVAL as minimum."""
        self.app._last_response = {'error': 'rate limited', 'rate_limited': True}
        self.app.cache = MagicMock()
        self.app.cache.rate_limit_remaining = 10.0
        interval = self.app._calculate_poll_interval()
        self.assertEqual(interval, 180)

    def test_rate_limited_with_large_remaining(self):
        """Rate-limited with large remaining uses that value."""
        self.app._last_response = {'error': 'rate limited', 'rate_limited': True}
        self.app.cache = MagicMock()
        self.app.cache.rate_limit_remaining = 480.0
        interval = self.app._calculate_poll_interval()
        self.assertEqual(interval, 480)

    def test_rate_limited_remaining_capped_by_cache(self):
        """Rate-limited remaining reflects cache's capped backoff (MAX_BACKOFF=900)."""
        self.app._last_response = {'error': 'rate limited', 'rate_limited': True}
        self.app.cache = MagicMock()
        self.app.cache.rate_limit_remaining = 900.0
        interval = self.app._calculate_poll_interval()
        self.assertEqual(interval, 900)

    def test_rate_limited_expired(self):
        """Rate-limited with expired backoff uses POLL_INTERVAL."""
        self.app._last_response = {'error': 'rate limited', 'rate_limited': True}
        self.app.cache = MagicMock()
        self.app.cache.rate_limit_remaining = 0.0
        interval = self.app._calculate_poll_interval()
        self.assertEqual(interval, 180)

    def test_empty_response_returns_normal_interval(self):
        """Empty _last_response (initial state) returns POLL_INTERVAL."""
        self.app._last_response = {}
        interval = self.app._calculate_poll_interval()
        self.assertEqual(interval, 180)


# ---------------------------------------------------------------------------
# _seconds_until_next_reset
# ---------------------------------------------------------------------------

class TestSecondsUntilNextReset(unittest.TestCase):
    """Tests for _seconds_until_next_reset() calculation."""

    def setUp(self):
        self.app = _make_app()

    def tearDown(self):
        _cleanup(self.app)

    def test_no_data_returns_none(self):
        """No response data returns None."""
        self.app._last_response = {}
        self.assertIsNone(self.app._seconds_until_next_reset())

    def test_no_resets_at_returns_none(self):
        """Entry without resets_at returns None."""
        self.app._last_response = {'five_hour': {'utilization': 50.0}}
        self.assertIsNone(self.app._seconds_until_next_reset())

    @patch('usage_monitor_for_codex.app.datetime')
    def test_returns_seconds_to_nearest_reset(self, mock_dt):
        """Returns seconds to the nearest future reset."""
        now = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        mock_dt.now.return_value = now
        mock_dt.fromisoformat = datetime.fromisoformat

        self.app._last_response = {
            'five_hour': {'utilization': 50.0, 'resets_at': '2025-01-15T12:30:00+00:00'},
            'seven_day': {'utilization': 30.0, 'resets_at': '2025-01-15T14:00:00+00:00'},
        }

        result = self.app._seconds_until_next_reset()
        assert result is not None
        self.assertAlmostEqual(result, 1800.0, places=0)  # 30 minutes

    @patch('usage_monitor_for_codex.app.datetime')
    def test_past_reset_ignored(self, mock_dt):
        """Past reset times are ignored."""
        now = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        mock_dt.now.return_value = now
        mock_dt.fromisoformat = datetime.fromisoformat

        self.app._last_response = {
            'five_hour': {'utilization': 50.0, 'resets_at': '2025-01-15T11:00:00+00:00'},
        }

        self.assertIsNone(self.app._seconds_until_next_reset())


# ---------------------------------------------------------------------------
# Poll interval reset alignment
# ---------------------------------------------------------------------------

class TestResetAlignment(unittest.TestCase):
    """Tests for poll interval alignment with imminent reset."""

    def setUp(self):
        self.app = _make_app()

    def tearDown(self):
        _cleanup(self.app)

    def test_imminent_reset_aligns_poll(self):
        """When reset is imminent, interval aligns to reset time."""
        self.app._last_response = {'five_hour': {'utilization': 50.0}}
        with patch.object(self.app, '_seconds_until_next_reset', return_value=160.0):
            interval = self.app._calculate_poll_interval()

        # next_reset(160) + 5 = 165 <= interval(180) * 1.5 = 270, so aligned
        # max(165, POLL_FAST=120) = 165
        self.assertEqual(interval, 165)

    def test_distant_reset_no_alignment(self):
        """When reset is far away, normal interval is used."""
        self.app._last_response = {'five_hour': {'utilization': 50.0}}
        with patch.object(self.app, '_seconds_until_next_reset', return_value=500.0):
            interval = self.app._calculate_poll_interval()

        # next_reset(500) + 5 = 505 > interval(180) * 1.5 = 270, no alignment
        self.assertEqual(interval, 180)

    def test_reset_alignment_sets_fast_polls(self):
        """Reset alignment sets fast_polls_remaining for post-reset follow-up."""
        self.app._last_response = {'five_hour': {'utilization': 50.0}}
        self.app._fast_polls_remaining = 0
        with patch.object(self.app, '_seconds_until_next_reset', return_value=100.0):
            self.app._calculate_poll_interval()

        self.assertGreaterEqual(self.app._fast_polls_remaining, 2)


# ---------------------------------------------------------------------------
# Menu actions
# ---------------------------------------------------------------------------

class TestMenuActions(unittest.TestCase):
    """Tests for menu action methods."""

    def setUp(self):
        self.app = _make_app()

    def tearDown(self):
        _cleanup(self.app)

    def test_on_show_popup_guards_against_double_open(self):
        """on_show_popup() does nothing when popup is already open."""
        self.app._popup_open = True
        with patch('usage_monitor_for_codex.app.threading.Thread') as mock_thread:
            self.app.on_show_popup()
            mock_thread.assert_not_called()

    def test_on_quit_stops_running(self):
        """on_quit() sets running to False and stops the icon."""
        self.app.on_quit()
        self.assertFalse(self.app.running)
        self.app.icon.stop.assert_called_once()


# ---------------------------------------------------------------------------
# _is_user_away (idle/lock detection)
# ---------------------------------------------------------------------------

class TestIsUserAway(unittest.TestCase):
    """Tests for _is_user_away() idle and lock detection."""

    def setUp(self):
        self.app = _make_app()

    def tearDown(self):
        _cleanup(self.app)

    @patch('usage_monitor_for_codex.app.is_workstation_locked', return_value=True)
    def test_locked_is_away(self, _locked):
        """User is away when workstation is locked."""
        self.assertTrue(self.app._is_user_away())

    @patch('usage_monitor_for_codex.app.is_workstation_locked', return_value=False)
    @patch('usage_monitor_for_codex.app.get_idle_seconds', return_value=400.0)
    @patch('usage_monitor_for_codex.app.IDLE_PAUSE', 300)
    def test_idle_over_threshold_is_away(self, _idle, _locked):
        """User is away when idle time exceeds IDLE_PAUSE."""
        self.assertTrue(self.app._is_user_away())

    @patch('usage_monitor_for_codex.app.is_workstation_locked', return_value=False)
    @patch('usage_monitor_for_codex.app.get_idle_seconds', return_value=200.0)
    @patch('usage_monitor_for_codex.app.IDLE_PAUSE', 300)
    def test_idle_under_threshold_not_away(self, _idle, _locked):
        """User is not away when idle time is below IDLE_PAUSE."""
        self.assertFalse(self.app._is_user_away())

    @patch('usage_monitor_for_codex.app.is_workstation_locked', return_value=False)
    @patch('usage_monitor_for_codex.app.get_idle_seconds', return_value=300.0)
    @patch('usage_monitor_for_codex.app.IDLE_PAUSE', 300)
    def test_idle_exactly_at_threshold_is_away(self, _idle, _locked):
        """User is away when idle time equals IDLE_PAUSE exactly."""
        self.assertTrue(self.app._is_user_away())

    @patch('usage_monitor_for_codex.app.is_workstation_locked', return_value=False)
    @patch('usage_monitor_for_codex.app.get_idle_seconds', return_value=9999.0)
    @patch('usage_monitor_for_codex.app.IDLE_PAUSE', 0)
    def test_idle_disabled_with_zero(self, _idle, _locked):
        """Idle detection disabled when IDLE_PAUSE is 0."""
        self.assertFalse(self.app._is_user_away())

    @patch('usage_monitor_for_codex.app.is_workstation_locked', return_value=True)
    @patch('usage_monitor_for_codex.app.IDLE_PAUSE', 0)
    def test_locked_detected_even_when_idle_disabled(self, _locked):
        """Lock detection works even when idle detection is disabled."""
        self.assertTrue(self.app._is_user_away())

    @patch('usage_monitor_for_codex.app.is_workstation_locked', return_value=False)
    @patch('usage_monitor_for_codex.app.get_idle_seconds', return_value=0.0)
    @patch('usage_monitor_for_codex.app.IDLE_PAUSE', 300)
    def test_active_user_not_away(self, _idle, _locked):
        """User is not away when active (0 idle seconds)."""
        self.assertFalse(self.app._is_user_away())


# ---------------------------------------------------------------------------
# _wait_for_activity
# ---------------------------------------------------------------------------

class TestWaitForActivity(unittest.TestCase):
    """Tests for _wait_for_activity() blocking behavior."""

    def setUp(self):
        self.app = _make_app()

    def tearDown(self):
        _cleanup(self.app)

    @patch('usage_monitor_for_codex.app.time.sleep')
    def test_exits_when_activity_resumes(self, mock_sleep):
        """Stops blocking when _is_user_away returns False."""
        with patch.object(self.app, '_is_user_away', side_effect=[True, True, False]):
            self.app._wait_for_activity()
        self.assertEqual(mock_sleep.call_count, 2)

    @patch('usage_monitor_for_codex.app.time.sleep')
    def test_exits_when_running_false(self, mock_sleep):
        """Stops blocking when running is set to False."""
        self.app.running = False
        with patch.object(self.app, '_is_user_away', return_value=True):
            self.app._wait_for_activity()
        mock_sleep.assert_not_called()

    @patch('usage_monitor_for_codex.app.time.sleep')
    def test_returns_immediately_if_not_away(self, mock_sleep):
        """Returns immediately when user is not away."""
        with patch.object(self.app, '_is_user_away', return_value=False):
            self.app._wait_for_activity()
        mock_sleep.assert_not_called()

    @patch('usage_monitor_for_codex.app.time.sleep')
    @patch('usage_monitor_for_codex.app.time.time')
    def test_until_deadline_exits_while_still_away(self, mock_time, mock_sleep):
        """Exits when deadline is reached even if user is still away."""
        mock_time.side_effect = [100.0, 105.0]  # first call < deadline, second >= deadline
        with patch.object(self.app, '_is_user_away', return_value=True):
            self.app._wait_for_activity(until=105.0)
        self.assertEqual(mock_sleep.call_count, 1)

    @patch('usage_monitor_for_codex.app.time.sleep')
    @patch('usage_monitor_for_codex.app.time.time')
    def test_until_deadline_already_passed(self, mock_time, mock_sleep):
        """Exits immediately when deadline is already in the past."""
        mock_time.return_value = 200.0
        with patch.object(self.app, '_is_user_away', return_value=True):
            self.app._wait_for_activity(until=100.0)
        mock_sleep.assert_not_called()

    @patch('usage_monitor_for_codex.app.time.sleep')
    def test_until_none_blocks_until_activity(self, mock_sleep):
        """With until=None, behaves like the original - blocks until activity."""
        with patch.object(self.app, '_is_user_away', side_effect=[True, True, False]):
            self.app._wait_for_activity(until=None)
        self.assertEqual(mock_sleep.call_count, 2)

    @patch('usage_monitor_for_codex.app.time.sleep')
    @patch('usage_monitor_for_codex.app.time.time')
    def test_until_user_returns_before_deadline(self, mock_time, mock_sleep):
        """Exits when user returns even if deadline has not been reached."""
        mock_time.return_value = 100.0  # well before deadline
        with patch.object(self.app, '_is_user_away', side_effect=[True, False]):
            self.app._wait_for_activity(until=999.0)
        self.assertEqual(mock_sleep.call_count, 1)


# ---------------------------------------------------------------------------
# Event command integration
# ---------------------------------------------------------------------------

class TestResetCommand(unittest.TestCase):
    """Tests for on_reset_command execution during quota reset."""

    def setUp(self):
        self.app = _make_app()

    def tearDown(self):
        _cleanup(self.app)

    @patch('usage_monitor_for_codex.app.ON_RESET_COMMAND', ['echo reset'])
    @patch('usage_monitor_for_codex.app.run_event_command')
    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_reset_command_fires_on_5h_drop(self, _icon, _tooltip, mock_cmd):
        """Reset command fires when 5h usage drops."""
        self.app._prev_utilization = {'five_hour': 98.0, 'seven_day': 10.0}
        data = {'five_hour': {'utilization': 20.0, 'resets_at': '2025-01-15T18:00:00Z'}, 'seven_day': {'utilization': 10.0}}
        self.app.cache = MagicMock()
        self.app.cache.update.return_value = UpdateResult(data=data)

        self.app.update()

        mock_cmd.assert_called_once()
        cmd, env = mock_cmd.call_args[0]
        self.assertEqual(cmd, ['echo reset'])
        self.assertEqual(env['USAGE_MONITOR_EVENT'], 'reset')
        self.assertEqual(env['USAGE_MONITOR_VARIANT'], 'five_hour')
        self.assertEqual(env['USAGE_MONITOR_UTILIZATION'], '20')
        self.assertEqual(env['USAGE_MONITOR_PREV_UTILIZATION'], '98')
        self.assertEqual(env['USAGE_MONITOR_UTILIZATION_FIVE_HOUR'], '20')
        self.assertEqual(env['USAGE_MONITOR_UTILIZATION_SEVEN_DAY'], '10')
        self.assertEqual(env['USAGE_MONITOR_RESETS_AT'], '2025-01-15T18:00:00Z')

    @patch('usage_monitor_for_codex.app.ON_RESET_COMMAND', ['echo reset'])
    @patch('usage_monitor_for_codex.app.run_event_command')
    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_reset_command_fires_on_7d_drop(self, _icon, _tooltip, mock_cmd):
        """Reset command fires when 7d usage drops from near-exhaustion."""
        self.app._prev_utilization = {'five_hour': 50.0, 'seven_day': 99.0}
        data = {'five_hour': {'utilization': 50.0}, 'seven_day': {'utilization': 10.0, 'resets_at': '2025-01-20T00:00:00Z'}}
        self.app.cache = MagicMock()
        self.app.cache.update.return_value = UpdateResult(data=data)

        self.app.update()

        mock_cmd.assert_called_once()
        env = mock_cmd.call_args[0][1]
        self.assertEqual(env['USAGE_MONITOR_VARIANT'], 'seven_day')
        self.assertEqual(env['USAGE_MONITOR_PREV_UTILIZATION'], '99')
        self.assertEqual(env['USAGE_MONITOR_UTILIZATION_FIVE_HOUR'], '50')
        self.assertEqual(env['USAGE_MONITOR_UTILIZATION_SEVEN_DAY'], '10')

    @patch('usage_monitor_for_codex.app.ON_RESET_COMMAND', ['echo reset'])
    @patch('usage_monitor_for_codex.app.run_event_command')
    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_reset_command_not_fired_on_minor_drop(self, _icon, _tooltip, mock_cmd):
        """A minor dip of the rolling window is NOT a reset and must not run the command."""
        self.app._prev_utilization = {'five_hour': 30.0, 'seven_day': 10.0}
        data = {'five_hour': {'utilization': 5.0}, 'seven_day': {'utilization': 10.0}}
        self.app.cache = MagicMock()
        self.app.cache.update.return_value = UpdateResult(data=data)

        self.app.update()

        mock_cmd.assert_not_called()

    @patch('usage_monitor_for_codex.app.ON_RESET_COMMAND', ['echo reset'])
    @patch('usage_monitor_for_codex.app.run_event_command')
    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_reset_command_missing_resets_at(self, _icon, _tooltip, mock_cmd):
        """USAGE_MONITOR_RESETS_AT is empty string when resets_at is absent from data."""
        self.app._prev_utilization = {'five_hour': 96.0, 'seven_day': 10.0}
        data = {'five_hour': {'utilization': 5.0}, 'seven_day': {'utilization': 10.0}}
        self.app.cache = MagicMock()
        self.app.cache.update.return_value = UpdateResult(data=data)

        self.app.update()

        mock_cmd.assert_called_once()
        env = mock_cmd.call_args[0][1]
        self.assertEqual(env['USAGE_MONITOR_RESETS_AT'], '')

    @patch('usage_monitor_for_codex.app.ON_RESET_COMMAND', [])
    @patch('usage_monitor_for_codex.app.run_event_command')
    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_no_command_when_setting_empty(self, _icon, _tooltip, mock_cmd):
        """No command executed when on_reset_command is empty."""
        self.app._prev_utilization = {'five_hour': 98.0, 'seven_day': 10.0}
        data = {'five_hour': {'utilization': 20.0}, 'seven_day': {'utilization': 10.0}}
        self.app.cache = MagicMock()
        self.app.cache.update.return_value = UpdateResult(data=data)

        self.app.update()

        mock_cmd.assert_not_called()

    @patch('usage_monitor_for_codex.app.ON_RESET_COMMAND', ['echo reset'])
    @patch('usage_monitor_for_codex.app.run_event_command')
    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_no_command_when_usage_increases(self, _icon, _tooltip, mock_cmd):
        """No command when usage is increasing."""
        self.app._prev_utilization = {'five_hour': 50.0, 'seven_day': 10.0}
        data = {'five_hour': {'utilization': 55.0}, 'seven_day': {'utilization': 10.0}}
        self.app.cache = MagicMock()
        self.app.cache.update.return_value = UpdateResult(data=data)

        self.app.update()

        mock_cmd.assert_not_called()

    @patch('usage_monitor_for_codex.app.ON_RESET_COMMAND', ['echo reset'])
    @patch('usage_monitor_for_codex.app.run_event_command')
    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_both_quotas_drop_fires_two_commands(self, _icon, _tooltip, mock_cmd):
        """Two commands fire when both 5h and 7d usage drop from near-exhaustion."""
        self.app._prev_utilization = {'five_hour': 96.0, 'seven_day': 99.0}
        data = {'five_hour': {'utilization': 10.0}, 'seven_day': {'utilization': 20.0}}
        self.app.cache = MagicMock()
        self.app.cache.update.return_value = UpdateResult(data=data)

        self.app.update()

        self.assertEqual(mock_cmd.call_count, 2)
        variants = {call[0][1]['USAGE_MONITOR_VARIANT'] for call in mock_cmd.call_args_list}
        self.assertEqual(variants, {'five_hour', 'seven_day'})

    @patch('usage_monitor_for_codex.app.ON_RESET_COMMAND', ['echo reset'])
    @patch('usage_monitor_for_codex.app.run_event_command')
    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_no_command_on_first_update(self, _icon, _tooltip, mock_cmd):
        """No reset command on first update (no previous values)."""
        data = {'five_hour': {'utilization': 50.0}, 'seven_day': {'utilization': 10.0}}
        self.app.cache = MagicMock()
        self.app.cache.update.return_value = UpdateResult(data=data)

        self.app.update()

        mock_cmd.assert_not_called()

    @patch('usage_monitor_for_codex.app.ON_RESET_COMMAND', ['echo reset'])
    @patch('usage_monitor_for_codex.app.run_event_command')
    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_no_command_when_usage_stable(self, _icon, _tooltip, mock_cmd):
        """No command when usage stays the same."""
        self.app._prev_utilization = {'five_hour': 50.0, 'seven_day': 10.0}
        data = {'five_hour': {'utilization': 50.0}, 'seven_day': {'utilization': 10.0}}
        self.app.cache = MagicMock()
        self.app.cache.update.return_value = UpdateResult(data=data)

        self.app.update()

        mock_cmd.assert_not_called()

    @patch('usage_monitor_for_codex.app.ON_RESET_COMMAND', ['echo reset'])
    @patch('usage_monitor_for_codex.app.run_event_command')
    @patch('usage_monitor_for_codex.app.is_workstation_locked', return_value=True)
    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_reset_command_fires_while_notification_deferred(self, _icon, _tooltip, _locked, mock_cmd):
        """Reset command fires immediately even when notification is deferred due to idle/lock."""
        self.app._prev_utilization = {'five_hour': 97.0, 'seven_day': 50.0}
        data = {'five_hour': {'utilization': 10.0}, 'seven_day': {'utilization': 50.0}}
        self.app.cache = MagicMock()
        self.app.cache.update.return_value = UpdateResult(data=data)

        self.app.update()

        mock_cmd.assert_called_once()
        self.assertEqual(mock_cmd.call_args[0][1]['USAGE_MONITOR_EVENT'], 'reset')
        self.assertIn('reset', self.app._deferred_notifications)
        self.app.icon.notify.assert_not_called()


class TestThresholdCommand(unittest.TestCase):
    """Tests for on_threshold_command execution during threshold alerts."""

    def setUp(self):
        self.app = _make_app()
        self.app._prev_utilization = {'five_hour': 0.0}
        self.app._first_update_done = True

    def tearDown(self):
        _cleanup(self.app)

    @patch('usage_monitor_for_codex.app.ON_THRESHOLD_COMMAND', ['notify.bat'])
    @patch('usage_monitor_for_codex.app.ALERT_TIME_AWARE', False)
    @patch('usage_monitor_for_codex.app.run_event_command')
    def test_threshold_command_fires_on_crossing(self, mock_cmd):
        """Threshold command fires when usage crosses a configured threshold."""
        self.app._check_threshold_alerts({'five_hour': {'utilization': 85.0, 'resets_at': '2025-01-15T18:00:00Z'}})

        mock_cmd.assert_called_once()
        cmd, env = mock_cmd.call_args[0]
        self.assertEqual(cmd, ['notify.bat'])
        self.assertEqual(env['USAGE_MONITOR_EVENT'], 'threshold')
        self.assertEqual(env['USAGE_MONITOR_VARIANT'], 'five_hour')
        self.assertEqual(env['USAGE_MONITOR_UTILIZATION'], '85')
        self.assertEqual(env['USAGE_MONITOR_THRESHOLD'], '80')
        self.assertEqual(env['USAGE_MONITOR_RESETS_AT'], '2025-01-15T18:00:00Z')
        self.assertIn('USAGE_MONITOR_TITLE', env)
        self.assertIn('USAGE_MONITOR_MESSAGE', env)

    @patch('usage_monitor_for_codex.app.ON_THRESHOLD_COMMAND', [])
    @patch('usage_monitor_for_codex.app.run_event_command')
    def test_no_command_when_setting_empty(self, mock_cmd):
        """No command executed when on_threshold_command is empty."""
        self.app._check_threshold_alerts({'five_hour': {'utilization': 85.0}})

        mock_cmd.assert_not_called()

    @patch('usage_monitor_for_codex.app.ON_THRESHOLD_COMMAND', ['notify.bat'])
    @patch('usage_monitor_for_codex.app.run_event_command')
    def test_no_command_below_threshold(self, mock_cmd):
        """No command when usage is below all thresholds."""
        self.app._check_threshold_alerts({'five_hour': {'utilization': 50.0}})

        mock_cmd.assert_not_called()

    @patch('usage_monitor_for_codex.app.ON_THRESHOLD_COMMAND', ['notify.bat'])
    @patch('usage_monitor_for_codex.app.run_event_command')
    def test_no_duplicate_command(self, mock_cmd):
        """No duplicate command for same threshold."""
        self.app._check_threshold_alerts({'five_hour': {'utilization': 85.0}})
        mock_cmd.reset_mock()

        self.app._check_threshold_alerts({'five_hour': {'utilization': 88.0}})

        mock_cmd.assert_not_called()

    @patch('usage_monitor_for_codex.app.ON_THRESHOLD_COMMAND', ['notify.bat'])
    @patch('usage_monitor_for_codex.app.run_event_command')
    def test_command_for_higher_threshold(self, mock_cmd):
        """Command fires again when usage crosses the next higher threshold."""
        self.app._check_threshold_alerts({'five_hour': {'utilization': 85.0}})
        mock_cmd.reset_mock()

        self.app._check_threshold_alerts({'five_hour': {'utilization': 97.0}})

        mock_cmd.assert_called_once()
        env = mock_cmd.call_args[0][1]
        self.assertEqual(env['USAGE_MONITOR_THRESHOLD'], '95')
        self.assertEqual(env['USAGE_MONITOR_UTILIZATION'], '97')

    @patch('usage_monitor_for_codex.app.ON_THRESHOLD_COMMAND', ['notify.bat'])
    @patch('usage_monitor_for_codex.app.ALERT_TIME_AWARE', True)
    @patch('usage_monitor_for_codex.app.ALERT_TIME_AWARE_BELOW', 90)
    @patch('usage_monitor_for_codex.app.run_event_command')
    def test_time_aware_suppression_suppresses_command(self, mock_cmd):
        """Time-aware suppression also suppresses the command."""
        with patch('usage_monitor_for_codex.app.elapsed_pct', return_value=90.0):
            self.app._check_threshold_alerts({'five_hour': {'utilization': 82.0, 'resets_at': '2025-01-15T18:00:00Z'}})

        mock_cmd.assert_not_called()

    @patch('usage_monitor_for_codex.app.ON_THRESHOLD_COMMAND', ['notify.bat'])
    @patch('usage_monitor_for_codex.app.ALERT_TIME_AWARE', False)
    @patch('usage_monitor_for_codex.app.run_event_command')
    def test_no_command_on_first_update(self, mock_cmd):
        """Threshold command is suppressed on first update (notification still fires)."""
        self.app._first_update_done = False

        self.app._check_threshold_alerts({'five_hour': {'utilization': 85.0, 'resets_at': '2025-01-15T18:00:00Z'}})

        # Notification fires (threshold was exceeded), but command does not
        self.app.icon.notify.assert_called_once()
        mock_cmd.assert_not_called()

    @patch('usage_monitor_for_codex.app.ON_THRESHOLD_COMMAND', ['notify.bat'])
    @patch('usage_monitor_for_codex.app.ALERT_TIME_AWARE', False)
    @patch('usage_monitor_for_codex.app.run_event_command')
    @patch('usage_monitor_for_codex.app.is_workstation_locked', return_value=True)
    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_threshold_command_fires_while_notification_deferred(self, _icon, _tooltip, _locked, mock_cmd):
        """Threshold command fires immediately even when notification is deferred due to idle/lock."""
        self.app._prev_utilization = {'five_hour': 50.0, 'seven_day': 10.0}
        data = {'five_hour': {'utilization': 85.0, 'resets_at': '2025-01-15T18:00:00Z'}, 'seven_day': {'utilization': 10.0}}
        self.app.cache = MagicMock()
        self.app.cache.update.return_value = UpdateResult(data=data)

        self.app.update()

        mock_cmd.assert_called_once()
        self.assertEqual(mock_cmd.call_args[0][1]['USAGE_MONITOR_EVENT'], 'threshold')
        self.assertIn('threshold_five_hour', self.app._deferred_notifications)
        self.app.icon.notify.assert_not_called()


class TestExtraUsageCommand(unittest.TestCase):
    """Tests for on_threshold_command with extra usage events."""

    def setUp(self):
        self.app = _make_app()
        self.app._prev_utilization = {'five_hour': 0.0}
        self.app._first_update_done = True

    def tearDown(self):
        _cleanup(self.app)

    @patch('usage_monitor_for_codex.app.ON_THRESHOLD_COMMAND', ['notify.bat'])
    @patch('usage_monitor_for_codex.app.run_event_command')
    def test_extra_usage_command_includes_amounts(self, mock_cmd):
        """Extra usage threshold command includes used and limit amounts."""
        data = {
            'extra_usage': {'is_enabled': True, 'monthly_limit': 1000, 'used_credits': 850},
        }
        self.app._check_threshold_alerts(data)

        mock_cmd.assert_called_once()
        env = mock_cmd.call_args[0][1]
        self.assertEqual(env['USAGE_MONITOR_VARIANT'], 'extra_usage')
        self.assertIn('USAGE_MONITOR_EXTRA_USED', env)
        self.assertIn('USAGE_MONITOR_EXTRA_LIMIT', env)

    @patch('usage_monitor_for_codex.app.ON_THRESHOLD_COMMAND', [])
    @patch('usage_monitor_for_codex.app.run_event_command')
    def test_extra_usage_no_command_when_empty(self, mock_cmd):
        """No command for extra usage when setting is empty."""
        data = {
            'extra_usage': {'is_enabled': True, 'monthly_limit': 1000, 'used_credits': 850},
        }
        self.app._check_threshold_alerts(data)

        mock_cmd.assert_not_called()


# ---------------------------------------------------------------------------
# Test event command handlers (tray context menu)
# ---------------------------------------------------------------------------

class TestTestEventCommands(unittest.TestCase):
    """Tests for on_test_* handlers that fire sample event commands from the tray menu."""

    def setUp(self):
        self.app = _make_app()

    def tearDown(self):
        _cleanup(self.app)

    @patch('usage_monitor_for_codex.app.ON_RESET_COMMAND', ['echo reset'])
    @patch('usage_monitor_for_codex.app.run_event_command')
    def test_reset_5h_fires_with_correct_env(self, mock_cmd):
        """Test reset 5h handler passes all required env vars with correct values."""
        self.app.on_test_reset_5h()

        mock_cmd.assert_called_once()
        cmd, env = mock_cmd.call_args[0]
        self.assertEqual(cmd, ['echo reset'])
        self.assertEqual(env['USAGE_MONITOR_EVENT'], 'reset')
        self.assertEqual(env['USAGE_MONITOR_VARIANT'], 'five_hour')
        self.assertEqual(env['USAGE_MONITOR_UTILIZATION'], '0')
        self.assertEqual(env['USAGE_MONITOR_PREV_UTILIZATION'], '95')
        self.assertEqual(env['USAGE_MONITOR_UTILIZATION_FIVE_HOUR'], '0')
        self.assertEqual(env['USAGE_MONITOR_UTILIZATION_SEVEN_DAY'], '45')
        self.assertIn('USAGE_MONITOR_RESETS_AT', env)
        self.assertIn('USAGE_MONITOR_TITLE', env)
        self.assertIn('USAGE_MONITOR_MESSAGE', env)

    @patch('usage_monitor_for_codex.app.ON_RESET_COMMAND', ['echo reset'])
    @patch('usage_monitor_for_codex.app.run_event_command')
    def test_reset_7d_fires_with_correct_env(self, mock_cmd):
        """Test reset 7d handler passes all required env vars with correct values."""
        self.app.on_test_reset_7d()

        mock_cmd.assert_called_once()
        cmd, env = mock_cmd.call_args[0]
        self.assertEqual(cmd, ['echo reset'])
        self.assertEqual(env['USAGE_MONITOR_EVENT'], 'reset')
        self.assertEqual(env['USAGE_MONITOR_VARIANT'], 'seven_day')
        self.assertEqual(env['USAGE_MONITOR_UTILIZATION'], '0')
        self.assertEqual(env['USAGE_MONITOR_PREV_UTILIZATION'], '99')
        self.assertEqual(env['USAGE_MONITOR_UTILIZATION_FIVE_HOUR'], '12')
        self.assertEqual(env['USAGE_MONITOR_UTILIZATION_SEVEN_DAY'], '0')
        self.assertIn('USAGE_MONITOR_RESETS_AT', env)

    @patch('usage_monitor_for_codex.app.ON_THRESHOLD_COMMAND', ['notify.bat'])
    @patch('usage_monitor_for_codex.app.run_event_command')
    def test_threshold_5h_fires_with_correct_env(self, mock_cmd):
        """Test threshold 5h handler passes all required env vars with correct values."""
        self.app.on_test_threshold_5h()

        mock_cmd.assert_called_once()
        cmd, env = mock_cmd.call_args[0]
        self.assertEqual(cmd, ['notify.bat'])
        self.assertEqual(env['USAGE_MONITOR_EVENT'], 'threshold')
        self.assertEqual(env['USAGE_MONITOR_VARIANT'], 'five_hour')
        self.assertEqual(env['USAGE_MONITOR_UTILIZATION'], '82')
        self.assertEqual(env['USAGE_MONITOR_THRESHOLD'], '80')
        self.assertIn('USAGE_MONITOR_RESETS_AT', env)
        self.assertIn('USAGE_MONITOR_TITLE', env)
        self.assertIn('USAGE_MONITOR_MESSAGE', env)

    @patch('usage_monitor_for_codex.app.ON_THRESHOLD_COMMAND', ['notify.bat'])
    @patch('usage_monitor_for_codex.app.run_event_command')
    def test_threshold_7d_fires_with_correct_env(self, mock_cmd):
        """Test threshold 7d handler passes all required env vars with correct values."""
        self.app.on_test_threshold_7d()

        mock_cmd.assert_called_once()
        cmd, env = mock_cmd.call_args[0]
        self.assertEqual(cmd, ['notify.bat'])
        self.assertEqual(env['USAGE_MONITOR_EVENT'], 'threshold')
        self.assertEqual(env['USAGE_MONITOR_VARIANT'], 'seven_day')
        self.assertEqual(env['USAGE_MONITOR_UTILIZATION'], '81')
        self.assertEqual(env['USAGE_MONITOR_THRESHOLD'], '80')
        self.assertIn('USAGE_MONITOR_RESETS_AT', env)

    @patch('usage_monitor_for_codex.app.ON_RESET_COMMAND', ['echo reset'])
    @patch('usage_monitor_for_codex.app.run_event_command')
    def test_reset_5h_resets_at_is_valid_iso_timestamp(self, mock_cmd):
        """USAGE_MONITOR_RESETS_AT is a parseable ISO 8601 timestamp in the future."""
        self.app.on_test_reset_5h()

        env = mock_cmd.call_args[0][1]
        resets_at = datetime.fromisoformat(env['USAGE_MONITOR_RESETS_AT'])
        self.assertGreater(resets_at, datetime.now(timezone.utc))

    @patch('usage_monitor_for_codex.app.ON_THRESHOLD_COMMAND', ['notify.bat'])
    @patch('usage_monitor_for_codex.app.run_event_command')
    def test_threshold_5h_resets_at_is_valid_iso_timestamp(self, mock_cmd):
        """USAGE_MONITOR_RESETS_AT is a parseable ISO 8601 timestamp in the future."""
        self.app.on_test_threshold_5h()

        env = mock_cmd.call_args[0][1]
        resets_at = datetime.fromisoformat(env['USAGE_MONITOR_RESETS_AT'])
        self.assertGreater(resets_at, datetime.now(timezone.utc))

    @patch('usage_monitor_for_codex.app.ON_THRESHOLD_COMMAND', ['notify.bat'])
    @patch('usage_monitor_for_codex.app.run_event_command')
    def test_threshold_message_contains_utilization_pct(self, mock_cmd):
        """USAGE_MONITOR_MESSAGE includes the utilization percentage."""
        self.app.on_test_threshold_5h()

        env = mock_cmd.call_args[0][1]
        self.assertIn('82', env['USAGE_MONITOR_MESSAGE'])


# ---------------------------------------------------------------------------
# poll_loop idle interruption for reset commands
# ---------------------------------------------------------------------------

class TestPollLoopIdleInterruption(unittest.TestCase):
    """Tests for poll_loop waking from idle to fire reset commands."""

    def setUp(self):
        self.app = _make_app()
        self.app.cache = MagicMock()
        self.app.cache.ensure_profile = MagicMock()
        self.app.cache.last_success_time = 0.0

    def tearDown(self):
        _cleanup(self.app)

    @patch('usage_monitor_for_codex.app.ON_RESET_COMMAND', ['echo reset'])
    @patch('usage_monitor_for_codex.app.time.sleep')
    @patch('usage_monitor_for_codex.app.time.time')
    def test_idle_interrupted_for_imminent_reset(self, mock_time, mock_sleep):
        """When idle and on_reset_command is set, idle wait is interrupted at reset deadline."""
        # Simulate: update runs, then inner loop detects idle, _wait_for_activity
        # returns at deadline while still idle, breaks to poll again.
        call_count = [0]

        def update_side_effect():
            call_count[0] += 1
            if call_count[0] >= 2:
                self.app.running = False

        mock_time.side_effect = [
            100.0,   # target = time() + interval
            100.0,   # inner loop: time() < target
            100.0,   # _wait_for_activity: time() for deadline calc
            200.0,   # second iteration target
            200.0,   # inner loop exits (running=False)
        ]

        with patch.object(self.app, 'update', side_effect=update_side_effect), \
             patch.object(self.app, '_calculate_poll_interval', return_value=180), \
             patch.object(self.app, '_is_user_away', return_value=True), \
             patch.object(self.app, '_wait_for_activity'), \
             patch.object(self.app, '_seconds_until_next_reset', return_value=30.0):
            self.app.poll_loop()

        self.assertEqual(call_count[0], 2)

    @patch('usage_monitor_for_codex.app.ON_RESET_COMMAND', [])
    @patch('usage_monitor_for_codex.app.time.sleep')
    @patch('usage_monitor_for_codex.app.time.time')
    def test_idle_not_interrupted_without_reset_command(self, mock_time, mock_sleep):
        """When on_reset_command is empty, idle wait uses no deadline."""
        wait_calls = []

        def capture_wait(until=None):
            wait_calls.append(until)
            self.app.running = False

        mock_time.side_effect = [
            100.0,   # target = time() + interval
            100.0,   # inner loop: time() < target
            200.0,   # after _wait_for_activity: time() - lst >= interval
        ]

        with patch.object(self.app, 'update'), \
             patch.object(self.app, '_calculate_poll_interval', return_value=180), \
             patch.object(self.app, '_is_user_away', return_value=True), \
             patch.object(self.app, '_wait_for_activity', side_effect=capture_wait), \
             patch.object(self.app, '_seconds_until_next_reset', return_value=30.0):
            self.app.poll_loop()

        self.assertEqual(wait_calls, [None])

    @patch('usage_monitor_for_codex.app.ON_RESET_COMMAND', ['echo reset'])
    @patch('usage_monitor_for_codex.app.time.sleep')
    @patch('usage_monitor_for_codex.app.time.time')
    def test_idle_no_deadline_when_no_imminent_reset(self, mock_time, mock_sleep):
        """When on_reset_command is set but no reset is imminent, idle wait uses no deadline."""
        wait_calls = []

        def capture_wait(until=None):
            wait_calls.append(until)
            self.app.running = False

        mock_time.side_effect = [
            100.0,   # target = time() + interval
            100.0,   # inner loop: time() < target
            200.0,   # after _wait_for_activity: time() - lst >= interval
        ]

        with patch.object(self.app, 'update'), \
             patch.object(self.app, '_calculate_poll_interval', return_value=180), \
             patch.object(self.app, '_is_user_away', return_value=True), \
             patch.object(self.app, '_wait_for_activity', side_effect=capture_wait), \
             patch.object(self.app, '_seconds_until_next_reset', return_value=None):
            self.app.poll_loop()

        self.assertEqual(wait_calls, [None])


    @patch('usage_monitor_for_codex.app.ON_RESET_COMMAND', ['echo reset'])
    @patch('usage_monitor_for_codex.app.POLL_INTERVAL', 180)
    @patch('usage_monitor_for_codex.app.time.sleep')
    @patch('usage_monitor_for_codex.app.time.time')
    def test_idle_retries_until_reset_confirmed(self, mock_time, mock_sleep):
        """After reset deadline poll, keeps retrying at POLL_INTERVAL until reset is confirmed."""
        wait_calls = []

        def capture_wait(until=None):
            wait_calls.append(until)

        update_count = [0]

        def update_side_effect():
            update_count[0] += 1
            if update_count[0] >= 3:
                self.app.running = False

        # First iteration: imminent reset at +30s -> deadline at now+35, sets _idle_reset_pending
        # Second iteration: reset already passed (None), _idle_reset_pending=True -> deadline at now+180
        # Third iteration: stops
        mock_time.side_effect = [
            100.0,    # 1st iter: target = time() + interval
            100.0,    # 1st inner loop: time() < target
            100.0,    # deadline calc: time() + 30 + 5
            200.0,    # 2nd iter: target = time() + interval
            200.0,    # 2nd inner loop: time() < target
            200.0,    # deadline calc (_idle_reset_pending path): time() + 180
            400.0,    # 3rd iter: target = time() + interval
            400.0,    # 3rd inner loop: time() < target -> running=False
        ]

        with patch.object(self.app, 'update', side_effect=update_side_effect), \
             patch.object(self.app, '_calculate_poll_interval', return_value=180), \
             patch.object(self.app, '_is_user_away', return_value=True), \
             patch.object(self.app, '_wait_for_activity', side_effect=capture_wait), \
             patch.object(self.app, '_seconds_until_next_reset', side_effect=[30.0, None, None]):
            self.app.poll_loop()

        self.assertEqual(update_count[0], 3)
        # First call: deadline based on reset time (100+30+5=135)
        self.assertAlmostEqual(wait_calls[0], 135.0, places=0)
        # Second call: deadline based on POLL_INTERVAL (200+180=380)
        self.assertAlmostEqual(wait_calls[1], 380.0, places=0)
        # _idle_reset_pending was set by the first idle detection
        self.assertTrue(self.app._idle_reset_pending)

    @patch('usage_monitor_for_codex.app.ON_RESET_COMMAND', ['echo reset'])
    @patch('usage_monitor_for_codex.app.POLL_INTERVAL', 180)
    @patch('usage_monitor_for_codex.app.time.sleep')
    @patch('usage_monitor_for_codex.app.time.time')
    def test_idle_reset_pending_survives_user_return(self, mock_time, mock_sleep):
        """_idle_reset_pending persists when user returns, so re-locking resumes idle polling."""
        def capture_wait(until=None):
            pass  # simulate immediate return (user came back)

        update_count = [0]

        def update_side_effect():
            update_count[0] += 1
            if update_count[0] >= 2:
                self.app.running = False

        # _is_user_away: True on first check (enter idle), False after _wait_for_activity (user returned)
        mock_time.side_effect = [
            100.0,    # 1st iter: target = time() + interval
            100.0,    # 1st inner loop: time() < target
            100.0,    # deadline calc: time() + 30 + 5
            200.0,    # after wait: time() - lst >= interval -> break
            200.0,    # 2nd iter: target = time() + interval
            200.0,    # 2nd inner loop: time() < target -> running=False
        ]

        with patch.object(self.app, 'update', side_effect=update_side_effect), \
             patch.object(self.app, '_calculate_poll_interval', return_value=180), \
             patch.object(self.app, '_is_user_away', side_effect=[True, False, False, False]), \
             patch.object(self.app, '_wait_for_activity', side_effect=capture_wait), \
             patch.object(self.app, '_seconds_until_next_reset', return_value=30.0):
            self.app.poll_loop()

        # Flag persists so that if the user locks again before the
        # reset is confirmed, idle polling resumes correctly.
        self.assertTrue(self.app._idle_reset_pending)

    @patch('usage_monitor_for_codex.app.ON_RESET_COMMAND', ['echo reset'])
    @patch('usage_monitor_for_codex.app.POLL_INTERVAL', 180)
    @patch('usage_monitor_for_codex.app.time.sleep')
    @patch('usage_monitor_for_codex.app.time.time')
    def test_idle_resumes_polling_after_network_failure_and_relock(self, mock_time, mock_sleep):
        """After network failure during user return, re-locking resumes idle polling."""
        wait_calls = []

        def capture_wait(until=None):
            wait_calls.append(until)

        update_count = [0]

        def update_side_effect():
            update_count[0] += 1
            if update_count[0] >= 3:
                self.app.running = False

        # Iteration 1: imminent reset -> sets _idle_reset_pending, wakes for deadline
        # Iteration 2: user returns briefly (network error), then re-locks.
        #   _is_user_away: True (enter idle), no next_reset but _idle_reset_pending
        #   -> deadline at now + POLL_INTERVAL -> keeps polling
        # Iteration 3: stops
        mock_time.side_effect = [
            100.0,    # 1st iter: target
            100.0,    # 1st inner loop check
            100.0,    # deadline calc: time() + 30 + 5
            200.0,    # 2nd iter: target
            200.0,    # 2nd inner loop check
            200.0,    # deadline calc (_idle_reset_pending): time() + 180
            400.0,    # 3rd iter: target
            400.0,    # 3rd inner loop check -> running=False
        ]

        with patch.object(self.app, 'update', side_effect=update_side_effect), \
             patch.object(self.app, '_calculate_poll_interval', return_value=180), \
             patch.object(self.app, '_is_user_away', return_value=True), \
             patch.object(self.app, '_wait_for_activity', side_effect=capture_wait), \
             patch.object(self.app, '_seconds_until_next_reset', side_effect=[30.0, None, None]):
            self.app.poll_loop()

        # All three iterations ran (idle polling continued after re-lock)
        self.assertEqual(update_count[0], 3)
        # Second wait used POLL_INTERVAL deadline (not None)
        self.assertAlmostEqual(wait_calls[1], 380.0, places=0)


class TestIdleResetPendingCleared(unittest.TestCase):
    """Tests for _idle_reset_pending being cleared on confirmed usage drop."""

    def setUp(self):
        self.app = _make_app()

    def tearDown(self):
        _cleanup(self.app)

    @patch('usage_monitor_for_codex.app.ON_RESET_COMMAND', ['echo reset'])
    @patch('usage_monitor_for_codex.app.run_event_command')
    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_5h_drop_clears_idle_reset_pending(self, _icon, _tooltip, _cmd):
        """_idle_reset_pending is cleared when a 5h quota reset (near-exhaustion drop) is detected."""
        self.app._idle_reset_pending = True
        self.app._prev_utilization = {'five_hour': 96.0, 'seven_day': 10.0}
        data = {'five_hour': {'utilization': 5.0}, 'seven_day': {'utilization': 10.0}}
        self.app.cache = MagicMock()
        self.app.cache.update.return_value = UpdateResult(data=data)

        self.app.update()

        self.assertFalse(self.app._idle_reset_pending)

    @patch('usage_monitor_for_codex.app.ON_RESET_COMMAND', ['echo reset'])
    @patch('usage_monitor_for_codex.app.run_event_command')
    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_7d_drop_clears_idle_reset_pending(self, _icon, _tooltip, _cmd):
        """_idle_reset_pending is cleared when a 7d quota reset (near-exhaustion drop) is detected."""
        self.app._idle_reset_pending = True
        self.app._prev_utilization = {'five_hour': 10.0, 'seven_day': 99.0}
        data = {'five_hour': {'utilization': 10.0}, 'seven_day': {'utilization': 5.0}}
        self.app.cache = MagicMock()
        self.app.cache.update.return_value = UpdateResult(data=data)

        self.app.update()

        self.assertFalse(self.app._idle_reset_pending)

    @patch('usage_monitor_for_codex.app.ON_RESET_COMMAND', ['echo reset'])
    @patch('usage_monitor_for_codex.app.run_event_command')
    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_no_drop_keeps_idle_reset_pending(self, _icon, _tooltip, _cmd):
        """_idle_reset_pending persists when usage stays stable (no drop)."""
        self.app._idle_reset_pending = True
        self.app._prev_utilization = {'five_hour': 50.0, 'seven_day': 10.0}
        data = {'five_hour': {'utilization': 55.0}, 'seven_day': {'utilization': 10.0}}
        self.app.cache = MagicMock()
        self.app.cache.update.return_value = UpdateResult(data=data)

        self.app.update()

        self.assertTrue(self.app._idle_reset_pending)

    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_error_response_keeps_idle_reset_pending(self, _icon, _tooltip):
        """_idle_reset_pending persists on API error (network failure)."""
        self.app._idle_reset_pending = True
        self.app._prev_utilization = {'five_hour': 80.0, 'seven_day': 10.0}
        data = {'error': 'server down'}
        self.app.cache = MagicMock()
        self.app.cache.update.return_value = UpdateResult(data=data)

        self.app.update()

        self.assertTrue(self.app._idle_reset_pending)


# ---------------------------------------------------------------------------
# Account switch detection
# ---------------------------------------------------------------------------

class TestAccountSwitchDetection(unittest.TestCase):
    """Tests for account switch detection and notification in update()."""

    def setUp(self):
        self.app = _make_app()
        self._cmd_patch = patch('usage_monitor_for_codex.app.run_event_command')
        self._cmd_patch.start()

    def tearDown(self):
        self._cmd_patch.stop()
        _cleanup(self.app)

    def _make_cache_mock(self, uuid, email, data):
        """Return a configured cache mock with given profile and usage data."""
        mock = MagicMock()
        mock.update.return_value = UpdateResult(data=data)
        mock.profile = {'account': {'uuid': uuid, 'email': email}}
        return mock

    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_account_switch_shows_notification(self, _icon, _tooltip):
        """Notification fires when account UUID changes between updates."""
        data = {'five_hour': {'utilization': 10.0}}
        self.app._prev_account_uuid = 'uuid-old'
        self.app.cache = self._make_cache_mock('uuid-new', 'new@example.com', data)

        self.app.update()

        self.app.icon.notify.assert_called_once()
        args = self.app.icon.notify.call_args[0]
        self.assertIn('new@example.com', args[0])

    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_no_notification_on_first_update(self, _icon, _tooltip):
        """No account switch notification on first update (_prev_account_uuid is None)."""
        data = {'five_hour': {'utilization': 10.0}}
        self.app.cache = self._make_cache_mock('uuid-1', 'user@example.com', data)

        self.app.update()

        self.app.icon.notify.assert_not_called()

    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_account_switch_rebaselines_prev_utilization(self, _icon, _tooltip):
        """Account switch drops the OLD account's high baseline and re-baselines to
        the NEW account's own values, so reset detection cannot fire on the old
        account's near-exhausted quota."""
        data = {'five_hour': {'utilization': 5.0}, 'seven_day': {'utilization': 5.0}}
        self.app._prev_utilization = {'five_hour': 97.0, 'seven_day': 99.0}
        self.app._prev_account_uuid = 'uuid-old'
        self.app.cache = self._make_cache_mock('uuid-new', 'new@example.com', data)

        self.app.update()

        # the old account's {97, 99} is gone; the baseline is the new account's values
        self.assertEqual(self.app._prev_utilization, {'five_hour': 5.0, 'seven_day': 5.0})

    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_account_switch_rebaselines_notified_thresholds(self, _icon, _tooltip):
        """Account switch drops the OLD account's notified-threshold state; the NEW
        account's already-exceeded level becomes its own baseline (recorded, not
        re-fired as a command), so only a genuinely higher new-account crossing fires
        later. The new value (95) proves it is the new account's baseline, not the old
        carried-over 80."""
        data = {'five_hour': {'utilization': 96.0}}
        self.app._notified_thresholds = {'five_hour': 80.0}
        self.app._prev_account_uuid = 'uuid-old'
        self.app.cache = self._make_cache_mock('uuid-new', 'new@example.com', data)

        self.app.update()

        self.assertEqual(self.app._notified_thresholds, {'five_hour': 95.0})

    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_account_switch_no_reset_notification(self, _icon, _tooltip):
        """No quota reset notification fires when account switches (even if utilization dropped from high)."""
        # Old account was near limit; new account has low utilization
        data = {'five_hour': {'utilization': 5.0}, 'seven_day': {'utilization': 5.0}}
        self.app._prev_utilization = {'five_hour': 97.0, 'seven_day': 99.0}
        self.app._prev_account_uuid = 'uuid-old'
        self.app.cache = self._make_cache_mock('uuid-new', 'new@example.com', data)

        self.app.update()

        # Only the account switch notification - no reset notification
        self.assertEqual(self.app.icon.notify.call_count, 1)
        title_arg = self.app.icon.notify.call_args[0][1]
        self.assertNotIn('Reset', title_arg)

    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_same_account_no_notification(self, _icon, _tooltip):
        """No account switch notification when UUID is unchanged."""
        data = {'five_hour': {'utilization': 50.0}}
        self.app._prev_account_uuid = 'uuid-same'
        self.app.cache = self._make_cache_mock('uuid-same', 'user@example.com', data)

        with patch.object(self.app, '_check_threshold_alerts'):
            self.app.update()

        self.app.icon.notify.assert_not_called()

    @patch('usage_monitor_for_codex.app.is_workstation_locked', return_value=True)
    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_account_switch_notification_deferred_while_idle(self, _icon, _tooltip, _locked):
        """Account switch notification is deferred when user is away."""
        data = {'five_hour': {'utilization': 10.0}}
        self.app._prev_account_uuid = 'uuid-old'
        self.app.cache = self._make_cache_mock('uuid-new', 'new@example.com', data)

        self.app.update()

        self.app.icon.notify.assert_not_called()
        self.assertIn('account_switched', self.app._deferred_notifications)

    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_account_switch_updates_prev_account_uuid(self, _icon, _tooltip):
        """After account switch, _prev_account_uuid is updated to the new UUID."""
        data = {'five_hour': {'utilization': 10.0}}
        self.app._prev_account_uuid = 'uuid-old'
        self.app.cache = self._make_cache_mock('uuid-new', 'new@example.com', data)

        self.app.update()

        self.assertEqual(self.app._prev_account_uuid, 'uuid-new')

    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_no_notification_when_profile_unavailable(self, _icon, _tooltip):
        """No account switch notification when profile could not be loaded (UUID unknown)."""
        data = {'five_hour': {'utilization': 10.0}}
        self.app._prev_account_uuid = 'uuid-old'
        mock = MagicMock()
        mock.update.return_value = UpdateResult(data=data)
        mock.profile = None
        self.app.cache = mock

        with patch.object(self.app, '_check_threshold_alerts'):
            self.app.update()

        self.app.icon.notify.assert_not_called()


# ---------------------------------------------------------------------------
# on_startup_command
# ---------------------------------------------------------------------------

class TestStartupCommand(unittest.TestCase):
    """Tests for on_startup_command firing on the first successful update."""

    def setUp(self):
        self.app = _make_app()
        self.app.cache = MagicMock()
        self.app.cache.profile = {'account': {'uuid': 'uuid-1', 'email': 'a@b'}}

    def tearDown(self):
        _cleanup(self.app)

    @patch('usage_monitor_for_codex.app.ON_STARTUP_COMMAND', ['echo startup'])
    @patch('usage_monitor_for_codex.app.run_event_command')
    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_fires_on_first_successful_update(self, _icon, _tooltip, mock_cmd):
        """Startup command fires once on the first successful update."""
        data = {
            'five_hour': {'utilization': 0.0, 'resets_at': None},
            'seven_day': {'utilization': 45.0, 'resets_at': '2025-01-20T12:00:00Z'},
        }
        self.app.cache.update.return_value = UpdateResult(data=data)

        self.app.update()

        mock_cmd.assert_called_once()
        cmd, env = mock_cmd.call_args[0]
        self.assertEqual(cmd, ['echo startup'])
        self.assertEqual(env['USAGE_MONITOR_EVENT'], 'startup')
        self.assertEqual(env['USAGE_MONITOR_UTILIZATION_FIVE_HOUR'], '0')
        self.assertEqual(env['USAGE_MONITOR_RESETS_AT_FIVE_HOUR'], '')
        self.assertEqual(env['USAGE_MONITOR_UTILIZATION_SEVEN_DAY'], '45')
        self.assertEqual(env['USAGE_MONITOR_RESETS_AT_SEVEN_DAY'], '2025-01-20T12:00:00Z')

    @patch('usage_monitor_for_codex.app.ON_STARTUP_COMMAND', ['echo startup'])
    @patch('usage_monitor_for_codex.app.run_event_command')
    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_fires_only_once_across_multiple_updates(self, _icon, _tooltip, mock_cmd):
        """Startup command does not fire again on subsequent updates."""
        data = {'five_hour': {'utilization': 0.0}, 'seven_day': {'utilization': 10.0}}
        self.app.cache.update.return_value = UpdateResult(data=data)

        self.app.update()
        self.app.update()
        self.app.update()

        mock_cmd.assert_called_once()

    @patch('usage_monitor_for_codex.app.ON_STARTUP_COMMAND', [])
    @patch('usage_monitor_for_codex.app.run_event_command')
    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_no_fire_when_command_unset(self, _icon, _tooltip, mock_cmd):
        """Startup command is not invoked when ON_STARTUP_COMMAND is empty."""
        data = {'five_hour': {'utilization': 0.0}, 'seven_day': {'utilization': 10.0}}
        self.app.cache.update.return_value = UpdateResult(data=data)

        self.app.update()

        mock_cmd.assert_not_called()

    @patch('usage_monitor_for_codex.app.ON_STARTUP_COMMAND', ['echo startup'])
    @patch('usage_monitor_for_codex.app.run_event_command')
    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_no_fire_on_error_response(self, _icon, _tooltip, mock_cmd):
        """Startup command is skipped when the first update returns an error."""
        self.app.cache.update.return_value = UpdateResult(data={'error': 'connection failed'})

        self.app.update()

        mock_cmd.assert_not_called()
        self.assertFalse(self.app._first_update_done)

    @patch('usage_monitor_for_codex.app.ON_STARTUP_COMMAND', ['echo startup'])
    @patch('usage_monitor_for_codex.app.run_event_command')
    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_fires_after_initial_error_then_success(self, _icon, _tooltip, mock_cmd):
        """Startup command fires on the first SUCCESSFUL update, even after errors."""
        ok_data = {'five_hour': {'utilization': 0.0}, 'seven_day': {'utilization': 10.0}}
        self.app.cache.update.side_effect = [
            UpdateResult(data={'error': 'offline'}),
            UpdateResult(data=ok_data),
        ]

        self.app.update()
        self.assertEqual(mock_cmd.call_count, 0)

        self.app.update()
        self.assertEqual(mock_cmd.call_count, 1)
        self.assertEqual(mock_cmd.call_args[0][1]['USAGE_MONITOR_EVENT'], 'startup')

    @patch('usage_monitor_for_codex.app.ON_STARTUP_COMMAND', ['echo startup'])
    @patch('usage_monitor_for_codex.app.run_event_command')
    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_fires_on_first_live_sample_after_session(self, _icon, _tooltip, mock_cmd):
        """After an initial session fallback, the startup command fires on the
        FIRST verified live (api) sample - not delayed to the second api poll."""
        session_data = {'five_hour': {'utilization': 5.0, 'resets_at': ''}, 'source': 'session'}
        # Verified live sample (account_id matches the setUp profile uuid 'uuid-1').
        api_data = {'five_hour': {'utilization': 5.0, 'resets_at': ''}, 'source': 'api', 'account_id': 'uuid-1'}
        self.app.cache.update.side_effect = [
            UpdateResult(data=session_data),
            UpdateResult(data=api_data),
            UpdateResult(data=api_data),
        ]

        self.app.update()  # session: display-only, startup suppressed
        self.assertEqual(mock_cmd.call_count, 0)
        self.assertFalse(self.app._first_update_done)

        self.app.update()  # first verified live sample (source switch): fire now
        self.assertEqual(mock_cmd.call_count, 1)
        self.assertTrue(self.app._first_update_done)
        self.assertEqual(mock_cmd.call_args[0][1]['USAGE_MONITOR_EVENT'], 'startup')

        self.app.update()  # subsequent api: no re-fire
        self.assertEqual(mock_cmd.call_count, 1)

    @patch('usage_monitor_for_codex.app.ON_STARTUP_COMMAND', ['echo startup'])
    @patch('usage_monitor_for_codex.app.run_event_command')
    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_extra_usage_env_vars_when_enabled(self, _icon, _tooltip, mock_cmd):
        """Extra usage env vars are emitted when extra_usage is enabled."""
        data = {
            'five_hour': {'utilization': 10.0, 'resets_at': '2025-01-15T18:00:00Z'},
            'extra_usage': {'is_enabled': True, 'used_credits': 8.20, 'monthly_limit': 10.0},
        }
        self.app.cache.update.return_value = UpdateResult(data=data)

        self.app.update()

        env = mock_cmd.call_args[0][1]
        self.assertIn('USAGE_MONITOR_EXTRA_USED', env)
        self.assertIn('USAGE_MONITOR_EXTRA_LIMIT', env)
        self.assertNotIn('USAGE_MONITOR_UTILIZATION_EXTRA_USAGE', env)

    @patch('usage_monitor_for_codex.app.ON_STARTUP_COMMAND', ['echo startup'])
    @patch('usage_monitor_for_codex.app.run_event_command')
    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_no_extra_usage_env_vars_for_credit_balance(self, _icon, _tooltip, mock_cmd):
        """A Codex credit BALANCE (is_enabled but no monthly_limit) emits no legacy
        EXTRA_USED / EXTRA_LIMIT vars - those belong to the used/limit ratio model
        only (docs say they are not produced for credit balances)."""
        data = {
            'five_hour': {'utilization': 10.0},
            'extra_usage': {'is_enabled': True, 'unlimited': False, 'balance': 1250},
        }
        self.app.cache.update.return_value = UpdateResult(data=data)

        self.app.update()

        env = mock_cmd.call_args[0][1]
        self.assertNotIn('USAGE_MONITOR_EXTRA_USED', env)
        self.assertNotIn('USAGE_MONITOR_EXTRA_LIMIT', env)

    @patch('usage_monitor_for_codex.app.ON_STARTUP_COMMAND', ['echo startup'])
    @patch('usage_monitor_for_codex.app.run_event_command')
    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_startup_deferred_on_recovery_account_mismatch(self, _icon, _tooltip, mock_cmd):
        """On a session->api recovery whose live sample's account does NOT match
        the refreshed profile (a mid-account-switch sample), the one-time startup
        command is deferred rather than run with the mismatched sample's usage."""
        self.app.cache = MagicMock()
        self.app.cache.profile = {'account': {'uuid': 'acct-B', 'email': 'b@example.com'}}
        self.app.cache.update.side_effect = [
            UpdateResult(data={'five_hour': {'utilization': 50.0, 'resets_at': ''}, 'source': 'session'}),
            UpdateResult(data={'five_hour': {'utilization': 91.0, 'resets_at': ''}, 'source': 'api', 'account_id': 'acct-A'}),
        ]
        self.app.update()  # session fallback
        self.app.update()  # recovery api: usage acct-A but profile acct-B -> defer startup
        mock_cmd.assert_not_called()
        self.assertFalse(self.app._first_update_done)

    @patch('usage_monitor_for_codex.app.ON_STARTUP_COMMAND', ['echo startup'])
    @patch('usage_monitor_for_codex.app.run_event_command')
    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_startup_fires_when_account_verified(self, _icon, _tooltip, mock_cmd):
        """When the live sample's account positively matches the cached profile,
        the startup command fires (the fail-closed gate allows verified samples)."""
        self.app.cache.profile = {'account': {'uuid': 'acct-V', 'email': 'v@example.com'}}
        self.app.cache.update.return_value = UpdateResult(
            data={'five_hour': {'utilization': 0.0, 'resets_at': ''}, 'source': 'api', 'account_id': 'acct-V'},
        )
        self.app.update()
        mock_cmd.assert_called_once()
        self.assertTrue(self.app._first_update_done)

    @patch('usage_monitor_for_codex.app.ON_STARTUP_COMMAND', ['echo startup'])
    @patch('usage_monitor_for_codex.app.run_event_command')
    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_no_extra_usage_env_vars_when_disabled(self, _icon, _tooltip, mock_cmd):
        """Extra usage env vars are not emitted when extra_usage is disabled."""
        data = {
            'five_hour': {'utilization': 10.0},
            'extra_usage': {'is_enabled': False, 'used_credits': 0, 'monthly_limit': 0},
        }
        self.app.cache.update.return_value = UpdateResult(data=data)

        self.app.update()

        env = mock_cmd.call_args[0][1]
        self.assertNotIn('USAGE_MONITOR_EXTRA_USED', env)
        self.assertNotIn('USAGE_MONITOR_EXTRA_LIMIT', env)

    @patch('usage_monitor_for_codex.app.ON_STARTUP_COMMAND', ['echo startup'])
    @patch('usage_monitor_for_codex.app.run_event_command')
    @patch('usage_monitor_for_codex.app.format_tooltip', return_value='tooltip')
    @patch('usage_monitor_for_codex.app.load_tray_icon')
    def test_handles_null_quota_field(self, _icon, _tooltip, mock_cmd):
        """Quota fields with value None (feature not enabled) are skipped without error."""
        data = {'five_hour': {'utilization': 10.0}, 'seven_day': None}
        self.app.cache.update.return_value = UpdateResult(data=data)

        self.app.update()

        mock_cmd.assert_called_once()
        env = mock_cmd.call_args[0][1]
        self.assertIn('USAGE_MONITOR_UTILIZATION_FIVE_HOUR', env)
        self.assertNotIn('USAGE_MONITOR_UTILIZATION_SEVEN_DAY', env)

    @patch('usage_monitor_for_codex.app.ON_STARTUP_COMMAND', ['echo startup'])
    @patch('usage_monitor_for_codex.app.run_event_command')
    def test_test_menu_handler_passes_expected_env(self, mock_cmd):
        """on_test_startup passes the documented env vars."""
        self.app.on_test_startup()

        mock_cmd.assert_called_once()
        cmd, env = mock_cmd.call_args[0]
        self.assertEqual(cmd, ['echo startup'])
        self.assertEqual(env['USAGE_MONITOR_EVENT'], 'startup')
        self.assertEqual(env['USAGE_MONITOR_RESETS_AT_FIVE_HOUR'], '')
        self.assertEqual(env['USAGE_MONITOR_UTILIZATION_FIVE_HOUR'], '0')
        self.assertNotEqual(env['USAGE_MONITOR_RESETS_AT_SEVEN_DAY'], '')


class TestSourceSwitchGuard(unittest.TestCase):
    """Re-baselining and alert suppression when the data source flips (api <-> session)."""

    def setUp(self):
        self.app = _make_app(thresholds=[80, 95])
        self._cmd_patch = patch('usage_monitor_for_codex.app.run_event_command')
        self._cmd = self._cmd_patch.start()

    def tearDown(self):
        self._cmd_patch.stop()
        _cleanup(self.app)

    def _feed(self, util: float, source: str) -> None:
        self.app.cache = MagicMock()
        data = {'five_hour': {'utilization': util, 'resets_at': ''}, 'source': source}
        if source == 'api':
            # A verified live sample: stamp the account and a matching profile so the
            # identity gate trusts it. These tests exercise source-switch / alert
            # logic, not the identity gate (which has its own dedicated tests).
            self.app.cache.profile = {'account': {'uuid': 'acct-feed', 'email': 'e@example.com'}}
            data['account_id'] = 'acct-feed'
        self.app.cache.update.return_value = UpdateResult(data=data)
        self.app.update()

    def test_first_poll_establishes_source_without_switch(self):
        """The first poll sets the source baseline and processes normally."""
        self._feed(82, 'api')
        self.app.icon.notify.assert_called_once()  # crosses 80 like a normal first sample
        self.assertEqual(self.app._prev_source, 'api')

    def test_session_sample_is_display_only_and_does_not_pollute_baseline(self):
        """A high SESSION sample is display-only: it must not alert AND must not
        touch the verified threshold baseline, so it can neither fire nor later
        suppress a real verified crossing (trusted state is api-only)."""
        self._feed(10, 'api')
        self.app.icon.notify.reset_mock()
        self._feed(90, 'session')  # display-only source
        self.app.icon.notify.assert_not_called()
        # the trusted baseline stays at the api@10 level (unset / 0), NOT raised to
        # 80 by the session reading
        self.assertEqual(self.app._notified_thresholds.get('five_hour', 0), 0)
        self.assertEqual(self.app._prev_source, 'session')

    def test_increase_after_returning_to_api_alerts(self):
        """After a session detour, a verified live increase past a threshold fires.

        Session data never alerts and never touches the trusted event state; the
        verified live (api) samples are processed normally, so a real increase past
        a threshold fires.
        """
        self._feed(10, 'api')      # verified, below thresholds
        self._feed(90, 'session')  # display-only: ignored for events
        self._feed(50, 'api')      # verified, still below 80 -> no fire
        self.app.icon.notify.reset_mock()
        self._feed(96, 'api')      # verified increase past 95 -> fires
        self.app.icon.notify.assert_called_once()

    def test_session_data_never_fires_alerts(self):
        """A genuine threshold crossing seen only in session data does not alert."""
        self._feed(10, 'api')
        self.app.icon.notify.reset_mock()
        self._feed(96, 'session')  # session mode: display only
        self._feed(98, 'session')  # still session: no alert despite crossing
        self.app.icon.notify.assert_not_called()

    def test_switch_suppresses_reset_notification(self):
        """A drop caused by switching sources must not be read as a quota reset."""
        self._feed(99, 'api')  # high (fires once on this first sample)
        self.app.icon.notify.reset_mock()
        self._feed(3, 'session')  # switch + big drop -> no reset notification
        self.app.icon.notify.assert_not_called()

    def test_source_switch_to_api_refreshes_profile(self):
        """The first verified live (api) sample after a session fallback refreshes
        the cached profile, so a `codex login` performed during the fallback cannot
        leave the old account's email beside the new account's live usage/Credits."""
        self.app.cache = MagicMock()
        self.app.cache.profile = {'account': {'uuid': 'uuid-A', 'email': 'a@example.com'}}
        self.app.cache.update.side_effect = [
            UpdateResult(data={'five_hour': {'utilization': 10.0, 'resets_at': ''}, 'source': 'api'}),
            UpdateResult(data={'five_hour': {'utilization': 50.0, 'resets_at': ''}, 'source': 'session'}),
            UpdateResult(data={'five_hour': {'utilization': 5.0, 'resets_at': ''}, 'source': 'api'}),
        ]

        self.app.update()  # api: account A established (normal path refreshes profile)
        self.app.update()  # session fallback: no profile refresh
        self.app.cache.ensure_profile.reset_mock()
        self.app.update()  # recovered api (source switch): MUST refresh profile
        self.app.cache.ensure_profile.assert_called_once()

    def test_mismatched_account_sample_ignored_preserves_baseline(self):
        """A same-token ChatGPT-Account-Id change whose profile fetch has not yet
        refreshed (the live usage's account_id differs from the displayed profile
        uuid) is a MISMATCH: it is ignored entirely - no false reset, and crucially
        it does NOT overwrite the verified account's baseline, so that account's
        events still work when its identity-consistent samples resume."""
        self.app.cache = MagicMock()
        # Profile is acct-A throughout; the SECOND sample (acct-B) cannot be verified
        # against it, so it must be ignored without touching trusted state.
        self.app.cache.profile = {'account': {'uuid': 'acct-A', 'email': 'a@example.com'}}
        self.app.cache.update.side_effect = [
            UpdateResult(data={'five_hour': {'utilization': 97.0, 'resets_at': ''}, 'source': 'api', 'account_id': 'acct-A'}),
            UpdateResult(data={'five_hour': {'utilization': 5.0, 'resets_at': ''}, 'source': 'api', 'account_id': 'acct-B'}),
        ]
        self.app.update()  # account A near its limit (verified) -> baseline {97}
        self.app.icon.notify.reset_mock()
        self.app.update()  # acct-B mismatch -> ignored, NOT a reset, baseline preserved
        self.app.icon.notify.assert_not_called()
        self.assertEqual(self.app._prev_utilization, {'five_hour': 97.0})

    def test_mismatched_samples_after_session_recovery_ignored(self):
        """After a session fallback, live samples whose stamped account never matches
        the displayed profile uuid are all mismatches - ignored for events and never
        written to the trusted baseline (fail-closed; no false reset)."""
        self.app.cache = MagicMock()
        self.app.cache.profile = {'account': {'uuid': 'uuid-fixed', 'email': 'x@example.com'}}
        self.app.cache.update.side_effect = [
            UpdateResult(data={'five_hour': {'utilization': 50.0, 'resets_at': ''}, 'source': 'session'}),
            UpdateResult(data={'five_hour': {'utilization': 97.0, 'resets_at': ''}, 'source': 'api', 'account_id': 'acct-A'}),
            UpdateResult(data={'five_hour': {'utilization': 5.0, 'resets_at': ''}, 'source': 'api', 'account_id': 'acct-B'}),
        ]
        self.app.update()  # session fallback (display-only)
        self.app.update()  # api acct-A but profile uuid-fixed -> mismatch -> ignored
        self.app.icon.notify.reset_mock()
        self.app.update()  # api acct-B -> mismatch -> ignored, NOT a reset
        self.app.icon.notify.assert_not_called()
        self.assertEqual(self.app._prev_utilization, {})

    @patch('usage_monitor_for_codex.app.ON_THRESHOLD_COMMAND', ['notify.bat'])
    @patch('usage_monitor_for_codex.app.ALERT_TIME_AWARE', False)
    def test_mismatched_sample_does_not_suppress_verified_crossing(self):
        """A mismatched (different-account) live sample between two verified samples
        of the DISPLAYED account must not write trusted state, so the displayed
        account's real crossing still fires (review 1633 re-review blocker 1)."""
        self.app.cache = MagicMock()
        self.app.cache.profile = {'account': {'uuid': 'acct-A', 'email': 'a@example.com'}}
        self.app.cache.update.side_effect = [
            UpdateResult(data={'five_hour': {'utilization': 10.0, 'resets_at': ''}, 'source': 'api', 'account_id': 'acct-A'}),
            UpdateResult(data={'five_hour': {'utilization': 96.0, 'resets_at': ''}, 'source': 'api', 'account_id': 'acct-B'}),  # mismatch (B != profile A)
            UpdateResult(data={'five_hour': {'utilization': 96.0, 'resets_at': ''}, 'source': 'api', 'account_id': 'acct-A'}),  # verified A crossing
        ]
        self.app.update()  # A@10 verified
        self.app.icon.notify.reset_mock()
        self._cmd.reset_mock()
        self.app.update()  # B@96 mismatch -> ignored, baseline untouched
        self.app.update()  # A@96 verified -> real 95% crossing fires
        self.app.icon.notify.assert_called_once()
        self._cmd.assert_called_once()
        self.assertEqual(self._cmd.call_args[0][1]['USAGE_MONITOR_EVENT'], 'threshold')

    @patch('usage_monitor_for_codex.app.ON_RESET_COMMAND', ['reset.bat'])
    def test_mismatched_sample_does_not_suppress_verified_reset(self):
        """A mismatched sample between two verified same-account samples must not
        break reset detection: the verified near-exhaustion -> verified drop is still
        a genuine reset (review 1633 re-review blocker 1)."""
        self.app.cache = MagicMock()
        self.app.cache.profile = {'account': {'uuid': 'acct-A', 'email': 'a@example.com'}}
        self.app.cache.update.side_effect = [
            UpdateResult(data={'five_hour': {'utilization': 97.0, 'resets_at': ''}, 'source': 'api', 'account_id': 'acct-A'}),
            UpdateResult(data={'five_hour': {'utilization': 97.0, 'resets_at': ''}, 'source': 'api', 'account_id': 'acct-B'}),  # mismatch
            UpdateResult(data={'five_hour': {'utilization': 5.0, 'resets_at': ''}, 'source': 'api', 'account_id': 'acct-A'}),
        ]
        self.app.update()  # A@97 verified
        self.app.icon.notify.reset_mock()
        self._cmd.reset_mock()
        self.app.update()  # B@97 mismatch -> ignored, baseline preserved at 97
        self.app.update()  # A@5 verified -> genuine reset fires
        self._cmd.assert_called_once()
        self.assertEqual(self._cmd.call_args[0][1]['USAGE_MONITOR_EVENT'], 'reset')

    @patch('usage_monitor_for_codex.app.ON_RESET_COMMAND', ['reset.bat'])
    def test_quotaless_response_preserves_verified_baseline(self):
        """A verified response with no quota windows (e.g. a credits-only Pro payload)
        must not clear the verified quota baseline, so a later genuine reset is still
        detected (review 1633 re-review blocker 2)."""
        self.app.cache = MagicMock()
        self.app.cache.profile = {'account': {'uuid': 'acct-A', 'email': 'a@example.com'}}
        self.app.cache.update.side_effect = [
            UpdateResult(data={'five_hour': {'utilization': 97.0, 'resets_at': ''}, 'source': 'api', 'account_id': 'acct-A'}),
            UpdateResult(data={'extra_usage': {'is_enabled': True, 'unlimited': False, 'balance': 1250}, 'source': 'api', 'account_id': 'acct-A'}),  # quota-less
            UpdateResult(data={'five_hour': {'utilization': 5.0, 'resets_at': ''}, 'source': 'api', 'account_id': 'acct-A'}),
        ]
        self.app.update()  # A@97 -> baseline {97}
        self.app.icon.notify.reset_mock()
        self._cmd.reset_mock()
        self.app.update()  # credits-only verified -> must NOT clear the baseline
        self.assertEqual(self.app._prev_utilization, {'five_hour': 97.0})
        self.app.update()  # A@5 -> genuine reset still detected
        self._cmd.assert_called_once()
        self.assertEqual(self._cmd.call_args[0][1]['USAGE_MONITOR_EVENT'], 'reset')

    @patch('usage_monitor_for_codex.app.ON_RESET_COMMAND', ['reset.bat'])
    def test_account_switch_with_transient_null_profile_no_false_reset(self):
        """A genuine A->B switch whose first B-profile fetch transiently returns None
        must not lose A's identity. When B's profile recovers, the uuid-switch fires
        and resets trusted state, so B@5 is NOT misread as a reset of A@97 (review
        1633 re-review blocker: _prev_account_uuid must survive a None profile)."""
        self.app.cache = MagicMock()
        self.app.cache.update.side_effect = [
            UpdateResult(data={'five_hour': {'utilization': 97.0, 'resets_at': ''}, 'source': 'api', 'account_id': 'acct-A'}),
            UpdateResult(data={'five_hour': {'utilization': 5.0, 'resets_at': ''}, 'source': 'api', 'account_id': 'acct-B'}),  # profile None
            UpdateResult(data={'five_hour': {'utilization': 5.0, 'resets_at': ''}, 'source': 'api', 'account_id': 'acct-B'}),  # profile recovered
        ]
        self.app.cache.profile = {'account': {'uuid': 'acct-A', 'email': 'a@example.com'}}
        self.app.update()  # A@97 verified -> baseline {97}, _prev_account_uuid='acct-A'
        self.app.cache.profile = None  # transient profile-fetch failure
        self.app.icon.notify.reset_mock()
        self._cmd.reset_mock()
        self.app.update()  # B@5 profile None -> mismatch, ignored; _prev_account_uuid kept as acct-A
        self.app.cache.profile = {'account': {'uuid': 'acct-B', 'email': 'b@example.com'}}  # recovered
        self.app.update()  # B@5 profile B -> uuid switch A->B resets state, NO false reset
        self.assertFalse(any(c[0][1].get('USAGE_MONITOR_EVENT') == 'reset' for c in self._cmd.call_args_list))
        self.assertEqual(self.app._prev_account_uuid, 'acct-B')
        # the verified switch sample re-baselines to the NEW account's value (5),
        # not the old account's 97, so no false reset
        self.assertEqual(self.app._prev_utilization, {'five_hour': 5.0})

    def test_account_mismatch_live_sample_suppresses_events(self):
        """A live sample whose stamped account differs from the displayed profile
        (e.g. a switch landing mid-request) is ignored like an unverified sample: no
        threshold/reset notification fires, even at high utilization, and the trusted
        event state is left untouched."""
        self.app.cache = MagicMock()
        self.app.cache.profile = {'account': {'uuid': 'acct-B', 'email': 'b@example.com'}}
        self.app.cache.update.return_value = UpdateResult(
            data={'five_hour': {'utilization': 96.0, 'resets_at': ''}, 'source': 'api', 'account_id': 'acct-A'},
        )
        self.app.update()  # usage acct-A @96% but profile acct-B -> mismatch -> suppressed
        self.app.icon.notify.assert_not_called()

    def test_unidentified_live_sample_suppresses_events(self):
        """A live sample with NO account_id, against a known profile account, cannot
        be verified (fail-closed) and is ignored (trusted state untouched) - no
        threshold notification fires even at high utilization."""
        self.app.cache = MagicMock()
        self.app.cache.profile = {'account': {'uuid': 'acct-B', 'email': 'b@example.com'}}
        self.app.cache.update.return_value = UpdateResult(
            data={'five_hour': {'utilization': 96.0, 'resets_at': ''}, 'source': 'api'},  # no account_id
        )
        self.app.update()
        self.app.icon.notify.assert_not_called()

    def test_unverified_sample_does_not_seed_false_reset(self):
        """An unidentified live sample's utilization must NOT become the baseline for
        a reset: an unverified api@97 then a verified api@5 is not a reset, because
        the prior baseline was untrusted (only verified->verified drops count)."""
        self.app.cache = MagicMock()
        self.app.cache.profile = {'account': {'uuid': 'acct-A', 'email': 'a@example.com'}}
        self.app.cache.update.side_effect = [
            UpdateResult(data={'five_hour': {'utilization': 97.0, 'resets_at': ''}, 'source': 'api'}),  # unverified
            UpdateResult(data={'five_hour': {'utilization': 5.0, 'resets_at': ''}, 'source': 'api', 'account_id': 'acct-A'}),  # verified
        ]
        self.app.update()  # unverified @97 -> suppressed, NOT a trusted baseline
        self.app.icon.notify.reset_mock()
        self.app.update()  # verified @5 -> no reset (prior baseline was unverified)
        self.app.icon.notify.assert_not_called()

    @patch('usage_monitor_for_codex.app.ON_THRESHOLD_COMMAND', ['notify.bat'])
    @patch('usage_monitor_for_codex.app.ALERT_TIME_AWARE', False)
    def test_unverified_sample_does_not_rearm_threshold(self):
        """An unverified live sample must NOT touch the trusted threshold baseline,
        so it can neither re-arm a threshold a previously *verified* sample notified
        (the next verified high sample would re-run on_threshold_command) nor get
        ahead of it.

        Repro: verified api@96 (notifies + commands at 95) -> unverified api@5 (no
        account_id) -> verified api@96 again. The middle (display-only) sample leaves
        the baseline at 95, so the final sample stays silent.
        """
        self.app.cache = MagicMock()
        self.app.cache.profile = {'account': {'uuid': 'acct-A', 'email': 'a@example.com'}}
        self.app.cache.update.side_effect = [
            UpdateResult(data={'five_hour': {'utilization': 96.0, 'resets_at': ''}, 'source': 'api', 'account_id': 'acct-A'}),  # verified -> notifies 95
            UpdateResult(data={'five_hour': {'utilization': 5.0, 'resets_at': ''}, 'source': 'api'}),  # unverified (no account_id) -> must not re-arm
            UpdateResult(data={'five_hour': {'utilization': 96.0, 'resets_at': ''}, 'source': 'api', 'account_id': 'acct-A'}),  # verified high again -> must NOT re-fire
        ]
        self.app.update()  # verified @96 -> crosses 95: notifies + runs command
        self.assertEqual(self.app._notified_thresholds.get('five_hour'), 95)
        self.app.icon.notify.reset_mock()
        self._cmd.reset_mock()
        self.app.update()  # unverified @5 -> suppressed; baseline must STAY at 95
        self.assertEqual(self.app._notified_thresholds.get('five_hour'), 95)
        self.app.update()  # verified @96 -> already notified at 95: no re-fire
        self.app.icon.notify.assert_not_called()
        self._cmd.assert_not_called()

    @patch('usage_monitor_for_codex.app.ON_THRESHOLD_COMMAND', ['notify.bat'])
    @patch('usage_monitor_for_codex.app.ALERT_TIME_AWARE', False)
    def test_unidentified_sample_does_not_suppress_verified_crossing(self):
        """An unidentified live sample between two verified samples must not raise
        the trusted baseline, so the real verified crossing still notifies AND runs
        on_threshold_command (review 1633 repro 1)."""
        self.app.cache = MagicMock()
        self.app.cache.profile = {'account': {'uuid': 'acct-A', 'email': 'a@example.com'}}
        self.app.cache.update.side_effect = [
            UpdateResult(data={'five_hour': {'utilization': 10.0, 'resets_at': ''}, 'source': 'api', 'account_id': 'acct-A'}),
            UpdateResult(data={'five_hour': {'utilization': 96.0, 'resets_at': ''}, 'source': 'api'}),  # unidentified (no account_id)
            UpdateResult(data={'five_hour': {'utilization': 96.0, 'resets_at': ''}, 'source': 'api', 'account_id': 'acct-A'}),  # verified crossing
        ]
        self.app.update()  # A@10 verified, below thresholds (sets first_update_done)
        self.app.icon.notify.reset_mock()
        self._cmd.reset_mock()
        self.app.update()  # unidentified@96 -> display-only, no event, baseline untouched
        self.app.update()  # verified A@96 -> real 95% crossing fires
        self.app.icon.notify.assert_called_once()
        self._cmd.assert_called_once()
        self.assertEqual(self._cmd.call_args[0][1]['USAGE_MONITOR_EVENT'], 'threshold')

    @patch('usage_monitor_for_codex.app.ON_THRESHOLD_COMMAND', ['notify.bat'])
    @patch('usage_monitor_for_codex.app.ALERT_TIME_AWARE', False)
    def test_session_high_sample_does_not_suppress_later_api_crossing(self):
        """A high SESSION reading during a fallback must not raise the trusted
        baseline, so a real verified crossing after recovery still fires - the docs'
        "events resume on the next live reading" contract (review 1633 repro 2)."""
        self.app.cache = MagicMock()
        self.app.cache.profile = {'account': {'uuid': 'acct-A', 'email': 'a@example.com'}}
        self.app.cache.update.side_effect = [
            UpdateResult(data={'five_hour': {'utilization': 10.0, 'resets_at': ''}, 'source': 'api', 'account_id': 'acct-A'}),
            UpdateResult(data={'five_hour': {'utilization': 96.0, 'resets_at': ''}, 'source': 'session'}),  # display-only
            UpdateResult(data={'five_hour': {'utilization': 10.0, 'resets_at': ''}, 'source': 'api', 'account_id': 'acct-A'}),  # recovery
            UpdateResult(data={'five_hour': {'utilization': 85.0, 'resets_at': ''}, 'source': 'api', 'account_id': 'acct-A'}),  # 80% crossing
        ]
        self.app.update()  # A@10
        self.app.icon.notify.reset_mock()
        self._cmd.reset_mock()
        self.app.update()  # session@96 -> ignored for events
        self.app.update()  # recovery A@10 -> no crossing
        self.app.update()  # A@85 -> crosses 80
        self.app.icon.notify.assert_called_once()
        self._cmd.assert_called_once()
        self.assertEqual(self._cmd.call_args[0][1]['USAGE_MONITOR_EVENT'], 'threshold')
        self.assertEqual(self._cmd.call_args[0][1]['USAGE_MONITOR_THRESHOLD'], '80')

    @patch('usage_monitor_for_codex.app.ON_THRESHOLD_COMMAND', ['notify.bat'])
    @patch('usage_monitor_for_codex.app.ALERT_TIME_AWARE', False)
    def test_session_high_sample_does_not_suppress_later_95_crossing(self):
        """Variant of the above for the higher threshold: a high session value does
        not pre-arm 95, so a verified climb to 96 after recovery still fires it
        (review 1633 repro 2)."""
        self.app.cache = MagicMock()
        self.app.cache.profile = {'account': {'uuid': 'acct-A', 'email': 'a@example.com'}}
        self.app.cache.update.side_effect = [
            UpdateResult(data={'five_hour': {'utilization': 10.0, 'resets_at': ''}, 'source': 'api', 'account_id': 'acct-A'}),
            UpdateResult(data={'five_hour': {'utilization': 96.0, 'resets_at': ''}, 'source': 'session'}),
            UpdateResult(data={'five_hour': {'utilization': 10.0, 'resets_at': ''}, 'source': 'api', 'account_id': 'acct-A'}),
            UpdateResult(data={'five_hour': {'utilization': 96.0, 'resets_at': ''}, 'source': 'api', 'account_id': 'acct-A'}),
        ]
        self.app.update()
        self.app.icon.notify.reset_mock()
        self._cmd.reset_mock()
        self.app.update()  # session@96 ignored
        self.app.update()  # recovery A@10
        self.app.update()  # A@96 -> crosses 95
        self.app.icon.notify.assert_called_once()
        self._cmd.assert_called_once()
        self.assertEqual(self._cmd.call_args[0][1]['USAGE_MONITOR_THRESHOLD'], '95')

    @patch('usage_monitor_for_codex.app.ON_RESET_COMMAND', ['reset.bat'])
    def test_unidentified_sample_does_not_suppress_verified_reset(self):
        """An unidentified live sample between two verified same-account samples must
        not break reset detection: the verified near-exhaustion -> verified drop is
        still a genuine reset and runs on_reset_command (review 1633 repro 3)."""
        self.app.cache = MagicMock()
        self.app.cache.profile = {'account': {'uuid': 'acct-A', 'email': 'a@example.com'}}
        self.app.cache.update.side_effect = [
            UpdateResult(data={'five_hour': {'utilization': 97.0, 'resets_at': ''}, 'source': 'api', 'account_id': 'acct-A'}),
            UpdateResult(data={'five_hour': {'utilization': 97.0, 'resets_at': ''}, 'source': 'api'}),  # unidentified
            UpdateResult(data={'five_hour': {'utilization': 5.0, 'resets_at': ''}, 'source': 'api', 'account_id': 'acct-A'}),
        ]
        self.app.update()  # A@97 verified (near limit)
        self.app.icon.notify.reset_mock()
        self._cmd.reset_mock()
        self.app.update()  # unidentified@97 -> ignored, baseline preserved at 97
        self.app.update()  # verified A@5 -> genuine reset
        self._cmd.assert_called_once()
        self.assertEqual(self._cmd.call_args[0][1]['USAGE_MONITOR_EVENT'], 'reset')

    @patch('usage_monitor_for_codex.app.ON_THRESHOLD_COMMAND', ['notify.bat'])
    @patch('usage_monitor_for_codex.app.ALERT_TIME_AWARE', False)
    def test_verified_drop_rearms_threshold(self):
        """A genuine VERIFIED drop below a threshold re-arms it, so a later verified
        climb past it fires on_threshold_command again - the legitimate re-arm path
        (review 1633)."""
        self.app.cache = MagicMock()
        self.app.cache.profile = {'account': {'uuid': 'acct-A', 'email': 'a@example.com'}}
        self.app.cache.update.side_effect = [
            UpdateResult(data={'five_hour': {'utilization': 96.0, 'resets_at': ''}, 'source': 'api', 'account_id': 'acct-A'}),
            UpdateResult(data={'five_hour': {'utilization': 5.0, 'resets_at': ''}, 'source': 'api', 'account_id': 'acct-A'}),  # verified drop
            UpdateResult(data={'five_hour': {'utilization': 96.0, 'resets_at': ''}, 'source': 'api', 'account_id': 'acct-A'}),  # climb back
        ]
        self.app.update()  # A@96 (first: notify 95, no command yet)
        self.app.update()  # A@5 verified drop -> re-arm (notified lowered to 0)
        self.app.icon.notify.reset_mock()
        self._cmd.reset_mock()
        self.app.update()  # A@96 again -> fires threshold + command
        self.app.icon.notify.assert_called_once()
        self._cmd.assert_called_once()
        self.assertEqual(self._cmd.call_args[0][1]['USAGE_MONITOR_EVENT'], 'threshold')

    @patch('usage_monitor_for_codex.app.ON_THRESHOLD_COMMAND', ['notify.bat'])
    @patch('usage_monitor_for_codex.app.ALERT_TIME_AWARE', False)
    @patch('usage_monitor_for_codex.app.ON_THRESHOLD_COMMAND', ['notify.bat'])
    @patch('usage_monitor_for_codex.app.ALERT_TIME_AWARE', False)
    def test_account_switch_to_high_account_does_not_fire_command(self):
        """Switching to an account that is ALREADY above a threshold is existing
        state, not an observed crossing, so on_threshold_command must NOT fire - the
        new account's first verified sample is a baseline (review 1745 repro 1)."""
        self.app.cache = MagicMock()
        self.app.cache.update.side_effect = [
            UpdateResult(data={'five_hour': {'utilization': 10.0, 'resets_at': ''}, 'source': 'api', 'account_id': 'acct-A'}),
            UpdateResult(data={'five_hour': {'utilization': 96.0, 'resets_at': ''}, 'source': 'api', 'account_id': 'acct-B'}),  # switch sample (baseline)
            UpdateResult(data={'five_hour': {'utilization': 96.0, 'resets_at': ''}, 'source': 'api', 'account_id': 'acct-B'}),  # no crossing (96->96)
        ]
        self.app.cache.profile = {'account': {'uuid': 'acct-A', 'email': 'a@example.com'}}
        self.app.update()  # A@10 verified
        self.app.cache.profile = {'account': {'uuid': 'acct-B', 'email': 'b@example.com'}}  # codex login to B
        self.app.icon.notify.reset_mock()
        self._cmd.reset_mock()
        self.app.update()  # B@96 switch sample -> baseline (no command)
        self.app.update()  # B@96 again -> no crossing
        self.assertFalse(any(c[0][1].get('USAGE_MONITOR_EVENT') == 'threshold' for c in self._cmd.call_args_list))

    @patch('usage_monitor_for_codex.app.ON_THRESHOLD_COMMAND', ['notify.bat'])
    @patch('usage_monitor_for_codex.app.ALERT_TIME_AWARE', False)
    def test_account_switch_via_mismatch_then_high_no_command(self):
        """When the switch is detected on a mid-switch mismatch sample (profile already
        B but the in-flight response is still A's), the first VERIFIED B sample is the
        baseline - an already-high B does not fire on_threshold_command (review 1745
        repro 2)."""
        self.app.cache = MagicMock()
        self.app.cache.update.side_effect = [
            UpdateResult(data={'five_hour': {'utilization': 10.0, 'resets_at': ''}, 'source': 'api', 'account_id': 'acct-A'}),
            UpdateResult(data={'five_hour': {'utilization': 10.0, 'resets_at': ''}, 'source': 'api', 'account_id': 'acct-A'}),  # in-flight A while profile is B
            UpdateResult(data={'five_hour': {'utilization': 96.0, 'resets_at': ''}, 'source': 'api', 'account_id': 'acct-B'}),  # first verified B (baseline)
        ]
        self.app.cache.profile = {'account': {'uuid': 'acct-A', 'email': 'a@example.com'}}
        self.app.update()  # A@10 verified
        self.app.cache.profile = {'account': {'uuid': 'acct-B', 'email': 'b@example.com'}}  # codex login to B (profile refreshed)
        self.app.icon.notify.reset_mock()
        self._cmd.reset_mock()
        self.app.update()  # switch detected (profile B), response A -> mismatch, returns; baseline pending
        self.app.update()  # first verified B@96 -> baseline (no command)
        self.assertFalse(any(c[0][1].get('USAGE_MONITOR_EVENT') == 'threshold' for c in self._cmd.call_args_list))

    @patch('usage_monitor_for_codex.app.ON_THRESHOLD_COMMAND', ['notify.bat'])
    @patch('usage_monitor_for_codex.app.ALERT_TIME_AWARE', False)
    def test_account_switch_then_observed_crossing_fires(self):
        """After a switch establishes the new account's baseline, a genuinely OBSERVED
        crossing for that account (B@10 -> B@96) fires on_threshold_command exactly
        once - the switch did not suppress B's real crossing (review 1745 repro 3)."""
        self.app.cache = MagicMock()
        self.app.cache.update.side_effect = [
            UpdateResult(data={'five_hour': {'utilization': 10.0, 'resets_at': ''}, 'source': 'api', 'account_id': 'acct-A'}),
            UpdateResult(data={'five_hour': {'utilization': 10.0, 'resets_at': ''}, 'source': 'api', 'account_id': 'acct-B'}),  # switch sample (baseline 10)
            UpdateResult(data={'five_hour': {'utilization': 96.0, 'resets_at': ''}, 'source': 'api', 'account_id': 'acct-B'}),  # observed B crossing
        ]
        self.app.cache.profile = {'account': {'uuid': 'acct-A', 'email': 'a@example.com'}}
        self.app.update()  # A@10 verified
        self.app.cache.profile = {'account': {'uuid': 'acct-B', 'email': 'b@example.com'}}  # codex login to B
        self.app.icon.notify.reset_mock()
        self._cmd.reset_mock()
        self.app.update()  # B@10 switch sample -> baseline 10 (no command)
        self.app.update()  # B@96 -> observed crossing from B's baseline -> fires once
        threshold_calls = [c for c in self._cmd.call_args_list if c[0][1].get('USAGE_MONITOR_EVENT') == 'threshold']
        self.assertEqual(len(threshold_calls), 1)
        self.assertEqual(threshold_calls[0][0][1]['USAGE_MONITOR_THRESHOLD'], '95')

    def test_flapping_fires_verified_crossing_once_not_spam(self):
        """Source flapping is neither silent nor spammy: intervening session samples
        are display-only (ignored for events), while the verified (api) progression
        past a threshold fires exactly once. A later api sample still above the same
        threshold does not re-fire - honoring the documented "resume on live API
        reading" contract without re-alerting on every flap."""
        self._feed(10, 'api')  # establish, low (no crossing)
        self.app.icon.notify.reset_mock()
        for src, util in (('session', 90), ('api', 91), ('session', 92), ('api', 93)):
            self._feed(util, src)
        # api@91 crosses 80 -> fires once; session@90/@92 ignored; api@93 is still
        # above 80 but already notified -> no re-fire.
        self.app.icon.notify.assert_called_once()


if __name__ == '__main__':
    unittest.main()
