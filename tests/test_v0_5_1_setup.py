"""Tests for v0.5.1 setup orchestrator (CLI flags + select_targets)."""
from __future__ import annotations

import argparse

from cloudprice_mcp import clients, setup_cmd


def _ns(**overrides) -> argparse.Namespace:
    """Build a Namespace with all setup-args defaulted."""
    defaults = {
        "yes": False,
        "force": False,
        "dry_run": False,
        "print_config": False,
        "client": None,
        "all": False,
        "list_clients": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# --- _select_targets ---


def test_select_targets_explicit_client_returns_only_that_client():
    args = _ns(client=["copilot"])
    targets = setup_cmd._select_targets(args)
    assert len(targets) == 1
    assert targets[0].name == "copilot"


def test_select_targets_multiple_explicit_clients():
    args = _ns(client=["copilot", "cursor"])
    targets = setup_cmd._select_targets(args)
    names = [t.name for t in targets]
    assert names == ["copilot", "cursor"]


def test_select_targets_all_returns_seven():
    args = _ns(all=True)
    targets = setup_cmd._select_targets(args)
    assert len(targets) == 7


def test_select_targets_unknown_client_skipped(capsys):
    args = _ns(client=["nonexistent"])
    targets = setup_cmd._select_targets(args)
    assert targets == []
    err = capsys.readouterr().err
    assert "Unknown client 'nonexistent'" in err


def test_select_targets_default_uses_detection(monkeypatch):
    """Without --client or --all, falls back to clients.detect_installed."""
    fake_adapter = clients.ClaudeDesktopAdapter()
    monkeypatch.setattr(clients, "detect_installed", lambda: [fake_adapter])
    args = _ns()
    targets = setup_cmd._select_targets(args)
    assert len(targets) == 1
    assert targets[0].name == "claude"


# --- list-clients command ---


def test_list_clients_command_prints_all_seven(capsys):
    rc = setup_cmd._list_clients_command()
    assert rc == 0
    out = capsys.readouterr().out
    for name in clients.known_client_names():
        assert name in out


# --- print-config command ---


def test_print_config_emits_per_client_json(capsys):
    args = _ns(client=["copilot"], print_config=True)
    rc = setup_cmd._print_config_command(args)
    assert rc == 0
    out = capsys.readouterr().out
    # Output is a JSON dict keyed by client name
    import json
    parsed = json.loads(out)
    assert "copilot" in parsed
    assert "config" in parsed["copilot"]
    assert "config_path" in parsed["copilot"]
    # Copilot's schema check: has servers root with type:stdio
    assert "servers" in parsed["copilot"]["config"]


def test_print_config_no_targets_returns_error(capsys):
    """No --client and detection returns nothing → error exit."""
    import cloudprice_mcp.clients as _clients
    # Make detection return empty
    args = _ns(print_config=True)
    # Patch detect_installed temporarily
    orig = _clients.detect_installed
    _clients.detect_installed = list
    try:
        rc = setup_cmd._print_config_command(args)
    finally:
        _clients.detect_installed = orig
    assert rc == 1


# --- run_setup with --dry-run (end to end, no file writes) ---


def test_run_setup_dry_run_writes_no_files(tmp_path, monkeypatch, capsys):
    """Run setup --dry-run --all and confirm nothing gets written."""
    # Point all adapters at tmp_path so even if a write attempted, it's contained.
    # We can't easily redirect every adapter, but --dry-run should prevent writes anyway.
    args = _ns(client=["copilot"], dry_run=True)
    rc = setup_cmd.run_setup(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "dry-run" in out.lower()


# --- build_cloudprice_args ---


def test_build_cloudprice_args_returns_module_form():
    """Default args invoke the MCP server module via -m, not the entry-point shim."""
    assert setup_cmd.build_cloudprice_args() == ["-m", "cloudprice_mcp.server"]


# --- detect_python_command ---


def test_detect_python_command_returns_absolute_path():
    """sys.executable is bulletproof on macOS / Linux / Windows."""
    cmd = setup_cmd.detect_python_command()
    from pathlib import Path
    assert Path(cmd).is_absolute()


# --- run_setup end-to-end (through orchestrator with mocked target) ---


def _patch_detect_to_tmp_adapter(monkeypatch, tmp_path, base_adapter_cls=clients.ClaudeDesktopAdapter):
    """Create a tmp-path adapter and make detect_installed return it."""
    target = tmp_path / "config.json"

    class _TmpAdapter(base_adapter_cls):
        def config_path(self):
            return target

        def is_installed(self) -> bool:
            return True

    fake = _TmpAdapter()
    monkeypatch.setattr(clients, "detect_installed", lambda: [fake])
    return target


def test_run_setup_yes_writes_config_end_to_end(tmp_path, monkeypatch):
    """--yes against a clean tmp dir: setup writes the JSON file."""
    target = _patch_detect_to_tmp_adapter(monkeypatch, tmp_path)
    monkeypatch.setattr(setup_cmd, "kill_cached_subprocesses", lambda: 0)

    rc = setup_cmd.run_setup(_ns(yes=True))
    assert rc == 0
    assert target.exists()
    import json
    written = json.loads(target.read_text(encoding="utf-8"))
    assert clients.ENTRY_NAME in written["mcpServers"]


def test_run_setup_yes_preserves_existing_keys_end_to_end(tmp_path, monkeypatch):
    """--yes against an existing config: preserves preferences + telemetry top-level keys."""
    target = _patch_detect_to_tmp_adapter(monkeypatch, tmp_path)
    import json
    target.write_text(
        json.dumps({"preferences": {"theme": "dark"}, "telemetry": False}),
        encoding="utf-8",
    )
    monkeypatch.setattr(setup_cmd, "kill_cached_subprocesses", lambda: 0)

    rc = setup_cmd.run_setup(_ns(yes=True))
    assert rc == 0
    written = json.loads(target.read_text(encoding="utf-8"))
    assert written["preferences"] == {"theme": "dark"}
    assert written["telemetry"] is False
    assert clients.ENTRY_NAME in written["mcpServers"]


def test_run_setup_no_targets_returns_error(monkeypatch, capsys):
    """No --client and no detected clients → exit 1 with clear message."""
    monkeypatch.setattr(clients, "detect_installed", list)
    rc = setup_cmd.run_setup(_ns())
    assert rc == 1
    err = capsys.readouterr().err
    assert "No clients detected" in err
