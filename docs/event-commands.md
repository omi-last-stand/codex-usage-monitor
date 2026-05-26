# Event Commands

Run a custom shell command when a quota resets or a usage threshold is crossed. Commands run asynchronously and do not block the app. Event details are passed as environment variables so your command or script can use them directly.

## Settings

Add these keys to your [`usage-monitor-settings.json`](configuration.md). After saving, use the **Restart** option in the tray context menu to apply the changes.

| Key | Default | Description |
|-----|---------|-------------|
| `on_reset_command` | *(none)* | Shell command (or array of commands) to run when a quota resets (usage drops) |
| `on_startup_command` | *(none)* | Shell command (or array of commands) to run once after the first successful usage update following app start |
| `on_threshold_command` | *(none)* | Shell command (or array of commands) to run when usage crosses a configured alert threshold |

Commands run with the same privileges as the app and **without a visible window** - no console pops up and no focus is stolen. This is ideal for background tasks like sending notifications, playing sounds, or running headless commands (e.g. `codex exec "..."`). Relative paths in commands are resolved relative to the executable's folder (or the project root when running from source).

Both settings accept a single command string or an array of strings to run multiple commands per event. When an array is provided, all commands are launched independently (fire-and-forget) - if one fails, the others still run.

Commands only fire on **state changes** detected while the app is running. On app startup, already-exceeded thresholds trigger a desktop notification but do not run `on_threshold_command` - this prevents duplicate commands after a restart or reboot.

When `on_reset_command` is configured, the app briefly wakes from idle/lock pause to poll at the expected reset time so the command fires promptly - even if the computer is unattended. If the reset has not been applied yet (server-side delay) or the network is temporarily unavailable, the app retries at regular intervals until the reset is confirmed. `on_threshold_command` does not wake from idle - thresholds are driven by active usage, so they are checked when polling resumes after the user returns. Desktop notifications that occur during idle are deferred and shown when the user returns.

> [!TIP]
> If you need a visible terminal, prefix your command with `start cmd /c`, e.g.:
> ```
> "on_reset_command": "start cmd /c codex resume --last"
> ```

> [!TIP]
> Use the **Test event commands** submenu in the tray context menu to fire your configured commands with sample data. This lets you verify your command and script setup without waiting for a real event.

## Examples

> [!NOTE]
> The Codex CLI examples below use illustrative flags. Check `codex --help` and `codex exec --help` for the exact options your installed version supports.

### Resume a Codex session when the quota resets

```json
{
  "on_reset_command": "codex resume --last"
}
```

`--last` resumes the most recent conversation. Use `codex resume <session-id>` to target a specific session.

### Kick off a fresh primary (5-hour) window at app start when none is active

Start the rolling 5-hour window immediately at app launch instead of waiting for your first real message. Only fires when no 5-hour window is currently active:

```json
{
  "on_startup_command": "if not defined USAGE_MONITOR_RESETS_AT_FIVE_HOUR codex exec \"ok\""
}
```

`USAGE_MONITOR_RESETS_AT_FIVE_HOUR` is empty when no five-hour window is active, so the ping only fires after a reset already happened (e.g. overnight, or while the app was closed).

### Always keep a primary (5-hour) window running

To cover both cases - the reset happening with the app running, **and** the app starting up after a reset already happened - configure both commands together:

```json
{
  "on_startup_command": "if not defined USAGE_MONITOR_RESETS_AT_FIVE_HOUR codex exec \"ok\"",
  "on_reset_command": "if \"%USAGE_MONITOR_VARIANT%\"==\"five_hour\" codex exec \"ok\""
}
```

`on_reset_command` handles the live case (the 5-hour window expires while the app is polling), `on_startup_command` handles the gap (app was closed when the reset happened, or you just turned the computer back on).

### Target a specific quota variant

Use `USAGE_MONITOR_VARIANT` to run a command only when a specific quota resets. This example sends a minimal Codex ping the moment the 5-hour window resets, so the next window starts immediately instead of waiting for your first real message:

```json
{
  "on_reset_command": "if \"%USAGE_MONITOR_VARIANT%\"==\"five_hour\" codex exec \"ok\""
}
```

The same pattern works for the weekly window (`seven_day`) and for `on_threshold_command`.

