"""
Application
=============

System tray application class with adaptive polling and event handling.
"""
from __future__ import annotations

import ctypes
import math
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any

import pystray  # type: ignore[import-untyped]  # no type stubs available

from .codex_api import api_headers
from .autostart import is_autostart_enabled, set_autostart, sync_autostart_path
from .cache import UsageCache
from .command import run_event_command
from .idle import get_idle_seconds, is_workstation_locked
from .settings import (
    ALERT_TIME_AWARE, ALERT_TIME_AWARE_BELOW, IDLE_PAUSE,
    ON_RESET_COMMAND, ON_STARTUP_COMMAND, ON_THRESHOLD_COMMAND,
    POLL_ERROR, POLL_FAST, POLL_FAST_EXTRA, POLL_INTERVAL, TOOLTIP_FIELDS, USAGE_SOURCE, get_alert_thresholds,
)
from .formatting import elapsed_pct, field_period, format_credits, format_tooltip, parse_field_name, popup_label
from .i18n import T
from .popup import SettingsWindow, UsagePopup, show_about_dialog
from .tray_icon import load_tray_icon

__all__ = ['UsageMonitorForCodex', 'crash_log']


def _future_iso(**kwargs: float) -> str:
    """Return an ISO 8601 timestamp offset from now by the given timedelta kwargs."""
    return (datetime.now(timezone.utc) + timedelta(**kwargs)).isoformat()


