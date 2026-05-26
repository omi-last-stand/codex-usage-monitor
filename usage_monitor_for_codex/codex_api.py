"""
Codex API Client
================

Reads OpenAI Codex CLI OAuth credentials (``~/.codex/auth.json``) and
fetches ChatGPT-plan rate-limit usage for Codex.  This is the only module
that handles credentials.

Two data sources, tried in order (configurable via ``usage_source``):

1. **Live API** - ``GET https://chatgpt.com/backend-api/wham/usage``, the
   same endpoint the Codex CLI itself polls for its ``/status`` rate
   limits.  Requires a valid access token + account id.
2. **Local session files** - the newest session rollout file's
   ``rate_limits`` snapshot
   (``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``).  Needs no network
   and no token; only as fresh as your last Codex turn.

Network communication is exclusively with ``chatgpt.com``.  Credentials
are used only in HTTP ``Authorization`` / ``ChatGPT-Account-Id`` headers;
they are never logged or written to disk.

The Codex rate-limit shape::

    {
      "primary":   {"used_percent": 4.0,  "window_minutes": 300,   "resets_at": 1779754106},
      "secondary": {"used_percent": 12.0, "window_minutes": 10080, "resets_at": 1780192198},
      "credits": null,
      "plan_type": "plus"
    }

is translated into the same internal model the rest of the app already
understands (``five_hour`` / ``seven_day`` quota entries with
``utilization`` + ISO-8601 ``resets_at``), so notifications, thresholds,
period bars and labels all work unchanged.
"""
from __future__ import annotations

import base64
import binascii
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from .i18n import T
from .settings import USAGE_SOURCE

__all__ = [
    'API_URL_USAGE', 'CODEX_CONFIG_DIR', 'CODEX_AUTH', 'CODEX_SESSIONS_DIR',
    'read_access_token', 'read_account_id', 'api_headers',
    'fetch_usage', 'fetch_profile', 'transform_rate_limits',
]

# API endpoint & credentials
API_URL_USAGE = 'https://chatgpt.com/backend-api/wham/usage'
CODEX_CONFIG_DIR = Path(os.environ['CODEX_HOME']) if os.environ.get('CODEX_HOME') else Path.home() / '.codex'
CODEX_AUTH = CODEX_CONFIG_DIR / 'auth.json'
CODEX_SESSIONS_DIR = CODEX_CONFIG_DIR / 'sessions'

_FALLBACK_USER_AGENT = 'codex_cli_rs/0.0.0'

# Reverse of formatting._NUMBER_WORDS: build parseable field keys like
# "five_hour" / "seven_day" from a window length so the existing field
# machinery (labels, periods, thresholds) applies without special-casing.
_NUMBER_WORDS = {
    1: 'one', 2: 'two', 3: 'three', 4: 'four', 5: 'five', 6: 'six',
    7: 'seven', 8: 'eight', 9: 'nine', 10: 'ten', 11: 'eleven', 12: 'twelve',
}

# Treat any reset timestamp above this as milliseconds rather than seconds.
# ~ year 2286 in seconds; real epochs (~1.8e9) stay well below it.
_MS_THRESHOLD = 1e12


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def _read_auth() -> dict[str, Any]:
    """Return the parsed ``auth.json`` mapping, or ``{}`` on any failure."""
    if not CODEX_AUTH.exists():
        return {}
    try:
        data = json.loads(CODEX_AUTH.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError, ValueError):
        return {}


def _read_tokens() -> dict[str, Any]:
    """Return the ``tokens`` mapping from ``auth.json``, or ``{}``.

    Guards against a malformed (but JSON-valid) file where ``tokens`` is
    ``null``, a list, or any non-dict value - the file is external state
    that Codex may truncate or rewrite at any moment.
    """
    tokens = _read_auth().get('tokens')
    return tokens if isinstance(tokens, dict) else {}


def read_access_token() -> str | None:
    """Read the current access token from the Codex credentials file."""
    return _read_tokens().get('access_token') or None


def read_account_id() -> str | None:
    """Read the ChatGPT account id from the Codex credentials file."""
    return _read_tokens().get('account_id') or None


