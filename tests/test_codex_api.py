"""
Codex API Client Tests
======================

Unit tests for the Codex usage client: credential reading, the live
wham/usage API, the pure ``transform_rate_limits`` mapping, JWT profile
decoding, and the local session-file fallback.
"""
from __future__ import annotations

import base64
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import requests

from usage_monitor_for_codex.codex_api import (
    API_URL_USAGE,
    _extract_server_message,
    _parse_retry_after,
    api_headers,
    fetch_profile,
    fetch_usage,
    read_access_token,
    read_account_id,
    transform_rate_limits,
)
from usage_monitor_for_codex.i18n import LOCALE_DIR

EN = json.loads((LOCALE_DIR / 'en.json').read_text(encoding='utf-8'))

# Canonical Codex rate_limits payload (from the wham/usage endpoint).
CANONICAL_RATE_LIMITS = {
    'primary': {'used_percent': 4.0, 'window_minutes': 300, 'resets_at': 1779754106},
    'secondary': {'used_percent': 12.0, 'window_minutes': 10080, 'resets_at': 1780192198},
    'credits': None,
    'plan_type': 'plus',
}


def _iso(epoch_seconds: float) -> str:
    """ISO-8601 the same way the module formats absolute resets."""
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat()


def _make_jwt(claims: dict) -> str:
    """Build an unsigned JWT (header.payload.signature) carrying ``claims``."""
    def b64(obj: dict) -> str:
        raw = json.dumps(obj).encode('utf-8')
        return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')

    header = b64({'alg': 'none', 'typ': 'JWT'})
    payload = b64(claims)
    return f'{header}.{payload}.signature'


# ---------------------------------------------------------------------------
# Credentials (auth.json)
# ---------------------------------------------------------------------------

class TestReadAccessToken(unittest.TestCase):
    """Tests for read_access_token() reading ~/.codex/auth.json."""

    def test_file_missing(self):
        """Missing auth file returns None."""
        with TemporaryDirectory() as tmp:
            fake_path = Path(tmp) / 'nonexistent.json'
            with patch('usage_monitor_for_codex.codex_api.CODEX_AUTH', fake_path):
                self.assertIsNone(read_access_token())

    def test_valid_token(self):
        """Extracts tokens.access_token from a well-formed auth file."""
        auth = {'tokens': {'access_token': 'codex-access-123', 'account_id': 'acct-1'}}
        with TemporaryDirectory() as tmp:
            auth_file = Path(tmp) / 'auth.json'
            auth_file.write_text(json.dumps(auth), encoding='utf-8')
            with patch('usage_monitor_for_codex.codex_api.CODEX_AUTH', auth_file):
                self.assertEqual(read_access_token(), 'codex-access-123')

    def test_malformed_json(self):
        """Malformed JSON returns None."""
        with TemporaryDirectory() as tmp:
            auth_file = Path(tmp) / 'auth.json'
            auth_file.write_text('not json', encoding='utf-8')
            with patch('usage_monitor_for_codex.codex_api.CODEX_AUTH', auth_file):
                self.assertIsNone(read_access_token())

    def test_missing_tokens_key(self):
        """Missing 'tokens' key returns None."""
        with TemporaryDirectory() as tmp:
            auth_file = Path(tmp) / 'auth.json'
            auth_file.write_text('{"otherKey": {}}', encoding='utf-8')
            with patch('usage_monitor_for_codex.codex_api.CODEX_AUTH', auth_file):
                self.assertIsNone(read_access_token())

    def test_missing_access_token_key(self):
        """tokens present but no access_token returns None."""
        auth = {'tokens': {'account_id': 'acct-1'}}
        with TemporaryDirectory() as tmp:
            auth_file = Path(tmp) / 'auth.json'
            auth_file.write_text(json.dumps(auth), encoding='utf-8')
            with patch('usage_monitor_for_codex.codex_api.CODEX_AUTH', auth_file):
                self.assertIsNone(read_access_token())

    def test_empty_token_string(self):
        """Empty access_token string returns None (falsy check)."""
        auth = {'tokens': {'access_token': ''}}
        with TemporaryDirectory() as tmp:
            auth_file = Path(tmp) / 'auth.json'
            auth_file.write_text(json.dumps(auth), encoding='utf-8')
            with patch('usage_monitor_for_codex.codex_api.CODEX_AUTH', auth_file):
                self.assertIsNone(read_access_token())


