# Privacy Policy

**Codex Usage Monitor** is a local desktop application that monitors your OpenAI Codex (ChatGPT-plan) rate-limit usage.

## Data Collection

This application does **not** collect, store, or transmit any personal data.

## Network Communication

The application communicates exclusively with `chatgpt.com` to retrieve your current Codex usage data.
No other network connections are made. (When the local-session data source is selected, it makes no
network connection at all.)

## Credentials

The application reads your existing Codex OAuth credentials from the local Codex CLI configuration
file (`~/.codex/auth.json`, or `$CODEX_HOME` if set). From it the app reads the access token and the
ChatGPT account id. These are:

- Used solely in HTTP `Authorization` and `ChatGPT-Account-Id` headers to authenticate with the Codex
  usage API
- Never logged, stored elsewhere, copied, or transmitted to any third party

Your account email and ChatGPT plan are decoded locally from the id-token JWT in the same file; this
involves no network call. The application never refreshes or writes these credentials - token refresh
is handled by the Codex CLI itself (re-run `codex login` if your session expires).

## Local Storage

The application reads, as a fallback when the live API is unavailable, the local Codex session rollout
files (`~/.codex/sessions/.../rollout-*.jsonl`) to obtain the most recent usage snapshot. It only reads
these files; it never modifies them.

The application writes one file, `CodexUsageMonitor.ini`, in the same folder as the executable. It
stores only the widget's display state - window position, the always-on-top preference, and which
usage fields are shown - and never contains credentials, account details, or usage values.

If you enable "Start with Windows", the application also creates a shortcut in your Windows Startup
folder, and removes it when you disable the option. No Windows Registry keys are written.

Usage data itself is kept in memory only and discarded when the application closes. An optional,
user-provided settings file (`usage-monitor-settings.json`) is only ever read, never written.

## Third-Party Services

The application does not integrate with any analytics, tracking, advertising, or telemetry services.

## Contact

For questions about this privacy policy, please open an issue at
https://github.com/omi-last-stand/codex-usage-monitor/issues
