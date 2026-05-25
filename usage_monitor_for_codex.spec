# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for Codex Usage Monitor.

Build:
  pyinstaller usage_monitor_for_codex.spec
"""

a = Analysis(
    ['usage_monitor_for_codex/__main__.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('locale/*.json', 'locale'),
        ('usage_monitor_for_codex/tray-icon.png', 'usage_monitor_for_codex'),
        ('usage_monitor_for_codex/popup/popup.html', 'usage_monitor_for_codex/popup'),
        ('usage_monitor_for_codex/popup/popup.css', 'usage_monitor_for_codex/popup'),
        ('usage_monitor_for_codex/popup/popup.js', 'usage_monitor_for_codex/popup'),
        ('usage_monitor_for_codex/popup/settings.html', 'usage_monitor_for_codex/popup'),
        ('usage_monitor_for_codex/popup/settings.css', 'usage_monitor_for_codex/popup'),
        ('usage_monitor_for_codex/popup/settings.js', 'usage_monitor_for_codex/popup'),
    ],
    hiddenimports=[
        'pystray._win32',
        'pystray._util',
        'pystray._util.win32',
        'webview',
        'webview.platforms.edgechromium',
        'clr_loader',
        'pythonnet',
        'bottle',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'unittest', 'test',
        'xmlrpc', 'pydoc',
        'tkinter', '_tkinter',
        'PIL._avif', 'PIL._webp',
        'PIL._imagingcms', 'PIL._imagingmath', 'PIL._imagingtk', 'PIL._imagingmorph',
        'setuptools', '_distutils_hack',
        'asyncio', 'concurrent',
        'multiprocessing',
        'xml', 'tomllib',
        'sqlite3',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='CodexUsageMonitor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    icon='codex_usage_monitor.ico',
    version='version_info.py',
)
