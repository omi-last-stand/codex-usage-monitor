"""
Settings
=========

Centralizes all user-tunable constants.  Structural constants (API URLs,
registry keys, file paths) remain in their respective modules.

Loads an optional ``usage-monitor-settings.json`` to let users override
any constant.  Search order:

1. Next to the executable (frozen) or project root (source)
2. ``$CODEX_HOME/usage-monitor-settings.json`` (if set and different from ``~/.codex/``)
3. ``~/.codex/usage-monitor-settings.json``

The app never creates this file - users place it manually.
"""
from __future__ import annotations

import ctypes
import json
import locale as _locale
import os
import re
import sys
from pathlib import Path

__all__ = [
    'ALERT_TIME_AWARE', 'ALERT_TIME_AWARE_BELOW',
    'BAR_BG', 'BAR_FG', 'BAR_FG_START', 'BAR_FG_WARN', 'BAR_MARKER', 'BG', 'BG2',
    'CURRENCY_SYMBOL',
    'FG', 'FG_DIM', 'FG_HEADING', 'FG_LINK',
    'IDLE_PAUSE',
    'LANGUAGE', 'MAX_BACKOFF',
    'ON_RESET_COMMAND', 'ON_STARTUP_COMMAND', 'ON_THRESHOLD_COMMAND',
    'POLL_ERROR', 'POLL_FAST', 'POLL_FAST_EXTRA', 'POLL_INTERVAL',
    'POPUP_FIELDS', 'SETTINGS_FILENAME', 'TOOLTIP_FIELDS', 'USAGE_SOURCE',
    'get_alert_thresholds',
]

SETTINGS_FILENAME = 'usage-monitor-settings.json'

_NUMERIC_BOUNDS: dict[str, int] = {
    'poll_interval': 1,
    'poll_fast': 1,
    'poll_fast_extra': 1,
    'poll_error': 1,
    'max_backoff': 1,
    'idle_pause': 0,
}
_COLOR_KEYS = frozenset({'bg', 'bg2', 'fg', 'fg_dim', 'fg_heading', 'fg_link', 'bar_bg', 'bar_fg', 'bar_fg_start', 'bar_fg_warn', 'bar_divider', 'bar_marker'})
# pywebview validates create_window(background_color=...) against exactly this
# shape and RAISES on anything else - an invalid 'bg' would silently kill the
# widget/settings window in their daemon threads. The other color keys feed
# only CSS, where an invalid value (or 4/8-digit hex like the '#000c' default
# for bar_divider) degrades gracefully, so they stay free-form strings.
_STRICT_HEX_RE = re.compile(r'^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$')
_STRICT_HEX_KEYS = frozenset({'bg'})
_THRESHOLD_KEY_PREFIX = 'alert_thresholds_'
_PERCENT_KEYS = frozenset({'alert_time_aware_below'})
_STRING_KEYS = frozenset({'currency_symbol', 'language', 'usage_source'})
_COMMAND_KEYS = frozenset({'on_reset_command', 'on_startup_command', 'on_threshold_command'})
_BOOL_KEYS = frozenset({'alert_time_aware'})
_STRING_LIST_KEYS = frozenset({'tooltip_fields'})
_WILDCARD_STRING_LIST_KEYS = frozenset({'popup_fields'})


def _load_settings() -> dict:
    """Read the first ``usage-monitor-settings.json`` found, or return ``{}``."""
    if getattr(sys, 'frozen', False):
        app_dir = Path(sys.executable).parent
    else:
        app_dir = Path(__file__).resolve().parent.parent

    home_codex = Path.home() / '.codex'
    custom_config = Path(os.environ['CODEX_HOME']) if os.environ.get('CODEX_HOME') else None

    search_paths = [app_dir / SETTINGS_FILENAME]
    if custom_config and custom_config != home_codex:
        search_paths.append(custom_config / SETTINGS_FILENAME)
    search_paths.append(home_codex / SETTINGS_FILENAME)

    for path in search_paths:
        if path.is_file():
            try:
                text = path.read_text(encoding='utf-8-sig').strip()
                if not text:
                    return {}
                data = json.loads(text)
                if not isinstance(data, dict):
                    raise ValueError(f'Expected a JSON object, got {type(data).__name__}')
                return _validate(data, path)
            except (json.JSONDecodeError, ValueError) as exc:
                ctypes.windll.user32.MessageBoxW(
                    0, f'Invalid JSON in settings file:\n{path}\n\n{exc}',
                    'Codex Usage Monitor - Settings Error', 0x30,
                )
                return {}
            except OSError:
                return {}

    return {}


