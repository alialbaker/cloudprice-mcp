"""Windows PATH helper — adds Python's Scripts/ folder to user PATH.

Why this exists: `pip install cloudprice-mcp` puts `cloudprice-mcp.exe` in Python's
Scripts/ folder. The Python installer's "Add to PATH" checkbox often only adds
`python.exe`, not `Scripts/`, so the cloudprice-mcp shim is invisible to PowerShell
and cmd. Result: users hit "command not recognized" even though install succeeded.

This module:
  - Detects the Scripts folder via sysconfig.get_path('scripts')
  - Reads HKCU\\Environment Path (the user-persistent PATH on Windows)
  - Appends the Scripts folder if missing
  - Broadcasts WM_SETTINGCHANGE so newly-spawned shells pick up the change

macOS / Linux: no-op. The pip-install location (~/.local/bin or /usr/local/bin) is
typically already on PATH, and modifying shell rc files is too invasive to do
silently.
"""
from __future__ import annotations

import os
import sys
import sysconfig
from pathlib import Path


def get_scripts_dir() -> Path:
    """Absolute path to the Python interpreter's Scripts directory (where pip puts shims)."""
    return Path(sysconfig.get_path("scripts"))


def shim_path() -> Path:
    """Path where pip would have installed the cloudprice-mcp shim."""
    if sys.platform == "win32":
        return get_scripts_dir() / "cloudprice-mcp.exe"
    return get_scripts_dir() / "cloudprice-mcp"


def shim_exists() -> bool:
    """True if the cloudprice-mcp shim exists at the pip-install location."""
    return shim_path().exists()


def _normalize(path_str: str) -> str:
    """Normalize a path for comparison — trailing slashes off, lowercased on Windows."""
    s = path_str.rstrip("\\/")
    return s.lower() if sys.platform == "win32" else s


def is_on_current_path(directory: Path) -> bool:
    """Whether `directory` appears in this process's PATH environment variable."""
    target = _normalize(str(directory))
    parts = [_normalize(p) for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    return target in parts


# --- Windows registry-based persistent PATH editing ---


def _read_user_path() -> str:
    """Read the user's persistent PATH from HKCU\\Environment. Returns '' if missing."""
    import winreg
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ
    ) as key:
        try:
            value, _type = winreg.QueryValueEx(key, "Path")
            return value
        except FileNotFoundError:
            return ""


def _write_user_path(value: str) -> None:
    """Write HKCU\\Environment\\Path. Uses REG_EXPAND_SZ so %VARS% expand correctly."""
    import winreg
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_ALL_ACCESS
    ) as key:
        winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, value)


def _broadcast_environment_change() -> None:
    """Send WM_SETTINGCHANGE so explorer + new shells refresh their environment.

    Existing shells won't pick up the change (Windows limitation) — they need to
    be restarted. New shells launched after this call will see the new PATH.
    """
    import ctypes
    from ctypes import wintypes
    HWND_BROADCAST = 0xFFFF
    WM_SETTINGCHANGE = 0x001A
    SMTO_ABORTIFHUNG = 0x0002
    result = wintypes.DWORD()
    ctypes.windll.user32.SendMessageTimeoutW(
        HWND_BROADCAST,
        WM_SETTINGCHANGE,
        0,
        ctypes.c_wchar_p("Environment"),
        SMTO_ABORTIFHUNG,
        5000,
        ctypes.byref(result),
    )


def is_in_user_path(directory: Path) -> bool:
    """Whether `directory` is in the user's persistent (registry) PATH on Windows."""
    if sys.platform != "win32":
        return False
    target = _normalize(str(directory))
    parts = [_normalize(p) for p in _read_user_path().split(";") if p]
    return target in parts


def add_to_user_path(directory: Path) -> bool:
    """Append `directory` to the user's persistent PATH if not already there.

    Returns True if the entry was added, False if it was already present.
    Raises RuntimeError on non-Windows platforms.
    """
    if sys.platform != "win32":
        raise RuntimeError("add_to_user_path is Windows-only")
    if is_in_user_path(directory):
        return False
    current = _read_user_path()
    new_value = (current + ";" + str(directory)) if current else str(directory)
    _write_user_path(new_value)
    _broadcast_environment_change()
    return True


def remove_from_user_path(directory: Path) -> bool:
    """Remove `directory` from the user's persistent PATH.

    Returns True if removed, False if not present. Raises RuntimeError on non-Windows.
    """
    if sys.platform != "win32":
        raise RuntimeError("remove_from_user_path is Windows-only")
    target = _normalize(str(directory))
    current = _read_user_path()
    parts = [p for p in current.split(";") if p]
    kept = [p for p in parts if _normalize(p) != target]
    if len(kept) == len(parts):
        return False
    _write_user_path(";".join(kept))
    _broadcast_environment_change()
    return True
