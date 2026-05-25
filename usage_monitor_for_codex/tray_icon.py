"""
Tray Icon
==========

Loads the static brand tray icon.  The app runs as an always-on-top widget
that shows live usage, so the tray icon is a fixed brand mark rather than a
dynamic gauge; its dark badge with a coral glyph stays legible on both light
and dark taskbars.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image

__all__ = ['load_tray_icon']

_ICON_PATH = Path(__file__).parent / 'tray-icon.png'


def load_tray_icon() -> Image.Image:
    """Return the brand tray icon as an RGBA image for pystray."""
    return Image.open(_ICON_PATH).convert('RGBA')