def _validate(data: dict, path: Path) -> dict:
    """Drop entries with invalid types or values and show a MessageBox listing errors."""
    errors: list[str] = []
    drop: list[str] = []

    for key, value in data.items():
        if key in _NUMERIC_BOUNDS:
            min_val = _NUMERIC_BOUNDS[key]
            if isinstance(value, bool) or not isinstance(value, int):
                errors.append(f'  {key}: expected an integer, got {type(value).__name__}')
                drop.append(key)
            elif value < min_val:
                errors.append(f'  {key}: must be >= {min_val}, got {value}')
                drop.append(key)

        elif key in _COLOR_KEYS:
            if not isinstance(value, str):
                errors.append(f'  {key}: expected a color string, got {type(value).__name__}')
                drop.append(key)
            elif key in _STRICT_HEX_KEYS and not _STRICT_HEX_RE.match(value):
                errors.append(f"  {key}: must be '#RGB' or '#RRGGBB' hex (it backs the native window), got {value!r}")
                drop.append(key)

        elif key.startswith(_THRESHOLD_KEY_PREFIX):
            if not isinstance(value, list):
                errors.append(f'  {key}: expected an array, got {type(value).__name__}')
                drop.append(key)
            else:
                bad = [v for v in value if isinstance(v, bool) or not isinstance(v, (int, float)) or not (1 <= v <= 100)]
                if bad:
                    errors.append(f'  {key}: all values must be numbers between 1 and 100')
                    drop.append(key)
                else:
                    data[key] = sorted(set(value))

        elif key in _PERCENT_KEYS:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                errors.append(f'  {key}: expected a number, got {type(value).__name__}')
                drop.append(key)
            elif not (1 <= value <= 100):
                errors.append(f'  {key}: must be between 1 and 100, got {value}')
                drop.append(key)

        elif key in _STRING_KEYS:
            if not isinstance(value, str):
                errors.append(f'  {key}: expected a string, got {type(value).__name__}')
                drop.append(key)

        elif key in _COMMAND_KEYS:
            if isinstance(value, str):
                data[key] = [value]
            elif isinstance(value, list):
                if any(not isinstance(item, str) for item in value):
                    errors.append(f'  {key}: all items must be strings')
                    drop.append(key)
            else:
                errors.append(f'  {key}: expected a string or array of strings, got {type(value).__name__}')
                drop.append(key)

        elif key in _BOOL_KEYS:
            if not isinstance(value, bool):
                errors.append(f'  {key}: expected true or false, got {type(value).__name__}')
                drop.append(key)

        elif key in _STRING_LIST_KEYS:
            if not isinstance(value, list):
                errors.append(f'  {key}: expected an array, got {type(value).__name__}')
                drop.append(key)
            elif any(not isinstance(item, str) or not item for item in value):
                errors.append(f'  {key}: all entries must be non-empty strings')
                drop.append(key)
            else:
                seen: set[str] = set()
                deduped: list[str] = []
                for item in value:
                    if item not in seen:
                        seen.add(item)
                        deduped.append(item)
                data[key] = deduped

        elif key in _WILDCARD_STRING_LIST_KEYS:
            if not isinstance(value, list):
                errors.append(f'  {key}: expected an array, got {type(value).__name__}')
                drop.append(key)
            elif any(not isinstance(item, str) or not item for item in value):
                errors.append(f'  {key}: all entries must be non-empty strings')
                drop.append(key)
            elif value.count('*') > 1:
                errors.append(f'  {key}: "*" may appear at most once')
                drop.append(key)
            else:
                seen_wc: set[str] = set()
                deduped_wc: list[str] = []
                for item in value:
                    if item == '*' or item not in seen_wc:
                        seen_wc.add(item)
                        deduped_wc.append(item)
                data[key] = deduped_wc

    for key in drop:
        del data[key]

    if errors:
        ctypes.windll.user32.MessageBoxW(
            0, f'Invalid values in settings file:\n{path}\n\n' + '\n'.join(errors),
            'Codex Usage Monitor - Settings Error', 0x30,
        )

    return data


_S = _load_settings()

