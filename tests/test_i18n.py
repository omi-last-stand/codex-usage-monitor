"""
i18n Tests
===========

Unit tests for detect_lang_code(), load_translations(), and locale file consistency.
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from usage_monitor_for_codex.i18n import LOCALE_DIR, detect_lang_code, load_translations

MOCK_LOCALE_FILES = [
    'en.json', 'de.json', 'es.json', 'fr.json', 'hi.json', 'id.json', 'it.json',
    'ja.json', 'ko.json', 'pt-BR.json', 'uk.json', 'zh-CN.json', 'zh-TW.json',
]

NORMALIZE_MAP = {
    'de_DE': 'de_DE.ISO8859-1',
    'en_US': 'en_US.ISO8859-1',
    'pt_BR': 'pt_BR.ISO8859-1',
    'ja_JP': 'ja_JP.eucJP',
    'fr_FR': 'fr_FR.ISO8859-1',
    'zh_CN': 'zh_CN.eucCN',
    'zh_TW': 'zh_TW.big5',
    'German_Germany': 'German_Germany',
    'German': 'de_DE.ISO8859-1',
    'Spanish_Mexico': 'Spanish_Mexico',
    'Spanish': 'es_ES.ISO8859-1',
    'Ukrainian_Ukraine': 'Ukrainian_Ukraine',
    'Ukrainian': 'Ukrainian',
    '': '',
}


def _mock_normalize(locale_string):
    """Simulate locale.normalize() for cross-platform test determinism."""
    return NORMALIZE_MAP.get(locale_string, locale_string)


# ---------------------------------------------------------------------------
# detect_lang_code
# ---------------------------------------------------------------------------

@patch('usage_monitor_for_codex.i18n.locale.normalize', side_effect=_mock_normalize)
class TestDetectLangCode(unittest.TestCase):
    """Tests for detect_lang_code()."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self._locale_dir = Path(self._tmp.name)
        for name in MOCK_LOCALE_FILES:
            (self._locale_dir / name).write_text('{}')
        self._patch_dir = patch('usage_monitor_for_codex.i18n.LOCALE_DIR', self._locale_dir)
        self._patch_dir.start()

    def tearDown(self):
        self._patch_dir.stop()
        self._tmp.cleanup()

    def test_de_DE_resolves_to_base(self, _mock_norm):
        """Standard ISO locale falls back to base language file."""
        self.assertEqual(detect_lang_code('de_DE'), 'de')

    def test_en_US_resolves_to_base(self, _mock_norm):
        self.assertEqual(detect_lang_code('en_US'), 'en')

    def test_fr_FR_resolves_to_base(self, _mock_norm):
        """Regional locale without regional file falls back to base."""
        self.assertEqual(detect_lang_code('fr_FR'), 'fr')

    def test_pt_BR_regional_file_found(self, _mock_norm):
        """Regional variant with matching file returns region-specific code."""
        self.assertEqual(detect_lang_code('pt_BR'), 'pt-BR')

    def test_zh_CN_regional_file_found(self, _mock_norm):
        self.assertEqual(detect_lang_code('zh_CN'), 'zh-CN')

    def test_zh_TW_regional_file_found(self, _mock_norm):
        self.assertEqual(detect_lang_code('zh_TW'), 'zh-TW')

    def test_ja_JP_no_regional_file(self, _mock_norm):
        """Locale with region but no regional file falls back to base."""
        self.assertEqual(detect_lang_code('ja_JP'), 'ja')

    def test_german_germany_windows_name(self, _mock_norm):
        """Windows-style long locale name resolves via normalize retry."""
        self.assertEqual(detect_lang_code('German_Germany'), 'de')

    def test_spanish_mexico_windows_name(self, _mock_norm):
        """Windows-style name without regional file falls back to base."""
        self.assertEqual(detect_lang_code('Spanish_Mexico'), 'es')

    def test_ukrainian_windows_name(self, _mock_norm):
        """Windows-style name with manual override resolves correctly."""
        self.assertEqual(detect_lang_code('Ukrainian_Ukraine'), 'uk')

    def test_chinese_simplified_windows_name(self, _mock_norm):
        """The Windows legacy name has no normalize() alias; the manual
        override must map it (zh-CN users otherwise silently get English)."""
        self.assertEqual(detect_lang_code('Chinese (Simplified)_China'), 'zh-CN')

    def test_chinese_traditional_windows_name(self, _mock_norm):
        self.assertEqual(detect_lang_code('Chinese (Traditional)_Taiwan'), 'zh-TW')

    def test_hindi_windows_name(self, _mock_norm):
        self.assertEqual(detect_lang_code('Hindi_India'), 'hi')

    def test_indonesian_windows_name(self, _mock_norm):
        self.assertEqual(detect_lang_code('Indonesian_Indonesia'), 'id')

    def test_bcp47_tag_matches_locale_file_directly(self, _mock_norm):
        """A BCP-47 tag (as returned by GetUserDefaultLocaleName) that names a
        shipped locale file resolves without any normalization."""
        self.assertEqual(detect_lang_code('zh-CN'), 'zh-CN')
        self.assertEqual(detect_lang_code('pt-BR'), 'pt-BR')

    def test_bcp47_tag_without_regional_file_falls_back_to_base(self, _mock_norm):
        """'ja-JP' has no ja-JP.json; the hyphenated tag still reaches ja.json."""
        self.assertEqual(detect_lang_code('ja-JP'), 'ja')
        self.assertEqual(detect_lang_code('de-DE'), 'de')

    def test_base_code_without_region(self, _mock_norm):
        """Base language code without region resolves directly."""
        self.assertEqual(detect_lang_code('fr'), 'fr')

    def test_unknown_locale_falls_back_to_en(self, _mock_norm):
        """Completely unknown locale falls back to English."""
        self.assertEqual(detect_lang_code('xx_YY'), 'en')

    def test_unknown_windows_name_falls_back_to_en(self, _mock_norm):
        """Windows-style name where normalize retry also fails."""
        self.assertEqual(detect_lang_code('Klingon_Qonos'), 'en')

    def test_empty_string_falls_back_to_en(self, _mock_norm):
        self.assertEqual(detect_lang_code(''), 'en')


