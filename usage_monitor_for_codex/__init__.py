"""
Codex Usage Monitor
===================

Displays your OpenAI Codex (ChatGPT-plan) rate-limit usage as an
always-on-top desktop widget plus a system tray icon.

Reads the Codex CLI OAuth credentials from the Codex config directory
(requires a Codex CLI login).  Respects ``CODEX_HOME`` if set, otherwise
defaults to ``~/.codex/``.
"""
from __future__ import annotations

__version__ = '1.0.1'
