"""
Internationalization
=====================

Loads translations for the detected system language with English fallback.
"""
from __future__ import annotations

import ctypes
import json
import locale
import sys
from pathlib import Path
from typing import Any

__all__ = ['LOCALE_DIR', 'detect_lang_code', 'load_translations', 'T']

LOCALE_DIR = Path(__file__).parent.parent / 'locale'

# Manual overrides for Windows legacy language names that locale.normalize()
# has no alias for (it knows 'german' but none of these), so they would all
# silently fall back to English despite shipped translations.
_WINDOWS_LANG_OVERRIDES = {
    'ukrainian': 'uk',
    'chinese (simplified)': 'zh-CN',
    'chinese (traditional)': 'zh-TW',
    'hindi': 'hi',
    'indonesian': 'id',
}


def _windows_user_locale() -> str:
    """Return the user-default locale as a BCP-47 tag (``'zh-CN'``), or ``''``.

    ``locale.getlocale()`` reports the legacy CRT name (e.g.
    ``'Chinese (Simplified)_China'``) which ``locale.normalize()`` cannot map
    for several shipped languages; ``GetUserDefaultLocaleName`` reports the
    modern tag that matches the locale filenames directly.
    """
    if sys.platform != 'win32':
        return ''
    try:
        buf = ctypes.create_unicode_buffer(85)  # LOCALE_NAME_MAX_LENGTH
        if ctypes.windll.kernel32.GetUserDefaultLocaleName(buf, len(buf)) > 0:
            return buf.value
    except Exception:
        pass
    return ''


def detect_lang_code(lang: str) -> str:
    """Detect locale file code from system locale string using convention-based lookup.

    Lookup chain: exact ``{lang}.json`` (BCP-47 tags) → ``{lang}-{REGION}.json``
    → ``{lang}.json`` → ``en.json``.  No mapping required - the locale
    directory structure *is* the configuration.

    Parameters
    ----------
    lang : str
        System locale string, e.g. ``'de_DE'``, ``'German_Germany'`` or ``'zh-CN'``.

    Returns
    -------
    str
        Locale file code (without ``.json``).
    """
    lang = lang.split('.')[0].strip()
    if not lang:
        return 'en'

    # A BCP-47 tag ('zh-CN' from GetUserDefaultLocaleName, or a regional code
    # like 'pt-BR') may name a locale file directly.
    if (LOCALE_DIR / f'{lang}.json').exists():
        return lang

    normalized = locale.normalize(lang.replace('-', '_')).split('.')[0]
    parts = normalized.split('_', 1)
    base = parts[0].lower()

    # On Windows, os.getlocale() returns e.g. 'German_Germany', and locale.normalize() fails to rewrite it to an ISO code,
    # so base becomes 'german'. Re-split using 'german' to hopefully trigger a match.
    if len(base) > 3:
        base = locale.normalize(parts[0]).split('.')[0].split('_')[0].lower()

    base = _WINDOWS_LANG_OVERRIDES.get(base, base)

    region = parts[1] if len(parts) > 1 and len(base) <= 3 else ''

    if region and (LOCALE_DIR / f'{base}-{region}.json').exists():
        return f'{base}-{region}'
    if (LOCALE_DIR / f'{base}.json').exists():
        return base

    return 'en'


def _load_file(code: str) -> dict[str, Any]:
    """Load and parse a single locale JSON file by code."""
    return json.loads((LOCALE_DIR / f'{code}.json').read_text(encoding='utf-8'))


def load_translations() -> dict[str, Any]:
    """Load translations for the chosen or detected language.

    Resolution order: the language chosen in the settings window (saved to the
    INI), then the ``language`` JSON setting, then the detected system locale.

    English is always loaded first as the base and the chosen language is
    overlaid on top, so any key missing from a translation falls back to the
    English text instead of raising ``KeyError`` at runtime.
    """
    from .settings import LANGUAGE
    from .widget_state import load_language

    code = ''
    for candidate in (load_language(), LANGUAGE):
        if candidate and (LOCALE_DIR / f'{candidate}.json').exists():
            code = candidate
            break
    if not code:
        lang = _windows_user_locale()
        if not lang:
            try:
                lang = locale.getlocale()[0] or ''
            except ValueError:
                # getlocale() raises on locale strings it cannot parse (e.g.
                # LC_CTYPE holding a BCP-47 tag like 'zh-CN'); this runs at
                # module import, so it must never propagate.
                lang = ''
        code = detect_lang_code(lang)

    try:
        translations = _load_file('en')
    except (OSError, json.JSONDecodeError):
        translations = {}
    if not isinstance(translations, dict):
        translations = {}
    if code != 'en':
        try:
            overlay = _load_file(code)
            if isinstance(overlay, dict):
                translations.update(overlay)
        except (OSError, json.JSONDecodeError):
            pass
    return translations


T: dict[str, Any] = load_translations()
