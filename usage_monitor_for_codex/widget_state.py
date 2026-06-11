"""
Widget State
============

Persists the resident widget's state to an INI file: window position,
the always-on-top preference, per-field display state (visible /
collapsed / hidden), and field order.

Unlike ``usage-monitor-settings.json`` (read-only user configuration),
this file is written by the app so the widget remembers where it was
placed and which fields the user chose to show, collapse, or hide.

Only window position and display preferences are stored here - never
credentials, account details, or usage values.
"""
from __future__ import annotations

import configparser
import sys
import threading
from pathlib import Path
from typing import NamedTuple

__all__ = [
    'FIELD_COLLAPSED', 'FIELD_HIDDEN', 'FIELD_VISIBLE', 'VALID_FIELD_STATES',
    'WidgetState', 'ini_path', 'load_language', 'load_widget_state', 'save_always_on_top',
    'save_expanded', 'save_field_config', 'save_language', 'save_window_position',
]

FIELD_VISIBLE = 'visible'
FIELD_COLLAPSED = 'collapsed'
FIELD_HIDDEN = 'hidden'
VALID_FIELD_STATES = frozenset({FIELD_VISIBLE, FIELD_COLLAPSED, FIELD_HIDDEN})

_INI_FILENAME = 'CodexUsageMonitor.ini'
_WINDOW_SECTION = 'window'
_WIDGET_SECTION = 'widget'
_FIELDS_SECTION = 'fields'

# Serializes each read-modify-write cycle so concurrent saves from different
# threads (window position, always-on-top toggle, settings window) cannot
# overwrite each other's sections.
_LOCK = threading.Lock()


class WidgetState(NamedTuple):
    """Persisted widget state loaded from the INI file.

    Attributes
    ----------
    window_x, window_y : int or None
        Last saved window position in logical pixels, or None when unset.
    field_states : dict[str, str]
        Ordered mapping of field name to display state.  Iteration order
        is the user's chosen field order.
    always_on_top : bool or None
        Last saved always-on-top preference, or None when unset (the app
        then falls back to its default).
    expanded : bool or None
        Last saved expanded-view preference, or None when unset (the app
        then starts in the compact view).
    """

    window_x: int | None
    window_y: int | None
    field_states: dict[str, str]
    always_on_top: bool | None = None
    expanded: bool | None = None


def ini_path() -> Path:
    """Return the INI file path next to the executable.

    Frozen builds use the executable's directory; running from source uses
    the project root (the package's parent). This keeps the INI alongside
    ``usage-monitor-settings.json``.
    """
    if getattr(sys, 'frozen', False):
        base = Path(sys.executable).parent
    else:
        base = Path(__file__).resolve().parent.parent
    return base / _INI_FILENAME


def _read() -> configparser.ConfigParser:
    """Return a parser populated from the INI file, or empty when absent."""
    # interpolation=None: the INI sits next to the EXE and invites hand edits;
    # with the default BasicInterpolation a stray '%' in any value raises
    # lazily at get() time - escaping every except below, up to module import
    # (load_language feeds the i18n bootstrap). Values are plain ints/bools/
    # codes, so interpolation buys nothing.
    parser = configparser.ConfigParser(interpolation=None)
    path = ini_path()
    if path.is_file():
        try:
            parser.read(path, encoding='utf-8')
        except (OSError, ValueError, configparser.Error):
            # ValueError covers UnicodeDecodeError: a file re-saved as UTF-16
            # (old Notepad "Unicode") must yield defaults, not kill startup.
            pass

    return parser


def load_widget_state() -> WidgetState:
    """Load window position and field display states from the INI file.

    Returns a ``WidgetState`` with ``None`` coordinates and an empty
    field map when the file is missing, unreadable, or incomplete.
    """
    with _LOCK:
        parser = _read()

    try:
        window_x = parser.getint(_WINDOW_SECTION, 'x', fallback=None)
        window_y = parser.getint(_WINDOW_SECTION, 'y', fallback=None)
    except ValueError:
        window_x = window_y = None

    try:
        always_on_top = parser.getboolean(_WIDGET_SECTION, 'always_on_top', fallback=None)
    except ValueError:
        always_on_top = None

    try:
        expanded = parser.getboolean(_WIDGET_SECTION, 'expanded', fallback=None)
    except ValueError:
        expanded = None

    field_states: dict[str, str] = {}
    if parser.has_section(_FIELDS_SECTION):
        for key, value in parser.items(_FIELDS_SECTION):
            if value in VALID_FIELD_STATES:
                field_states[key] = value

    return WidgetState(window_x, window_y, field_states, always_on_top, expanded)


def _write(parser: configparser.ConfigParser) -> None:
    """Write the parser to the INI file, creating the directory if needed."""
    path = ini_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('w', encoding='utf-8') as handle:
            parser.write(handle)
    except OSError:
        pass


def save_window_position(x: int, y: int) -> None:
    """Persist the widget window position, preserving other settings."""
    with _LOCK:
        parser = _read()
        if not parser.has_section(_WINDOW_SECTION):
            parser.add_section(_WINDOW_SECTION)
        parser.set(_WINDOW_SECTION, 'x', str(int(x)))
        parser.set(_WINDOW_SECTION, 'y', str(int(y)))
        _write(parser)


def save_always_on_top(value: bool) -> None:
    """Persist the always-on-top preference, preserving other settings."""
    with _LOCK:
        parser = _read()
        if not parser.has_section(_WIDGET_SECTION):
            parser.add_section(_WIDGET_SECTION)
        parser.set(_WIDGET_SECTION, 'always_on_top', 'true' if value else 'false')
        _write(parser)


def save_expanded(value: bool) -> None:
    """Persist the compact/expanded view state, preserving other settings."""
    with _LOCK:
        parser = _read()
        if not parser.has_section(_WIDGET_SECTION):
            parser.add_section(_WIDGET_SECTION)
        parser.set(_WIDGET_SECTION, 'expanded', 'true' if value else 'false')
        _write(parser)


def load_language() -> str:
    """Return the saved UI language code, or '' to follow the system/JSON setting."""
    with _LOCK:
        parser = _read()
    return parser.get(_WIDGET_SECTION, 'language', fallback='') or ''


def save_language(code: str) -> None:
    """Persist the chosen UI language code; an empty code follows the system setting."""
    with _LOCK:
        parser = _read()
        if not parser.has_section(_WIDGET_SECTION):
            parser.add_section(_WIDGET_SECTION)
        if code:
            parser.set(_WIDGET_SECTION, 'language', code)
        elif parser.has_option(_WIDGET_SECTION, 'language'):
            parser.remove_option(_WIDGET_SECTION, 'language')
        _write(parser)


def save_field_config(ordered_states: list[tuple[str, str]]) -> None:
    """Persist field display states in display order.

    The fields section is rebuilt from scratch so that removed fields
    and reordering both take effect.

    Parameters
    ----------
    ordered_states : list of (str, str)
        Field name and display state pairs in display order.  Entries
        with an invalid state are skipped.
    """
    with _LOCK:
        parser = _read()
        if parser.has_section(_FIELDS_SECTION):
            parser.remove_section(_FIELDS_SECTION)
        parser.add_section(_FIELDS_SECTION)
        for name, state in ordered_states:
            if state in VALID_FIELD_STATES:
                parser.set(_FIELDS_SECTION, name, state)
        _write(parser)