# ---------------------------------------------------------------------------
# load_translations
# ---------------------------------------------------------------------------

class TestLoadTranslations(unittest.TestCase):
    """Tests for load_translations()."""

    @patch('usage_monitor_for_codex.settings.LANGUAGE', '')
    @patch('usage_monitor_for_codex.i18n._windows_user_locale', return_value='')
    @patch('usage_monitor_for_codex.i18n.locale.normalize', side_effect=_mock_normalize)
    @patch('usage_monitor_for_codex.i18n.locale.getlocale', return_value=('de_DE', 'UTF-8'))
    def test_loads_detected_locale(self, _mock_get, _mock_norm, _mock_win):
        """Loads the JSON file matching the detected system locale."""
        with TemporaryDirectory() as tmp:
            locale_dir = Path(tmp)
            (locale_dir / 'en.json').write_text('{"title": "English"}')
            (locale_dir / 'de.json').write_text('{"title": "Deutsch"}')

            with patch('usage_monitor_for_codex.i18n.LOCALE_DIR', locale_dir), \
                 patch('usage_monitor_for_codex.widget_state.load_language', return_value=''):
                result = load_translations()

        self.assertEqual(result['title'], 'Deutsch')

    @patch('usage_monitor_for_codex.settings.LANGUAGE', '')
    @patch('usage_monitor_for_codex.i18n._windows_user_locale', return_value='')
    @patch('usage_monitor_for_codex.i18n.locale.normalize', side_effect=_mock_normalize)
    @patch('usage_monitor_for_codex.i18n.locale.getlocale', return_value=(None, None))
    def test_none_locale_falls_back_to_english(self, _mock_get, _mock_norm, _mock_win):
        """None from getlocale() falls back to English."""
        with TemporaryDirectory() as tmp:
            locale_dir = Path(tmp)
            (locale_dir / 'en.json').write_text('{"title": "English"}')

            with patch('usage_monitor_for_codex.i18n.LOCALE_DIR', locale_dir), \
                 patch('usage_monitor_for_codex.widget_state.load_language', return_value=''):
                result = load_translations()

        self.assertEqual(result['title'], 'English')

    def test_language_setting_overrides_locale(self):
        """LANGUAGE setting bypasses locale detection entirely."""
        with TemporaryDirectory() as tmp:
            locale_dir = Path(tmp)
            (locale_dir / 'en.json').write_text('{"title": "English"}')
            (locale_dir / 'ja.json').write_text('{"title": "Japanese"}')

            with patch('usage_monitor_for_codex.settings.LANGUAGE', 'ja'), \
                 patch('usage_monitor_for_codex.i18n.LOCALE_DIR', locale_dir), \
                 patch('usage_monitor_for_codex.widget_state.load_language', return_value=''):
                result = load_translations()

        self.assertEqual(result['title'], 'Japanese')

    @patch('usage_monitor_for_codex.settings.LANGUAGE', 'xx')
    @patch('usage_monitor_for_codex.i18n._windows_user_locale', return_value='')
    @patch('usage_monitor_for_codex.i18n.locale.normalize', side_effect=_mock_normalize)
    @patch('usage_monitor_for_codex.i18n.locale.getlocale', return_value=('de_DE', 'UTF-8'))
    def test_invalid_language_setting_falls_back_to_locale(self, _mock_get, _mock_norm, _mock_win):
        """Invalid LANGUAGE setting falls back to locale detection."""
        with TemporaryDirectory() as tmp:
            locale_dir = Path(tmp)
            (locale_dir / 'en.json').write_text('{"title": "English"}')
            (locale_dir / 'de.json').write_text('{"title": "Deutsch"}')

            with patch('usage_monitor_for_codex.i18n.LOCALE_DIR', locale_dir), \
                 patch('usage_monitor_for_codex.widget_state.load_language', return_value=''):
                result = load_translations()

        self.assertEqual(result['title'], 'Deutsch')


    @patch('usage_monitor_for_codex.settings.LANGUAGE', '')
    @patch('usage_monitor_for_codex.i18n._windows_user_locale', return_value='de-DE')
    @patch('usage_monitor_for_codex.i18n.locale.getlocale', side_effect=AssertionError('must not be consulted'))
    def test_windows_user_locale_takes_priority(self, _mock_get, _mock_win):
        """GetUserDefaultLocaleName (BCP-47) is the primary signal; the legacy
        CRT getlocale() name is only a fallback."""
        with TemporaryDirectory() as tmp:
            locale_dir = Path(tmp)
            (locale_dir / 'en.json').write_text('{"title": "English"}')
            (locale_dir / 'de.json').write_text('{"title": "Deutsch"}')

            with patch('usage_monitor_for_codex.i18n.LOCALE_DIR', locale_dir), \
                 patch('usage_monitor_for_codex.widget_state.load_language', return_value=''):
                result = load_translations()

        self.assertEqual(result['title'], 'Deutsch')

    @patch('usage_monitor_for_codex.settings.LANGUAGE', '')
    @patch('usage_monitor_for_codex.i18n._windows_user_locale', return_value='')
    @patch('usage_monitor_for_codex.i18n.locale.getlocale', side_effect=ValueError('unknown locale: zh-CN'))
    def test_getlocale_valueerror_falls_back_to_english(self, _mock_get, _mock_win):
        """getlocale() can raise ValueError (e.g. LC_CTYPE holding a BCP-47
        tag); this runs at module import and must never propagate."""
        with TemporaryDirectory() as tmp:
            locale_dir = Path(tmp)
            (locale_dir / 'en.json').write_text('{"title": "English"}')

            with patch('usage_monitor_for_codex.i18n.LOCALE_DIR', locale_dir), \
                 patch('usage_monitor_for_codex.widget_state.load_language', return_value=''):
                result = load_translations()

        self.assertEqual(result['title'], 'English')

    def test_widget_language_overrides_json_and_locale(self):
        """The language saved from the settings window wins over JSON and locale."""
        with TemporaryDirectory() as tmp:
            locale_dir = Path(tmp)
            (locale_dir / 'en.json').write_text('{"title": "English"}')
            (locale_dir / 'ja.json').write_text('{"title": "Japanese"}')

            with patch('usage_monitor_for_codex.settings.LANGUAGE', 'en'), \
                 patch('usage_monitor_for_codex.i18n.LOCALE_DIR', locale_dir), \
                 patch('usage_monitor_for_codex.widget_state.load_language', return_value='ja'):
                result = load_translations()

        self.assertEqual(result['title'], 'Japanese')

    def test_english_loaded_as_base(self):
        """English is always loaded first as the base before overlaying the chosen language."""
        with TemporaryDirectory() as tmp:
            locale_dir = Path(tmp)
            (locale_dir / 'en.json').write_text('{"title": "English", "only_in_en": "Base"}')
            (locale_dir / 'ja.json').write_text('{"title": "Japanese"}')

            with patch('usage_monitor_for_codex.settings.LANGUAGE', 'ja'), \
                 patch('usage_monitor_for_codex.i18n.LOCALE_DIR', locale_dir), \
                 patch('usage_monitor_for_codex.widget_state.load_language', return_value=''):
                result = load_translations()

        # Overlaid key comes from ja, missing key falls back to the English base.
        self.assertEqual(result['title'], 'Japanese')
        self.assertEqual(result['only_in_en'], 'Base')

    def test_missing_key_falls_back_to_english(self):
        """A key absent from the chosen language resolves to the English text."""
        with TemporaryDirectory() as tmp:
            locale_dir = Path(tmp)
            (locale_dir / 'en.json').write_text('{"title": "English", "quit": "Quit"}')
            (locale_dir / 'de.json').write_text('{"title": "Titel"}')  # no "quit" key

            with patch('usage_monitor_for_codex.settings.LANGUAGE', 'de'), \
                 patch('usage_monitor_for_codex.i18n.LOCALE_DIR', locale_dir), \
                 patch('usage_monitor_for_codex.widget_state.load_language', return_value=''):
                result = load_translations()

        self.assertEqual(result['title'], 'Titel')
        self.assertEqual(result['quit'], 'Quit')

    def test_corrupt_overlay_keeps_english_base(self):
        """If the chosen language file is unreadable, the English base is still returned."""
        with TemporaryDirectory() as tmp:
            locale_dir = Path(tmp)
            (locale_dir / 'en.json').write_text('{"title": "English"}')
            (locale_dir / 'de.json').write_text('{not valid json')

            with patch('usage_monitor_for_codex.settings.LANGUAGE', 'de'), \
                 patch('usage_monitor_for_codex.i18n.LOCALE_DIR', locale_dir), \
                 patch('usage_monitor_for_codex.widget_state.load_language', return_value=''):
                result = load_translations()

        self.assertEqual(result['title'], 'English')

    def test_non_dict_overlay_keeps_english_base(self):
        """A locale file that is valid JSON but not an object is ignored, keeping English."""
        with TemporaryDirectory() as tmp:
            locale_dir = Path(tmp)
            (locale_dir / 'en.json').write_text('{"title": "English"}')
            (locale_dir / 'de.json').write_text('["not", "an", "object"]')

            with patch('usage_monitor_for_codex.settings.LANGUAGE', 'de'), \
                 patch('usage_monitor_for_codex.i18n.LOCALE_DIR', locale_dir), \
                 patch('usage_monitor_for_codex.widget_state.load_language', return_value=''):
                result = load_translations()

        self.assertEqual(result['title'], 'English')


