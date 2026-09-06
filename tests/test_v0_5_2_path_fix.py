"""Tests for v0.5.2 path_fix module + fix-path CLI command.

Strategy: tests run on all platforms but mock winreg / sys.platform to exercise
both Windows and non-Windows behavior.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

from cloudprice_mcp import fix_path_cmd, path_fix

# --- get_scripts_dir / shim_path ---


def test_get_scripts_dir_returns_absolute_path():
    p = path_fix.get_scripts_dir()
    assert p.is_absolute()


def test_shim_path_has_correct_name_per_platform():
    p = path_fix.shim_path()
    if sys.platform == "win32":
        assert p.name == "cloudprice-mcp.exe"
    else:
        assert p.name == "cloudprice-mcp"


# --- is_on_current_path ---


def test_is_on_current_path_true_for_directory_in_env(monkeypatch, tmp_path):
    sep = ";" if sys.platform == "win32" else ":"
    monkeypatch.setenv("PATH", f"{tmp_path}{sep}/some/other/dir")
    assert path_fix.is_on_current_path(tmp_path)


def test_is_on_current_path_false_when_absent(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", "/some/other/dir")
    assert not path_fix.is_on_current_path(tmp_path)


def test_is_on_current_path_handles_trailing_slashes(monkeypatch, tmp_path):
    sep = ";" if sys.platform == "win32" else ":"
    # Path with trailing separator should still match
    monkeypatch.setenv("PATH", f"{tmp_path}/{sep}/some/other")
    assert path_fix.is_on_current_path(tmp_path)


# --- Non-Windows guards ---


def test_add_to_user_path_raises_on_non_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(RuntimeError, match="Windows-only"):
        path_fix.add_to_user_path(tmp_path)


def test_remove_from_user_path_raises_on_non_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    with pytest.raises(RuntimeError, match="Windows-only"):
        path_fix.remove_from_user_path(tmp_path)


def test_is_in_user_path_returns_false_on_non_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    assert not path_fix.is_in_user_path(tmp_path)


# --- Windows registry behavior (mocked) ---


@pytest.fixture
def fake_user_path(monkeypatch):
    """Mock _read_user_path / _write_user_path / broadcast to a mutable holder."""
    holder = {"value": "C:\\Windows;C:\\Windows\\System32"}

    def fake_read():
        return holder["value"]

    def fake_write(value):
        holder["value"] = value

    def fake_broadcast():
        pass

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(path_fix, "_read_user_path", fake_read)
    monkeypatch.setattr(path_fix, "_write_user_path", fake_write)
    monkeypatch.setattr(path_fix, "_broadcast_environment_change", fake_broadcast)
    return holder


def test_add_to_user_path_appends_when_missing(fake_user_path):
    target = Path("C:\\Python311\\Scripts")
    added = path_fix.add_to_user_path(target)
    assert added is True
    assert "C:\\Python311\\Scripts" in fake_user_path["value"]
    assert fake_user_path["value"].startswith("C:\\Windows")  # original kept


def test_add_to_user_path_skips_when_already_present(fake_user_path):
    fake_user_path["value"] = "C:\\Windows;C:\\Python311\\Scripts;C:\\Windows\\System32"
    target = Path("C:\\Python311\\Scripts")
    added = path_fix.add_to_user_path(target)
    assert added is False
    # Path unchanged
    assert fake_user_path["value"] == "C:\\Windows;C:\\Python311\\Scripts;C:\\Windows\\System32"


def test_add_to_user_path_case_insensitive_match(fake_user_path):
    """Windows PATH is case-insensitive — matching mixed case shouldn't double-add."""
    fake_user_path["value"] = "C:\\Python311\\Scripts"
    target = Path("c:\\python311\\scripts")
    added = path_fix.add_to_user_path(target)
    assert added is False


