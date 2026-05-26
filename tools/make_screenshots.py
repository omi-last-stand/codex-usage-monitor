"""
Generate the README widget screenshots.

Renders the real popup.html / settings.html with representative Codex data
via Playwright (driving the installed Microsoft Edge, so no Chromium
download is needed) and writes tight PNGs to docs/images/.

Prerequisites:
  - pip install playwright   (already in the project venv)
  - a local static server for the popup dir, e.g.:
        python -m http.server 8753 -d usage_monitor_for_codex/popup

Usage:
    python tools/make_screenshots.py
"""
from __future__ import annotations

import json
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
LOCALE = ROOT / 'locale'
OUT = ROOT / 'docs' / 'images'
BASE = 'http://localhost:8753'

# Codex theme (mirrors settings.py defaults)
COLORS = {
    'bg': '#0f1838', 'bg2': '#1e1247', 'fg': '#cbc9d6', 'fg_dim': '#8a879c',
    'fg_heading': '#ffffff', 'fg_link': '#9d8cff', 'bar_bg': '#2a2740',
    'bar_fg': '#8b6cff', 'bar_fg_start': '#46a6ff', 'bar_fg_warn': '#ff5470',
    'bar_divider': '#000c', 'bar_marker': '#fffc',
}

LABELS = {
    'en': {'five_hour': 'Session (5hr)', 'seven_day': 'Weekly (7 day)'},
    'ja': {'five_hour': 'セッション（5hr）', 'seven_day': '週間（7 day）'},
}
RESET = {
    'en': {'five_hour': 'Resets in 2h 30m (14:30)', 'seven_day': 'Resets on Sat, 12:00'},
    'ja': {'five_hour': '2時間30分後にリセット (14:30)', 'seven_day': '土にリセット、12:00'},
}
SPENT = {'en': '$4.60 / $20.00 used', 'ja': '$4.60 / $20.00 使用済み'}
UPDATED = {'en': 'Updated 15s ago', 'ja': '15秒前に更新'}


def load_locale(code: str) -> dict:
    return json.loads((LOCALE / f'{code}.json').read_text(encoding='utf-8'))


def popup_t(L: dict) -> dict:
    return {
        'title': L['popup_title'], 'account': L['account'], 'email': L['email'], 'plan': L['plan'],
        'usage': L['usage'], 'extra_usage': L['extra_usage'], 'claude_code': L['claude_code'],
        'changelog': L['changelog'],
        'status_updated_s': L['status_updated_s'], 'status_updated': L['status_updated'],
        'status_next_update': L['status_next_update'], 'status_refreshing': L['status_refreshing'],
        'duration_hm': L['duration_hm'], 'duration_m': L['duration_m'], 'duration_s': L['duration_s'],
        'menu_always_on_top': L['always_on_top'], 'menu_settings': L['settings_title'],
        'menu_about': L['about_title'], 'menu_quit': L['quit'],
    }


def popup_data(lang: str) -> dict:
    return {
        'profile': {'email': 'user@example.com', 'plan': 'Plus'},
        'usage': [
            {'key': 'five_hour', 'label': LABELS[lang]['five_hour'], 'pct_text': '42%', 'fill_pct': 0.42,
             'warn': False, 'reset_text': RESET[lang]['five_hour'], 'midnights': [], 'marker_rel': 0.35},
            {'key': 'seven_day', 'label': LABELS[lang]['seven_day'], 'pct_text': '65%', 'fill_pct': 0.65,
             'warn': True, 'reset_text': RESET[lang]['seven_day'],
             'midnights': [0.143, 0.286, 0.429, 0.571, 0.714, 0.857], 'marker_rel': 0.55},
        ],
        # No CREDITS bar: a Plus/Pro account returns rate_limits.credits=null,
        # so the typical widget shows only the 5h + weekly bars (matches the demo GIF).
        'extra': None,
        'installations': [{'name': 'CLI', 'version': '0.5.0'}, {'name': 'VS Code', 'version': '0.5.0'}],
        'status': {'text': UPDATED[lang], 'is_error': False},
        'layout': [
            {'key': 'account', 'state': 'collapsed'},
            {'key': 'five_hour', 'state': 'visible'},
            {'key': 'seven_day', 'state': 'visible'},
            {'key': 'installations', 'state': 'collapsed'},
            {'key': 'status', 'state': 'collapsed'},
        ],
    }


