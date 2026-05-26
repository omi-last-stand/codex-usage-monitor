# API Reference

How the app obtains Codex (ChatGPT-plan) rate-limit usage, the shapes it reads, and how they map onto the app's internal model. This serves as implementation reference - endpoints, field names, data types, and structure.

> [!NOTE]
> These are real-world examples with anonymized data. The usage endpoint and the on-disk session format are undocumented internals of the Codex CLI and may change without notice. If your data contains fields not listed here, please open an issue with an anonymized example so we can keep this reference up to date.

The app has two data sources, selected by the [`usage_source`](configuration.md#usage-data-source) setting:

1. **Live API** (`"api"`, and the first choice of `"auto"`) - the same endpoint the Codex CLI polls for its `/status` rate limits.
2. **Local session files** (`"session"`, and the fallback of `"auto"`) - the newest session rollout file's `rate_limits` snapshot. No network, no token.

Credentials are read only from `~/.codex/auth.json` and used only in HTTP headers. Network communication is exclusively with `chatgpt.com`.

## Credentials — `~/.codex/auth.json`

The Codex CLI writes its OAuth credentials here (honoring `CODEX_HOME`, default `~/.codex`). The app reads three fields, all under `tokens`:

```json
{
  "tokens": {
    "access_token": "eyJhbGciOi...",
    "account_id": "a1b2c3d4-...",
    "id_token": "eyJhbGciOi..."
  }
}
```

- **`access_token`** - sent as `Authorization: Bearer <token>` to the usage API.
- **`account_id`** - sent as the `ChatGPT-Account-Id` header.
- **`id_token`** - a JWT decoded locally (no network) for the account email and plan; see [JWT profile](#jwt-profile) below.

The app never writes this file. There is **no automatic token refresh** - `codex login` is interactive, and the Codex CLI refreshes `auth.json` on its own while it runs. If the token expires, the app falls back to local session data (under `"auto"`).

## Live usage API — `GET /backend-api/wham/usage`

```
https://chatgpt.com/backend-api/wham/usage
```

Request headers:

```
Authorization: Bearer <access_token>
ChatGPT-Account-Id: <account_id>
Accept: application/json
```

The response contains a `rate_limits` object (the app also accepts the windows at the top level, or under `rate_limit`). The relevant shape:

```json
{
  "rate_limits": {
    "primary":   { "used_percent": 4.0,  "window_minutes": 300,   "resets_at": 1779754106 },
    "secondary": { "used_percent": 12.0, "window_minutes": 10080, "resets_at": 1780192198 },
    "credits": null,
    "plan_type": "plus"
  }
}
```

### `rate_limits` fields

| Field | Type | Description |
|-------|------|-------------|
| `primary` | object \| null | The rolling short window (typically 5 hours). |
| `secondary` | object \| null | The longer window (typically weekly / 7 days). |
| `credits` | object \| null | Optional credits balance (paid overage); see [Extra usage](#extra-usage-credits). Often `null`. |
| `plan_type` | string | ChatGPT plan, e.g. `"plus"`, `"pro"`, `"team"`. |

Each window (`primary` / `secondary`) has:

| Field | Type | Description |
|-------|------|-------------|
| `used_percent` | number | Percentage of the window consumed (0–100). |
| `window_minutes` | number | Length of the window in minutes (e.g. `300` = 5h, `10080` = 7d). |
| `resets_at` | number | When the window resets, as a Unix epoch (seconds; milliseconds if very large). |

A relative `resets_in_seconds` / `reset_after_seconds` / `resets_in` is also accepted in place of `resets_at` and converted to an absolute time.

## Internal mapping — `primary`/`secondary` → `five_hour`/`seven_day`

To reuse the app's existing field machinery (labels, period bars, thresholds, notifications), each window is translated into a quota entry keyed by its **length**:

- `window_minutes` that is a whole number of hours → `<n>_hour` (e.g. `300` → `five_hour`).
- a whole number of days → `<n>_day` (e.g. `10080` → `seven_day`).
- anything else keeps the raw slot name (`primary` / `secondary`) as the key.

Each entry carries `utilization` (from `used_percent`) and an ISO-8601 `resets_at` (converted from the epoch). So the example above becomes:

```json
{
  "five_hour":  { "utilization": 4.0,  "resets_at": "2026-05-25T...+00:00", "window_minutes": 300 },
  "seven_day":  { "utilization": 12.0, "resets_at": "2026-05-30T...+00:00", "window_minutes": 10080 },
  "plan_type": "plus",
  "source": "api"
}
```

`source` is `"api"` when the data came from the live endpoint, or `"session"` when it came from a local rollout file. Under `"auto"`, an `api_error` field is added to a session result when the live API was tried first but failed.

### Extra usage (credits)

The Codex `rate_limits.credits` field is a `CreditsSnapshot` — `{ "has_credits": bool, "unlimited": bool, "balance": string | null }`:

```json
{ "has_credits": true, "unlimited": false, "balance": "1250" }
```

It is mapped to an `extra_usage` entry the popup renders as a **balance line** (not a percentage bar):

```json
{ "extra_usage": { "is_enabled": true, "unlimited": false, "balance": "1250" } }
```

- `unlimited: true` → shows "Unlimited".
- a non-null `balance` → shows the remaining balance (e.g. "1,250 credits remaining").
- `credits: null`, `has_credits: false`, or a null/empty `balance` → the Credits row is hidden.

Codex credits are a *balance*, so there is no spend percentage and no `$` conversion (the value is shown as a credit count), and the threshold alerts that apply to the time-window quotas do not apply here.

## Session-file fallback — `~/.codex/sessions/.../rollout-*.jsonl`

When the API is unavailable (or `usage_source` is `"session"`), the app reads the newest `rate_limits` snapshot from the local session rollout files:

```
~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
```

These are JSONL transcripts the Codex CLI writes per session. The app scans the most recently modified files newest-first, and for performance only JSON-parses lines that mention `rate_limits`. The `rate_limits` object is found in any of these shapes per line:

```json
{ "payload": { "info": { "rate_limits": { "primary": { "used_percent": 4.0, "window_minutes": 300, "resets_at": 1779754106 } } } } }
```

(`payload.info.rate_limits`, `payload.rate_limits`, or a top-level `rate_limits`). The most recent snapshot found is transformed with the same `primary`/`secondary` → `five_hour`/`seven_day` mapping described above, and tagged `"source": "session"`. This path needs **no network and no token**, but the numbers are only as fresh as your last Codex turn.

## JWT profile

The account email and plan are read locally from the `id_token` JWT in `auth.json` - **no network call**. The token's payload segment is base64url-decoded and its (unverified) claims are read:

```json
{
  "email": "user@example.com",
  "https://api.openai.com/auth": {
    "chatgpt_plan_type": "plus",
    "chatgpt_account_id": "a1b2c3d4-..."
  }
}
```

The signature is intentionally **not** verified - the token comes from the user's own machine and is used only for display, never for authorization. This is shaped into the structure the popup expects:

```json
{
  "account": { "uuid": "a1b2c3d4-...", "email": "user@example.com" },
  "organization": { "organization_type": "plus" }
}
```
