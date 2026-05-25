"""
Tray Icon Tests
================

Unit tests for the static brand tray icon loader.
"""
from __future__ import annotations

import unittest

from PIL import Image

import usage_monitor_for_codex.tray_icon as tray_icon_mod


class TestLoadTrayIcon(unittest.TestCase):
    """Tests for load_tray_icon()."""

    def test_returns_rgba_image(self):
        """The brand icon loads as an RGBA image."""
        img = tray_icon_mod.load_tray_icon()

        self.assertIsInstance(img, Image.Image)
        self.assertEqual(img.mode, 'RGBA')

    def test_is_non_empty_square(self):
        """The icon is a non-empty square bitmap."""
        img = tray_icon_mod.load_tray_icon()
        width, height = img.size

        self.assertEqual(width, height)
        self.assertGreater(width, 0)

    def test_has_opaque_pixels(self):
        """The icon has opaque (non-transparent) pixels so it shows in the tray."""
        img = tray_icon_mod.load_tray_icon()
        _min_alpha, max_alpha = img.getchannel('A').getextrema()

        self.assertGreater(max_alpha, 0)

    def test_asset_file_bundled(self):
        """The brand icon asset sits next to the module so PyInstaller bundles it."""
        self.assertTrue(tray_icon_mod._ICON_PATH.is_file())


if __name__ == '__main__':
    unittest.main()