def api_headers() -> dict[str, str] | None:
    """Return auth headers for the Codex usage API, or ``None`` if no token."""
    tokens = _read_tokens()
    token = tokens.get('access_token')
    if not token:
        return None

    headers = {
        'Authorization': f'Bearer {token}',
        'Accept': 'application/json',
        'User-Agent': _user_agent(),
        'originator': 'codex_cli_rs',
        'Origin': 'https://chatgpt.com',
        'Referer': 'https://chatgpt.com/',
    }
    account_id = tokens.get('account_id')
    if account_id:
        headers['ChatGPT-Account-Id'] = account_id
    return headers


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------

def fetch_usage() -> dict[str, Any]:
    """Fetch usage data, preferring the live API then local session files.

    Honors the ``usage_source`` setting: ``'auto'`` (default) tries the
    API then falls back to session files; ``'api'`` uses only the API;
    ``'session'`` uses only local session files (no network).

    Returns the internal usage model (``five_hour`` / ``seven_day`` quota
    entries) on success, or ``{'error': ...}`` on failure.  Data sourced
    from session files carries ``'source': 'session'``.
    """
    if USAGE_SOURCE == 'session':
        return _fetch_usage_session() or {'error': T['no_session_data']}

    api_result = _fetch_usage_api()
    if 'error' not in api_result:
        return api_result

    if USAGE_SOURCE == 'api':
        return api_result

    # Rate-limited (429): honor the backoff rather than masking it with a
    # stale local snapshot (which would clear the cooldown and hammer the API).
    if api_result.get('rate_limited'):
        return api_result

    # Other errors (expired token, connection, server, no data): degrade to the
    # newest local session snapshot so the widget keeps showing usage.  It
    # carries ``source='session'``; the app re-baselines change-detection on a
    # source switch so a stale sample is not misread as a reset or threshold.
    fallback = _fetch_usage_session()
    if fallback:
        fallback['api_error'] = api_result.get('error')
        return fallback

    return api_result


def _fetch_usage_api() -> dict[str, Any]:
    """Fetch and transform rate limits from the live Codex usage API."""
    headers = api_headers()
    if not headers:
        return {'error': T['no_token']}

    try:
        resp = requests.get(API_URL_USAGE, headers=headers, timeout=10)
        resp.raise_for_status()
        payload = resp.json()
    except requests.ConnectionError:
        return {'error': T['connection_error']}
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else 0
        extra: dict[str, Any] = {}
        server_msg = _extract_server_message(e.response)
        if server_msg:
            extra['server_message'] = server_msg
        if code == 401:
            return {**extra, 'error': T['auth_expired'], 'auth_error': True}
        if code == 429:
            retry = _parse_retry_after(e.response)
            if retry is not None:
                extra['retry_after'] = retry
            return {**extra, 'error': T['http_error'].format(code=429), 'rate_limited': True}
        if 500 <= code < 600:
            return {**extra, 'error': T['server_error'].format(code=code)}
        return {**extra, 'error': T['http_error'].format(code=code or '?')}
    except Exception:
        return {'error': T['connection_error']}

    rate_limits = _locate_rate_limits(payload)
    if not rate_limits:
        return {'error': T['no_usage_data']}

    result = transform_rate_limits(rate_limits)
    if not _has_quota(result):
        return {'error': T['no_usage_data']}
    result['source'] = 'api'
    return result


def _fetch_usage_session() -> dict[str, Any] | None:
    """Read the newest ``rate_limits`` snapshot from local session files.

    Scans recent ``rollout-*.jsonl`` files newest-first and returns the
    most recent snapshot found, transformed into the internal model.
    Returns ``None`` when no snapshot is available.
    """
    rate_limits = _latest_session_rate_limits()
    if not rate_limits:
        return None
    result = transform_rate_limits(rate_limits)
    if not _has_quota(result):
        return None
    result['source'] = 'session'
    return result


def fetch_profile() -> dict[str, Any] | None:
    """Return account/plan info decoded from the local id-token JWT.

    No network call: the id token written by ``codex login`` is a JWT
    whose (unverified) claims include the account email and ChatGPT plan
    type.  Shaped to match what the popup expects (``account.email`` /
    ``organization.organization_type``).
    """
    tokens = _read_tokens()
    account_id = tokens.get('account_id') or ''
    claims = _decode_jwt_claims(tokens.get('id_token', ''))

    email = claims.get('email') or ''
    auth_claim = claims.get('https://api.openai.com/auth', {})
    plan = ''
    if isinstance(auth_claim, dict):
        plan = auth_claim.get('chatgpt_plan_type') or ''
        account_id = account_id or auth_claim.get('chatgpt_account_id') or ''

    if not (email or plan or account_id):
        return None

    return {
        'account': {'uuid': account_id, 'email': email},
        'organization': {'organization_type': plan},
    }