class TestReadAccountId(unittest.TestCase):
    """Tests for read_account_id()."""

    def test_valid_account_id(self):
        """Extracts tokens.account_id from a well-formed auth file."""
        auth = {'tokens': {'access_token': 't', 'account_id': 'acct-xyz'}}
        with TemporaryDirectory() as tmp:
            auth_file = Path(tmp) / 'auth.json'
            auth_file.write_text(json.dumps(auth), encoding='utf-8')
            with patch('usage_monitor_for_codex.codex_api.CODEX_AUTH', auth_file):
                self.assertEqual(read_account_id(), 'acct-xyz')

    def test_missing_account_id(self):
        """Missing account_id returns None."""
        auth = {'tokens': {'access_token': 't'}}
        with TemporaryDirectory() as tmp:
            auth_file = Path(tmp) / 'auth.json'
            auth_file.write_text(json.dumps(auth), encoding='utf-8')
            with patch('usage_monitor_for_codex.codex_api.CODEX_AUTH', auth_file):
                self.assertIsNone(read_account_id())

    def test_file_missing(self):
        """Missing auth file returns None."""
        with TemporaryDirectory() as tmp:
            with patch('usage_monitor_for_codex.codex_api.CODEX_AUTH', Path(tmp) / 'nope.json'):
                self.assertIsNone(read_account_id())


class TestApiHeaders(unittest.TestCase):
    """Tests for api_headers()."""

    def _write_auth(self, tmp: str, auth: dict) -> Path:
        auth_file = Path(tmp) / 'auth.json'
        auth_file.write_text(json.dumps(auth), encoding='utf-8')
        return auth_file

    def test_no_token_returns_none(self):
        """Without an access token, api_headers() returns None."""
        with TemporaryDirectory() as tmp:
            auth_file = self._write_auth(tmp, {'tokens': {'account_id': 'acct-1'}})
            with patch('usage_monitor_for_codex.codex_api.CODEX_AUTH', auth_file):
                self.assertIsNone(api_headers())

    def test_missing_file_returns_none(self):
        """Missing auth file returns None."""
        with TemporaryDirectory() as tmp:
            with patch('usage_monitor_for_codex.codex_api.CODEX_AUTH', Path(tmp) / 'nope.json'):
                self.assertIsNone(api_headers())

    def test_authorization_bearer_header(self):
        """A valid token produces an Authorization: Bearer header."""
        with TemporaryDirectory() as tmp:
            auth_file = self._write_auth(tmp, {'tokens': {'access_token': 'tok-1', 'account_id': 'acct-1'}})
            with patch('usage_monitor_for_codex.codex_api.CODEX_AUTH', auth_file):
                headers = api_headers()
        assert headers is not None
        self.assertEqual(headers['Authorization'], 'Bearer tok-1')

    def test_account_id_header_present(self):
        """The ChatGPT-Account-Id header carries the account id when present."""
        with TemporaryDirectory() as tmp:
            auth_file = self._write_auth(tmp, {'tokens': {'access_token': 'tok-1', 'account_id': 'acct-9'}})
            with patch('usage_monitor_for_codex.codex_api.CODEX_AUTH', auth_file):
                headers = api_headers()
        assert headers is not None
        self.assertEqual(headers['ChatGPT-Account-Id'], 'acct-9')

    def test_account_id_header_omitted_when_absent(self):
        """No ChatGPT-Account-Id header when the auth file has no account id."""
        with TemporaryDirectory() as tmp:
            auth_file = self._write_auth(tmp, {'tokens': {'access_token': 'tok-1'}})
            with patch('usage_monitor_for_codex.codex_api.CODEX_AUTH', auth_file):
                headers = api_headers()
        assert headers is not None
        self.assertNotIn('ChatGPT-Account-Id', headers)


# ---------------------------------------------------------------------------
# fetch_usage - live API (usage_source='api' to isolate from session fallback)
# ---------------------------------------------------------------------------

