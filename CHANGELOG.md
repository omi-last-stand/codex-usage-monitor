# Changelog

All notable changes to Codex Usage Monitor are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Codex Usage Monitor is an adaptation of
[Claude Usage Monitor](https://github.com/omi-last-stand/claude-usage-monitor) for OpenAI Codex,
which is itself a fork of
[Usage Monitor for Claude](https://github.com/jens-duttke/usage-monitor-for-claude) by Jens Duttke.

## [1.0.1] - 2026-06-11

Maintenance release. A fresh full-codebase audit (five parallel deep reviews plus a
clean `pyright` static type check) hardened startup, eventing, and data handling.
No new features, no settings changes required.

### Fixed

- **Reset commands while away**: when a quota deadline passed while you were still
  active and you locked the machine before the next poll, the idle wake never armed
  and `on_reset_command` only fired on your return. The retry now arms from the
  retained deadline state, so the command fires promptly even unattended
- **Language auto-detection** on Windows for Simplified Chinese, Traditional Chinese,
  Hindi, and Indonesian - these locales silently fell back to English despite shipped
  translations. The user-default locale is now read as a BCP-47 tag via the Windows
  API first, with the legacy-name mapping extended as a fallback
- **Single instance vs. elevation**: launching a normal instance while an elevated
  ("Run as administrator") one is running no longer starts a duplicate (double
  polling, double event commands) - the guard now fails closed with a dialog.
  Replacing a running instance can no longer terminate an unrelated process if the
  confirmation dialog sat unanswered while Windows recycled the old PID
- **Startup robustness**: a corrupted or hand-edited `CodexUsageMonitor.ini`
  (UTF-16 re-save, stray `%`) no longer aborts launch - it falls back to defaults;
  the DPI-awareness call no longer kills startup on Windows 10 releases before 1703;
  a widget that fails to open now shows the crash dialog instead of dying silently
- **Settings validation**: an invalid `bg` color (e.g. `navy`, missing `#`, 8-digit
  hex) is rejected with the usual settings warning instead of silently preventing
  the widget and settings windows from ever opening
- **Credentials parsing**: a malformed `auth.json` with a non-string `id_token` no
  longer crashes the poll loop; a UTF-8 BOM (e.g. written by PowerShell 5.1
  `Set-Content -Encoding UTF8`) no longer reads as "no token"
- **Codex CLI probing**: a hanging `codex --version` no longer re-blocks every poll
  and popup refresh for its 10-second timeout (failed probes are cached per binary
  change); an unreadable IDE-extension folder no longer kills the widget's update loop
- **Reset-event state machine**: a near-exhausted window that disappeared from the
  API without a reset deadline can no longer suppress other windows' reset events
  indefinitely (it now ages out after its own window length); threshold notifications
  no longer re-fire every poll when the server lingers past a reset deadline with a
  slightly drifting timestamp; deadline comparisons across the reset logic now share
  one tolerance
- **Polling**: error retries are no longer slowed from 30 s to 120 s by reset
  alignment exactly when a reset is imminent, and a rate-limit backoff is never
  shortened below the server's Retry-After; a backward system-clock step (VM resume,
  manual change, large NTP step) no longer freezes polling and the fetch cooldown
  for the size of the step; a rare race in deferred-notification flushing could
  permanently stop polling
- **Start with Windows** now resolves the real Startup folder via the Windows
  known-folder API, so it works with GPO Folder Redirection / roaming profiles;
  tray **Restart** now keeps command-line flags such as `--verbose`

### Changed

- `requirements.txt` now pins the exact dependency versions the release executable
  is built with, so a from-source build reproduces the released binary
- Docs: corrected the `bg` default color (`#0f1838`) and documented the `bg2`
  background-gradient end; fixed the "check only on weekly resets" example in the
  update-check guide (the variant guard now actually guards)
- Internal: `pyright` type check is clean; 47 new regression tests (953 total)

[Show all code changes](https://github.com/omi-last-stand/codex-usage-monitor/compare/v1.0.0...v1.0.1)

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
