"""
Popup Window
=============

Dark-themed HTML popup window showing account info and usage bars.
Uses pywebview with Edge WebView2 for smooth CSS transitions and
flexible layout.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import threading
import time
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING, Any

import webview  # type: ignore[import-untyped]  # no type stubs available

from . import __version__
from .codex_cli import CHANGELOG_URL, find_installations
from .formatting import elapsed_pct, expand_popup_fields, field_period, format_balance, format_credits, midnight_positions, popup_label, time_until
from .i18n import T
from .settings import BAR_BG, BAR_DIVIDER, BAR_FG, BAR_FG_START, BAR_FG_WARN, BAR_MARKER, BG, BG2, FG, FG_DIM, FG_HEADING, FG_LINK, POPUP_FIELDS
from .task_dialog import show_info_dialog
from .widget_state import FIELD_COLLAPSED, FIELD_HIDDEN, FIELD_VISIBLE, load_language, load_widget_state, save_always_on_top, save_expanded, save_field_config, save_language, save_window_position

_POPUP_DIR = Path(__file__).parent / 'popup'
_BASELINE_DPI = 96
_GWL_EXSTYLE = -20
_WS_EX_APPWINDOW = 0x00040000
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_LAYERED = 0x00080000
_LWA_ALPHA = 0x00000002


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ('cbSize', ctypes.wintypes.DWORD),
        ('rcMonitor', ctypes.wintypes.RECT),
        ('rcWork', ctypes.wintypes.RECT),
        ('dwFlags', ctypes.wintypes.DWORD),
    ]


__all__ = ['UsagePopup']

if TYPE_CHECKING:
    from .app import UsageMonitorForCodex
    from .cache import CacheSnapshot


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def resolve_field_order(
    usage: dict[str, Any], field_states: dict[str, str], *, include_hidden: bool = False,
) -> list[tuple[str, str]]:
    """Merge the saved per-field display states with the fields the API reports.

    Returns ordered ``(key, state)`` pairs for every currently available
    field.  Saved entries set both the state and the order; fields the user
    has not configured yet default to ``visible`` and follow in the usual
    sort order.  Saved entries for fields the API no longer returns are
    dropped.

    Hidden fields are omitted for rendering (the default) but kept when
    *include_hidden* is True, which the settings window needs so the user can
    un-hide them.
    """
    available = expand_popup_fields(['*'], usage)
    available_set = set(available)
    ordered: list[tuple[str, str]] = []
    seen: set[str] = set()

    for key, state in field_states.items():
        if key in available_set and key not in seen:
            seen.add(key)
            if include_hidden or state != FIELD_HIDDEN:
                ordered.append((key, state))

    for key in available:
        if key not in seen:
            ordered.append((key, FIELD_VISIBLE))

    return ordered


# The non-usage-bar blocks, in their default order relative to the usage bars
# (which sit between 'account' and 'extra_usage').
_PSEUDO_BLOCKS = ('account', 'extra_usage', 'installations', 'status')


def _default_block_state(key: str) -> str:
    """Usage bars default to visible; account/extra/version/status blocks to collapsed."""
    return FIELD_COLLAPSED if key in _PSEUDO_BLOCKS else FIELD_VISIBLE


def resolve_block_order(
    quota_fields: list[str], pseudo_available: set[str], field_states: dict[str, str], *, include_hidden: bool = False,
) -> list[tuple[str, str]]:
    """Merge saved block states with the available display blocks into ordered (key, state).

    The popup is one flat, reorderable list of blocks: the account row, each usage
    bar, the extra-usage bar, the Codex versions, and the status line.

    *quota_fields* are the available usage-bar keys in default order.  *pseudo_available*
    is the subset of ``account`` / ``extra_usage`` / ``installations`` / ``status`` to
    include (each present only when it has something to show).  Saved entries set state
    and order; unconfigured blocks use their default state (usage bars visible, the rest
    collapsed) and follow in the default order: account, usage bars, extra usage,
    versions, status.  Hidden blocks are omitted unless *include_hidden* is True.
    """
    default_order: list[str] = []
    if 'account' in pseudo_available:
        default_order.append('account')
    default_order.extend(quota_fields)
    for key in ('extra_usage', 'installations', 'status'):
        if key in pseudo_available:
            default_order.append(key)

    available_set = set(default_order)
    ordered: list[tuple[str, str]] = []
    seen: set[str] = set()

    for key, state in field_states.items():
        if key in available_set and key not in seen:
            seen.add(key)
            if include_hidden or state != FIELD_HIDDEN:
                ordered.append((key, state))

    for key in default_order:
        if key not in seen:
            ordered.append((key, _default_block_state(key)))

    return ordered


def _block_label(key: str) -> str:
    """Human-readable label for a display block, used in the settings list."""
    labels = {
        'account': T['account'],
        'extra_usage': T['extra_usage'],
        'installations': T['claude_code'],
        'status': T['status_label'],
    }
    return labels.get(key) or popup_label(key)


def _field_config_changed(current: dict[str, str], previous: dict[str, str]) -> bool:
    """Return True if the field config differs, treating a reorder as a change.

    Dict equality ignores order, so compare the ordered items - otherwise a
    pure reorder (same keys and states) would not be detected and the widget
    would keep the old order until the next data refresh or restart.
    """
    return list(current.items()) != list(previous.items())


def _usage_entries(
    usage: dict[str, Any], field_states: dict[str, str] | None = None,
) -> list[tuple[str, str, dict[str, Any] | None, int | None, str]]:
    """Return ``(key, label, entry, period, state)`` tuples for the usage bars.

    With *field_states* (the resident widget), fields are ordered and filtered
    by the saved 3-state config and hidden fields are excluded.  Without it,
    falls back to the ``POPUP_FIELDS`` setting with every field visible.
    """
    if field_states is not None:
        ordered = resolve_field_order(usage, field_states)
    else:
        ordered = [(key, FIELD_VISIBLE) for key in expand_popup_fields(POPUP_FIELDS, usage)]
    return [(key, popup_label(key), usage.get(key), field_period(key), state) for key, state in ordered]


def _snapshot_to_dict(
    snap: CacheSnapshot, installations: list[dict[str, str]] | None = None, next_poll_time: float | None = None,
    *, field_states: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Convert a CacheSnapshot to a JSON-serializable dict for the popup JS.

    Parameters
    ----------
    snap : CacheSnapshot
        Immutable snapshot of the cache state.
    installations : list or None
        Pre-computed installation list, or None to detect now.
    next_poll_time : float or None
        Unix timestamp of the next scheduled API poll.
    field_states : dict[str, str] or None
        Saved per-field display states for the resident widget.  When given,
        usage bars are ordered and filtered accordingly (hidden fields are
        omitted) and ``layout`` orders every display block; when None, all
        available fields are shown.
    """
    # Profile - truthiness check (not `is not None`): hides the account section when the API
    # returns an empty or incomplete response, instead of rendering empty Email/Plan fields.
    #
    # On unverified local session data the whole account block is hidden: the app
    # cannot confirm the session snapshot belongs to the currently-authenticated
    # account, so showing the logged-in email beside (possibly foreign) session
    # usage bars would misattribute that usage to the email. This applies to both
    # an explicit usage_source="session" and an "auto" API-failure fallback; the
    # status line already flags local/session mode.
    #
    # Also hide it when the live usage and the cached profile provably belong to
    # DIFFERENT accounts. The usage is stamped with the account_id it was fetched
    # for and the profile carries that account's uuid; if a `codex login` (or a
    # changed ChatGPT-Account-Id) happened mid-poll they can disagree, and showing
    # one account's email beside another's usage / Credits would misattribute
    # billing info. On a mismatch, show usage bars only until the next poll
    # reconciles them.
    profile = None
    usage_data = snap.usage or {}
    source = usage_data.get('source')
    profile_uuid = snap.profile.get('account', {}).get('uuid') if snap.profile else None
    account_mismatch = (
        source == 'api'
        and bool(usage_data.get('account_id')) and bool(profile_uuid)
        and usage_data.get('account_id') != profile_uuid
    )
    hide_account = source == 'session' or account_mismatch
    if snap.profile and not hide_account:
        account = snap.profile.get('account', {})
        org = snap.profile.get('organization', {})
        # Prefer the live plan from the usage response (rate_limits.plan_type);
        # the id-token JWT's plan claim can be stale after an upgrade.
        plan = usage_data.get('plan_type') or org.get('organization_type', '')
        profile = {
            'email': account.get('email', ''),
            'plan': str(plan).replace('_', ' ').title(),
        }

    # Usage bars
    usage = []
    if snap.usage:
        for key, label, entry, period, state in _usage_entries(snap.usage, field_states):
            if not entry or entry.get('utilization') is None:
                continue
            pct = entry.get('utilization', 0) or 0
            resets_at = entry.get('resets_at', '')
            time_pct = elapsed_pct(resets_at, period) if period else None
            warn = pct >= 100 or (time_pct is not None and pct > time_pct)
            marker_rel = max(0.0, min(1.0, time_pct / 100)) if time_pct is not None else None

            usage.append({
                'key': key,
                'state': state,
                'label': label,
                'pct_text': f'{pct:.0f}%',
                'fill_pct': max(0.0, min(1.0, pct / 100)),
                'warn': warn,
                'reset_text': time_until(resets_at) if resets_at else '',
                'midnights': midnight_positions(resets_at, period) if period else [],
                'marker_rel': marker_rel,
            })

    # Extra usage / Codex credits.
    # Codex credits are a *balance* (or "unlimited"), shown as a text line.
    # The legacy used/monthly_limit ratio bar is kept as a fallback for any
    # future payload that carries a spend limit.
    extra = None
    # Credits are billing-sensitive, so only show them from a verified live (api)
    # response whose account matches the displayed profile: hidden on local
    # session-fallback data (unverified / possibly another account) and on an
    # account mismatch (same reasoning as the account block above).
    if snap.usage and not hide_account:
        extra_data = usage_data.get('extra_usage')
        if extra_data and extra_data.get('is_enabled'):
            if extra_data.get('unlimited'):
                extra = {'mode': 'text', 'text': T['credits_unlimited']}
            elif extra_data.get('balance') is not None:
                extra = {'mode': 'text', 'text': T['credits_remaining'].format(balance=format_balance(extra_data['balance']))}
            else:
                limit = extra_data.get('monthly_limit', 0) or 0
                if limit > 0:
                    used = extra_data.get('used_credits', 0) or 0
                    pct = used / limit * 100
                    extra = {
                        'mode': 'bar',
                        'pct_text': f'{pct:.0f}%',
                        'fill_pct': max(0.0, min(1.0, pct / 100)),
                        'spent_text': T['extra_usage_spent'].format(
                            used=format_credits(used), limit=format_credits(limit),
                        ),
                    }

    # Installations
    if installations is None:
        installations = [{'name': i.name, 'version': i.version} for i in find_installations()]

    # Status - pass raw timestamps for JS live timer; fallback text for initial load
    if not snap.usage:
        if snap.last_error:
            status: dict[str, Any] = {'text': snap.last_error[:120], 'is_error': True}
        else:
            status = {'text': T['status_refreshing'], 'is_error': False, 'refreshing': True}
    else:
        status = {
            'last_success_time': snap.last_success_time,
            'next_poll_time': next_poll_time,
            'refreshing': snap.refreshing,
            'error': snap.last_error[:120] if snap.last_error else None,
        }
        # Degraded mode: the data is a local session snapshot (the live API
        # was unavailable), so the "Updated Xs ago" timer would be misleading -
        # flag it so the UI shows a clear local/stale indicator instead.
        if snap.usage.get('source') == 'session':
            status['source'] = 'session'
            status['snapshot_at'] = snap.usage.get('snapshot_at')
            status['api_error'] = snap.usage.get('api_error')

    # Layout - one flat, ordered list of every shown block (account, usage bars,
    # extra usage, versions, status), driving block order and 3-state in the widget.
    quota_fields = expand_popup_fields(['*'], snap.usage) if snap.usage else []
    pseudo_available = {'status'}
    if profile:
        pseudo_available.add('account')
    if extra:
        pseudo_available.add('extra_usage')
    if installations:
        pseudo_available.add('installations')
    block_order = resolve_block_order(quota_fields, pseudo_available, field_states or {})
    layout = [{'key': key, 'state': state} for key, state in block_order]
    # Never leave the compact view empty: before the first fetch and on errors
    # there are no usage bars, so if nothing is "visible" surface the status line
    # (this also guards a hand-edited INI that hides/collapses every block).
    if layout and not any(block['state'] == FIELD_VISIBLE for block in layout):
        for block in layout:
            if block['key'] == 'status':
                block['state'] = FIELD_VISIBLE
                break

    # Degraded (local-snapshot) mode: force the status line visible so the
    # "local snapshot / live API unavailable" indicator shows even in the
    # compact view, where the status block is normally collapsed.
    if snap.usage and snap.usage.get('source') == 'session':
        for block in layout:
            if block['key'] == 'status':
                block['state'] = FIELD_VISIBLE
                break

    return {
        'profile': profile,
        'usage': usage,
        'extra': extra,
        'installations': installations,
        'status': status,
        'layout': layout,
    }


