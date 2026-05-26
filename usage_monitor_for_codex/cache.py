"""
Usage Cache
============

Thread-safe cache for API data - single source of truth for all usage
state.  All API refresh requests go through ``UsageCache.update()``,
which uses a lock to prevent concurrent calls and a cooldown to prevent
calls that are too close together (HTTP 429).
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from .codex_api import fetch_profile, fetch_usage, read_access_token, read_effective_account_id
from .codex_cli import RefreshResult, refresh_token
from .settings import MAX_BACKOFF, POLL_FAST, POLL_INTERVAL

__all__ = ['CacheSnapshot', 'UpdateResult', 'UsageCache']

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CacheSnapshot:
    """Immutable, consistent snapshot of cache state for the popup."""

    usage: dict[str, Any]
    profile: dict[str, Any] | None
    last_success_time: float | None
    refreshing: bool
    last_error: str | None
    version: int


@dataclass(frozen=True)
class UpdateResult:
    """Result of a ``UsageCache.update()`` call.

    Attributes
    ----------
    data : dict or None
        Raw API response dict, or ``None`` when the call was skipped
        (lock held or cooldown active).
    token_refresh : RefreshResult or None
        Set when a token refresh was attempted after a 401 auth error.
    """

    data: dict[str, Any] | None
    token_refresh: RefreshResult | None = None


class UsageCache:
    """Thread-safe cache managing API data, cooldown, and error state.

    All callers (poll loop, popup) go through ``update()`` instead
    of calling ``fetch_usage()`` directly.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._profile_lock = threading.Lock()
        self._usage: dict[str, Any] = {}
        self._profile: dict[str, Any] | None = None
        self._profile_token: str | None = None
        self._profile_account_id: str | None = None
        self._last_success_time: float | None = None
        self._refreshing = False
        self._last_error: str | None = None
        self._version = 0
        self._consecutive_errors = 0
        self._last_failed_token: str | None = None
        self._rate_limit_until: float = 0

    # Public properties

    @property
    def usage(self) -> dict[str, Any]:
        """Last successful usage data (empty dict before first success)."""
        return self._usage

    @property
    def profile(self) -> dict[str, Any] | None:
        return self._profile

    @property
    def last_success_time(self) -> float | None:
        return self._last_success_time

    @property
    def refreshing(self) -> bool:
        return self._refreshing

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def version(self) -> int:
        """Change counter - incremented on every state change."""
        return self._version

    @property
    def consecutive_errors(self) -> int:
        return self._consecutive_errors

    @property
    def rate_limit_remaining(self) -> float:
        """Seconds remaining in the rate-limit backoff window, or 0."""
        return max(self._rate_limit_until - time.time(), 0)

    @property
    def snapshot(self) -> CacheSnapshot:
        """Return a consistent snapshot for the popup to display."""
        with self._state_lock:
            return CacheSnapshot(
                usage=self._usage,
                profile=self._profile,
                last_success_time=self._last_success_time,
                refreshing=self._refreshing,
                last_error=self._last_error,
                version=self._version,
            )

    # Public methods

    def ensure_profile(self) -> None:
        """Fetch the account profile if not loaded, or re-fetch if the access token
        OR the effective account id changed (thread-safe).

        Both the access token and the account select which account the usage API
        and the profile describe, so a change in EITHER - a ``codex login`` (new
        token) or a workspace/account switch on the same token - must invalidate the
        cached profile. The account component is the EFFECTIVE id
        (``read_effective_account_id``: top-level ChatGPT-Account-Id, else the
        id-token JWT claim), matching the identity that stamps usage and decodes the
        profile, so a switch visible only via the JWT claim still invalidates here.
        Acquires ``_lock`` around the HTTP call to prevent concurrent API requests.
        """
        current = (read_access_token(), read_effective_account_id())
        if self._profile is not None and (self._profile_token, self._profile_account_id) == current:
            return

        with self._profile_lock:
            current = (read_access_token(), read_effective_account_id())
            if self._profile is not None and (self._profile_token, self._profile_account_id) == current:
                return
            log.info('fetch_profile started')
            with self._lock:
                profile = fetch_profile()
            with self._state_lock:
                self._profile = profile
                self._profile_token, self._profile_account_id = current
                self._version += 1
            log.info('fetch_profile -> %s', 'OK' if profile else 'failed')

    def update(self) -> UpdateResult:
        """Fetch usage data with lock and cooldown protection.

        Returns
        -------
        UpdateResult
            Contains the API response dict (``data``), or ``None``
            when the call was skipped.  If a token refresh was
            attempted, ``token_refresh`` carries the outcome.
        """
        if not self._lock.acquire(blocking=False):
            log.debug('update skipped (another update in progress)')
            return UpdateResult(data=None)

        try:
            return self._update_locked()
        finally:
            self._lock.release()

    # Private helpers

    def _update_locked(self) -> UpdateResult:
        """Execute the actual update while holding ``_lock``."""
        if self._last_success_time is not None and time.time() - self._last_success_time < POLL_FAST:
            log.debug('update skipped (cooldown, %.0fs remaining)', POLL_FAST - (time.time() - self._last_success_time))
            return UpdateResult(data=None)

        if time.time() < self._rate_limit_until:
            log.debug('update skipped (rate-limit backoff, %.0fs remaining)', self._rate_limit_until - time.time())
            return UpdateResult(data=None)

        if self._last_failed_token is not None:
            if read_access_token() == self._last_failed_token:
                log.debug('update skipped (token unchanged after auth failure)')
                return UpdateResult(data=None)
            self._last_failed_token = None

        with self._state_lock:
            self._refreshing = True
            self._version += 1

        try:
            return self._fetch_and_process()
        except Exception:
            with self._state_lock:
                self._refreshing = False
                self._version += 1
            raise

    def _fetch_and_process(self) -> UpdateResult:
        """Fetch usage data and process the response."""
        token_before = read_access_token()
        log.info('fetch_usage started')
        data = fetch_usage()

        if 'error' in data:
            self._record_error(data)

            if data.get('rate_limited'):
                retry_after = data.get('retry_after')
                if retry_after is not None and retry_after > 0:
                    delay = min(max(retry_after, POLL_INTERVAL), MAX_BACKOFF)
                else:
                    delay = min(POLL_INTERVAL * (2 ** max(self._consecutive_errors - 1, 0)), MAX_BACKOFF)
                self._rate_limit_until = time.time() + delay
                log.warning('fetch_usage -> rate limited, backoff %.0fs', delay)

            token_refresh = None
            if data.get('auth_error'):
                log.warning('fetch_usage -> auth error, attempting token refresh')
                token_refresh = self._try_token_refresh(token_before)
                if token_refresh is not None and self._last_error is None:
                    # Token refresh succeeded and retry was successful
                    return UpdateResult(data=self._usage, token_refresh=token_refresh)
                if token_refresh is None:
                    # Refresh failed or token unchanged - block this token
                    self._last_failed_token = token_before
            elif not data.get('rate_limited'):
                log.warning('fetch_usage -> error: %s', data['error'])

            with self._state_lock:
                self._refreshing = False
                self._version += 1
            return UpdateResult(data=data, token_refresh=token_refresh)

        pct_5h = (data.get('five_hour') or {}).get('utilization')
        pct_7d = (data.get('seven_day') or {}).get('utilization')
        log.info('fetch_usage -> OK [source=%s] (5h: %s%%, 7d: %s%%)', data.get('source', '?'),
                 pct_5h if pct_5h is not None else '?', pct_7d if pct_7d is not None else '?')
        self._record_success(data)
        return UpdateResult(data=data)

    def _record_error(self, data: dict[str, Any], *, count: bool = True) -> None:
        """Apply common state updates after a failed API response.

        Parameters
        ----------
        data : dict
            API response containing ``'error'`` and optional ``'server_message'``.
        count : bool
            If True (default), increment ``_consecutive_errors``.
        """
        with self._state_lock:
            if count:
                self._consecutive_errors += 1
            error = data['error']
            server_msg = data.get('server_message')
            if server_msg:
                error += f'\n{server_msg}'
            self._last_error = error

    def _record_success(self, data: dict[str, Any]) -> None:
        """Apply common state updates after a successful API response."""
        # If a profile is already cached and the credentials it was decoded from
        # have since changed - the access token (a `codex login`) OR the effective
        # account id (a same-token workspace/account switch, top-level or JWT-claim)
        # - refresh the profile in the SAME critical section as the usage. The
        # popup reads CacheSnapshot on its
        # own 500ms timer, so committing usage now and the profile later (via
        # ensure_profile) would leave a window where one account's usage/Credits
        # render beside another account's email. fetch_profile() is a local JWT
        # decode of auth.json - no network. (When no profile is cached yet, the
        # cold-start path is left to ensure_profile() as before.)
        new_profile = self._profile
        new_profile_token = self._profile_token
        new_profile_account_id = self._profile_account_id
        if self._profile is not None:
            current_token = read_access_token()
            current_account_id = read_effective_account_id()
            if self._profile_token != current_token or self._profile_account_id != current_account_id:
                new_profile = fetch_profile()
                new_profile_token = current_token
                new_profile_account_id = current_account_id

        # _usage is always reassigned (never mutated in place), so existing
        # CacheSnapshot references remain valid after this update.
        with self._state_lock:
            self._consecutive_errors = 0
            self._last_error = None
            self._last_success_time = time.time()
            self._rate_limit_until = 0
            self._last_failed_token = None
            self._usage = data
            self._profile = new_profile
            self._profile_token = new_profile_token
            self._profile_account_id = new_profile_account_id
            self._refreshing = False
            self._version += 1

    def _try_token_refresh(self, token_before: str | None) -> RefreshResult | None:
        """Attempt to refresh the OAuth token via the CLI.

        For Codex this is effectively a no-op (``codex_cli.refresh_token``
        cannot refresh non-interactively), so it usually returns ``None``
        and the cache waits for Codex itself to refresh ``auth.json``.

        Parameters
        ----------
        token_before : str or None
            The token that was used for the failed request.  Used to
            detect whether the refresh actually produced a new token.

        Returns
        -------
        RefreshResult or None
            The refresh outcome, or ``None`` if the CLI is not available
            or the token didn't change.
        """
        result = refresh_token()
        if not result.success:
            log.info('token refresh failed: %s', result.error)
            return None

        if read_access_token() == token_before:
            log.info('token refresh succeeded but token unchanged')
            return None

        # Token changed - retry the API call
        log.info('token changed, retrying fetch_usage')
        data = fetch_usage()
        if 'error' not in data:
            log.info('retry -> OK')
            self._record_success(data)
            return result

        log.warning('retry -> error: %s', data['error'])
        # Update error message but do not increment _consecutive_errors
        # again (the caller already counted this update cycle as one error).
        self._record_error(data, count=False)

        return result
