# Configuration

All settings work out of the box - no configuration file is needed. To customize behavior, create a file called `usage-monitor-settings.json` with only the keys you want to change:

```json
{
  "poll_interval": 180,
  "usage_source": "auto",
  "bar_fg": "#8b6cff",
  "bar_fg_warn": "#ff5470"
}
```

The app searches for this file in these locations (first match wins):

1. **Next to the EXE** (or project root when running from source)
2. **`$CODEX_HOME/usage-monitor-settings.json`** (only if `CODEX_HOME` is set and differs from `~/.codex/`)
3. **`~/.codex/usage-monitor-settings.json`**

The app never creates or modifies this file. To start, create an empty file and add keys as needed. Settings are read at startup - after editing the file, use the **Restart** option in the tray context menu to apply changes.

## Usage data source

Codex usage can come from two places. The live API gives the freshest numbers; the local session files need no network and no token, but are only as fresh as your last Codex turn.

| Key | Default | Description |
|-----|---------|-------------|
| `usage_source` | `"auto"` | Where to read usage from: `"auto"`, `"api"`, or `"session"` |

- **`"auto"`** (default) - try the live API (`https://chatgpt.com/backend-api/wham/usage`) first, then fall back to the newest local session rollout file if the API is unavailable (offline, token expired, etc.).
- **`"api"`** - use only the live API. Requires a valid access token in `~/.codex/auth.json`; never reads session files.
- **`"session"`** - use only the local session rollout files (`~/.codex/sessions/.../rollout-*.jsonl`). Makes **no network connection** and needs no token. The numbers are as fresh as your most recent Codex turn.

An unrecognized value falls back to `"auto"`. See [API Reference](api-reference.md) for the underlying data sources.

## Alert thresholds

Configure usage percentage thresholds that trigger Windows notifications. The primary (5-hour) and weekly (7-day) windows have separate thresholds since their time horizons differ significantly. Set to an empty array `[]` to disable alerts for a specific quota type.

| Key | Default | Description |
|-----|---------|-------------|
| `alert_thresholds_five_hour` | `[50, 80, 95]` | Thresholds (%) for the primary 5-hour window |
| `alert_thresholds_seven_day` | `[95]` | Thresholds (%) for the weekly (7-day) window |
| `alert_thresholds_extra_usage` | `[50, 80, 95]` | Thresholds (%) for Extra Usage (credits balance) |
| `alert_time_aware` | `true` | Only alert when usage outpaces elapsed time |
| `alert_time_aware_below` | `90` | Time-aware check applies only to thresholds below this value; thresholds at or above always fire |