# Polling intervals (seconds)
POLL_INTERVAL = _S.get('poll_interval', 180)
POLL_FAST = _S.get('poll_fast', 120)
POLL_FAST_EXTRA = _S.get('poll_fast_extra', 2)
POLL_ERROR = _S.get('poll_error', 30)
MAX_BACKOFF = _S.get('max_backoff', 900)
IDLE_PAUSE = _S.get('idle_pause', 300)

# Popup theme - Codex palette (blue->violet gradient background + bars).
# BG is the gradient start (also the solid native-window backing); BG2 is the
# gradient end, giving the widget the Codex blue->purple wash.
BG = _S.get('bg', '#0f1838')
BG2 = _S.get('bg2', '#1e1247')
FG = _S.get('fg', '#cbc9d6')
FG_DIM = _S.get('fg_dim', '#8a879c')
FG_HEADING = _S.get('fg_heading', '#ffffff')
FG_LINK = _S.get('fg_link', '#9d8cff')
BAR_BG = _S.get('bar_bg', '#2a2740')
# bar_fg is the gradient end + the solid accent for borders/toggles;
# bar_fg_start is the gradient start. Together they make the signature
# Codex blue->violet usage bar.
BAR_FG = _S.get('bar_fg', '#8b6cff')
BAR_FG_START = _S.get('bar_fg_start', '#46a6ff')
BAR_FG_WARN = _S.get('bar_fg_warn', '#ff5470')
BAR_DIVIDER = _S.get('bar_divider', '#000c')
BAR_MARKER = _S.get('bar_marker', '#fffc')

# Tooltip fields
TOOLTIP_FIELDS: list[str] = _S.get('tooltip_fields', ['five_hour', 'seven_day'])

# Popup fields
POPUP_FIELDS: list[str] = _S.get('popup_fields', ['*'])

# Usage data source: 'auto' (live API, then local session files),
# 'api' (live API only), or 'session' (local session files only, no network).
_usage_source = _S.get('usage_source', 'auto')
USAGE_SOURCE: str = _usage_source if _usage_source in ('auto', 'api', 'session') else 'auto'

# Alert thresholds
ALERT_TIME_AWARE: bool = _S.get('alert_time_aware', True)
ALERT_TIME_AWARE_BELOW: float = _S.get('alert_time_aware_below', 90)

# Currency

def _detect_currency_symbol() -> str:
    """Detect the system locale currency symbol for monetary formatting."""
    try:
        _locale.setlocale(_locale.LC_MONETARY, '')
        return _locale.localeconv().get('currency_symbol', '') or ''
    except _locale.Error:
        return ''


_SYSTEM_CURRENCY_SYMBOL = _detect_currency_symbol()
CURRENCY_SYMBOL: str = _S.get('currency_symbol', _SYSTEM_CURRENCY_SYMBOL)

# Language override
LANGUAGE: str = _S.get('language', '')

# Event commands
ON_RESET_COMMAND: list[str] = _S.get('on_reset_command', [])
ON_STARTUP_COMMAND: list[str] = _S.get('on_startup_command', [])
ON_THRESHOLD_COMMAND: list[str] = _S.get('on_threshold_command', [])

_ALERT_THRESHOLDS: dict[str, list[float]] = {
    'five_hour': [50, 80, 95],
    'seven_day': [95],
    'extra_usage': [50, 80, 95],
}


def get_alert_thresholds(variant_key: str) -> list[float]:
    """Return the alert thresholds for a usage variant.

    Uses a fallback chain: exact user override, built-in default for
    the exact key, user override for the base period, built-in default
    for the base period, then empty list (alerts disabled).

    Parameters
    ----------
    variant_key : str
        API variant key, e.g. ``'five_hour'``, ``'seven_day_sonnet'``,
        or ``'extra_usage'``.
    """
    exact_settings_key = f'{_THRESHOLD_KEY_PREFIX}{variant_key}'
    if exact_settings_key in _S:
        return _S[exact_settings_key]

    if variant_key in _ALERT_THRESHOLDS:
        return _ALERT_THRESHOLDS[variant_key]

    # Fallback to base period (strip variant suffix)
    parts = variant_key.split('_', 2)
    if len(parts) >= 3:
        base_key = f'{parts[0]}_{parts[1]}'
        base_settings_key = f'{_THRESHOLD_KEY_PREFIX}{base_key}'
        if base_settings_key in _S:
            return _S[base_settings_key]
        if base_key in _ALERT_THRESHOLDS:
            return _ALERT_THRESHOLDS[base_key]

    return []