@patch('usage_monitor_for_codex.codex_api.T', EN)
@patch('usage_monitor_for_codex.codex_api.USAGE_SOURCE', 'api')
class TestFetchUsageApi(unittest.TestCase):
    """Tests for fetch_usage() against the wham/usage endpoint."""

    @patch('usage_monitor_for_codex.codex_api.api_headers', return_value=None)
    def test_no_token_returns_error(self, _mock_headers):
        """Missing token returns the no_token error."""
        result = fetch_usage()
        self.assertEqual(result, {'error': EN['no_token']})

    @patch('usage_monitor_for_codex.codex_api.requests.get')
    @patch('usage_monitor_for_codex.codex_api.api_headers', return_value={'Authorization': 'Bearer test'})
    def test_success_transforms_payload(self, _mock_headers, mock_get):
        """A successful response is transformed into the internal model."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {'rate_limits': CANONICAL_RATE_LIMITS}
        mock_get.return_value = mock_resp

        result = fetch_usage()

        self.assertEqual(result['five_hour'], {
            'utilization': 4.0, 'resets_at': _iso(1779754106), 'window_minutes': 300,
        })
        self.assertEqual(result['seven_day'], {
            'utilization': 12.0, 'resets_at': _iso(1780192198), 'window_minutes': 10080,
        })
        self.assertEqual(result['plan_type'], 'plus')
        self.assertEqual(result['source'], 'api')
        mock_get.assert_called_once_with(API_URL_USAGE, headers={'Authorization': 'Bearer test'}, timeout=10)

    @patch('usage_monitor_for_codex.codex_api.requests.get')
    @patch('usage_monitor_for_codex.codex_api.api_headers', return_value={'Authorization': 'Bearer test'})
    def test_top_level_windows_located(self, _mock_headers, mock_get):
        """rate_limit windows at the top level (no wrapper key) are still found."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = dict(CANONICAL_RATE_LIMITS)
        mock_get.return_value = mock_resp

        result = fetch_usage()
        self.assertIn('five_hour', result)
        self.assertIn('seven_day', result)

    @patch('usage_monitor_for_codex.codex_api.requests.get')
    @patch('usage_monitor_for_codex.codex_api.api_headers', return_value={'Authorization': 'Bearer test'})
    def test_no_rate_limits_returns_no_usage_data(self, _mock_headers, mock_get):
        """A response without any rate-limit windows returns no_usage_data."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {'something_else': 1}
        mock_get.return_value = mock_resp

        result = fetch_usage()
        self.assertEqual(result, {'error': EN['no_usage_data']})

    @patch('usage_monitor_for_codex.codex_api.requests.get')
    @patch('usage_monitor_for_codex.codex_api.api_headers', return_value={'Authorization': 'Bearer test'})
    def test_connection_error(self, _mock_headers, mock_get):
        """ConnectionError returns connection_error message."""
        mock_get.side_effect = requests.ConnectionError()
        result = fetch_usage()
        self.assertEqual(result, {'error': EN['connection_error']})

    @patch('usage_monitor_for_codex.codex_api.requests.get')
    @patch('usage_monitor_for_codex.codex_api.api_headers', return_value={'Authorization': 'Bearer test'})
    def test_401_returns_auth_error(self, _mock_headers, mock_get):
        """HTTP 401 returns auth_expired with the auth_error flag."""
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.json.return_value = {}
        mock_resp.raise_for_status.side_effect = requests.HTTPError(response=mock_resp)
        mock_get.return_value = mock_resp

        result = fetch_usage()
        self.assertEqual(result['error'], EN['auth_expired'])
        self.assertTrue(result['auth_error'])

    @patch('usage_monitor_for_codex.codex_api.requests.get')
    @patch('usage_monitor_for_codex.codex_api.api_headers', return_value={'Authorization': 'Bearer test'})
    def test_429_returns_rate_limited(self, _mock_headers, mock_get):
        """HTTP 429 sets the rate_limited flag."""
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.headers = {}
        mock_resp.json.return_value = {}
        mock_resp.raise_for_status.side_effect = requests.HTTPError(response=mock_resp)
        mock_get.return_value = mock_resp

        result = fetch_usage()
        self.assertTrue(result['rate_limited'])
        self.assertEqual(result['error'], EN['http_error'].format(code=429))

    @patch('usage_monitor_for_codex.codex_api.requests.get')
    @patch('usage_monitor_for_codex.codex_api.api_headers', return_value={'Authorization': 'Bearer test'})
    def test_429_with_retry_after(self, _mock_headers, mock_get):
        """HTTP 429 with Retry-After header includes retry_after."""
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.headers = {'Retry-After': '60'}
        mock_resp.json.return_value = {}
        mock_resp.raise_for_status.side_effect = requests.HTTPError(response=mock_resp)
        mock_get.return_value = mock_resp

        result = fetch_usage()
        self.assertEqual(result['retry_after'], 60)
        self.assertTrue(result['rate_limited'])

    @patch('usage_monitor_for_codex.codex_api.requests.get')
    @patch('usage_monitor_for_codex.codex_api.api_headers', return_value={'Authorization': 'Bearer test'})
    def test_server_error_500(self, _mock_headers, mock_get):
        """HTTP 500 returns the server_error message."""
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.json.return_value = {}
        mock_resp.raise_for_status.side_effect = requests.HTTPError(response=mock_resp)
        mock_get.return_value = mock_resp

        result = fetch_usage()
        self.assertEqual(result, {'error': EN['server_error'].format(code=500)})

    @patch('usage_monitor_for_codex.codex_api.requests.get')
    @patch('usage_monitor_for_codex.codex_api.api_headers', return_value={'Authorization': 'Bearer test'})
    def test_server_error_503(self, _mock_headers, mock_get):
        """HTTP 503 returns the server_error message."""
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        mock_resp.json.return_value = {}
        mock_resp.raise_for_status.side_effect = requests.HTTPError(response=mock_resp)
        mock_get.return_value = mock_resp

        result = fetch_usage()
        self.assertEqual(result, {'error': EN['server_error'].format(code=503)})

    @patch('usage_monitor_for_codex.codex_api.requests.get')
    @patch('usage_monitor_for_codex.codex_api.api_headers', return_value={'Authorization': 'Bearer test'})
    def test_client_http_error(self, _mock_headers, mock_get):
        """Non-5xx, non-401/429 HTTP error returns http_error with the code."""
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.json.return_value = {}
        mock_resp.raise_for_status.side_effect = requests.HTTPError(response=mock_resp)
        mock_get.return_value = mock_resp

        result = fetch_usage()
        self.assertEqual(result, {'error': EN['http_error'].format(code=403)})

    @patch('usage_monitor_for_codex.codex_api.requests.get')
    @patch('usage_monitor_for_codex.codex_api.api_headers', return_value={'Authorization': 'Bearer test'})
    def test_generic_exception(self, _mock_headers, mock_get):
        """An unexpected exception returns connection_error."""
        mock_get.side_effect = RuntimeError('unexpected')
        result = fetch_usage()
        self.assertEqual(result, {'error': EN['connection_error']})

    @patch('usage_monitor_for_codex.codex_api.requests.get')
    @patch('usage_monitor_for_codex.codex_api.api_headers', return_value={'Authorization': 'Bearer test'})
    def test_only_calls_chatgpt_usage_url(self, _mock_headers, mock_get):
        """The request goes exclusively to the ChatGPT wham/usage endpoint."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {'rate_limits': CANONICAL_RATE_LIMITS}
        mock_get.return_value = mock_resp

        fetch_usage()

        called_url = mock_get.call_args[0][0]
        self.assertEqual(called_url, 'https://chatgpt.com/backend-api/wham/usage')

    @patch('usage_monitor_for_codex.codex_api.requests.get')
    @patch('usage_monitor_for_codex.codex_api.api_headers', return_value={'Authorization': 'Bearer test'})
    def test_429_with_server_message(self, _mock_headers, mock_get):
        """HTTP 429 with a JSON error body surfaces the server_message."""
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.headers = {'Retry-After': '0'}
        mock_resp.json.return_value = {'error': {'message': 'Rate limited. Please try again later.'}}
        mock_resp.raise_for_status.side_effect = requests.HTTPError(response=mock_resp)
        mock_get.return_value = mock_resp

        result = fetch_usage()
        self.assertEqual(result['server_message'], 'Rate limited.')