class UsageMonitorForCodex:
    """System tray application displaying Codex usage."""

    def __init__(self) -> None:
        """Set up the tray icon with context menu and polling state."""
        self.running = True
        self.cache = UsageCache()

        # Last raw API response (may contain 'error') - for icon and polling decisions
        self._last_response: dict[str, Any] = {}

        # Notification state
        self._prev_utilization: dict[str, float] = {}
        self._prev_account_uuid: str | None = None
        self._prev_source: str | None = None
        self._first_update_done = False
        self._notified_thresholds: dict[str, float] = {}

        # Adaptive polling state
        self._fast_polls_remaining = 0
        self._idle_reset_pending = False
        self._deferred_notifications: dict[str, tuple[str, str]] = {}

        # Popup state
        self._popup_lock = threading.Lock()
        self._popup_open = False
        self._popup_closed_at = 0.0
        self._next_poll_time: float | None = None

        # Settings window state (one instance at a time)
        self._settings_lock = threading.Lock()
        self._settings_open = False

        self.restart_requested = False

        self.icon = pystray.Icon(
            'codex_usage_monitor',
            icon=load_tray_icon(),
            title=T['loading'],
            menu=pystray.Menu(
                # default=True: left-clicking the tray icon reopens the widget,
                # so closing it (the X button) is never a dead end.
                pystray.MenuItem(T['show_widget'], self.on_show_popup, default=True),
                pystray.MenuItem(T['settings_title'], self.on_open_settings),
                pystray.MenuItem(T['about_title'], self.on_about),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(
                    T['autostart'], self.on_toggle_autostart,
                    checked=lambda item: is_autostart_enabled(),
                    visible=getattr(sys, 'frozen', False),
                ),
                pystray.MenuItem(T['test_commands'], pystray.Menu(
                    pystray.MenuItem(T['test_reset_5h'], self.on_test_reset_5h, enabled=bool(ON_RESET_COMMAND)),
                    pystray.MenuItem(T['test_reset_7d'], self.on_test_reset_7d, enabled=bool(ON_RESET_COMMAND)),
                    pystray.MenuItem(T['test_threshold_5h'], self.on_test_threshold_5h, enabled=bool(ON_THRESHOLD_COMMAND)),
                    pystray.MenuItem(T['test_threshold_7d'], self.on_test_threshold_7d, enabled=bool(ON_THRESHOLD_COMMAND)),
                    pystray.MenuItem(T['test_startup'], self.on_test_startup, enabled=bool(ON_STARTUP_COMMAND)),
                ), visible=bool(ON_RESET_COMMAND or ON_STARTUP_COMMAND or ON_THRESHOLD_COMMAND)),
                pystray.MenuItem(T['restart'], self.on_restart),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(T['quit'], self.on_quit),
            ),
        )

    # Menu actions

    def on_show_popup(self, icon: Any = None, item: Any = None) -> None:
        with self._popup_lock:
            if self._popup_open:
                return
            if time.time() - self._popup_closed_at < 0.15:
                return
            self._popup_open = True
        threading.Thread(target=self._open_popup, daemon=True).start()

    def on_toggle_autostart(self, icon: Any = None, item: Any = None) -> None:
        set_autostart(not is_autostart_enabled())

    def on_restart(self, icon: Any = None, item: Any = None) -> None:
        self.restart_requested = True
        self.on_quit(icon, item)

    def on_open_settings(self, icon: Any = None, item: Any = None) -> None:
        self.open_settings()

    def on_about(self, icon: Any = None, item: Any = None) -> None:
        threading.Thread(target=show_about_dialog, daemon=True).start()

    def on_test_reset_5h(self, icon: Any = None, item: Any = None) -> None:
        run_event_command(ON_RESET_COMMAND, {
            'USAGE_MONITOR_EVENT': 'reset',
            'USAGE_MONITOR_VARIANT': 'five_hour',
            'USAGE_MONITOR_UTILIZATION': '0',
            'USAGE_MONITOR_PREV_UTILIZATION': '95',
            'USAGE_MONITOR_UTILIZATION_FIVE_HOUR': '0',
            'USAGE_MONITOR_UTILIZATION_SEVEN_DAY': '45',
            'USAGE_MONITOR_RESETS_AT': _future_iso(hours=5),
            'USAGE_MONITOR_TITLE': T['notify_reset_title'],
            'USAGE_MONITOR_MESSAGE': T['notify_reset'],
        })

    def on_test_reset_7d(self, icon: Any = None, item: Any = None) -> None:
        run_event_command(ON_RESET_COMMAND, {
            'USAGE_MONITOR_EVENT': 'reset',
            'USAGE_MONITOR_VARIANT': 'seven_day',
            'USAGE_MONITOR_UTILIZATION': '0',
            'USAGE_MONITOR_PREV_UTILIZATION': '99',
            'USAGE_MONITOR_UTILIZATION_FIVE_HOUR': '12',
            'USAGE_MONITOR_UTILIZATION_SEVEN_DAY': '0',
            'USAGE_MONITOR_RESETS_AT': _future_iso(days=7),
            'USAGE_MONITOR_TITLE': T['notify_reset_title'],
            'USAGE_MONITOR_MESSAGE': T['notify_reset'],
        })

    def on_test_threshold_5h(self, icon: Any = None, item: Any = None) -> None:
        run_event_command(ON_THRESHOLD_COMMAND, {
            'USAGE_MONITOR_EVENT': 'threshold',
            'USAGE_MONITOR_VARIANT': 'five_hour',
            'USAGE_MONITOR_UTILIZATION': '82',
            'USAGE_MONITOR_THRESHOLD': '80',
            'USAGE_MONITOR_RESETS_AT': _future_iso(hours=3),
            'USAGE_MONITOR_TITLE': T['notify_threshold_title'],
            'USAGE_MONITOR_MESSAGE': T['notify_threshold_generic'].format(label=popup_label('five_hour'), pct='82'),
        })

    def on_test_threshold_7d(self, icon: Any = None, item: Any = None) -> None:
        run_event_command(ON_THRESHOLD_COMMAND, {
            'USAGE_MONITOR_EVENT': 'threshold',
            'USAGE_MONITOR_VARIANT': 'seven_day',
            'USAGE_MONITOR_UTILIZATION': '81',
            'USAGE_MONITOR_THRESHOLD': '80',
            'USAGE_MONITOR_RESETS_AT': _future_iso(days=4),
            'USAGE_MONITOR_TITLE': T['notify_threshold_title'],
            'USAGE_MONITOR_MESSAGE': T['notify_threshold_generic'].format(label=popup_label('seven_day'), pct='81'),
        })

    def on_test_startup(self, icon: Any = None, item: Any = None) -> None:
        run_event_command(ON_STARTUP_COMMAND, {
            'USAGE_MONITOR_EVENT': 'startup',
            'USAGE_MONITOR_UTILIZATION_FIVE_HOUR': '0',
            'USAGE_MONITOR_RESETS_AT_FIVE_HOUR': '',
            'USAGE_MONITOR_UTILIZATION_SEVEN_DAY': '45',
            'USAGE_MONITOR_RESETS_AT_SEVEN_DAY': _future_iso(days=3),
        })

    def on_quit(self, icon: Any = None, item: Any = None) -> None:
        self.running = False
        self.icon.stop()

    def open_settings(self) -> None:
        """Open the field-selection settings window (one instance at a time)."""
        with self._settings_lock:
            if self._settings_open:
                return
            self._settings_open = True
        threading.Thread(target=self._open_settings_window, daemon=True).start()

    def _open_settings_window(self) -> None:
        """Create the settings window; runs off the JS-bridge thread."""
        try:
            SettingsWindow(self)
        except Exception:
            self._settings_open = False
            crash_log(traceback.format_exc())

    # Popup

    def _open_popup(self) -> None:
        # _popup_open is set True under _popup_lock (in on_show_popup) and
        # reset here without the lock.  This is safe because False is the
        # permissive default - a momentary stale True only delays the next open.
        try:
            needs_profile = not self.cache.profile
            needs_refresh = self.cache.last_success_time is None or time.time() - self.cache.last_success_time >= POLL_FAST
            if needs_profile or needs_refresh:
                # Single thread: ensure_profile() and update() both acquire
                # cache._lock, so they must run sequentially.  Two threads
                # would cause update()'s non-blocking acquire to fail while
                # ensure_profile() holds the lock.
                def _bg_refresh() -> None:
                    if needs_profile:
                        self.cache.ensure_profile()
                    if needs_refresh:
                        self.update()
                threading.Thread(target=_bg_refresh, daemon=True).start()
            UsagePopup(self)
        finally:
            self._popup_closed_at = time.time()
            self._popup_open = False

    # Tray rendering

    def _render_tray(self) -> None:
        """Refresh the tray tooltip from the current state.

        The tray icon is a static brand mark - the always-on-top widget shows
        live usage - so only the hover tooltip changes here.
        """
        self.icon.title = format_tooltip(self._last_response)

    # Update orchestration

    def update(self) -> None:
        """Request a data refresh from the cache and process the result."""
        result = self.cache.update()
        if result.data is None:
            return

        self._last_response = result.data
        self._render_tray()

        # Handle CLI update notification from token refresh
        if result.token_refresh and result.token_refresh.updated:
            self.icon.notify(
                T['notify_update'].format(old=result.token_refresh.old_version, new=result.token_refresh.new_version),
                T['notify_update_title'],
            )

        if 'error' in result.data:
            return

        # Collect all quota fields with utilization (extra_usage has a different structure)
        quota_fields: dict[str, float] = {}
        for key, value in result.data.items():
            if key == 'extra_usage':
                continue
            if isinstance(value, dict) and 'utilization' in value:
                quota_fields[key] = value.get('utilization', 0) or 0

        # Local session-fallback data (and the first sample after any source
        # switch) is unverified - it may be stale or from a different account -
        # so it must drive the DISPLAY only, never notifications or event
        # commands. Re-baseline silently and skip: a genuine change is acted on
        # later from a verified live (api) sample. This also prevents source
        # flapping from re-firing alerts/commands.
        source = result.data.get('source')
        source_changed = self._prev_source is not None and source != self._prev_source
        self._prev_source = source
        if source == 'session' or source_changed:
            self._prev_utilization = quota_fields
            self._seed_threshold_baseline(result.data)
            if source == 'api':
                # Refresh the cached profile before this first verified live (api)
                # sample reaches the display. A `codex login` performed during a
                # session fallback changes the access token; without re-fetching
                # here, the source-switch early return would leave the OLD
                # account's email shown beside the NEW account's live usage and
                # Credits until the next poll (account / credit misattribution).
                self.cache.ensure_profile()
                # The one-time startup command must also fire on this first live
                # sample even when it follows a session fallback; threshold/reset
                # events stay suppressed (re-baselined above).
                if not self._first_update_done:
                    self._run_startup_command(result.data)
                    self._first_update_done = True
            return

        # Detect account switch: re-fetch profile if the access token changed, then compare UUIDs.
        # When the user runs 'codex login', the token changes and the next profile fetch
        # returns a different account id, preventing a false quota-reset notification.
        self.cache.ensure_profile()
        current_profile = self.cache.profile
        current_account_uuid = current_profile.get('account', {}).get('uuid') if isinstance(current_profile, dict) else None
        if self._prev_account_uuid is not None and current_account_uuid is not None and current_account_uuid != self._prev_account_uuid:
            email = current_profile.get('account', {}).get('email', '')
            message = T['notify_account_switched'].format(email=email) if email else T['notify_account_switched_title']
            self._notify_or_defer('account_switched', message, T['notify_account_switched_title'])
            self._prev_utilization = {}
            self._notified_thresholds = {}
            self._prev_account_uuid = current_account_uuid
            return
        self._prev_account_uuid = current_account_uuid

        # Quota reset: notify AND run the reset command only on a *genuine* reset -
        # usage was near-exhausted and then dropped, with no other quota blocking.
        # A minor dip of the rolling window is not a reset and must not trigger an
        # auto-command such as `codex resume`. While idle/locked, notifications are
        # deferred until the user returns (avoids lock-screen privacy concerns).
        for key, pct in quota_fields.items():
            prev = self._prev_utilization.get(key)
            if prev is None:
                continue

            parsed = parse_field_name(key)
            if parsed is None:
                continue

            _, unit, _ = parsed
            reset_threshold = 95 if unit == 'hour' else 98
            any_blocking = any(other_pct >= 99 for other_key, other_pct in quota_fields.items() if other_key != key)

            if prev > reset_threshold and pct < prev and not any_blocking:
                self._notify_or_defer('reset', T['notify_reset'], T['notify_reset_title'])
                self._run_reset_command(key, pct, prev, data=result.data, entry=result.data.get(key, {}))
                self._idle_reset_pending = False

        self._check_threshold_alerts(result.data)

        # Adaptive polling: speed up when the primary quota's usage is increasing
        watch_key = TOOLTIP_FIELDS[0] if TOOLTIP_FIELDS else 'five_hour'
        watch_pct = quota_fields.get(watch_key, 0)
        watch_prev = self._prev_utilization.get(watch_key)
        if watch_prev is not None and watch_pct > watch_prev:
            self._fast_polls_remaining = POLL_FAST_EXTRA + 1
        elif self._fast_polls_remaining > 0:
            self._fast_polls_remaining -= 1

        self._prev_utilization = quota_fields

        if not self._first_update_done:
            self._run_startup_command(result.data)

        self._first_update_done = True

    # Notifications

    def _notify_or_defer(self, category: str, message: str, title: str) -> None:
        """Show a notification immediately, or defer it if the user is away.

        Parameters
        ----------
        category : str
            Deduplication key (e.g. ``'reset'``, ``'threshold_five_hour'``).
            While deferred, only the latest notification per category is
            kept so the user does not get a flood on return.
        message : str
            Notification body text.
        title : str
            Notification title.
        """
        if self._is_user_away():
            self._deferred_notifications[category] = (message, title)
        else:
            self.icon.notify(message, title)

    def _flush_deferred_notifications(self) -> None:
        """Show all deferred notifications and clear the queue."""
        for message, title in self._deferred_notifications.values():
            self.icon.notify(message, title)
        self._deferred_notifications.clear()

    def _seed_threshold_baseline(self, data: dict[str, Any]) -> None:
        """Seed notified-threshold baselines from the current sample without firing.

        Used when the data source switches (live API <-> session fallback) so an
        already-exceeded threshold in the freshly-switched sample is treated as
        already-notified and does not emit a spurious alert or command.  A genuine
        later increase past a higher threshold still fires on the next poll.
        """
        for variant_key, entry in data.items():
            if variant_key == 'extra_usage':
                continue
            if not isinstance(entry, dict) or entry.get('utilization') is None:
                continue
            pct = entry['utilization']
            exceeded = [t for t in get_alert_thresholds(variant_key) if pct >= t]
            self._notified_thresholds[variant_key] = max(exceeded) if exceeded else 0

        extra = data.get('extra_usage')
        if extra and extra.get('is_enabled'):
            limit = extra.get('monthly_limit', 0) or 0
            if limit > 0:
                pct = (extra.get('used_credits', 0) or 0) / limit * 100
                exceeded = [t for t in get_alert_thresholds('extra_usage') if pct >= t]
                self._notified_thresholds['extra_usage'] = max(exceeded) if exceeded else 0

    def _check_threshold_alerts(self, data: dict[str, Any]) -> None:
        """Show a notification when usage crosses a configured threshold.

        Dynamically detects all quota fields in the API response.  For
        each field, finds the highest threshold exceeded by current
        utilization.  If it exceeds a threshold not yet notified, shows a
        single notification with the current usage percentage.  When usage
        drops (e.g. after reset), tracking resets so thresholds can
        re-trigger in the next cycle.
        """
        for variant_key, entry in data.items():
            if variant_key == 'extra_usage':
                continue
            if not isinstance(entry, dict) or entry.get('utilization') is None:
                continue

            pct = entry['utilization']
            thresholds = get_alert_thresholds(variant_key)
            if not thresholds:
                continue

            exceeded = [t for t in thresholds if pct >= t]
            highest_exceeded = max(exceeded) if exceeded else 0
            last_notified = self._notified_thresholds.get(variant_key, 0)

            if ALERT_TIME_AWARE and highest_exceeded > last_notified and highest_exceeded < ALERT_TIME_AWARE_BELOW:
                period = field_period(variant_key)
                if period:
                    time_pct = elapsed_pct(entry.get('resets_at'), period)
                    if time_pct is not None and pct <= time_pct:
                        self._notified_thresholds[variant_key] = highest_exceeded
                        continue

            if highest_exceeded > last_notified:
                title = T['notify_threshold_title']
                label = popup_label(variant_key)
                message = T['notify_threshold_generic'].format(label=label, pct=f'{pct:.0f}')
                self._notify_or_defer(f'threshold_{variant_key}', message, title)
                self._run_threshold_command(variant_key, pct, highest_exceeded, entry, title, message)
                self._notified_thresholds[variant_key] = highest_exceeded
            elif highest_exceeded < last_notified:
                self._notified_thresholds[variant_key] = highest_exceeded

        self._check_extra_usage_alerts(data)

    def _check_extra_usage_alerts(self, data: dict[str, Any]) -> None:
        """Show a notification when extra usage crosses a configured threshold.

        Extra usage has a different data format (``used_credits`` /
        ``monthly_limit``) and no time-based reset, so it is handled
        separately from the sliding-window quotas.
        """
        extra = data.get('extra_usage')
        if not extra or not extra.get('is_enabled'):
            return

        limit = extra.get('monthly_limit', 0) or 0
        if limit <= 0:
            return

        used = extra.get('used_credits', 0) or 0
        pct = used / limit * 100

        thresholds = get_alert_thresholds('extra_usage')
        if not thresholds:
            return

        exceeded = [t for t in thresholds if pct >= t]
        highest_exceeded = max(exceeded) if exceeded else 0
        last_notified = self._notified_thresholds.get('extra_usage', 0)

        if highest_exceeded > last_notified:
            title = T['notify_threshold_title']
            message = T['notify_threshold_extra_usage'].format(
                pct=f'{pct:.0f}', used=format_credits(used), limit=format_credits(limit),
            )
            self._notify_or_defer('threshold_extra_usage', message, title)
            self._run_threshold_command(
                'extra_usage', pct, highest_exceeded, extra, title, message,
                extra_used=format_credits(used), extra_limit=format_credits(limit),
            )
            self._notified_thresholds['extra_usage'] = highest_exceeded
        elif highest_exceeded < last_notified:
            self._notified_thresholds['extra_usage'] = highest_exceeded

    # Event commands

    def _run_startup_command(self, data: dict[str, Any]) -> None:
        """Run the user-configured startup command if set.

        Fires once after the first successful API update.  Receives the
        full quota state so the command can decide what to do (e.g. only
        ping Codex when no five-hour session is active).
        """
        if not ON_STARTUP_COMMAND:
            return

        env_vars: dict[str, str] = {
            'USAGE_MONITOR_EVENT': 'startup',
        }
        for key, entry in data.items():
            if key == 'extra_usage' or not isinstance(entry, dict) or 'utilization' not in entry:
                continue
            env_vars[f'USAGE_MONITOR_UTILIZATION_{key.upper()}'] = str(round(entry.get('utilization', 0) or 0))
            env_vars[f'USAGE_MONITOR_RESETS_AT_{key.upper()}'] = entry.get('resets_at') or ''

        extra = data.get('extra_usage') or {}
        if extra.get('is_enabled'):
            limit = extra.get('monthly_limit', 0) or 0
            used = extra.get('used_credits', 0) or 0
            env_vars['USAGE_MONITOR_EXTRA_USED'] = format_credits(used)
            env_vars['USAGE_MONITOR_EXTRA_LIMIT'] = format_credits(limit)

        run_event_command(ON_STARTUP_COMMAND, env_vars)

    def _run_reset_command(
        self, variant: str, pct: float, prev_pct: float, *, data: dict[str, Any], entry: dict[str, Any],
    ) -> None:
        """Run the user-configured reset command if set."""
        if not ON_RESET_COMMAND:
            return

        pct_5h = (data.get('five_hour') or {}).get('utilization', 0) or 0
        pct_7d = (data.get('seven_day') or {}).get('utilization', 0) or 0
        run_event_command(ON_RESET_COMMAND, {
            'USAGE_MONITOR_EVENT': 'reset',
            'USAGE_MONITOR_VARIANT': variant,
            'USAGE_MONITOR_UTILIZATION': str(round(pct)),
            'USAGE_MONITOR_PREV_UTILIZATION': str(round(prev_pct)),
            'USAGE_MONITOR_UTILIZATION_FIVE_HOUR': str(round(pct_5h)),
            'USAGE_MONITOR_UTILIZATION_SEVEN_DAY': str(round(pct_7d)),
            'USAGE_MONITOR_RESETS_AT': entry.get('resets_at', ''),
            'USAGE_MONITOR_TITLE': T['notify_reset_title'],
            'USAGE_MONITOR_MESSAGE': T['notify_reset'],
        })

    def _run_threshold_command(
        self, variant: str, pct: float, threshold: float,
        entry: dict[str, Any], title: str, message: str,
        *, extra_used: str = '', extra_limit: str = '',
    ) -> None:
        """Run the user-configured threshold command if set.

        Skipped on the first update (before ``_first_update_done`` is set)
        so that already-exceeded thresholds at app startup do not trigger
        commands.  Notifications still fire - commands react to *events*,
        not *state*.
        """
        if not ON_THRESHOLD_COMMAND or not self._first_update_done:
            return

        env_vars = {
            'USAGE_MONITOR_EVENT': 'threshold',
            'USAGE_MONITOR_VARIANT': variant,
            'USAGE_MONITOR_UTILIZATION': str(round(pct)),
            'USAGE_MONITOR_THRESHOLD': str(round(threshold)),
            'USAGE_MONITOR_RESETS_AT': entry.get('resets_at', ''),
            'USAGE_MONITOR_TITLE': title,
            'USAGE_MONITOR_MESSAGE': message,
        }
        if extra_used:
            env_vars['USAGE_MONITOR_EXTRA_USED'] = extra_used
        if extra_limit:
            env_vars['USAGE_MONITOR_EXTRA_LIMIT'] = extra_limit

        run_event_command(ON_THRESHOLD_COMMAND, env_vars)

    # Polling

    def _seconds_until_next_reset(self) -> float | None:
        """Return seconds until the earliest upcoming quota reset, or None."""
        now = datetime.now(timezone.utc)
        earliest = None
        for key, entry in self._last_response.items():
            if not isinstance(entry, dict) or not entry.get('resets_at'):
                continue
            try:
                reset_time = datetime.fromisoformat(entry['resets_at'])
                seconds = (reset_time - now).total_seconds()
                if seconds > 0 and (earliest is None or seconds < earliest):
                    earliest = seconds
            except Exception:
                continue

        return earliest

    def _calculate_poll_interval(self) -> int:
        """Determine the next poll interval based on current state.

        Returns
        -------
        int
            Seconds to wait before the next poll.
        """
        data = self._last_response

        if data.get('rate_limited'):
            remaining = self.cache.rate_limit_remaining
            interval = max(math.ceil(remaining), POLL_INTERVAL) if remaining > 0 else POLL_INTERVAL
        elif 'error' in data:
            interval = POLL_ERROR
        elif self._fast_polls_remaining > 0:
            interval = POLL_FAST
        else:
            interval = POLL_INTERVAL

        # Align next poll to an imminent reset for faster feedback.
        # The +5s buffer guards against minor timing differences
        # (clocks, caches, processing delays). Follow-up uses POLL_FAST
        # regardless of user activity (quota was likely exhausted).
        next_reset = self._seconds_until_next_reset()
        if next_reset is not None and next_reset + 5 <= interval * 1.5:
            interval = max(int(next_reset) + 5, POLL_FAST)
            self._fast_polls_remaining = max(self._fast_polls_remaining, 2)

        return interval

    def _is_user_away(self) -> bool:
        """Return True if the user is idle or the workstation is locked."""
        if is_workstation_locked():
            return True
        return IDLE_PAUSE > 0 and get_idle_seconds() >= IDLE_PAUSE

    def _wait_for_activity(self, until: float | None = None) -> None:
        """Block until user activity resumes or the app is stopping.

        Parameters
        ----------
        until : float | None
            Optional deadline (``time.time()`` epoch).  When set, the
            wait ends even if the user is still away, allowing a
            time-critical poll (e.g. quota reset command) to proceed.
        """
        while self.running and self._is_user_away():
            if until is not None and time.time() >= until:
                break
            time.sleep(2)

    def poll_loop(self) -> None:
        """Poll the API in a loop with adaptive intervals.

        Pauses polling when the user is idle or the workstation is
        locked.  On resume, polls immediately if the regular interval
        has elapsed since the last successful fetch.
        """
        self.cache.ensure_profile()
        while self.running:
            self.update()
            interval = self._calculate_poll_interval()

            target = time.time() + interval
            self._next_poll_time = target
            while self.running and time.time() < target:
                time.sleep(1)
                # If another thread (popup) fetched successfully,
                # push the next poll forward to avoid a redundant
                # fetch right after.
                lst = self.cache.last_success_time
                if lst is not None:
                    new_target = max(target, lst + interval)
                    if new_target != target:
                        target = new_target
                        self._next_poll_time = target

                # Pause polling while the user is away.
                # Regular polling stops entirely during idle/lock.
                # The only exception: when on_reset_command is configured
                # and a quota reset is due, the idle pause is interrupted
                # so the command fires on time.  The flag
                # _idle_reset_pending keeps polling at POLL_INTERVAL
                # until the reset is actually confirmed (usage drop) -
                # this covers server-side delays and transient network
                # errors.  The flag is cleared when update() detects the
                # drop, or when the user returns (they'll see it anyway).
                if self._is_user_away():
                    reset_deadline = None
                    if ON_RESET_COMMAND:
                        next_reset = self._seconds_until_next_reset()
                        if next_reset is not None:
                            reset_deadline = time.time() + next_reset + 5
                            self._idle_reset_pending = True
                        elif self._idle_reset_pending:
                            reset_deadline = time.time() + POLL_INTERVAL

                    self._wait_for_activity(until=reset_deadline)

                    if reset_deadline is not None and self._is_user_away():
                        # Woke up for a reset while still idle - poll once
                        break

                    # User returned - show any notifications deferred
                    # during idle and poll immediately if interval elapsed.
                    # _idle_reset_pending is intentionally kept: if the
                    # user locks again before a successful poll confirms
                    # the reset (e.g. network was down), idle polling
                    # must resume.  The flag is only cleared by update()
                    # when a usage drop is actually detected.
                    self._flush_deferred_notifications()
                    lst = self.cache.last_success_time
                    if lst is not None and time.time() - lst >= interval:
                        break

    # Lifecycle

    def _on_icon_ready(self, icon: Any) -> None:
        """Called by pystray in a separate thread once the tray icon is set up."""
        try:
            icon.visible = True
            if getattr(sys, 'frozen', False):
                sync_autostart_path()
            # Only warn about missing credentials when the live API is actually
            # used. In explicit session mode the app reads local rollout files and
            # needs no token, so a "run codex login" warning would be a false alarm.
            if USAGE_SOURCE != 'session' and not api_headers():
                icon.notify(f"{T['warn_no_token']}\n{T['warn_login']}", T['popup_title'])
            self.on_show_popup()
            self.poll_loop()
        except Exception:
            crash_log(traceback.format_exc())

    def run(self) -> None:
        self.icon.run(setup=self._on_icon_ready)


def crash_log(msg: str) -> None:
    """Show a crash message box (for windowless EXE builds)."""
    ctypes.windll.user32.MessageBoxW(0, msg[:2000], 'Codex Usage Monitor - Error', 0x10)
