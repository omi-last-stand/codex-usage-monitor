# Changelog

All notable changes to Codex Usage Monitor are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Codex Usage Monitor is an adaptation of
[Claude Usage Monitor](https://github.com/omi-last-stand/claude-usage-monitor) for OpenAI Codex,
which is itself a fork of
[Usage Monitor for Claude](https://github.com/jens-duttke/usage-monitor-for-claude) by Jens Duttke.

## [1.0.0] - 2026-05-26

Initial release of Codex Usage Monitor, adapted from Claude Usage Monitor for OpenAI Codex
(ChatGPT-plan) rate-limit usage.

### Added

- Resident, always-on-top desktop widget plus system-tray icon showing your Codex primary (rolling 5-hour) and secondary (weekly / 7-day) rate-limit usage, with an optional credits line for a credits balance
- Reads Codex CLI OAuth credentials from `~/.codex/auth.json` (honoring `CODEX_HOME`); the access token and account id are used only in HTTP headers
- Polls the live API `GET https://chatgpt.com/backend-api/wham/usage` - the same endpoint the Codex CLI uses for its `/status` rate limits
- Falls back to the newest local session rollout file (`~/.codex/sessions/.../rollout-*.jsonl`) when the API is unavailable, needing no network and no token
- New `usage_source` setting: `"auto"` (API then session files), `"api"` (API only), or `"session"` (local session files only - zero network)
- Account email and ChatGPT plan decoded locally from the id-token JWT (no network call)
- Codex blue-to-violet theme, with a new `bar_fg_start` color key for the usage-bar gradient start
- Compact view with click-to-expand and drag-to-move; the window position and the compact/expanded view are remembered and restored next launch (off-screen coordinates are auto-corrected; the first run opens centered and compact)
- Right-click widget menu: always-on-top toggle, settings, about, quit
- Settings window to choose which blocks are shown and in what order - the account row, each usage bar, the credits line, the Codex CLI version, and the status line - each with show / collapse (shown only when expanded) / hide and drag-to-reorder
- Language selector in the settings window (system default plus all 13 languages); the app restarts automatically to apply the new language
- Smart, time-aware threshold alerts and reset notifications; adaptive polling (faster while active, paused while idle or locked); event commands (run a command on reset, threshold, or startup)
- About dialog with clickable links (a native Win32 task dialog)
- Single standalone executable, `CodexUsageMonitor.exe`; widget state is stored in `CodexUsageMonitor.ini` next to it (optional advanced settings in `usage-monitor-settings.json`, searched in the app dir, then `$CODEX_HOME`, then `~/.codex`); no Windows Registry use

### Notes

- Unlike the Claude version, there is **no automatic token refresh** - `codex login` is interactive. The Codex CLI refreshes `auth.json` on its own while it runs; if the session expires, re-run `codex login`. The app falls back to local session data meanwhile.

[Show all code changes](https://github.com/omi-last-stand/codex-usage-monitor/releases/tag/v1.0.0)