# ---------------------------------------------------------------------------
# transform_rate_limits (pure function)
# ---------------------------------------------------------------------------

class TestTransformRateLimits(unittest.TestCase):
    """Tests for the pure transform_rate_limits() mapping."""

    def test_canonical_shape(self):
        """The canonical Codex shape maps to five_hour / seven_day with ISO resets."""
        result = transform_rate_limits(CANONICAL_RATE_LIMITS)
        self.assertEqual(result, {
            'five_hour': {'utilization': 4.0, 'resets_at': _iso(1779754106), 'window_minutes': 300},
            'seven_day': {'utilization': 12.0, 'resets_at': _iso(1780192198), 'window_minutes': 10080},
            'plan_type': 'plus',
        })

    def test_window_keyed_by_duration(self):
        """A 300-minute primary becomes five_hour; 10080-minute secondary becomes seven_day."""
        result = transform_rate_limits(CANONICAL_RATE_LIMITS)
        self.assertIn('five_hour', result)
        self.assertIn('seven_day', result)
        self.assertNotIn('primary', result)
        self.assertNotIn('secondary', result)

    def test_one_hour_window(self):
        """A 60-minute window becomes one_hour."""
        result = transform_rate_limits({'primary': {'used_percent': 5.0, 'window_minutes': 60, 'resets_at': 1779754106}})
        self.assertIn('one_hour', result)

    def test_non_standard_window_keeps_raw_key(self):
        """A window that is not a whole number of hours/days keeps its raw slot key."""
        rate_limits = {
            'primary': {'used_percent': 7.0, 'window_minutes': 90, 'resets_at': 1779754106},
            'secondary': {'used_percent': 8.0, 'window_minutes': 100, 'resets_at': 1780192198},
        }
        result = transform_rate_limits(rate_limits)
        self.assertIn('primary', result)
        self.assertIn('secondary', result)
        self.assertEqual(result['primary']['utilization'], 7.0)
        self.assertEqual(result['secondary']['utilization'], 8.0)

    def test_missing_used_percent_skipped(self):
        """A window without used_percent is dropped from the result."""
        rate_limits = {
            'primary': {'window_minutes': 300, 'resets_at': 1779754106},
            'secondary': {'used_percent': 12.0, 'window_minutes': 10080, 'resets_at': 1780192198},
        }
        result = transform_rate_limits(rate_limits)
        self.assertNotIn('five_hour', result)
        self.assertIn('seven_day', result)

    def test_non_dict_window_skipped(self):
        """A non-dict window value is skipped without error."""
        rate_limits = {'primary': None, 'secondary': {'used_percent': 1.0, 'window_minutes': 10080, 'resets_at': 1780192198}}
        result = transform_rate_limits(rate_limits)
        self.assertNotIn('five_hour', result)
        self.assertIn('seven_day', result)

    def test_relative_resets_in_seconds(self):
        """A relative resets_in_seconds yields a future ISO timestamp."""
        before = datetime.now(timezone.utc)
        result = transform_rate_limits({'primary': {'used_percent': 3.0, 'window_minutes': 300, 'resets_in_seconds': 3600}})
        reset_at = datetime.fromisoformat(result['five_hour']['resets_at'])
        # ~1 hour out, comfortably in the future.
        self.assertGreater(reset_at, before)

    def test_relative_reset_after_seconds(self):
        """The reset_after_seconds alias is also honored."""
        before = datetime.now(timezone.utc)
        result = transform_rate_limits({'primary': {'used_percent': 3.0, 'window_minutes': 300, 'reset_after_seconds': 1800}})
        reset_at = datetime.fromisoformat(result['five_hour']['resets_at'])
        self.assertGreater(reset_at, before)

    def test_millisecond_timestamp(self):
        """A very large resets_at is treated as milliseconds, not seconds."""
        result = transform_rate_limits({'primary': {'used_percent': 4.0, 'window_minutes': 300, 'resets_at': 1779754106000}})
        # 1779754106000 ms == 1779754106 s
        self.assertEqual(result['five_hour']['resets_at'], _iso(1779754106))

    def test_no_resets_yields_empty_string(self):
        """A window with no reset information yields an empty resets_at."""
        result = transform_rate_limits({'primary': {'used_percent': 4.0, 'window_minutes': 300}})
        self.assertEqual(result['five_hour']['resets_at'], '')

    def test_plan_type_passthrough(self):
        """plan_type is copied through when present."""
        self.assertEqual(transform_rate_limits(CANONICAL_RATE_LIMITS)['plan_type'], 'plus')

    def test_no_plan_type(self):
        """Absent plan_type is simply omitted."""
        result = transform_rate_limits({'primary': {'used_percent': 4.0, 'window_minutes': 300, 'resets_at': 1779754106}})
        self.assertNotIn('plan_type', result)

    def test_empty_input(self):
        """An empty rate_limits object maps to an empty result."""
        self.assertEqual(transform_rate_limits({}), {})

    def test_string_used_percent_coerced(self):
        """A numeric-string used_percent is coerced to float."""
        result = transform_rate_limits({'primary': {'used_percent': '4.5', 'window_minutes': 300, 'resets_at': 1779754106}})
        self.assertEqual(result['five_hour']['utilization'], 4.5)

    def test_non_numeric_used_percent_skipped(self):
        """A non-numeric used_percent is skipped."""
        result = transform_rate_limits({'primary': {'used_percent': 'abc', 'window_minutes': 300, 'resets_at': 1779754106}})
        self.assertNotIn('five_hour', result)