# ---------------------------------------------------------------------------
# Transform
# ---------------------------------------------------------------------------

def transform_rate_limits(rate_limits: dict[str, Any]) -> dict[str, Any]:
    """Translate a Codex ``rate_limits`` object into the internal usage model.

    The ``primary`` / ``secondary`` windows become quota entries keyed by
    their duration (e.g. ``five_hour`` for a 300-minute window,
    ``seven_day`` for 10080 minutes), each with ``utilization`` (percent)
    and an ISO-8601 ``resets_at``.  Windows whose length is not a whole
    number of hours or days keep the raw ``primary`` / ``secondary`` key.
    """
    result: dict[str, Any] = {}
    for slot in ('primary', 'secondary'):
        window = rate_limits.get(slot)
        if not isinstance(window, dict):
            continue
        used = window.get('used_percent')
        if used is None or isinstance(used, bool):
            continue
        try:
            utilization = float(used)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(utilization):
            continue

        window_minutes = window.get('window_minutes')
        key = _window_to_key(window_minutes, slot)
        # Avoid clobbering if two windows somehow map to the same key.
        if key in result:
            key = slot
        result[key] = {
            'utilization': utilization,
            'resets_at': _iso_from_window(window),
            'window_minutes': window_minutes,
        }

    extra = _credits_to_extra_usage(rate_limits.get('credits'))
    if extra is not None:
        result['extra_usage'] = extra

    plan = rate_limits.get('plan_type')
    if plan:
        result['plan_type'] = plan

    return result


def _window_to_key(window_minutes: Any, fallback: str) -> str:
    """Build a parseable field key (e.g. ``five_hour``) from a window length."""
    try:
        minutes = int(round(float(window_minutes)))
    except (TypeError, ValueError):
        return fallback
    if minutes <= 0:
        return fallback

    if minutes % (24 * 60) == 0:
        number, unit = minutes // (24 * 60), 'day'
    elif minutes % 60 == 0:
        number, unit = minutes // 60, 'hour'
    else:
        return fallback

    word = _NUMBER_WORDS.get(number)
    return f'{word}_{unit}' if word else fallback


def _iso_from_window(window: dict[str, Any]) -> str:
    """Return an ISO-8601 reset timestamp from a rate-limit window.

    Accepts an absolute ``resets_at`` (epoch seconds, or milliseconds when
    large) or a relative ``resets_in_seconds`` / ``reset_after_seconds``.
    """
    absolute = window.get('resets_at')
    if absolute is not None:
        try:
            value = float(absolute)
            if value > _MS_THRESHOLD:
                value /= 1000.0
            return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
        except (TypeError, ValueError, OSError, OverflowError):
            pass

    for key in ('resets_in_seconds', 'reset_after_seconds', 'resets_in'):
        relative = window.get(key)
        if relative is not None:
            try:
                from datetime import timedelta
                return (datetime.now(timezone.utc) + timedelta(seconds=float(relative))).isoformat()
            except (TypeError, ValueError, OverflowError):
                pass

    return ''


def _credits_to_extra_usage(credits: Any) -> dict[str, Any] | None:
    """Map a Codex ``credits`` snapshot to the internal extra-usage model.

    The real Codex ``CreditsSnapshot`` is ``{has_credits, unlimited, balance}``
    (snake_case in the wham/session payload; ``balance`` is a string or null).
    It is a *credit balance*, not a spend-vs-limit ratio, so it is surfaced as
    a balance / unlimited display rather than a percentage bar.

    Returns ``None`` (no CREDITS row) when credits are absent, disabled, or the
    balance is unknown - e.g. ``{has_credits: false, unlimited: false,
    balance: null}`` on an account without purchased credits.
    """
    if not isinstance(credits, dict):
        return None

    if credits.get('unlimited'):
        return {'is_enabled': True, 'unlimited': True, 'balance': None}

    balance = credits.get('balance')
    if balance is None or balance == '' or isinstance(balance, bool) or not isinstance(balance, (str, int, float)):
        return None

    return {'is_enabled': True, 'unlimited': False, 'balance': balance}


def _has_quota(data: dict[str, Any]) -> bool:
    """Return True if the transformed data has at least one usage quota."""
    return any(
        isinstance(value, dict) and value.get('utilization') is not None
        for key, value in data.items()
        if key != 'extra_usage'
    )