def _init_config(snap: CacheSnapshot, next_poll_time: float | None = None, *, always_on_top: bool = True, field_states: dict[str, str] | None = None, expanded: bool = False) -> dict[str, Any]:
    """Build the config object passed to JS ``init()`` after the page loads."""
    return {
        'colors': {
            'bg': BG, 'bg2': BG2, 'fg': FG, 'fg_dim': FG_DIM, 'fg_heading': FG_HEADING, 'fg_link': FG_LINK,
            'bar_bg': BAR_BG, 'bar_fg': BAR_FG, 'bar_fg_start': BAR_FG_START, 'bar_fg_warn': BAR_FG_WARN,
            'bar_divider': BAR_DIVIDER, 'bar_marker': BAR_MARKER,
        },
        't': {
            'title': T['popup_title'], 'account': T['account'], 'email': T['email'], 'plan': T['plan'],
            'usage': T['usage'], 'extra_usage': T['extra_usage'],
            'claude_code': T['claude_code'], 'changelog': T['changelog'],
            'status_updated_s': T['status_updated_s'], 'status_updated': T['status_updated'],
            'status_next_update': T['status_next_update'], 'status_refreshing': T['status_refreshing'],
            'status_local_snapshot': T['status_local_snapshot'], 'status_session_mode': T['status_session_mode'],
            'duration_hm': T['duration_hm'], 'duration_m': T['duration_m'], 'duration_s': T['duration_s'],
            'menu_always_on_top': T['always_on_top'], 'menu_settings': T['settings_title'],
            'menu_about': T['about_title'], 'menu_quit': T['quit'],
        },
        'app_version': __version__,
        'always_on_top': always_on_top,
        'expanded': expanded,
        'data': _snapshot_to_dict(snap, next_poll_time=next_poll_time, field_states=field_states),
    }