# ---------------------------------------------------------------------------
# fetch_profile (local id_token JWT)
# ---------------------------------------------------------------------------

class TestFetchProfile(unittest.TestCase):
    """Tests for fetch_profile() decoding the local id-token JWT."""

    def _write_auth(self, tmp: str, auth: dict) -> Path:
        auth_file = Path(tmp) / 'auth.json'
        auth_file.write_text(json.dumps(auth), encoding='utf-8')
        return auth_file

    def test_decodes_email_and_plan(self):
        """Email and ChatGPT plan are decoded from the id_token claims."""
        claims = {
            'email': 'dev@example.com',
            'https://api.openai.com/auth': {'chatgpt_plan_type': 'plus'},
        }
        auth = {'tokens': {'account_id': 'acct-1', 'id_token': _make_jwt(claims)}}
        with TemporaryDirectory() as tmp:
            auth_file = self._write_auth(tmp, auth)
            with patch('usage_monitor_for_codex.codex_api.CODEX_AUTH', auth_file):
                result = fetch_profile()

        self.assertEqual(result, {
            'account': {'uuid': 'acct-1', 'email': 'dev@example.com'},
            'organization': {'organization_type': 'plus'},
        })

    def test_account_id_from_auth_claim_when_tokens_missing(self):
        """When tokens.account_id is absent, the chatgpt_account_id claim is used."""
        claims = {
            'email': 'dev@example.com',
            'https://api.openai.com/auth': {'chatgpt_plan_type': 'pro', 'chatgpt_account_id': 'acct-claim'},
        }
        auth = {'tokens': {'id_token': _make_jwt(claims)}}
        with TemporaryDirectory() as tmp:
            auth_file = self._write_auth(tmp, auth)
            with patch('usage_monitor_for_codex.codex_api.CODEX_AUTH', auth_file):
                result = fetch_profile()

        assert result is not None
        self.assertEqual(result['account']['uuid'], 'acct-claim')
        self.assertEqual(result['organization']['organization_type'], 'pro')

    def test_no_credentials_returns_none(self):
        """No usable claims and no account id returns None."""
        with TemporaryDirectory() as tmp:
            auth_file = self._write_auth(tmp, {'tokens': {}})
            with patch('usage_monitor_for_codex.codex_api.CODEX_AUTH', auth_file):
                self.assertIsNone(fetch_profile())

    def test_malformed_jwt_but_account_id_present(self):
        """A malformed id_token still yields a profile from the account id alone."""
        auth = {'tokens': {'account_id': 'acct-only', 'id_token': 'not-a-jwt'}}
        with TemporaryDirectory() as tmp:
            auth_file = self._write_auth(tmp, auth)
            with patch('usage_monitor_for_codex.codex_api.CODEX_AUTH', auth_file):
                result = fetch_profile()

        assert result is not None
        self.assertEqual(result['account']['uuid'], 'acct-only')
        self.assertEqual(result['account']['email'], '')
        self.assertEqual(result['organization']['organization_type'], '')

    def test_missing_auth_file_returns_none(self):
        """A missing auth file returns None."""
        with TemporaryDirectory() as tmp:
            with patch('usage_monitor_for_codex.codex_api.CODEX_AUTH', Path(tmp) / 'nope.json'):
                self.assertIsNone(fetch_profile())