def test_remove_from_user_path_removes_when_present(fake_user_path):
    fake_user_path["value"] = "C:\\Windows;C:\\Python311\\Scripts;C:\\Windows\\System32"
    target = Path("C:\\Python311\\Scripts")
    removed = path_fix.remove_from_user_path(target)
    assert removed is True
    assert "C:\\Python311\\Scripts" not in fake_user_path["value"]
    assert "C:\\Windows" in fake_user_path["value"]
    assert "C:\\Windows\\System32" in fake_user_path["value"]


def test_remove_from_user_path_noop_when_absent(fake_user_path):
    fake_user_path["value"] = "C:\\Windows;C:\\Windows\\System32"
    target = Path("C:\\Python311\\Scripts")
    removed = path_fix.remove_from_user_path(target)
    assert removed is False
    # Path unchanged
    assert fake_user_path["value"] == "C:\\Windows;C:\\Windows\\System32"


def test_is_in_user_path_true_when_present(fake_user_path):
    fake_user_path["value"] = "C:\\Windows;C:\\Python311\\Scripts"
    assert path_fix.is_in_user_path(Path("C:\\Python311\\Scripts"))


def test_is_in_user_path_false_when_absent(fake_user_path):
    fake_user_path["value"] = "C:\\Windows"
    assert not path_fix.is_in_user_path(Path("C:\\Python311\\Scripts"))


# --- fix_path_cmd CLI behavior ---


def _ns(check=False, remove=False, yes=False):
    return argparse.Namespace(check=check, remove=remove, yes=yes)


def test_run_fix_path_non_windows_is_noop(monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "linux")
    rc = fix_path_cmd.run_fix_path(_ns())
    assert rc == 0
    out = capsys.readouterr().out
    assert "Windows" in out


def test_run_fix_path_check_passes_when_on_persistent_path(fake_user_path, monkeypatch, capsys):
    scripts = path_fix.get_scripts_dir()
    fake_user_path["value"] = f"C:\\Windows;{scripts}"
    monkeypatch.setattr(path_fix, "shim_exists", lambda: True)
    rc = fix_path_cmd.run_fix_path(_ns(check=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "in your persistent user PATH" in out


def test_run_fix_path_check_fails_when_not_on_persistent_path(fake_user_path, monkeypatch, capsys):
    fake_user_path["value"] = "C:\\Windows"
    monkeypatch.setattr(path_fix, "shim_exists", lambda: True)
    rc = fix_path_cmd.run_fix_path(_ns(check=True))
    assert rc == 1
    out = capsys.readouterr().out
    assert "NOT in your persistent" in out


def test_run_fix_path_yes_adds_path(fake_user_path, monkeypatch, capsys):
    fake_user_path["value"] = "C:\\Windows"
    monkeypatch.setattr(path_fix, "shim_exists", lambda: True)
    rc = fix_path_cmd.run_fix_path(_ns(yes=True))
    assert rc == 0
    assert str(path_fix.get_scripts_dir()) in fake_user_path["value"]
    out = capsys.readouterr().out
    assert "Added" in out


def test_run_fix_path_remove_undoes_addition(fake_user_path, monkeypatch, capsys):
    scripts = path_fix.get_scripts_dir()
    fake_user_path["value"] = f"C:\\Windows;{scripts};C:\\Other"
    monkeypatch.setattr(path_fix, "shim_exists", lambda: True)
    rc = fix_path_cmd.run_fix_path(_ns(remove=True))
    assert rc == 0
    assert str(scripts) not in fake_user_path["value"]
    assert "C:\\Windows" in fake_user_path["value"]
    assert "C:\\Other" in fake_user_path["value"]


def test_run_fix_path_already_present_is_idempotent(fake_user_path, monkeypatch, capsys):
    scripts = path_fix.get_scripts_dir()
    fake_user_path["value"] = f"C:\\Windows;{scripts}"
    monkeypatch.setattr(path_fix, "shim_exists", lambda: True)
    before = fake_user_path["value"]
    rc = fix_path_cmd.run_fix_path(_ns(yes=True))
    assert rc == 0
    assert fake_user_path["value"] == before  # unchanged
    out = capsys.readouterr().out
    assert "already in your persistent" in out