def show_about_dialog(parent_hwnd: int = 0) -> None:
    """Show the version/about dialog with clickable GitHub links, crediting upstream.

    Rendered as a Win32 task dialog (a plain MessageBox would show the URLs as
    text only).  Shared by the widget's right-click menu and the tray menu; the
    tray call passes no parent (0).
    """
    project_url = 'https://github.com/omi-last-stand/codex-usage-monitor'
    based_on_url = 'https://github.com/omi-last-stand/claude-usage-monitor'
    heading = f'Codex Usage Monitor v{__version__}'
    content = (
        f'{T["about_description"]}\n\n'
        f'<a href="{project_url}">{project_url}</a>\n\n'
        f'{T["about_acknowledgement"]}\n\n'
        f'<a href="{based_on_url}">{based_on_url}</a>'
    )
    show_info_dialog(parent_hwnd, T['about_title'], heading, content, on_link=webbrowser.open)


# ---------------------------------------------------------------------------
# JS-callable API
# ---------------------------------------------------------------------------

class _PopupApi:
    """Methods exposed to JavaScript via pywebview's JS bridge."""

    def __init__(self, popup: UsagePopup) -> None:
        self._popup = popup

    def close(self) -> None:
        self._popup._close()

    def open_url(self) -> None:
        webbrowser.open(CHANGELOG_URL)

    def quit_app(self) -> None:
        self._popup.app.on_quit()

    def toggle_always_on_top(self) -> bool:
        """Toggle always-on-top and return the new state for the menu checkmark."""
        return self._popup.toggle_always_on_top()

    def set_expanded(self, expanded: bool) -> None:
        """Persist the compact/expanded view state (called from JS on toggle)."""
        save_expanded(bool(expanded))

    def open_settings(self) -> None:
        self._popup.app.open_settings()

    def show_about(self) -> None:
        self._popup.show_about()

    def report_height(self, height: int) -> None:
        """Called by JS ResizeObserver when content height changes."""
        if height and height != self._popup._last_height:
            self._popup._last_height = height
            self._popup._resize_and_position(height)
            if not self._popup._shown:
                self._popup._show_window()


