"""
Task Dialog
===========

Thin ``ctypes`` wrapper around the Win32 ``TaskDialogIndirect`` API.  Unlike
the classic ``MessageBox``, a task dialog can render clickable hyperlinks, so
the About box can show real links instead of bare URL text.

``TaskDialogIndirect`` lives in the common-controls v6 side-by-side assembly,
which is declared in both CPython's and PyInstaller's application manifests.
When it is unavailable or fails, callers fall back to a plain ``MessageBox``
with the hyperlink markup stripped down to its visible text.
"""
from __future__ import annotations

import ctypes
import re
from collections.abc import Callable
from ctypes import wintypes

# TASKDIALOGCONFIG.dwFlags
_TDF_ENABLE_HYPERLINKS = 0x0001
_TDF_ALLOW_DIALOG_CANCELLATION = 0x0008
_TDF_POSITION_RELATIVE_TO_WINDOW = 0x1000

# TASKDIALOG_COMMON_BUTTON_FLAGS
_TDCBF_OK_BUTTON = 0x0001

# Task dialog notification codes (sent to the callback)
_TDN_HYPERLINK_CLICKED = 3

# Stock icon: MAKEINTRESOURCEW(-3) == 0xFFFD, the blue "information" glyph
_TD_INFORMATION_ICON = 0xFFFD

# MessageBoxW uType: MB_OK | MB_ICONINFORMATION
_MB_ICONINFORMATION = 0x40

_S_OK = 0

# LONG_PTR: a pointer-sized signed integer.
_LONG_PTR = ctypes.c_ssize_t

# HRESULT (CALLBACK *PFTASKDIALOGCALLBACK)(HWND, UINT, WPARAM, LPARAM, LONG_PTR)
_PFTASKDIALOGCALLBACK = ctypes.WINFUNCTYPE(
    ctypes.c_long,
    wintypes.HWND,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
    _LONG_PTR,
)

_LINK_RE = re.compile(r'<a href="[^"]*">(.*?)</a>', re.DOTALL)


class _TASKDIALOGCONFIG(ctypes.Structure):
    # commctrl.h wraps this struct in ``#include <pshpack1.h>`` (1-byte
    # packing).  Natural alignment would insert 4 bytes of padding after
    # ``cbSize`` and shift every pointer that follows on 64-bit, corrupting
    # the whole struct -- so pack tightly to match the C ABI.
    _pack_ = 1
    _fields_ = [
        ('cbSize', wintypes.UINT),
        ('hwndParent', wintypes.HWND),
        ('hInstance', wintypes.HINSTANCE),
        ('dwFlags', wintypes.UINT),
        ('dwCommonButtons', wintypes.UINT),
        ('pszWindowTitle', wintypes.LPCWSTR),
        ('pszMainIcon', ctypes.c_void_p),            # union: hMainIcon / pszMainIcon
        ('pszMainInstruction', wintypes.LPCWSTR),
        ('pszContent', wintypes.LPCWSTR),
        ('cButtons', wintypes.UINT),
        ('pButtons', ctypes.c_void_p),
        ('nDefaultButton', ctypes.c_int),
        ('cRadioButtons', wintypes.UINT),
        ('pRadioButtons', ctypes.c_void_p),
        ('nDefaultRadioButton', ctypes.c_int),
        ('pszVerificationText', wintypes.LPCWSTR),
        ('pszExpandedInformation', wintypes.LPCWSTR),
        ('pszExpandedControlText', wintypes.LPCWSTR),
        ('pszCollapsedControlText', wintypes.LPCWSTR),
        ('pszFooterIcon', ctypes.c_void_p),          # union: hFooterIcon / pszFooterIcon
        ('pszFooter', wintypes.LPCWSTR),
        ('pfCallback', _PFTASKDIALOGCALLBACK),
        ('lpCallbackData', _LONG_PTR),
        ('cxWidth', wintypes.UINT),
    ]


def _strip_hyperlinks(content: str) -> str:
    """Replace ``<a href="...">text</a>`` markup with its visible *text*."""
    return _LINK_RE.sub(lambda m: m.group(1), content)


def _show_message_box(parent_hwnd: int | None, title: str, heading: str, content: str) -> bool:
    """Fallback: a plain MessageBox with hyperlink markup stripped to text."""
    body = _strip_hyperlinks(content)
    text = f'{heading}\n\n{body}' if heading else body
    ctypes.windll.user32.MessageBoxW(parent_hwnd or 0, text, title, _MB_ICONINFORMATION)
    return False


def show_info_dialog(
    parent_hwnd: int | None,
    title: str,
    heading: str,
    content: str,
    *,
    on_link: Callable[[str], None] | None = None,
) -> bool:
    """Show a native info dialog; return ``True`` if shown as a task dialog.

    *content* may embed ``<a href="URL">text</a>`` hyperlinks.  Clicking one
    calls *on_link* with its URL and leaves the dialog open.  When the task
    dialog API is unavailable or fails, falls back to a plain ``MessageBox``
    (links rendered as text) and returns ``False``.
    """
    try:
        task_dialog_indirect = ctypes.windll.comctl32.TaskDialogIndirect
    except (AttributeError, OSError):
        return _show_message_box(parent_hwnd, title, heading, content)

    task_dialog_indirect.restype = ctypes.c_long
    task_dialog_indirect.argtypes = [
        ctypes.POINTER(_TASKDIALOGCONFIG),
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(wintypes.BOOL),
    ]

    def _on_notify(hwnd, msg, wparam, lparam, ref_data):  # noqa: ANN001 - Win32 callback
        if msg == _TDN_HYPERLINK_CLICKED and on_link is not None:
            try:
                on_link(ctypes.wstring_at(lparam))
            except Exception:
                pass  # never let a browser-launch error crash the UI thread
        return _S_OK

    # Keep a reference for the duration of the (synchronous) call so the
    # trampoline is not garbage-collected while the dialog is open.
    callback = _PFTASKDIALOGCALLBACK(_on_notify)

    config = _TASKDIALOGCONFIG()
    config.cbSize = ctypes.sizeof(_TASKDIALOGCONFIG)
    config.hwndParent = parent_hwnd or None
    config.dwFlags = (
        _TDF_ENABLE_HYPERLINKS
        | _TDF_ALLOW_DIALOG_CANCELLATION
        | _TDF_POSITION_RELATIVE_TO_WINDOW
    )
    config.dwCommonButtons = _TDCBF_OK_BUTTON
    config.pszWindowTitle = title
    config.pszMainIcon = _TD_INFORMATION_ICON
    config.pszMainInstruction = heading
    config.pszContent = content
    config.pfCallback = callback

    pressed = ctypes.c_int()
    try:
        hr = task_dialog_indirect(ctypes.byref(config), ctypes.byref(pressed), None, None)
    except OSError:
        return _show_message_box(parent_hwnd, title, heading, content)

    if hr != _S_OK:
        return _show_message_box(parent_hwnd, title, heading, content)
    return True
