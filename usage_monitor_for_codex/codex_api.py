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


def _jwt_account_id(tokens: dict[str, Any] | None = None) -> str | None:
    """The token's own account id, decoded from the id-token JWT.

    Used to stamp a usage result fetched WITHOUT an explicit ``ChatGPT-Account-Id``
    header (the request then targets the token's default account), so every live
    result still carries the account identity it was fetched for. Accepts a
    pre-read ``tokens`` dict to share one credentials snapshot with the request.
    """
    if tokens is None:
        tokens = _read_tokens()
    claims = _decode_jwt_claims(tokens.get('id_token', ''))
    auth = claims.get('https://api.openai.com/auth', {})
    if isinstance(auth, dict):
        return auth.get('chatgpt_account_id') or None
    return None


def api_headers(tokens: dict[str, Any] | None = None) -> dict[str, str] | None:
    """Return auth headers for the Codex usage API, or ``None`` if no token.

    Accepts a pre-read ``tokens`` dict so a caller can derive the headers and the
    account stamp from one credentials snapshot (no torn read between them).
    """
    if tokens is None:
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
    # carries ``source='session'``; the app treats session data as display-only and
    # never lets it touch event state, so a stale sample drives no reset or threshold.
    fallback = _fetch_usage_session()
    if fallback:
        fallback['api_error'] = api_result.get('error')
        return fallback

    return api_result


def _fetch_usage_api() -> dict[str, Any]:
    """Fetch and transform rate limits from the live Codex usage API."""
    # Read credentials ONCE so the request token, the ChatGPT-Account-Id header,
    # and the stamped account identity all come from the SAME snapshot. (A
    # concurrent `codex login` between separate reads could otherwise stamp a
    # different account than the token actually used.) Capturing before the
    # request also means a switch landing mid-request leaves this stamp on the old
    # account, so the post-request profile refresh differs and the popup hides
    # account/Credits rather than misattributing them.
    tokens = _read_tokens()
    headers = api_headers(tokens)
    if not headers:
        return {'error': T['no_token']}
    request_account_id = tokens.get('account_id') or _jwt_account_id(tokens)

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

    if not isinstance(payload, dict):
        return {'error': T['no_usage_data']}
    # The wham/usage response is a RateLimitStatusPayload with rate_limit /
    # credits / plan_type at the top level; some deployments may wrap it under
    # "rate_limits". transform_rate_limits handles either nesting.
    obj = payload.get('rate_limits') if isinstance(payload.get('rate_limits'), dict) else payload
    result = transform_rate_limits(obj)
    # Keep a credits-only live response: the official client tolerates a payload
    # whose primary rate_limit is absent, and discarding a valid credit balance
    # just because no usage window is present would hide it from the user.
    if not _has_quota(result) and not result.get('extra_usage'):
        return {'error': T['no_usage_data']}
    result['source'] = 'api'
    # Stamp the account this usage was fetched for (captured above, before the
    # request), so the popup shows the account block / Credits only when they
    # match the cached profile's account - never pairing one account's email with
    # another's usage if `auth.json` changed mid-request.
    if request_account_id:
        result['account_id'] = request_account_id
    return result


def _fetch_usage_session() -> dict[str, Any] | None:
    """Read the newest ``rate_limits`` snapshot from local session files.

    Scans recent ``rollout-*.jsonl`` files newest-first and returns the
    most recent snapshot found, transformed into the internal model.
    Returns ``None`` when no snapshot is available.
    """
    found = _latest_session_rate_limits()
    if not found:
        return None
    snapshot_at, rate_limits = found
    result = transform_rate_limits(rate_limits)
    if not _has_quota(result):
        return None
    result['source'] = 'session'
    if snapshot_at:
        result['snapshot_at'] = snapshot_at
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
    """Translate a Codex rate-limit object into the internal usage model.

    Handles BOTH shapes that carry the same information:

    * the live ``wham/usage`` HTTP response (``RateLimitStatusPayload``) -
      ``{rate_limit: {primary_window, secondary_window: {used_percent,
      limit_window_seconds, reset_at}}, credits, plan_type}``; and
    * the session-file snapshot (``RateLimitSnapshot``) - ``{primary,
      secondary: {used_percent, window_minutes, resets_at}, credits, plan_type}``.

    Each window becomes a quota entry keyed by its duration (``five_hour`` for a
    300-minute window, ``seven_day`` for 10080), with ``utilization`` and an
    ISO-8601 ``resets_at``.  A window whose length is not a whole number of
    hours or days keeps the raw ``primary`` / ``secondary`` key.
    """
    result: dict[str, Any] = {}

    # The HTTP payload nests the windows under "rate_limit" as
    # "primary_window"/"secondary_window"; the session snapshot puts
    # "primary"/"secondary" at the top level. Support both.
    detail = rate_limits.get('rate_limit')
    if isinstance(detail, dict):
        slots = (('primary', detail.get('primary_window')), ('secondary', detail.get('secondary_window')))
    else:
        slots = (('primary', rate_limits.get('primary')), ('secondary', rate_limits.get('secondary')))

    for slot, window in slots:
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

        minutes = _window_minutes(window)
        key = _window_to_key(minutes, slot)
        # Avoid clobbering if two windows somehow map to the same key.
        if key in result:
            key = slot
        result[key] = {
            'utilization': utilization,
            'resets_at': _iso_from_window(window),
            'window_minutes': minutes,
        }

    extra = _credits_to_extra_usage(rate_limits.get('credits'))
    if extra is not None:
        result['extra_usage'] = extra

    plan = rate_limits.get('plan_type')
    if plan:
        result['plan_type'] = plan

    return result