# ---------------------------------------------------------------------------
# Popup window
# ---------------------------------------------------------------------------

class UsagePopup:
    """Dark-themed HTML popup window showing account info and usage bars."""

    WIDTH = 340
    _CHECK_MS = 500  # widget refresh tick (ms): how soon saved field/position/data changes show

    def __init__(self, app: UsageMonitorForCodex) -> None:
        """Create and display the resident widget window.

        Blocks the calling thread until the window is closed.
        Requires ``webview.start()`` to be running on the main thread.

        Parameters
        ----------
        app : UsageMonitorForCodex
            Parent application providing ``cache`` for data access.
        """
        self.app = app
        self._always_on_top = True
        self._running = True
        self._closed = threading.Event()
        self._popup_hwnd = 0
        self._hook_thread_id = 0
        initial_height = 400
        self._last_height = initial_height
        snap = app.cache.snapshot
        self._last_version = snap.version

        # Restore the last saved window position (logical pixels), the
        # always-on-top state, and the per-field display config from the INI.
        self._saved_pos: tuple[int, int] | None = None
        self._positioned = False
        state = load_widget_state()
        if state.window_x is not None and state.window_y is not None:
            self._saved_pos = (state.window_x, state.window_y)
        if state.always_on_top is not None:
            self._always_on_top = state.always_on_top
        self._field_states: dict[str, str] = state.field_states
        self._start_expanded = bool(state.expanded)

        api = _PopupApi(self)

        self._window = webview.create_window(
            '', url=str(_POPUP_DIR / 'popup.html'),
            width=self.WIDTH, height=initial_height,
            resizable=False, frameless=True, shadow=False,
            easy_drag=True,
            on_top=self._always_on_top, hidden=True,
            background_color=BG,
            js_api=api,
        )
        self._shown = False
        self._window.events.loaded += self._on_loaded
        self._window.events.closed += self._on_window_closed
        threading.Thread(target=self._menu_dismiss_watch, daemon=True).start()
        self._closed.wait()

    def _on_loaded(self) -> None:
        """Inject config and show the window transparently for layout."""
        config = _init_config(self.app.cache.snapshot, next_poll_time=self.app._next_poll_time, always_on_top=self._always_on_top, field_states=self._field_states, expanded=self._start_expanded)
        self._window.evaluate_js(f'init({json.dumps(config)})')

        self._popup_hwnd = self._window.native.Handle.ToInt32()

        # Hide the taskbar icon and enable layered mode for opacity control.
        # WinForms sets WS_EX_APPWINDOW by default, which forces a taskbar
        # button even when WS_EX_TOOLWINDOW is present - both must be fixed.
        # WS_EX_LAYERED is needed for SetLayeredWindowAttributes (opacity).
        ex_style = ctypes.windll.user32.GetWindowLongW(self._popup_hwnd, _GWL_EXSTYLE)
        ctypes.windll.user32.SetWindowLongW(
            self._popup_hwnd, _GWL_EXSTYLE,
            (ex_style | _WS_EX_TOOLWINDOW | _WS_EX_LAYERED) & ~_WS_EX_APPWINDOW,
        )

        # Show fully transparent so JS can layout and report the real height
        ctypes.windll.user32.SetLayeredWindowAttributes(self._popup_hwnd, 0, 0, _LWA_ALPHA)
        self._window.show()

    def _show_window(self) -> None:
        """Make the popup visible after the first resize positioned it correctly."""
        # Remove the layered style to restore normal rendering
        ex_style = ctypes.windll.user32.GetWindowLongW(self._popup_hwnd, _GWL_EXSTYLE)
        ctypes.windll.user32.SetWindowLongW(self._popup_hwnd, _GWL_EXSTYLE, ex_style & ~_WS_EX_LAYERED)
        self._shown = True
        threading.Thread(target=self._update_loop, daemon=True).start()

    def _close_menu(self) -> None:
        """Ask the page to close the right-click context menu (no-op if closed)."""
        try:
            self._window.evaluate_js('closeContextMenu()')
        except Exception:
            pass

    def _menu_dismiss_watch(self) -> None:
        """Close the context menu when the user clicks outside the widget.

        Widget mode only.  A low-level mouse hook watches for clicks outside
        the window bounds and dismisses the right-click menu.  The widget is
        never closed or collapsed.  The dismiss runs on a worker thread so the
        hook callback stays fast.

        The thread blocks in ``GetMessageW`` to pump the low-level hook; closing
        the widget posts ``WM_QUIT`` to this thread (``_wake_hook_thread``) so the
        loop ends and ``UnhookWindowsHookEx`` runs - otherwise reopening the
        widget would leak a global hook on every open/close cycle.
        """
        self._hook_thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        _call_next = ctypes.windll.user32.CallNextHookEx
        _call_next.argtypes = [ctypes.wintypes.HANDLE, ctypes.c_int, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM]
        _call_next.restype = ctypes.c_long

        class MSLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [('pt', ctypes.wintypes.POINT), ('mouseData', ctypes.wintypes.DWORD),
                         ('flags', ctypes.wintypes.DWORD), ('time', ctypes.wintypes.DWORD),
                         ('dwExtraInfo', ctypes.POINTER(ctypes.c_ulong))]

        @ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_int, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM)
        def mouse_proc(code, wparam, lparam):
            if code >= 0 and wparam == 0x0201:  # WM_LBUTTONDOWN
                popup_hwnd = self._popup_hwnd
                if popup_hwnd and self._shown:
                    rect = ctypes.wintypes.RECT()
                    ctypes.windll.user32.GetWindowRect(popup_hwnd, ctypes.byref(rect))
                    info = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                    if not (rect.left <= info.pt.x <= rect.right and rect.top <= info.pt.y <= rect.bottom):
                        threading.Thread(target=self._close_menu, daemon=True).start()
            return _call_next(None, code, wparam, lparam)

        mouse_hook = ctypes.windll.user32.SetWindowsHookExW(14, mouse_proc, None, 0)  # WH_MOUSE_LL

        try:
            msg = ctypes.wintypes.MSG()
            while self._running and ctypes.windll.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                pass
        finally:
            ctypes.windll.user32.UnhookWindowsHookEx(mouse_hook)
            # Cleared so a late wake from the other close path can't post to this
            # (now-terminated, possibly-recycled) thread id.
            self._hook_thread_id = 0

    def _wake_hook_thread(self) -> None:
        """Wake the mouse-hook thread (blocked in GetMessageW) so it unhooks and exits.

        Single-shot: the id is claimed and cleared in one step so a second close
        path can't post ``WM_QUIT`` to a stale id after the thread exited (Windows
        may recycle thread ids). The post only ever targets the still-live hook
        thread - it cannot exit until it receives this very ``WM_QUIT``.
        """
        tid, self._hook_thread_id = self._hook_thread_id, 0
        if tid:
            # WM_QUIT (0x0012) makes the thread's GetMessageW return 0, ending the
            # hook loop so UnhookWindowsHookEx runs in the finally.
            ctypes.windll.user32.PostThreadMessageW(tid, 0x0012, 0, 0)

    def _on_window_closed(self) -> None:
        self._running = False
        self._wake_hook_thread()
        self._closed.set()

    def _close(self) -> None:
        self._running = False
        self._wake_hook_thread()
        try:
            self._window.destroy()
        except Exception:
            pass
        self._closed.set()

    def toggle_always_on_top(self) -> bool:
        """Toggle the window's always-on-top state. Returns the new state."""
        self._always_on_top = not self._always_on_top
        HWND_TOPMOST = -1
        HWND_NOTOPMOST = -2
        SWP_NOMOVE = 0x0002
        SWP_NOSIZE = 0x0001
        insert_after = HWND_TOPMOST if self._always_on_top else HWND_NOTOPMOST
        ctypes.windll.user32.SetWindowPos(
            ctypes.wintypes.HWND(self._popup_hwnd), ctypes.wintypes.HWND(insert_after),
            0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE,
        )
        save_always_on_top(self._always_on_top)
        return self._always_on_top

    def show_about(self) -> None:
        """Show the version/about dialog, parented to the widget window."""
        show_about_dialog(self._popup_hwnd)

    def _update_loop(self) -> None:
        """Poll for data changes and push updates to the popup."""
        cached_installations = [{'name': i.name, 'version': i.version} for i in find_installations()]
        last_next_poll_time = self.app._next_poll_time
        while self._running:
            time.sleep(self._CHECK_MS / 1000)
            if not self._running:
                break
            self._save_position_if_moved()
            try:
                snap = self.app.cache.snapshot
                next_poll_time = self.app._next_poll_time
                field_states = load_widget_state().field_states
                if (snap.version == self._last_version and next_poll_time == last_next_poll_time
                        and not _field_config_changed(field_states, self._field_states)):
                    continue
                if snap.version != self._last_version:
                    self._last_version = snap.version
                    cached_installations = [{'name': i.name, 'version': i.version} for i in find_installations()]
                last_next_poll_time = next_poll_time
                self._field_states = field_states
                data = _snapshot_to_dict(snap, installations=cached_installations, next_poll_time=next_poll_time, field_states=field_states)
                self._window.evaluate_js(f'updateData({json.dumps(data)})')
            except Exception:
                break

    def _center_position(self, physical_width: int, physical_height: int) -> tuple[int, int]:
        """Calculate a centered position on the monitor that owns the taskbar.

        Used as the first-run default (no saved position) so the widget
        appears clearly in the middle of the screen instead of tucked
        behind the taskbar.

        Parameters
        ----------
        physical_width : int
            Window width in physical pixels.
        physical_height : int
            Window height in physical pixels.

        Returns
        -------
        tuple[int, int]
            Logical (x, y) coordinates.
        """
        tray_hwnd = ctypes.windll.user32.FindWindowW('Shell_TrayWnd', None)
        hmon = ctypes.windll.user32.MonitorFromWindow(tray_hwnd, 2)  # MONITOR_DEFAULTTONEAREST

        mon_info = _MONITORINFO()
        mon_info.cbSize = ctypes.sizeof(_MONITORINFO)
        ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(mon_info))
        work = mon_info.rcWork

        dpi = ctypes.windll.user32.GetDpiForWindow(self._popup_hwnd) or ctypes.windll.user32.GetDpiForSystem()
        scale = dpi / _BASELINE_DPI

        x = work.left + (work.right - work.left - physical_width) // 2
        y = work.top + (work.bottom - work.top - physical_height) // 2

        return int(x / scale), int(y / scale)

    def _is_position_visible(self, x_logical: int, y_logical: int) -> bool:
        """Return True if the logical top-left point lies on some monitor.

        Validates a restored window position so a spot that is now off-screen
        (negative coordinates, a disconnected monitor, or a resolution change)
        falls back to the centered default instead of vanishing.
        """
        dpi = ctypes.windll.user32.GetDpiForWindow(self._popup_hwnd) or ctypes.windll.user32.GetDpiForSystem()
        scale = dpi / _BASELINE_DPI
        point = ctypes.wintypes.POINT(int(x_logical * scale), int(y_logical * scale))
        # MONITOR_DEFAULTTONULL = 0 -> returns NULL when the point is off every monitor
        return bool(ctypes.windll.user32.MonitorFromPoint(point, 0))

    def _resize_and_position(self, height: int) -> None:
        """Resize the window and place it at its saved (or centered) position.

        The first call happens while the window is still transparent
        (opacity 0), so separate resize/move calls cause no visible jump.

        pywebview 6.x ``resize()`` applies DPI scaling internally (consistent
        with ``move()``), so both expect logical pixels.  Physical dimensions
        are still computed for ``_center_position``, which needs them to
        calculate the correct logical position against the physical work-area
        coordinates returned by Win32.
        """
        dpi = ctypes.windll.user32.GetDpiForWindow(self._popup_hwnd) or ctypes.windll.user32.GetDpiForSystem()
        scale = dpi / _BASELINE_DPI
        physical_width = int(self.WIDTH * scale)
        physical_height = int(height * scale)
        self._window.resize(self.WIDTH, height)

        # Position the window only on the first layout. After that the user
        # owns the position (drag), so height changes (expand/collapse, data
        # refresh) must not snap it back.
        if self._positioned:
            return

        if self._saved_pos is not None and self._is_position_visible(*self._saved_pos):
            x, y = self._saved_pos
        else:
            x, y = self._center_position(physical_width, physical_height)
        self._window.move(x, y)
        self._positioned = True
        self._saved_pos = (x, y)
        save_window_position(x, y)

    def _save_position_if_moved(self) -> None:
        """Persist the window's top-left position after the user drags it.

        Compares the current position (converted to logical pixels) against
        the last saved position and writes to the INI file only when it
        changed by more than a couple of pixels, to ride out DPI rounding.
        """
        if not self._popup_hwnd:
            return

        rect = ctypes.wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(self._popup_hwnd, ctypes.byref(rect))
        dpi = ctypes.windll.user32.GetDpiForWindow(self._popup_hwnd) or ctypes.windll.user32.GetDpiForSystem()
        scale = dpi / _BASELINE_DPI
        x = int(rect.left / scale)
        y = int(rect.top / scale)

        # Ignore positions that are off every monitor (e.g. the brief
        # off-screen state at startup before the window is placed); saving
        # them would hide the widget next launch. Negative coordinates on a
        # monitor left of or above the primary are valid and do persist.
        if not self._is_position_visible(x, y):
            return

        if self._saved_pos is not None and abs(self._saved_pos[0] - x) <= 2 and abs(self._saved_pos[1] - y) <= 2:
            return

        self._saved_pos = (x, y)
        save_window_position(x, y)


