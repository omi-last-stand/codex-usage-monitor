"""
Internationalization
=====================

Loads translations for the detected system language with English fallback.
"""
from __future__ import annotations

import json
import locale
from pathlib import Path
from typing import Any

__all__ = ['LOCALE_DIR', 'detect_lang_code', 'load_translations', 'T']

LOCALE_DIR = Path(__file__).parent.parent / 'locale'


def detect_lang_code(lang: str) -> str:
    """Detect locale file code from system locale string using convention-based lookup.

    Lookup chain: ``{lang}-{REGION}.json`` → ``{lang}.json`` → ``en.json``.
    No mapping required - the locale directory structure *is* the configuration.

    Parameters
    ----------
    lang : str
        System locale string, e.g. ``'de_DE'`` or ``'German_Germany'``.

    Returns
    -------
    str
        Locale file code (without ``.json``).
    """
    normalized = locale.normalize(lang).split('.')[0]
    parts = normalized.split('_', 1)
    base = parts[0].lower()

    # On Windows, os.getlocale() returns e.g. 'German_Germany', and locale.normalize() fails to rewrite it to an ISO code,
    # so base becomes 'german'. Re-split using 'german' to hopefully trigger a match.
    if len(base) > 3:
        base = locale.normalize(parts[0]).split('.')[0].split('_')[0].lower()

    # Manual overrides for Windows locales that do not normalize cleanly to ISO codes.
    if base == 'ukrainian':
        base = 'uk'

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
        lang = locale.getlocale()[0] or ''
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