### Only resume when both windows have enough headroom

```json
{
  "on_reset_command": "if %USAGE_MONITOR_UTILIZATION_FIVE_HOUR% LSS 80 if %USAGE_MONITOR_UTILIZATION_SEVEN_DAY% LSS 95 codex resume --last"
}
```

### Play a sound and send a push notification when the quota resets

```json
{
  "on_reset_command": [
    "powershell -Command \"(New-Object Media.SoundPlayer 'C:\\Windows\\Media\\notify.wav').PlaySync()\"",
    "curl -s -d \"token=<APP_TOKEN>&user=<USER_KEY>&title=%USAGE_MONITOR_TITLE%&message=%USAGE_MONITOR_MESSAGE%\" https://api.pushover.net/1/messages.json"
  ]
}
```

### Send a Telegram message when a threshold is crossed

```json
{
  "on_threshold_command": "curl -s -X POST \"https://api.telegram.org/bot<TOKEN>/sendMessage\" -d chat_id=<ID> -d text=%USAGE_MONITOR_MESSAGE%"
}
```

### Play a sound when a threshold is crossed

```json
{
  "on_threshold_command": "powershell -Command \"(New-Object Media.SoundPlayer 'C:\\Windows\\Media\\notify.wav').PlaySync()\""
}
```

Any `.wav` file works - Windows ships with several sounds in `C:\Windows\Media\`. For `.mp3` files:

```json
{
  "on_threshold_command": "powershell -Command \"Add-Type -AssemblyName presentationCore; $p = New-Object System.Windows.Media.MediaPlayer; $p.Open([uri]'C:\\alert.mp3'); $p.Play(); Start-Sleep 3\""
}
```

### Use a script file for complex logic

Different actions depending on quota type and threshold:

```json
{
  "alert_thresholds_five_hour": [80, 95],
  "alert_thresholds_seven_day": [95],
  "on_threshold_command": "powershell -ExecutionPolicy Bypass -File .\\notify.ps1"
}
```

```powershell
# notify.ps1 - different actions depending on quota type and threshold
$variant = $env:USAGE_MONITOR_VARIANT
$threshold = [int]$env:USAGE_MONITOR_THRESHOLD

# Primary (5h) window: play a warning sound at 80%, a critical sound at 95%
if ($variant -eq "five_hour") {
    if ($threshold -ge 95) {
        (New-Object Media.SoundPlayer 'C:\Windows\Media\Windows Critical Stop.wav').PlaySync()
    } elseif ($threshold -ge 80) {
        (New-Object Media.SoundPlayer 'C:\Windows\Media\Windows Notify.wav').PlaySync()
    }
}