# ---------------------------------------------------------------------------
# Session-file fallback
# ---------------------------------------------------------------------------

def _latest_session_rate_limits(max_files: int = 40) -> dict[str, Any] | None:
    """Return the most recent ``rate_limits`` snapshot from session files.

    Scans the newest ``rollout-*.jsonl`` files (by modification time) and
    returns the last snapshot found in the newest file that contains one.
    Only lines mentioning ``rate_limits`` are JSON-parsed, so large
    transcripts are skipped cheaply.
    """
    if not CODEX_SESSIONS_DIR.is_dir():
        return None

    try:
        files = sorted(
            CODEX_SESSIONS_DIR.glob('*/*/*/rollout-*.jsonl'),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:max_files]
    except OSError:
        return None

    for path in files:
        snapshot = _scan_file_for_rate_limits(path)
        if snapshot:
            return snapshot
    return None


def _scan_file_for_rate_limits(path: Path) -> dict[str, Any] | None:
    """Return the last ``rate_limits`` snapshot in a rollout file, or None."""
    latest: dict[str, Any] | None = None
    try:
        # errors='replace': a rollout file may be mid-write, so a truncated
        # multibyte sequence must not raise UnicodeDecodeError and crash the
        # poll loop - the affected line simply fails json.loads and is skipped.
        with path.open('r', encoding='utf-8', errors='replace') as handle:
            for line in handle:
                if '"rate_limits"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                snapshot = _extract_record_rate_limits(record)
                if snapshot:
                    latest = snapshot
    except (OSError, UnicodeDecodeError):
        return None
    return latest


def _extract_record_rate_limits(record: Any) -> dict[str, Any] | None:
    """Pull a ``rate_limits`` object out of a session record, if present."""
    if not isinstance(record, dict):
        return None
    payload = record.get('payload')
    if isinstance(payload, dict):
        info = payload.get('info')
        if isinstance(info, dict) and isinstance(info.get('rate_limits'), dict):
            return info['rate_limits']
        if isinstance(payload.get('rate_limits'), dict):
            return payload['rate_limits']
    if isinstance(record.get('rate_limits'), dict):
        return record['rate_limits']
    return None


def _locate_rate_limits(payload: Any) -> dict[str, Any] | None:
    """Find the ``rate_limits`` object in an API response of varying shape."""
    if not isinstance(payload, dict):
        return None
    for key in ('rate_limits', 'rate_limit'):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    # The endpoint may also return the windows at the top level.
    if 'primary' in payload or 'secondary' in payload:
        return payload
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _decode_jwt_claims(token: str) -> dict[str, Any]:
    """Decode the (unverified) claims of a JWT, or return ``{}``.

    Used only to read the account email / plan for display; the signature
    is intentionally not verified because the token is read from the
    user's own machine and never trusted for authorization.
    """
    if not token or token.count('.') < 2:
        return {}
    try:
        payload_b64 = token.split('.')[1]
        padding = '=' * (-len(payload_b64) % 4)
        decoded = base64.urlsafe_b64decode(payload_b64 + padding)
        claims = json.loads(decoded)
        return claims if isinstance(claims, dict) else {}
    except (binascii.Error, json.JSONDecodeError, ValueError, UnicodeDecodeError):
        return {}


def _user_agent() -> str:
    """Return the User-Agent string with the installed Codex CLI version."""
    from .codex_cli import CODEX_CLI_PATH, cli_version

    version = cli_version(CODEX_CLI_PATH)
    return f'codex_cli_rs/{version}' if version else _FALLBACK_USER_AGENT


def _extract_server_message(response: requests.Response | None) -> str | None:
    """Extract a human-readable error message from a JSON error body."""
    if response is None:
        return None
    try:
        body = response.json()
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    error = body.get('error')
    if isinstance(error, dict):
        msg = error.get('message')
    elif isinstance(error, str):
        msg = error
    else:
        msg = body.get('detail') or body.get('message')
    if not msg:
        return None
    msg = str(msg).removesuffix(' Please try again later.').removesuffix(' Please try again later').strip()
    return msg or None


def _parse_retry_after(response: requests.Response | None) -> int | None:
    """Parse the ``Retry-After`` header as an integer number of seconds."""
    if response is None:
        return None
    raw = response.headers.get('Retry-After')
    if raw is None:
        return None
    try:
        return max(int(raw), 0)
    except (ValueError, TypeError):
        return None