> [!NOTE]
> `alert_thresholds_extra_usage` applies only to the legacy used/limit ratio model. It is **inactive for Codex credit balances**, which are shown as a balance or "Unlimited" with no spend percentage to compare against a threshold (see [API Reference](api-reference.md#extra-usage-credits), "Extra usage (credits)").

Threshold lookup uses a fallback chain: exact match (e.g. `alert_thresholds_seven_day`), then base period, then no alerts. This lets you configure stricter thresholds per variant when needed:

```json
{
    "alert_thresholds_seven_day": [50, 80, 95]
}
```

## Tooltip fields

The tray tooltip shows a quick usage summary when you hover over the icon. By default, it displays the primary (5h) and weekly (7d) windows. Use `tooltip_fields` to choose which usage fields appear in the tooltip.

| Key | Default | Description |
|-----|---------|-------------|
| `tooltip_fields` | `["five_hour", "seven_day"]` | Which usage fields to show in the tray tooltip, in order |

Must be an array of non-empty strings. Duplicates are silently removed. An empty array `[]` is valid (tooltip shows only the title, no usage fields). Unknown field names are accepted - if a field is `null` or missing from the data, it is simply skipped.

**Known field names:** `five_hour` (the primary window), `seven_day` (the weekly window). The Codex API exposes two windows (`primary` and `secondary`), which the app maps to these keys by their length (see [API Reference](api-reference.md)).

**Example** - show only the primary (5h) window in the tooltip:

```json
{
    "tooltip_fields": ["five_hour"]
}
```

## Popup fields

The popup shows usage bars for all active quota types by default. Use `popup_fields` to control which bars appear and in what order.

| Key | Default | Description |
|-----|---------|-------------|
| `popup_fields` | `["*"]` | Which usage fields to show in the popup, in order. `"*"` is a wildcard meaning "all remaining non-null fields in default order" |

Must be an array of non-empty strings. `"*"` may appear at most once. Duplicates are silently removed. Unknown field names are accepted - if a field is `null` or missing from the data, it is simply skipped.

**Known field names:** `five_hour` (the primary window), `seven_day` (the weekly window).

**Default order** (used for `"*"` and when no setting is present): shorter periods first (`hour` before `day`).

**Examples:**

| Setting | Result |
|---------|--------|
| *(not set)* | All non-null fields in default order |
| `["five_hour", "seven_day"]` | Only these two, everything else hidden |
| `["seven_day", "*"]` | Weekly first, then all remaining |
| `["*"]` | Same as not set |

```json
{
    "popup_fields": ["five_hour", "seven_day"]
}
```

## Event commands

Run a shell command when a usage event occurs. See [Event Commands](event-commands.md) for examples and available environment variables.

| Key | Default | Description |
|-----|---------|-------------|
| `on_reset_command` | *(none)* | Shell command (or array of commands) to run when a quota resets (usage drops) |
| `on_startup_command` | *(none)* | Shell command (or array of commands) to run once after the first successful usage update following app start |
| `on_threshold_command` | *(none)* | Shell command (or array of commands) to run when usage crosses a configured alert threshold |

## Polling intervals

| Key | Default | Description |
|-----|---------|-------------|
| `poll_interval` | `180` | Seconds between usage updates |
| `poll_fast` | `120` | Seconds when usage is actively increasing |
| `poll_fast_extra` | `2` | Extra fast polls after usage stops increasing |
| `poll_error` | `30` | Seconds after a transient error (5xx, network). Rate-limit errors (429) use exponential backoff instead |
| `max_backoff` | `900` | Maximum backoff in seconds for rate-limit errors (15 min) |
| `idle_pause` | `300` | Seconds of inactivity before polling pauses (0 = disable). Polling also pauses when the workstation is locked |

When `usage_source` is `"session"`, the app reads local files only and never makes a network request, but the same intervals govern how often it re-reads them.

## Language

| Key | Default | Description |
|-----|---------|-------------|
| `language` | *(auto-detected)* | Override the UI language with a language code. Available: `de`, `en`, `es`, `fr`, `hi`, `id`, `it`, `ja`, `ko`, `pt-BR`, `uk`, `zh-CN`, `zh-TW` |

You can also pick the language from the **Settings** window (right-click the widget); the app restarts automatically to apply the new language. That choice is saved to `CodexUsageMonitor.ini` and takes precedence over this JSON key; both fall back to the detected system locale.

## Currency

The Codex usage data does not include currency information, so the app detects the currency symbol from your Windows locale settings. If your Windows locale currency differs from the currency OpenAI bills you in, you can override just the symbol here. Number formatting (decimal separator, symbol position) always follows your system locale.

| Key | Default | Description |
|-----|---------|-------------|
| `currency_symbol` | *(auto-detected)* | Override the auto-detected currency symbol (e.g., `"$"`, `"€"`, `"¥"`) |

## Popup colors

The defaults are the signature Codex palette: a near-black indigo background with a blue-to-violet gradient on the usage bars.

| Key | Default | Description |
|-----|---------|-------------|
| `bg` | `"#12101b"` | Background |
| `fg` | `"#cbc9d6"` | Text |
| `fg_dim` | `"#8a879c"` | Dimmed text (labels, reset times) |
| `fg_heading` | `"#ffffff"` | Section headings |
| `fg_link` | `"#9d8cff"` | Link text (e.g. changelog) |
| `bar_bg` | `"#2a2740"` | Progress bar background |
| `bar_fg_start` | `"#46a6ff"` | Progress bar fill — gradient start (blue) |
| `bar_fg` | `"#8b6cff"` | Progress bar fill — gradient end (violet), also the solid accent for borders/toggles |
| `bar_fg_warn` | `"#ff5470"` | Progress bar fill when usage outpaces elapsed time, error text |
| `bar_divider` | `"#000c"` | Midnight divider on weekly progress bars |
| `bar_marker` | `"#fffc"` | Time-position marker on progress bars |

Each usage bar is drawn as a gradient from `bar_fg_start` to `bar_fg`. Set both to the same value for a flat, single-color fill.

## Widget

The monitor runs as a resident, always-on-top desktop widget. Click it to toggle the compact/expanded view; right-click for its menu (always on top, settings, about, quit).

Which blocks appear - the account row, each usage bar, the extra-usage bar, the Codex CLI version, and the status line - and their order are chosen in the widget's **Settings** window (show / collapse / hide, with drag-to-reorder), not in this JSON file. The window position, the always-on-top state, the compact/expanded view, and that per-block configuration are saved to `CodexUsageMonitor.ini` next to the EXE.