# ---------------------------------------------------------------------------
# locale file consistency
# ---------------------------------------------------------------------------

class TestLocaleConsistency(unittest.TestCase):
    """Verify all locale files are consistent with en.json (reference)."""

    @classmethod
    def setUpClass(cls):
        cls.locale_files = sorted(LOCALE_DIR.glob('*.json'))
        cls.translations = {}
        for path in cls.locale_files:
            cls.translations[path.stem] = json.loads(path.read_text(encoding='utf-8'))
        cls.reference = cls.translations['en']

    def test_at_least_two_locale_files_exist(self):
        self.assertGreaterEqual(len(self.locale_files), 2)

    def test_thirteen_locales_present(self):
        """The full set of 13 supported locales ships with the app."""
        expected = {
            'en', 'de', 'es', 'fr', 'hi', 'id', 'it',
            'ja', 'ko', 'pt-BR', 'uk', 'zh-CN', 'zh-TW',
        }
        self.assertEqual(set(self.translations.keys()), expected)

    def test_new_codex_keys_present_in_all_locales(self):
        """The Codex-introduced keys exist in every locale (key parity with en.json)."""
        for key in ('no_usage_data', 'no_session_data'):
            self.assertIn(key, self.reference, f'en.json missing key {key}')
            for lang, data in self.translations.items():
                self.assertIn(key, data, f'{lang}.json missing key {key}')

    def test_claude_code_value_is_codex(self):
        """The 'claude_code' label now reads 'CODEX' in English."""
        self.assertEqual(self.reference['claude_code'], 'CODEX')

    def test_popup_title_is_codex(self):
        """The popup title is the Codex product name in English."""
        self.assertEqual(self.reference['popup_title'], 'Codex Usage Monitor')

    def test_all_files_have_same_keys_as_english(self):
        """Every locale file must have exactly the same keys as en.json."""
        ref_keys = set(self.reference.keys())

        for lang, data in self.translations.items():
            if lang == 'en':
                continue
            lang_keys = set(data.keys())
            missing = ref_keys - lang_keys
            extra = lang_keys - ref_keys
            self.assertFalse(missing, f'{lang}.json missing keys: {missing}')
            self.assertFalse(extra, f'{lang}.json has extra keys: {extra}')

    def test_weekdays_have_seven_entries(self):
        """Every locale must have exactly 7 weekday names."""
        for lang, data in self.translations.items():
            self.assertEqual(len(data['weekdays']), 7, f'{lang}.json weekdays count != 7')

    def test_format_placeholders_match_english(self):
        """Format placeholders ({name}) in each translation must match en.json."""
        placeholder_re = re.compile(r'\{(\w+)\}')

        for key, en_value in self.reference.items():
            if not isinstance(en_value, str):
                continue
            en_placeholders = set(placeholder_re.findall(en_value))
            if not en_placeholders:
                continue

            for lang, data in self.translations.items():
                if lang == 'en':
                    continue
                lang_placeholders = set(placeholder_re.findall(data[key]))
                self.assertEqual(
                    en_placeholders, lang_placeholders,
                    f'{lang}.json key "{key}": placeholders {lang_placeholders} != expected {en_placeholders}',
                )

    def test_no_empty_translations(self):
        """No translation value should be an empty string."""
        for lang, data in self.translations.items():
            for key, value in data.items():
                if isinstance(value, str):
                    self.assertNotEqual(value, '', f'{lang}.json key "{key}" is empty')

    def test_value_types_match_english(self):
        """Value types (str, list) must match en.json for each key."""
        for lang, data in self.translations.items():
            if lang == 'en':
                continue
            for key in self.reference:
                self.assertIsInstance(
                    data[key], type(self.reference[key]),
                    f'{lang}.json key "{key}": expected {type(self.reference[key]).__name__}, '
                    f'got {type(data[key]).__name__}',
                )


if __name__ == '__main__':
    unittest.main()
