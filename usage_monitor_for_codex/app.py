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

        # Notification state. _prev_utilization and _notified_thresholds are TRUSTED
        # event state: written only by a verified live (api) sample of the currently
        # displayed account (or explicitly reset on a verified account switch). An
        # unverified / session / mismatched reading never writes them, so it can
        # neither suppress nor spuriously fire a threshold or reset event.
        self._prev_utilization: dict[str, float] = {}
        self._prev_account_uuid: str | None = None
        self._prev_source: str | None = None
        self._first_update_done = False
        self._notified_thresholds: dict[str, float] = {}
        # Best-known per-window ENTRY view (full window dicts, carrying resets_at) -
        # the entry-level companion to _prev_utilization: identical keyset and
        # lifecycle (reset on a verified account switch, merged from each verified
        # live sample). Reset SCHEDULING reads this, not the raw latest response, so
        # a partial or credits-only verified response that omits a window cannot
        # erase that window's known reset deadline (which the idle/lock
        # on_reset_command wake-up depends on).
        self._prev_entries: dict[str, dict[str, Any]] = {}
        # Last verified extra_usage (Credits) utilization for the current account,
        # the extra_usage counterpart to _prev_utilization (reset on a verified
        # account switch). Gates the extra_usage threshold command to an OBSERVED
        # crossing: None means "not yet observed for this account", so a first
        # observation seeds the baseline without firing on_threshold_command.
        self._prev_extra_usage_pct: float | None = None
        # Per window: the reset deadline (resets_at) whose reset has already been
        # CONFIRMED for the current account (a reset command fired for it). Durable
        # across polls so a window that already reset but lingers under the SAME stale
        # passed deadline (the server has not advanced it) is not repeatedly re-counted
        # as an unconfirmed overdue reset. Reset on a verified account switch.
        self._reset_confirmed: dict[str, str] = {}
        # Per window: when it FIRST went missing from verified responses (cleared
        # the moment it is present again). The ageing signal that lets Phase 4
        # retire a DEADLINE-LESS retained entry once its absence exceeds its own
        # window length - such an entry has no other retirement path. Account-
        # scoped (reset on a verified account switch) like its companions.
        self._absent_since: dict[str, datetime] = {}

        # Adaptive polling state
        self._fast_polls_remaining = 0
        # Per-account: an awaited reset deadline for the CURRENT account is pending
        # confirmation. Reset on a verified account switch (the new account has its
        # own deadlines), else a stale pending flag would keep polling a switched-to
        # account that has no due reset while the user is idle/locked.
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
        except Exception:
            # Mirror _open_settings_window: this runs on a daemon thread, so an
            # unreported failure here means the widget silently never opens.
            crash_log(traceback.format_exc())
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

    @staticmethod
    def _same_deadline(rs_a: str | None, rs_b: str | None) -> bool:
        """Return True when two ISO ``resets_at`` strings denote the SAME period
        boundary.

        Compared with a tolerance rather than byte equality:
        ``transform_rate_limits`` may derive ``resets_at`` from a RELATIVE
        countdown (``resets_in_seconds`` etc.), which yields a slightly
        different absolute string each poll within the same period. The 600 s
        tolerance absorbs that jitter / minor server clock adjustment and is an
        order of magnitude below the smallest window (5 h), so a genuine
        reset's forward jump is never within it. Used by the in-period dip
        classifier, Phase 4's same-period KEEP guard, and the reset-confirmed
        bookkeeping, so all three classify a drifting deadline identically.
        """
        if not rs_a or not rs_b:
            return False
        if rs_a == rs_b:
            return True
        try:
            dt_a = datetime.fromisoformat(rs_a)
            dt_b = datetime.fromisoformat(rs_b)
        except (ValueError, TypeError):
            return False
        return abs((dt_b - dt_a).total_seconds()) <= 600

    @staticmethod
    def _window_length_seconds(key: str) -> float:
        """Length of *key*'s usage window in seconds (the longest shipped
        window, 7 days, for an unparseable slot key like ``primary``) - the
        upper bound on how long a retained value can stay meaningful: no
        window outlives its own period."""
        period = field_period(key)
        return float(period) if period else 7 * 24 * 3600.0

    def _is_in_period_dip(self, key: str, data: dict[str, Any], now: datetime) -> bool:
        """Return True when ``key``'s utilization change is a dip WITHIN the current
        active period rather than a GENUINE reset.

        Reset events (the reset notification and ``on_reset_command``) must fire only on
        a genuine reset - the quota period actually ended - never on a rolling-window
        dip while the same period is still active (docs/event-commands.md). The signal
        for "still the same active period" is the reset deadline: the window is present
        in this verified response under (essentially) the SAME reset deadline as the
        retained trusted entry, and that deadline is still in the future. Everything
        else is a genuine reset and is NOT a dip:
          - the window VANISHED (absent -> no current resets_at);
          - its deadline ADVANCED to a new period (a reset moves it forward by at least
            a full window, >= 5h);
          - the retained deadline has already PASSED (overdue; the server may lag,
            still reporting the old deadline, but the period has ended).
        When no trusted retained deadline is available the period cannot be classified,
        so this returns False (a drop is treated as a reset, the prior behaviour).

        Deadlines are compared with a tolerance rather than by exact string equality:
        ``transform_rate_limits`` may derive ``resets_at`` from a RELATIVE countdown
        (``resets_in_seconds`` etc.) as ``now + delta``, which yields a slightly
        different absolute string each poll within the same period. The tolerance
        (seconds-scale jitter) is far below a genuine reset's forward jump (a whole
        window), so both an absolute (byte-identical) and a relative (drifting)
        deadline are classified correctly."""
        retained_entry = self._prev_entries.get(key)
        retained_rs = retained_entry.get('resets_at') if isinstance(retained_entry, dict) else None
        current_entry = data.get(key)
        current_rs = current_entry.get('resets_at') if isinstance(current_entry, dict) else None
        if not retained_rs or not current_rs:
            return False
        try:
            retained_dt = datetime.fromisoformat(retained_rs)
        except (ValueError, TypeError):
            return False
        if retained_dt <= now:
            return False  # retained deadline already passed -> overdue, a genuine reset
        # Same active period iff the deadline has not jumped forward into a new
        # period (see _same_deadline for the tolerance rationale; an unparseable
        # current deadline compares unequal there -> treated as a genuine reset,
        # matching the previous behaviour).
        return self._same_deadline(current_rs, retained_rs)

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

        # Local session-fallback data is unverified - it may be stale or from a
        # different account - so it drives the DISPLAY only, never notifications or
        # event commands, and it must NEVER touch the trusted event state
        # (_prev_utilization / _notified_thresholds). That state belongs exclusively
        # to verified live (api) samples; letting a session reading write it would
        # let a stale high session value suppress a later genuine verified threshold
        # crossing, or a session drop be misread as a reset. The popup renders from
        # the cache snapshot, so returning here costs nothing visible, and the
        # documented contract holds: notifications and commands "resume the moment a
        # live API reading succeeds" - that next live sample is processed normally
        # below (including the first one after a session fallback, which refreshes
        # the profile and can fire the startup command on the shared path).
        source = result.data.get('source')
        self._prev_source = source
        if source == 'session':
            return

        # Detect an account switch by comparing the profile UUID across polls. The
        # profile cache is keyed on (access_token, effective account id), so
        # ensure_profile() re-fetches when EITHER the access token changes (a `codex
        # login`) OR the account changes (a same-token workspace/account switch) -
        # either yields a new account UUID here. On a switch, reset the OLD account's
        # trusted event state. The new account's windows then have no prior value, so
        # the per-window baseline guard in _check_threshold_alerts treats each
        # window's FIRST observed value as a baseline (no command) - merely switching
        # to an already-high account is existing state, not an OBSERVED crossing. Do
        # NOT return: the switch sample itself, if verified for the new account,
        # records its windows' baselines below (so a later observed B-low -> B-high
        # crossing still fires); a mid-switch mismatch / unverified sample instead
        # returns at the identity gate.
        self.cache.ensure_profile()
        current_profile = self.cache.profile
        account = current_profile.get('account') if isinstance(current_profile, dict) else None
        if not isinstance(account, dict):
            account = {}
        current_account_uuid = account.get('uuid')
        if self._prev_account_uuid is not None and current_account_uuid is not None and current_account_uuid != self._prev_account_uuid:
            # Drop the OLD account's deferred notifications BEFORE enqueueing the
            # switch notice below: while the user was away, a reset/threshold
            # notification may have been deferred for account A; flushing it on
            # return would surface a stale, misattributed alert against the now-
            # displayed account B (the same cross-account leak this block prevents).
            self._deferred_notifications.clear()
            email = account.get('email', '')
            message = T['notify_account_switched'].format(email=email) if email else T['notify_account_switched_title']
            self._notify_or_defer('account_switched', message, T['notify_account_switched_title'])
            # Clear ALL account-scoped trusted state so no old-account value leaks
            # into the new account's events, display, or idle polling. _prev_account_uuid
            # is advanced (not cleared) below; global/lifecycle state persists.
            self._prev_utilization = {}
            self._prev_entries = {}
            self._notified_thresholds = {}
            self._prev_extra_usage_pct = None
            self._reset_confirmed = {}
            self._absent_since = {}
            # A pending reset belongs to the OLD account; the new account has its own
            # deadlines. Leaving this set would keep idle/locked polling alive for a
            # switched-to account that has no due reset (unnecessary background wakes).
            self._idle_reset_pending = False
            # The fast-poll burst is derived from the OLD account's observed quota
            # increase; the new account hasn't shown a rise yet. Leaving it would
            # carry A's accelerated cadence into B for a couple of polls - account-
            # derived polling state must not cross a switch. B re-arms its own burst
            # the moment its usage is seen to increase.
            self._fast_polls_remaining = 0
        # Preserve the last known account UUID across a transient profile-fetch
        # failure (current_account_uuid is None). Overwriting it with None here would
        # lose the prior identity, so a genuine switch A -> B whose first B-profile
        # fetch failed would, once B's profile recovers, be MISSED by the uuid-switch
        # above and B's usage misread against A's baseline (false reset / suppressed
        # crossing). Only advance the baseline when we actually resolved a UUID.
        if current_account_uuid is not None:
            self._prev_account_uuid = current_account_uuid

        # Identity gate (fail-closed): a live (api) sample drives events and updates
        # the trusted event state only when its stamped account POSITIVELY matches
        # the displayed profile. An unidentified, partially identified, or mismatched
        # sample - including a same-token account switch whose profile fetch has not
        # refreshed yet, so the stamp is for a different account than is displayed -
        # drives NO notifications or event commands and leaves the trusted event
        # state untouched, so it can neither suppress nor spuriously fire an event for
        # the verified account. The displayed account's baseline resumes unchanged on
        # the next identity-consistent sample. (This check runs BEFORE any trusted-
        # state write, so a stray cross-account sample can never write that state.)
        if not self._identity_ok(result.data):
            return

        # Quota reset: notify AND run the reset command only on a *genuine* reset -
        # usage was near-exhausted and then dropped, with no other quota blocking.
        # A minor dip of the rolling window is not a reset and must not trigger an
        # auto-command such as `codex resume`. _prev_utilization only ever holds a
        # verified live sample's values (every untrusted sample returned above), so
        # this compares verified-to-verified for the same account. While idle/locked,
        # notifications are deferred until the user returns (lock-screen privacy).
        # "Is another quota still blocking?" must consider EVERY window's best-known
        # current utilization, not only the windows in this (possibly partial)
        # response. A window omitted from this response keeps its last verified value
        # in _prev_utilization; if that was blocking (>=99) a reset for a different
        # window must still be suppressed. Overlay the current response on the
        # accumulated last-known state. (For normal Codex responses, which carry all
        # windows, this equals quota_fields, so behavior is unchanged.)
        # Build the reset-evaluation set: every window PRESENT in this response (at
        # its current pct) PLUS any previously-observed window that is ABSENT from
        # this verified response AND whose retained deadline has already PASSED. An
        # expired window dropping out of the payload is a genuine reset completion
        # (the live API may return a null rate_limit once no window is active, so the
        # window simply disappears), evaluated as pct=0 so the existing
        # near-exhausted-then-dropped rule fires the reset command exactly once. A
        # window absent but still BEFORE its deadline is a transient partial omission
        # (kept retained as before, NOT a reset).
        now = datetime.now(timezone.utc)
        expired_absent: set[str] = set()
        reset_candidates: dict[str, float] = dict(quota_fields)
        for key, entry in self._prev_entries.items():
            if key in quota_fields:
                continue
            resets_at = entry.get('resets_at') if isinstance(entry, dict) else None
            if not resets_at:
                continue
            try:
                deadline_passed = datetime.fromisoformat(resets_at) <= now
            except (ValueError, TypeError):
                continue
            if deadline_passed:
                reset_candidates[key] = 0.0
                expired_absent.add(key)

        # Track how long each retained window has been ABSENT from verified
        # responses. A deadline-less retained entry cannot be expiry-evaluated
        # or deadline-retired, so this is its only ageing signal: within its
        # own window length the last-known value keeps blocking (a transient
        # partial omission, review 1919); beyond it the period has CERTAINLY
        # ended - no window outlives its own length.
        for key in list(self._absent_since):
            if key in quota_fields or key not in self._prev_entries:
                self._absent_since.pop(key, None)
        for key in self._prev_entries:
            if key not in quota_fields and key not in self._absent_since:
                self._absent_since[key] = now

        # Retire aged-out DEADLINE-LESS ghosts BEFORE the blocking view below
        # is snapshotted: on the very poll a ghost crosses its age horizon, a
        # sibling's genuine reset arriving in the SAME response must not be
        # suppressed by it - a suppressed reset command is never replayed, so
        # it would be lost permanently. A >=99 deadline-less ghost retained
        # past its horizon would otherwise suppress every other window's reset
        # events forever (it has no other retirement path). A genuine return
        # later seeds a fresh baseline like any first observation.
        for key in list(self._prev_entries):
            entry = self._prev_entries.get(key)
            if not isinstance(entry, dict) or entry.get('resets_at'):
                continue
            absent_from = self._absent_since.get(key)
            if (absent_from is not None
                    and (now - absent_from).total_seconds() > self._window_length_seconds(key)):
                self._prev_utilization.pop(key, None)
                self._prev_entries.pop(key, None)
                self._notified_thresholds.pop(key, None)
                self._absent_since.pop(key, None)

        # "Is another window still blocking?" uses best-known current utilization:
        # an expired-absent window counts as 0 (it reset), a present window at its
        # current pct, any other retained window at its last-known value. Snapshot it
        # BEFORE any purge below so the blocking view is stable.
        effective_util = {**self._prev_utilization, **reset_candidates}

        # Phase 1: decide which windows are a genuine reset - near-exhausted, dropped,
        # and not blocked by another window still at >=99.
        firing: list[tuple[str, float, float]] = []  # (key, current_pct, prev_pct)
        for key, pct in reset_candidates.items():
            prev = self._prev_utilization.get(key)
            if prev is None:
                continue
            parsed = parse_field_name(key)
            if parsed is None:
                continue
            _, unit, _ = parsed
            reset_threshold = 95 if unit == 'hour' else 98
            # A reset event requires a near-exhausted window to DROP (prev > threshold,
            # pct < prev) AND that drop to be a GENUINE reset, not an in-period dip
            # (same still-future deadline). This is the single gate for both the reset
            # notification and on_reset_command.
            if prev > reset_threshold and pct < prev and not self._is_in_period_dip(key, result.data, now):
                # An overdue reset DROP has been OBSERVED for this window (it is genuine
                # and its retained deadline has passed). Record it confirmed for that
                # deadline, INDEPENDENT of any_blocking: the reset happened; blocking
                # only suppresses the command (which is not replayed later), so a
                # blocked-but-observed drop must not keep the idle retry alive after the
                # blocker clears. (A genuine reset via an ADVANCED deadline leaves the
                # old deadline in the past too, but is retired in Phase 4.)
                retained_entry = self._prev_entries.get(key)
                retained_rs = retained_entry.get('resets_at') if isinstance(retained_entry, dict) else None
                if retained_rs:
                    try:
                        if datetime.fromisoformat(retained_rs) <= now:
                            self._reset_confirmed[key] = retained_rs
                    except (ValueError, TypeError):
                        pass
                any_blocking = any(other_pct >= 99 for other_key, other_pct in effective_util.items() if other_key != key)
                if not any_blocking:
                    firing.append((key, pct, prev))

        # Phase 2: purge EVERY firing expired-absent window up front (before running
        # any command), so each completed window reports as 0 in the reset command
        # payload - even when several windows expire in the SAME response, where
        # otherwise the first command would still read a sibling's stale pre-reset
        # value via _prev_utilization - and so none can re-fire on a later response
        # that also omits it. A genuinely new window arriving later re-baselines
        # cleanly. Present windows are not purged here: the end-of-update merge
        # refreshes them, and the command reads their current value from the payload.
        for key, _pct, _prev in firing:
            if key in expired_absent:
                self._prev_utilization.pop(key, None)
                self._prev_entries.pop(key, None)
                self._notified_thresholds.pop(key, None)
                self._absent_since.pop(key, None)

        # Phase 3: notify + run the reset command for each firing window. Whether to
        # clear the idle reset-wait is decided once after Phase 4 (a fired reset alone
        # is not enough - an expired sibling may still be awaiting confirmation).
        reset_fired = False
        for key, pct, prev in firing:
            entry_payload: dict[str, Any] = {} if key in expired_absent else result.data.get(key, {})
            self._notify_or_defer('reset', T['notify_reset'], T['notify_reset_title'])
            self._run_reset_command(key, pct, prev, data=result.data, entry=entry_payload)
            reset_fired = True

        # Phase 4: retire EVERY window whose period has ENDED, independent of whether
        # its reset command fired. Disposal of an ended period's trusted state must be
        # decoupled from on_reset_command firing: a window that expired at low usage,
        # or whose reset was suppressed because another quota was blocking, has still
        # ended its period. A window's period is over once its RETAINED deadline has
        # passed; the new period's first observed value must then be a FRESH baseline
        # (a notification may show, but on_threshold_command must NOT fire - it is not
        # an in-period crossing), exactly like a window first appearing. This covers
        # both a window that VANISHED from the response and one PRESENT again under a
        # NEW deadline (the new period is already in use). Reset detection above
        # already consumed the old value; firing expired-absent windows were purged in
        # Phase 2; the end-of-update merge re-records a present window's new-period
        # baseline and deadline.
        had_overdue = False
        still_awaiting = False
        for key in list(self._prev_entries):
            entry = self._prev_entries.get(key)
            resets_at = entry.get('resets_at') if isinstance(entry, dict) else None
            if not resets_at:
                # No deadline -> nothing to expiry-evaluate here. Ageing-out of
                # deadline-less ABSENT entries happens before the blocking
                # snapshot above (it must not suppress a same-poll sibling
                # reset); a PRESENT one keeps refreshing through the merge.
                continue
            try:
                if datetime.fromisoformat(resets_at) > now:
                    continue  # period still active - keep the baseline
            except (ValueError, TypeError):
                continue
            # The retained deadline has passed: this window's reset is overdue and is
            # being evaluated against this verified response (whether it then retires,
            # or lingers under the KEEP guard below).
            had_overdue = True
            current = result.data.get(key)
            if (isinstance(current, dict) and current.get('utilization') is not None
                    and self._same_deadline(current.get('resets_at'), resets_at)):
                # The same already-passed deadline is still reported (the server has
                # not advanced the period yet): not a rollover, keep the baseline so
                # the lingering value is not misread as a fresh period. This window is
                # genuinely AWAITING its overdue reset only if a reset command is
                # actually pending AND its drop has not yet been observed: it must be
                # reset-command-ELIGIBLE (parseable - Phase 1 cannot fire for an
                # unparseable fallback key like primary/secondary, so mirror its parse
                # gate), still above its reset threshold (near-exhausted, no drop seen),
                # and this exact deadline's reset not already recorded confirmed in
                # _reset_confirmed. A low / unparseable / already-confirmed lingering
                # window has no pending command and must not hold the idle retry open.
                parsed_keep = parse_field_name(key)
                if parsed_keep is not None:
                    keep_threshold = 95 if parsed_keep[1] == 'hour' else 98
                    if ((current.get('utilization') or 0) > keep_threshold
                            and not self._same_deadline(self._reset_confirmed.get(key), resets_at)):
                        still_awaiting = True
                continue
            self._prev_utilization.pop(key, None)
            self._prev_entries.pop(key, None)
            self._notified_thresholds.pop(key, None)
            self._absent_since.pop(key, None)

        # Single decision point for the idle/lock reset-deadline wait. _idle_reset_pending
        # is scheduler state ("an overdue reset still needs confirming"), not an event.
        # The wait is COMPLETE when this verified response either confirmed a reset (a
        # drop fired) OR actually evaluated an overdue window (had_overdue: a retained
        # deadline had passed, whether the window then retired or lingered) - AND no
        # window is still genuinely awaiting confirmation (present, unadvanced past
        # deadline, still above its reset threshold, reset not already confirmed for
        # that deadline). Requiring (reset_fired or had_overdue) avoids clearing the
        # flag on a poll that only saw still-FUTURE deadlines (poll_loop armed it for an
        # upcoming deadline and re-arms it via _seconds_until_next_reset); requiring
        # `not still_awaiting` preserves the overdue retry (which _seconds_until_next_reset
        # cannot schedule, as it ignores past deadlines) until the drop is seen. API
        # error / identity-mismatch / session samples returned before this and never
        # clear the flag.
        if (reset_fired or had_overdue) and not still_awaiting:
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

        # MERGE (not replace) the per-window baseline, so each window's last verified
        # value PERSISTS across responses that omit it. _prev_utilization is the
        # accumulated last-known utilization per quota window for the current account
        # (reset on an account switch). Merging is what makes both the reset check
        # (prev value for a returning window) and the per-window threshold guard
        # (a window stays "observed" once seen) survive a partial or credits-only
        # response. A quota-less response (e.g. credits-only Pro) merges nothing, so
        # it neither clobbers nor loses a real prior baseline.
        if quota_fields:
            self._prev_utilization.update(quota_fields)
            # Retain each present window's full entry (resets_at included), keyed
            # like _prev_utilization, so a later partial / credits-only response that
            # omits the window still exposes its reset deadline to scheduling.
            self._prev_entries.update({key: result.data[key] for key in quota_fields})

        # Fire the one-time startup command on the first successful update, but
        # only when this live sample's account matches the displayed profile -
        # never export a mid-account-switch sample. A deferred hook fires on the
        # next identity-consistent sample.
        if not self._first_update_done and self._identity_ok(result.data):
            self._run_startup_command(result.data)
            self._first_update_done = True

    def _identity_ok(self, data: dict[str, Any]) -> bool:
        """Fail-closed account check gating billing-sensitive event export for LIVE
        (api) samples: a live sample drives notifications / event commands only when
        its account is POSITIVELY verified - the usage carries an account_id, the
        cached profile carries a uuid, and they match. An unidentified, partially
        identified, or mismatched live sample is suppressed: the caller returns and
        leaves the trusted event state untouched. Non-api samples (session is already
        gated upstream) are not account-checked here.
        """
        if data.get('source') != 'api':
            return True
        prof = self.cache.profile
        profile_uuid = prof.get('account', {}).get('uuid') if isinstance(prof, dict) else None
        return bool(data.get('account_id')) and bool(profile_uuid) and data.get('account_id') == profile_uuid

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
        """Show all deferred notifications and clear the queue.

        Drained via ``popitem()`` rather than iterated: ``_notify_or_defer``
        can insert concurrently from a popup-thread ``update()``, and a dict
        mutated mid-iteration raises RuntimeError - out of the poll loop,
        killing polling for good.
        """
        while self._deferred_notifications:
            try:
                _category, (message, title) = self._deferred_notifications.popitem()
            except KeyError:
                break
            self.icon.notify(message, title)

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
                # Per-window baseline guard: run the command only on an OBSERVED
                # crossing - this window must have a verified prior value for the
                # current account (i.e. it is already in _prev_utilization, which
                # still holds the previous sample's windows here; it is reset on an
                # account switch and updated only at the end of update()). A window's
                # FIRST observed value - app startup, the first sample after an
                # account switch, or a window that only now appears in the response
                # (e.g. five_hour arriving after a Credits-only or partial switch
                # sample) - is existing state, not a crossing: it seeds the baseline
                # and shows a notification, but must not fire on_threshold_command.
                if variant_key in self._prev_utilization:
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
        # Per-account baseline guard, the extra_usage counterpart to the per-window
        # guard for the sliding-window quotas in _check_threshold_alerts: fire the
        # command only on an OBSERVED crossing - extra_usage must have a verified
        # prior value for the current account (reset on an account switch). A first
        # observation - app startup, the first sample after an account switch, or
        # extra_usage first appearing mid-session - is existing state, not a
        # crossing: it seeds the baseline and shows a notification, but must not
        # fire on_threshold_command. (The live Codex path emits credits as
        # balance/unlimited, not the monthly_limit/used_credits shape this branch
        # consumes, so this hardens the legacy/compat shape rather than a currently
        # reachable payload - kept consistent with the quota path regardless.)
        observed_before = self._prev_extra_usage_pct is not None

        if highest_exceeded > last_notified:
            title = T['notify_threshold_title']
            message = T['notify_threshold_extra_usage'].format(
                pct=f'{pct:.0f}', used=format_credits(used), limit=format_credits(limit),
            )
            self._notify_or_defer('threshold_extra_usage', message, title)
            if observed_before:
                self._run_threshold_command(
                    'extra_usage', pct, highest_exceeded, extra, title, message,
                    extra_used=format_credits(used), extra_limit=format_credits(limit),
                )
            self._notified_thresholds['extra_usage'] = highest_exceeded
        elif highest_exceeded < last_notified:
            self._notified_thresholds['extra_usage'] = highest_exceeded

        # Record this verified observation so a later crossing for the SAME account
        # is recognised as observed (reset to None on an account switch).
        self._prev_extra_usage_pct = pct

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
            # Only the legacy used/limit ratio model carries a spend amount and a
            # monthly limit; a Codex credit BALANCE has neither, so don't emit
            # ¥0/¥0 EXTRA_USED/EXTRA_LIMIT for it (matches docs/event-commands.md:
            # these are not produced for Codex credit balances).
            limit = extra.get('monthly_limit', 0) or 0
            if limit > 0:
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

        # Export every window's BEST-KNOWN utilization: the value from this response
        # if present, else the retained last-known value from _prev_utilization (this
        # response may be partial - e.g. the API omitted `secondary`). Otherwise a
        # window merely absent from this response would be exported as 0 and could
        # mislead a user command that gates on both quota values.
        def _effective_pct(field: str) -> float:
            window = data.get(field)
            if isinstance(window, dict) and window.get('utilization') is not None:
                return window.get('utilization') or 0
            return self._prev_utilization.get(field, 0) or 0

        pct_5h = _effective_pct('five_hour')
        pct_7d = _effective_pct('seven_day')
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

        Skipped on the first update (before ``_first_update_done`` is set) so that
        an already-exceeded threshold at app startup does not trigger a command.
        The caller (:meth:`_check_threshold_alerts`) additionally fires this only for
        a window with a verified prior value (an OBSERVED crossing), so a window's
        first observed value - including after an account switch or when a window
        first appears in the response - never triggers a command.  Notifications
        still fire - commands react to *events*, not *state*.
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
        """Return seconds until the earliest upcoming quota reset, or None.

        Reads ONLY the trusted retained entries (_prev_entries): they hold every
        verified window's deadline for the CURRENTLY displayed account, merged across
        partial / credits-only responses that omit a window and cleared on a verified
        account switch. The raw _last_response is NOT consulted: it is display state
        written for EVERY sample (line ~273) BEFORE the identity gate, so it can carry
        an identity-rejected cross-account sample's deadline (e.g. an in-flight A
        response seen just after the profile refreshed to B). Trusting it here would
        let that stale A deadline re-seed B's idle on_reset_command polling. A verified
        sample writes both _prev_entries and _last_response, so reading the trusted
        view alone loses no legitimate deadline while staying fail-closed.
        """
        now = datetime.now(timezone.utc)
        earliest = None
        for key, entry in self._prev_entries.items():
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

    def _has_unconfirmed_overdue(self) -> bool:
        """Return True when a retained window's reset is OVERDUE and unconfirmed.

        ``_seconds_until_next_reset()`` ignores PAST deadlines, and
        ``_idle_reset_pending`` is armed only inside the away-branch - so when a
        deadline passes while the user is still ACTIVE and they lock before the
        next poll evaluates it, neither signal exists and the idle wait would
        block with no deadline: ``on_reset_command`` would fire only when the
        user returns. This predicate closes that gap from RETAINED state alone
        (no fresh response is available at scheduling time), mirroring Phase 4's
        still-awaiting gate: the window must be command-eligible (parseable
        key), its retained value above the reset threshold (no drop observed
        yet), its retained deadline passed, and that deadline not already
        recorded in ``_reset_confirmed``.
        """
        now = datetime.now(timezone.utc)
        for key, entry in self._prev_entries.items():
            if not isinstance(entry, dict):
                continue
            resets_at = entry.get('resets_at')
            if not resets_at:
                continue
            try:
                if datetime.fromisoformat(resets_at) > now:
                    continue
            except (ValueError, TypeError):
                continue
            parsed = parse_field_name(key)
            if parsed is None:
                continue
            threshold = 95 if parsed[1] == 'hour' else 98
            if ((entry.get('utilization') or 0) > threshold
                    and not self._same_deadline(self._reset_confirmed.get(key), resets_at)):
                return True
        return False

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
        # Healthy responses only: after an error the POLL_ERROR retry must not
        # be slowed to >=POLL_FAST by alignment (error recovery and the
        # post-reset reading are both time-critical), and a rate-limit backoff
        # must never be shortened below the server's Retry-After.
        next_reset = self._seconds_until_next_reset()
        if 'error' not in data and next_reset is not None and next_reset + 5 <= interval * 1.5:
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
            while self.running:
                now_wall = time.time()
                if now_wall >= target:
                    break
                if target - now_wall > interval + 1:
                    # The wall clock stepped BACKWARDS: the remaining wait just
                    # grew beyond the intended interval and would stall polling
                    # for the size of the step. Re-anchor from now.
                    target = now_wall + interval
                    self._next_poll_time = target
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
                        elif self._idle_reset_pending or self._has_unconfirmed_overdue():
                            # _idle_reset_pending covers a deadline armed while
                            # away; _has_unconfirmed_overdue covers one that
                            # passed while the user was still ACTIVE (never
                            # armed) before they locked - both keep the retry
                            # poll alive until update() confirms the reset.
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