# ---------------------------------------------------------------------------
# Session-file fallback
# ---------------------------------------------------------------------------

def _write_session_file(sessions_dir: Path, rate_limits: dict) -> Path:
    """Create a fake rollout-*.jsonl with a token_count rate_limits record."""
    day_dir = sessions_dir / '2026' / '05' / '26'
    day_dir.mkdir(parents=True)
    rollout = day_dir / 'rollout-2026-05-26T00-00-00-abc.jsonl'
    lines = [
        # A noise line that should be cheaply skipped (no rate_limits).
        json.dumps({'type': 'message', 'payload': {'role': 'user', 'content': 'hi'}}),
        # The token_count record that carries the rate_limits snapshot.
        json.dumps({
            'type': 'event_msg',
            'payload': {'type': 'token_count', 'info': {'rate_limits': rate_limits}},
        }),
    ]
    rollout.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return rollout


@patch('usage_monitor_for_codex.codex_api.T', EN)
class TestSessionFallback(unittest.TestCase):
    """Tests for the local session-file usage fallback."""

    @patch('usage_monitor_for_codex.codex_api.USAGE_SOURCE', 'session')
    def test_session_source_reads_rollout_file(self):
        """usage_source='session' reads the newest rollout snapshot, no network."""
        with TemporaryDirectory() as tmp:
            sessions = Path(tmp)
            _write_session_file(sessions, CANONICAL_RATE_LIMITS)
            with patch('usage_monitor_for_codex.codex_api.CODEX_SESSIONS_DIR', sessions):
                result = fetch_usage()

        self.assertEqual(result['source'], 'session')
        self.assertEqual(result['five_hour']['utilization'], 4.0)
        self.assertEqual(result['seven_day']['utilization'], 12.0)

    @patch('usage_monitor_for_codex.codex_api.USAGE_SOURCE', 'session')
    def test_session_source_no_files_returns_error(self):
        """usage_source='session' with no rollout files returns no_session_data."""
        with TemporaryDirectory() as tmp:
            empty = Path(tmp) / 'sessions'
            empty.mkdir()
            with patch('usage_monitor_for_codex.codex_api.CODEX_SESSIONS_DIR', empty):
                result = fetch_usage()
        self.assertEqual(result, {'error': EN['no_session_data']})

    @patch('usage_monitor_for_codex.codex_api.USAGE_SOURCE', 'session')
    def test_session_tolerates_invalid_utf8(self):
        """A rollout file mid-write (truncated multibyte) must not crash the scan."""
        with TemporaryDirectory() as tmp:
            sessions = Path(tmp)
            day = sessions / '2026' / '05' / '26'
            day.mkdir(parents=True)
            rollout = day / 'rollout-2026-05-26T00-00-00-bad.jsonl'
            valid = json.dumps({
                'type': 'event_msg',
                'payload': {'type': 'token_count', 'info': {'rate_limits': CANONICAL_RATE_LIMITS}},
            })
            # First line carries invalid/truncated UTF-8 bytes; second line is valid.
            rollout.write_bytes(b'\xff\xfe broken line\n' + valid.encode('utf-8') + b'\n')
            with patch('usage_monitor_for_codex.codex_api.CODEX_SESSIONS_DIR', sessions):
                result = fetch_usage()

        self.assertEqual(result['source'], 'session')
        self.assertEqual(result['five_hour']['utilization'], 4.0)

    @patch('usage_monitor_for_codex.codex_api.USAGE_SOURCE', 'session')
    def test_selects_by_snapshot_timestamp_not_mtime(self):
        """The newest *record timestamp* wins, even if an older snapshot's file
        has a newer mtime (touch / sync / restore must not promote stale data)."""
        import os
        import time
        with TemporaryDirectory() as tmp:
            sessions = Path(tmp)
            day = sessions / '2026' / '05' / '26'
            day.mkdir(parents=True)

            def write(name, ts, used_percent):
                rl = {'primary': {'used_percent': used_percent, 'window_minutes': 300, 'resets_at': 1779754106}}
                rec = {'timestamp': ts, 'type': 'event_msg',
                       'payload': {'type': 'token_count', 'info': {'rate_limits': rl}}}
                p = day / f'rollout-{name}.jsonl'
                p.write_text(json.dumps(rec) + '\n', encoding='utf-8')
                return p

            newer = write('A', '2026-05-26T10:00:00Z', 10)  # newer timestamp, low usage
            older = write('B', '2026-05-26T08:00:00Z', 90)  # older timestamp, high usage
            # The trap: give the OLDER-content file the NEWER mtime.
            now = time.time()
            os.utime(newer, (now - 100, now - 100))
            os.utime(older, (now, now))

            with patch('usage_monitor_for_codex.codex_api.CODEX_SESSIONS_DIR', sessions):
                result = fetch_usage()

        self.assertEqual(result['five_hour']['utilization'], 10.0)  # newer-timestamp snapshot
        self.assertEqual(result['source'], 'session')
        self.assertIn('snapshot_at', result)

    @patch('usage_monitor_for_codex.codex_api.USAGE_SOURCE', 'auto')
    @patch('usage_monitor_for_codex.codex_api.requests.get', side_effect=requests.ConnectionError())
    @patch('usage_monitor_for_codex.codex_api.api_headers', return_value={'Authorization': 'Bearer test'})
    def test_auto_falls_back_to_session_on_api_failure(self, _mock_headers, _mock_get):
        """In 'auto' mode, an API failure falls back to the local session snapshot."""
        with TemporaryDirectory() as tmp:
            sessions = Path(tmp)
            _write_session_file(sessions, CANONICAL_RATE_LIMITS)
            with patch('usage_monitor_for_codex.codex_api.CODEX_SESSIONS_DIR', sessions):
                result = fetch_usage()

        self.assertEqual(result['source'], 'session')
        self.assertEqual(result['five_hour']['utilization'], 4.0)
        # The live API error is preserved as a hint.
        self.assertEqual(result['api_error'], EN['connection_error'])

    @patch('usage_monitor_for_codex.codex_api.USAGE_SOURCE', 'auto')
    @patch('usage_monitor_for_codex.codex_api.requests.get', side_effect=requests.ConnectionError())
    @patch('usage_monitor_for_codex.codex_api.api_headers', return_value={'Authorization': 'Bearer test'})
    def test_auto_returns_api_error_when_no_session(self, _mock_headers, _mock_get):
        """In 'auto' mode with no session files, the API error is returned."""
        with TemporaryDirectory() as tmp:
            empty = Path(tmp) / 'sessions'
            empty.mkdir()
            with patch('usage_monitor_for_codex.codex_api.CODEX_SESSIONS_DIR', empty):
                result = fetch_usage()
        self.assertEqual(result, {'error': EN['connection_error']})

    @patch('usage_monitor_for_codex.codex_api.USAGE_SOURCE', 'session')
    def test_payload_level_rate_limits_record(self):
        """A record carrying rate_limits directly under payload is also parsed."""
        with TemporaryDirectory() as tmp:
            sessions = Path(tmp)
            day_dir = sessions / '2026' / '05' / '26'
            day_dir.mkdir(parents=True)
            rollout = day_dir / 'rollout-x.jsonl'
            rollout.write_text(json.dumps({'payload': {'rate_limits': CANONICAL_RATE_LIMITS}}) + '\n', encoding='utf-8')
            with patch('usage_monitor_for_codex.codex_api.CODEX_SESSIONS_DIR', sessions):
                result = fetch_usage()
        self.assertEqual(result['source'], 'session')
        self.assertEqual(result['five_hour']['utilization'], 4.0)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