def _window_minutes(window: dict[str, Any]) -> Any:
    """Return the window length in minutes from either rate-limit shape.

    Session snapshots carry ``window_minutes`` directly; the HTTP payload
    carries ``limit_window_seconds`` (converted with ceiling division, matching
    the official client's ``window_minutes_from_seconds``).
    """
    minutes = window.get('window_minutes')
    if minutes is not None:
        return minutes
    seconds = window.get('limit_window_seconds')
    if seconds is not None:
        try:
            secs = int(seconds)
        except (TypeError, ValueError):
            return None
        return (secs + 59) // 60 if secs > 0 else None
    return None


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

    Accepts an absolute ``resets_at`` / ``reset_at`` (epoch seconds, or
    milliseconds when large) or a relative ``resets_in_seconds`` /
    ``reset_after_seconds`` / ``resets_in``.
    """
    for abs_key in ('resets_at', 'reset_at'):
        absolute = window.get(abs_key)
        if absolute is None:
            continue
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

    # No credit balance: an explicit has_credits=false means no row (avoids
    # showing e.g. "0 credits remaining" for an account without credits).
    if credits.get('has_credits') is False:
        return None

    balance = credits.get('balance')
    if balance is None or balance == '' or isinstance(balance, bool) or not isinstance(balance, (str, int, float)):
        return None
    # Reject non-finite numbers (NaN / Infinity), including numeric strings, so
    # they never reach format_balance() / int() and crash the popup update loop.
    try:
        if not math.isfinite(float(str(balance).replace(',', '').strip())):
            return None
    except (TypeError, ValueError):
        pass  # a genuinely non-numeric string balance is left as-is for display

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

def _latest_session_rate_limits(max_files: int = 40) -> tuple[str | None, dict[str, Any]] | None:
    """Return ``(snapshot_at, rate_limits)`` from the freshest session record.

    The candidate files are a cheap prefilter for *which* recent
    ``rollout-*.jsonl`` files to scan; the chosen snapshot is the one with the
    newest *record* timestamp, so a merely touched / synced / restored file
    cannot win over genuinely fresher data.  The scan is deliberately bounded
    (we never open every file), so the candidate set unions two prefilters whose
    blind spots differ, which surfaces the freshest record in every realistic
    case:

    * **newest by mtime** - surfaces a long-running, older-dated session that was
      appended to most recently (a write updates mtime); and
    * **newest by path** - the ``YYYY/MM/DD`` directory plus the ISO-timestamp
      ``rollout-<ts>-<uuid>.jsonl`` filename sort chronologically, so a freshly
      created session is still considered even when its mtime was clobbered by a
      sync / restore / touch.

    The only residual gap is pathological - the newest record living in an
    *old-named* file whose mtime was *also* clobbered, behind ``max_files``
    newer entries in **both** orderings at once - which does not occur in normal
    use.  Only lines mentioning ``rate_limits`` are JSON-parsed.  Returns
    ``None`` when none is found.
    """
    if not CODEX_SESSIONS_DIR.is_dir():
        return None

    try:
        all_files = list(CODEX_SESSIONS_DIR.glob('*/*/*/rollout-*.jsonl'))
    except OSError:
        return None

    try:
        by_mtime = sorted(all_files, key=lambda p: p.stat().st_mtime, reverse=True)[:max_files]
    except OSError:
        by_mtime = []
    by_path = sorted(all_files, reverse=True)[:max_files]
    # dict.fromkeys dedups while preserving the mtime-first ordering.
    files = list(dict.fromkeys(by_mtime + by_path))

    best: tuple[datetime, str, dict[str, Any]] | None = None
    fallback: tuple[str | None, dict[str, Any]] | None = None
    for path in files:
        ts_str, snapshot = _scan_file_for_rate_limits(path)
        if snapshot is None:
            continue
        ts = _parse_timestamp(ts_str)
        if ts is not None:
            if best is None or ts > best[0]:
                best = (ts, ts_str, snapshot)
        elif fallback is None:
            # mtime-newest snapshot that lacks a parseable timestamp
            fallback = (ts_str, snapshot)

    if best is not None:
        return (best[1], best[2])
    return fallback


def _scan_file_for_rate_limits(path: Path) -> tuple[str | None, dict[str, Any] | None]:
    """Return ``(timestamp, snapshot)`` for the last rate_limits record in a file.

    *timestamp* is the record's ISO ``timestamp`` (or ``None``).  Returns
    ``(None, None)`` when the file has no usable snapshot.
    """
    latest_ts: str | None = None
    latest_snapshot: dict[str, Any] | None = None
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
                    latest_snapshot = snapshot
                    latest_ts = record.get('timestamp') if isinstance(record, dict) else None
    except (OSError, UnicodeDecodeError):
        return (None, None)
    return (latest_ts, latest_snapshot)


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


def _parse_timestamp(ts_str: Any) -> datetime | None:
    """Parse a session record's ISO ``timestamp`` into a tz-aware datetime.

    A timezone-naive timestamp is treated as UTC so comparisons never mix
    aware and naive datetimes (which would raise ``TypeError``).
    """
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(str(ts_str).replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


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