# ---------------------------------------------------------------------------
# Settings window
# ---------------------------------------------------------------------------

class _SettingsApi:
    """Methods exposed to the settings window's JavaScript."""

    def __init__(self, window: SettingsWindow) -> None:
        self._window = window

    def save(self, payload: dict[str, Any]) -> None:
        """Persist the chosen field order/states and language, then close."""
        self._window.apply(payload)

    def cancel(self) -> None:
        """Close the window without saving."""
        self._window.close()


class SettingsWindow:
    """A separate window for choosing which usage fields to show, and their order.

    Decoupled from the widget: saving writes the field config to the INI and
    the running widget picks the change up on its next refresh, so no direct
    window-to-window reference is needed.
    """

    WIDTH = 400
    HEIGHT = 560

    def __init__(self, app: UsageMonitorForCodex) -> None:
        self.app = app
        self._window = webview.create_window(
            T['settings_title'],
            url=str(_POPUP_DIR / 'settings.html'),
            width=self.WIDTH, height=self.HEIGHT,
            resizable=True, on_top=True,
            background_color=BG,
            js_api=_SettingsApi(self),
        )
        self._window.events.loaded += self._on_loaded
        self._window.events.closed += self._on_closed

    def _on_loaded(self) -> None:
        """Inject theme colors and the current field list after the page loads."""
        config = {
            'colors': {
                'bg': BG, 'fg': FG, 'fg_dim': FG_DIM, 'fg_heading': FG_HEADING,
                'fg_link': FG_LINK, 'bar_bg': BAR_BG, 'bar_fg': BAR_FG,
            },
            't': {
                'heading': T['settings_heading'], 'hint': T['settings_hint'],
                'collapse': T['settings_collapse'], 'hide': T['settings_hide'],
                'save': T['settings_save'], 'cancel': T['settings_cancel'],
                'empty': T['settings_empty'],
                'language': T['settings_language'], 'language_system': T['settings_language_system'],
                'language_hint': T['settings_language_hint'],
            },
            'language': load_language(),
            'fields': self._current_fields(),
        }
        self._window.evaluate_js(f'initSettings({json.dumps(config)})')

    def _current_fields(self) -> list[dict[str, str]]:
        """Every available display block (account, usage bars, extra, versions, status) with its saved order and state."""
        usage = self.app.cache.snapshot.usage or {}
        saved = load_widget_state().field_states
        quota_fields = expand_popup_fields(['*'], usage)
        pseudo_available = {'account', 'installations', 'status'}
        extra = usage.get('extra_usage')
        if extra and extra.get('is_enabled'):
            pseudo_available.add('extra_usage')
        ordered = resolve_block_order(quota_fields, pseudo_available, saved, include_hidden=True)
        return [{'key': key, 'label': _block_label(key), 'state': state} for key, state in ordered]

    def apply(self, payload: dict[str, Any]) -> None:
        """Persist the chosen field config and language (called from JS).

        Field changes are picked up live by the running widget, so the window
        just closes.  The language is only read at startup, so when it changes
        the app restarts automatically to apply the new language.
        """
        fields = payload.get('fields') or []
        pairs = [
            (item['key'], item['state'])
            for item in fields
            if isinstance(item, dict) and item.get('key') and item.get('state')
        ]
        save_field_config(pairs)
        new_language = payload.get('language') or ''
        language_changed = new_language != load_language()
        save_language(new_language)
        self.close()
        if language_changed:
            self.app.on_restart()

    def close(self) -> None:
        """Destroy the settings window (idempotent)."""
        try:
            self._window.destroy()
        except Exception:
            pass

    def _on_closed(self) -> None:
        """Clear the app's open-settings guard so it can be reopened."""
        self.app._settings_open = False