class TestExtractServerMessage(unittest.TestCase):
    """Tests for _extract_server_message()."""

    def test_none_response(self):
        self.assertIsNone(_extract_server_message(None))

    def test_json_error_message(self):
        resp = MagicMock()
        resp.json.return_value = {'error': {'message': 'Something went wrong.'}}
        self.assertEqual(_extract_server_message(resp), 'Something went wrong.')

    def test_strips_retry_suffix(self):
        """Strips 'Please try again later.' since the app retries automatically."""
        resp = MagicMock()
        resp.json.return_value = {'error': {'message': 'Rate limited. Please try again later.'}}
        self.assertEqual(_extract_server_message(resp), 'Rate limited.')

    def test_error_as_string(self):
        resp = MagicMock()
        resp.json.return_value = {'error': 'plain error'}
        self.assertEqual(_extract_server_message(resp), 'plain error')

    def test_detail_fallback(self):
        resp = MagicMock()
        resp.json.return_value = {'detail': 'detailed message'}
        self.assertEqual(_extract_server_message(resp), 'detailed message')

    def test_empty_message(self):
        resp = MagicMock()
        resp.json.return_value = {'error': {'message': ''}}
        self.assertIsNone(_extract_server_message(resp))

    def test_no_error_key(self):
        resp = MagicMock()
        resp.json.return_value = {'status': 'ok'}
        self.assertIsNone(_extract_server_message(resp))

    def test_html_body(self):
        resp = MagicMock()
        resp.json.side_effect = ValueError('not JSON')
        self.assertIsNone(_extract_server_message(resp))