def settings_t(L: dict) -> dict:
    return {
        'heading': L['settings_heading'], 'hint': L['settings_hint'],
        'collapse': L['settings_collapse'], 'hide': L['settings_hide'],
        'save': L['settings_save'], 'cancel': L['settings_cancel'], 'empty': L['settings_empty'],
        'language': L['settings_language'], 'language_system': L['settings_language_system'],
        'language_hint': L['settings_language_hint'],
    }


def settings_fields(lang: str, L: dict) -> list:
    return [
        {'key': 'account', 'label': L['account'], 'state': 'collapsed'},
        {'key': 'five_hour', 'label': LABELS[lang]['five_hour'], 'state': 'visible'},
        {'key': 'seven_day', 'label': LABELS[lang]['seven_day'], 'state': 'visible'},
        {'key': 'installations', 'label': L['claude_code'], 'state': 'collapsed'},
        {'key': 'status', 'label': L['status_label'], 'state': 'collapsed'},
    ]


def _shot(page, path: Path, width: int) -> None:
    page.wait_for_timeout(250)
    h = page.evaluate('Math.ceil(document.body.getBoundingClientRect().height)')
    page.set_viewport_size({'width': width, 'height': int(h)})
    page.wait_for_timeout(150)
    page.screenshot(path=str(path), clip={'x': 0, 'y': 0, 'width': width, 'height': int(h)})
    print('wrote', path.name, f'({width}x{h})')


def render_popup(page, lang: str, L: dict, *, expanded: bool, menu: bool, out: Path) -> None:
    cfg = {'colors': COLORS, 't': popup_t(L), 'app_version': '1.0.0',
           'always_on_top': True, 'expanded': expanded, 'data': popup_data(lang)}
    page.set_viewport_size({'width': 340, 'height': 900})
    page.goto(f'{BASE}/popup.html')
    page.wait_for_function("typeof init === 'function'")
    page.evaluate('cfg => init(cfg)', cfg)
    page.wait_for_timeout(450)
    if menu:
        page.evaluate("() => { const m=document.getElementById('contextMenu');"
                      " m.classList.add('open'); m.style.left='92px'; m.style.top='112px'; }")
    _shot(page, out, 340)


def render_settings(page, lang: str, L: dict, out: Path) -> None:
    # The real settings window is a fixed 400x560; render at that size so the
    # footer sits naturally at the bottom (no stretched empty middle).
    cfg = {'colors': COLORS, 't': settings_t(L), 'language': lang, 'fields': settings_fields(lang, L)}
    page.set_viewport_size({'width': 400, 'height': 560})
    page.goto(f'{BASE}/settings.html')
    page.wait_for_function("typeof initSettings === 'function'")
    page.evaluate('cfg => initSettings(cfg)', cfg)
    page.wait_for_timeout(300)
    page.screenshot(path=str(out), clip={'x': 0, 'y': 0, 'width': 400, 'height': 560})
    print('wrote', out.name, '(400x560)')


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    locales = {code: load_locale(code) for code in ('en', 'ja')}
    with sync_playwright() as p:
        browser = p.chromium.launch(channel='msedge')
        ctx = browser.new_context(device_scale_factor=2)
        page = ctx.new_page()
        for lang in ('ja', 'en'):
            sfx = '' if lang == 'ja' else '-en'
            L = locales[lang]
            render_popup(page, lang, L, expanded=False, menu=False, out=OUT / f'widget-compact{sfx}.png')
            render_popup(page, lang, L, expanded=True, menu=False, out=OUT / f'widget-expanded{sfx}.png')
            render_popup(page, lang, L, expanded=True, menu=True, out=OUT / f'widget-menu{sfx}.png')
            render_settings(page, lang, L, out=OUT / f'settings{sfx}.png')
        browser.close()
    print('done')


if __name__ == '__main__':
    main()