# Weekly (7d) window: send a Pushover notification at 95%
if ($variant -eq "seven_day" -and $threshold -ge 95) {
    $body = @{ token = "<APP_TOKEN>"; user = "<USER_KEY>"; title = $env:USAGE_MONITOR_TITLE; message = $env:USAGE_MONITOR_MESSAGE }
    Invoke-WebRequest -Uri "https://api.pushover.net/1/messages.json" -Method POST -Body $body | Out-Null
}
```

## Environment Variables

Commands receive event details as environment variables. Access them with `%VAR%` in cmd.exe or `$env:VAR` in PowerShell.

### Common

Available in all event commands:

| Variable | Example | Description |
|---|---|---|
| `USAGE_MONITOR_VERSION` | `1.0.0` | Running app version |

### `on_reset_command`

Fires whenever usage drops (not only when nearly exhausted).

| Variable | Example | Description |
|---|---|---|
| `USAGE_MONITOR_EVENT` | `reset` | Event type |
| `USAGE_MONITOR_VARIANT` | `five_hour` or `seven_day` | Which quota reset |
| `USAGE_MONITOR_UTILIZATION` | `5` | Current usage of the reset quota (integer) |
| `USAGE_MONITOR_PREV_UTILIZATION` | `98` | Usage before the reset (integer) |
| `USAGE_MONITOR_UTILIZATION_FIVE_HOUR` | `5` | Current primary (5h) usage (integer) |
| `USAGE_MONITOR_UTILIZATION_SEVEN_DAY` | `42` | Current weekly (7d) usage (integer) |
| `USAGE_MONITOR_RESETS_AT` | `2025-01-15T18:00:00Z` | When the quota resets next (ISO 8601, UTC) |
| `USAGE_MONITOR_TITLE` | `Quota Reset` | Notification title (localized) |
| `USAGE_MONITOR_MESSAGE` | `Your quota has been reset...` | Notification message (localized) |

Both quota values are included so your script can check whether you are actually unblocked. For example, the primary (5h) window may reset while the weekly window is still at the limit. Use `USAGE_MONITOR_PREV_UTILIZATION` to filter if you only want to act on significant resets.

### `on_threshold_command`

Fires when usage crosses a configured alert threshold.

| Variable | Example | Description |
|---|---|---|
| `USAGE_MONITOR_EVENT` | `threshold` | Event type |
| `USAGE_MONITOR_VARIANT` | `five_hour`, `seven_day`, `extra_usage` | Which quota is affected |
| `USAGE_MONITOR_UTILIZATION` | `84` | Current usage percentage (integer) |
| `USAGE_MONITOR_THRESHOLD` | `80` | Threshold that was crossed (integer) |
| `USAGE_MONITOR_RESETS_AT` | `2025-01-15T18:00:00Z` | When the quota resets (ISO 8601, UTC) |
| `USAGE_MONITOR_TITLE` | `Usage Alert` | Notification title (localized) |
| `USAGE_MONITOR_MESSAGE` | `Your 5-hour usage has reached 84%` | Notification message (localized) |
| `USAGE_MONITOR_EXTRA_USED` | `$8.20` | Amount spent (extra usage only) |
| `USAGE_MONITOR_EXTRA_LIMIT` | `$10.00` | Monthly limit (extra usage only) |

`USAGE_MONITOR_EXTRA_USED` and `USAGE_MONITOR_EXTRA_LIMIT` are only set when `USAGE_MONITOR_VARIANT` is `extra_usage`.

> [!NOTE]
> The `extra_usage` threshold event, and the `USAGE_MONITOR_EXTRA_USED` / `USAGE_MONITOR_EXTRA_LIMIT` variables, fire only for the legacy used/limit ratio model. They are **not produced for Codex credit balances**: a balance is not a spend amount, so it has no percentage to cross a threshold (see [API Reference](api-reference.md#extra-usage-credits), "Extra usage (credits)").

### `on_startup_command`

Fires once after the first successful usage update following app start (also after using the **Restart** menu option). Receives the full quota state so the command can decide what to do based on which windows are active. Skipped when the first update fails (auth error, offline, no session data) - retries on the next successful poll.

| Variable | Example | Description |
|---|---|---|
| `USAGE_MONITOR_EVENT` | `startup` | Event type |
| `USAGE_MONITOR_UTILIZATION_FIVE_HOUR` | `0` | Current primary (5h) usage (integer) |
| `USAGE_MONITOR_RESETS_AT_FIVE_HOUR` | `2025-01-15T18:00:00Z` | When the 5h window resets, or empty if no window is active |
| `USAGE_MONITOR_UTILIZATION_SEVEN_DAY` | `42` | Current weekly (7d) usage (integer) |
| `USAGE_MONITOR_RESETS_AT_SEVEN_DAY` | `2025-01-20T12:00:00Z` | When the 7d window resets, or empty if no window is active |
| `USAGE_MONITOR_EXTRA_USED` | `$8.20` | Amount spent (only set when extra usage is enabled) |
| `USAGE_MONITOR_EXTRA_LIMIT` | `$10.00` | Monthly limit (only set when extra usage is enabled) |

As above, `USAGE_MONITOR_EXTRA_USED` / `USAGE_MONITOR_EXTRA_LIMIT` belong to the legacy used/limit ratio model and are **not produced for Codex credit balances** (see [API Reference](api-reference.md#extra-usage-credits)).

Per-quota variables are emitted for every quota field the data returns, following the `USAGE_MONITOR_UTILIZATION_<VARIANT>` / `USAGE_MONITOR_RESETS_AT_<VARIANT>` pattern. An empty `USAGE_MONITOR_RESETS_AT_*` indicates that the quota has no active window (either never used, or the previous window has expired).