class TestParseRetryAfter(unittest.TestCase):
    """Tests for _parse_retry_after()."""

    def test_none_response(self):
        self.assertIsNone(_parse_retry_after(None))

    def test_valid_integer(self):
        resp = MagicMock()
        resp.headers = {'Retry-After': '120'}
        self.assertEqual(_parse_retry_after(resp), 120)

    def test_zero_value(self):
        resp = MagicMock()
        resp.headers = {'Retry-After': '0'}
        self.assertEqual(_parse_retry_after(resp), 0)

    def test_negative_clamped_to_zero(self):
        resp = MagicMock()
        resp.headers = {'Retry-After': '-5'}
        self.assertEqual(_parse_retry_after(resp), 0)

    def test_missing_header(self):
        resp = MagicMock()
        resp.headers = {}
        self.assertIsNone(_parse_retry_after(resp))

    def test_non_numeric_value(self):
        resp = MagicMock()
        resp.headers = {'Retry-After': 'Wed, 21 Oct 2026 07:28:00 GMT'}
        self.assertIsNone(_parse_retry_after(resp))


class TestCreditsTransform(unittest.TestCase):
    """transform_rate_limits maps the real Codex CreditsSnapshot to extra_usage.

    The real shape is {has_credits, unlimited, balance(string|null)} - a credit
    balance, not a used/limit ratio.
    """

    def _extra(self, credits):
        rl = {'primary': {'used_percent': 4, 'window_minutes': 300, 'resets_at': 1779754106}, 'credits': credits}
        return transform_rate_limits(rl).get('extra_usage')

    def test_null_credits_no_extra(self):
        self.assertIsNone(self._extra(None))

    def test_no_balance_no_extra(self):
        self.assertIsNone(self._extra({'has_credits': False, 'unlimited': False, 'balance': None}))

    def test_unlimited(self):
        self.assertEqual(self._extra({'has_credits': True, 'unlimited': True, 'balance': None}),
                         {'is_enabled': True, 'unlimited': True, 'balance': None})

    def test_balance_string(self):
        self.assertEqual(self._extra({'has_credits': True, 'unlimited': False, 'balance': '1250'}),
                         {'is_enabled': True, 'unlimited': False, 'balance': '1250'})

    def test_balance_number(self):
        self.assertEqual(self._extra({'has_credits': True, 'unlimited': False, 'balance': 1250}),
                         {'is_enabled': True, 'unlimited': False, 'balance': 1250})

    def test_balance_zero_is_shown(self):
        self.assertEqual(self._extra({'has_credits': True, 'unlimited': False, 'balance': 0}),
                         {'is_enabled': True, 'unlimited': False, 'balance': 0})

    def test_empty_balance_no_extra(self):
        self.assertIsNone(self._extra({'has_credits': True, 'unlimited': False, 'balance': ''}))

    def test_bool_balance_no_extra(self):
        self.assertIsNone(self._extra({'has_credits': True, 'balance': True}))

    def test_non_dict_credits_no_extra(self):
        self.assertIsNone(self._extra('nope'))

    def test_nan_balance_no_extra(self):
        """A non-finite balance string is rejected (would crash format_balance)."""
        self.assertIsNone(self._extra({'has_credits': True, 'unlimited': False, 'balance': 'NaN'}))

    def test_infinity_balance_no_extra(self):
        self.assertIsNone(self._extra({'has_credits': True, 'unlimited': False, 'balance': 'Infinity'}))

    def test_float_inf_balance_no_extra(self):
        self.assertIsNone(self._extra({'has_credits': True, 'unlimited': False, 'balance': float('inf')}))


if __name__ == '__main__':
    unittest.main()
