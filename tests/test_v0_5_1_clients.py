"""Tests for v0.5.1 multi-client adapter layer."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from cloudprice_mcp import clients
from cloudprice_mcp.clients import (
    ENTRY_NAME,
    ClaudeDesktopAdapter,
    ClineAdapter,
    ContinueAdapter,
    CopilotAdapter,
    CursorAdapter,
    WindsurfAdapter,
    ZedAdapter,
)

# --- Registry ---


def test_known_client_names_returns_seven():
    names = clients.known_client_names()
    assert len(names) == 7
    assert set(names) == {"claude", "copilot", "cursor", "windsurf", "cline", "continue", "zed"}


def test_adapter_by_name_returns_correct_class():
    assert isinstance(clients.adapter_by_name("claude"), ClaudeDesktopAdapter)
    assert isinstance(clients.adapter_by_name("copilot"), CopilotAdapter)
    assert isinstance(clients.adapter_by_name("zed"), ZedAdapter)


def test_adapter_by_name_unknown_returns_none():
    assert clients.adapter_by_name("nonexistent") is None


def test_all_adapters_returns_distinct_instances():
    adapters = clients.all_adapters()
    assert len(adapters) == 7
    names = [a.name for a in adapters]
    assert len(set(names)) == 7  # all unique


# --- Claude Desktop schema ---


def test_claude_merge_into_empty():
    adapter = ClaudeDesktopAdapter()
    config = adapter.merge_entry(None, "python", ["-m", "cloudprice_mcp.server"])
    assert config == {
        "mcpServers": {
            ENTRY_NAME: {"command": "python", "args": ["-m", "cloudprice_mcp.server"]}
        }
    }


def test_claude_merge_preserves_other_servers_and_top_keys():
    adapter = ClaudeDesktopAdapter()
    existing = {
        "preferences": {"sidebarMode": "task"},
        "mcpServers": {"other-server": {"command": "node", "args": ["server.js"]}},
    }
    config = adapter.merge_entry(existing, "python", ["-m", "cloudprice_mcp.server"])
    assert config["preferences"] == {"sidebarMode": "task"}
    assert "other-server" in config["mcpServers"]
    assert ENTRY_NAME in config["mcpServers"]


# --- Copilot schema (servers + type:stdio) ---


def test_copilot_uses_servers_root_with_type_stdio():
    adapter = CopilotAdapter()
    config = adapter.merge_entry(None, "cloudprice-mcp", [])
    assert "servers" in config
    assert "mcpServers" not in config  # critical: NOT the Claude shape
    assert config["servers"][ENTRY_NAME] == {
        "type": "stdio",
        "command": "cloudprice-mcp",
        "args": [],
    }


def test_copilot_merge_preserves_other_servers():
    adapter = CopilotAdapter()
    existing = {"servers": {"other": {"type": "stdio", "command": "x"}}}
    config = adapter.merge_entry(existing, "cloudprice-mcp", [])
    assert "other" in config["servers"]
    assert ENTRY_NAME in config["servers"]


# --- Cursor schema (mcpServers + type:stdio) ---


def test_cursor_uses_mcpServers_with_type_stdio():
    adapter = CursorAdapter()
    config = adapter.merge_entry(None, "cloudprice-mcp", [])
    assert config["mcpServers"][ENTRY_NAME]["type"] == "stdio"
    assert config["mcpServers"][ENTRY_NAME]["command"] == "cloudprice-mcp"


# --- Windsurf schema (mcpServers, no type field) ---


def test_windsurf_uses_mcpServers_without_type_field():
    adapter = WindsurfAdapter()
    config = adapter.merge_entry(None, "cloudprice-mcp", [])
    entry = config["mcpServers"][ENTRY_NAME]
    assert "type" not in entry  # Windsurf doesn't require it
    assert entry["command"] == "cloudprice-mcp"


# --- Cline schema ---


def test_cline_path_includes_globalstorage():
    adapter = ClineAdapter()
    path = adapter.config_path()
    assert "globalStorage" in str(path)
    assert "saoudrizwan.claude-dev" in str(path)
    assert path.name == "cline_mcp_settings.json"


def test_cline_uses_mcpServers_root():
    adapter = ClineAdapter()
    config = adapter.merge_entry(None, "cloudprice-mcp", [])
    assert "mcpServers" in config
    assert config["mcpServers"][ENTRY_NAME]["command"] == "cloudprice-mcp"


# --- Continue schema (per-server file is the entry) ---


def test_continue_writes_per_server_file_with_name_field():
    adapter = ContinueAdapter()
    config = adapter.merge_entry(None, "cloudprice-mcp", [])
    # Continue's per-server JSON file IS the entry — no nesting
    assert config == {"name": ENTRY_NAME, "command": "cloudprice-mcp", "args": []}


def test_continue_path_under_dot_continue_mcpservers():
    adapter = ContinueAdapter()
    path = adapter.config_path()
    assert ".continue" in str(path)
    assert "mcpServers" in str(path)
    assert path.name == "cloudprice.json"


# --- Zed schema (context_servers — not experimental) ---


def test_zed_uses_context_servers_root():
    adapter = ZedAdapter()
    config = adapter.merge_entry(None, "cloudprice-mcp", [])
    assert "context_servers" in config
    assert "experimental" not in config  # critical: was experimental, now top-level
    assert config["context_servers"][ENTRY_NAME] == {
        "command": "cloudprice-mcp",
        "args": [],
    }


def test_zed_merge_preserves_other_settings():
    adapter = ZedAdapter()
    existing = {"theme": "One Dark", "tab_size": 2}
    config = adapter.merge_entry(existing, "cloudprice-mcp", [])
    assert config["theme"] == "One Dark"
    assert config["tab_size"] == 2
    assert ENTRY_NAME in config["context_servers"]


# --- existing_matches + plan_action logic ---


def test_existing_matches_true_when_identical():
    adapter = CopilotAdapter()
    existing = {
        "servers": {
            ENTRY_NAME: {"type": "stdio", "command": "python", "args": ["-m", "cloudprice_mcp.server"]}
        }
    }
    assert adapter.existing_matches(existing, "python", ["-m", "cloudprice_mcp.server"])


def test_existing_matches_false_when_command_differs():
    adapter = CopilotAdapter()
    existing = {
        "servers": {
            ENTRY_NAME: {"type": "stdio", "command": "OTHER", "args": ["-m", "cloudprice_mcp.server"]}
        }
    }
    assert not adapter.existing_matches(existing, "python", ["-m", "cloudprice_mcp.server"])


def test_plan_action_no_existing_means_write_new():
    adapter = CopilotAdapter()
    action = adapter.plan_action(None, "python", [], force=False, dry_run=False)
    assert action == clients.ACTION_WROTE_NEW


def test_plan_action_skip_when_identical_and_no_force():
    adapter = ClaudeDesktopAdapter()
    existing = {"mcpServers": {ENTRY_NAME: {"command": "python", "args": []}}}
    action = adapter.plan_action(existing, "python", [], force=False, dry_run=False)
    assert action == clients.ACTION_SKIPPED_IDENTICAL


def test_plan_action_refresh_when_different():
    adapter = ClaudeDesktopAdapter()
    existing = {"mcpServers": {ENTRY_NAME: {"command": "OLD", "args": []}}}
    action = adapter.plan_action(existing, "python", [], force=False, dry_run=False)
    assert action == clients.ACTION_REFRESHED


def test_plan_action_force_overrides_skip():
    adapter = ClaudeDesktopAdapter()
    existing = {"mcpServers": {ENTRY_NAME: {"command": "python", "args": []}}}
    # Without force, identical → skip
    assert adapter.plan_action(existing, "python", [], force=False, dry_run=False) == clients.ACTION_SKIPPED_IDENTICAL
    # With force, refresh anyway
    assert adapter.plan_action(existing, "python", [], force=True, dry_run=False) == clients.ACTION_REFRESHED


def test_plan_action_dry_run_uses_would_prefix():
    adapter = CopilotAdapter()
    action = adapter.plan_action(None, "python", [], force=False, dry_run=True)
    assert action == clients.ACTION_WOULD_WRITE_NEW
    assert action.startswith("would_")


# --- Apply (end-to-end with tmp_path) ---


def test_apply_writes_file_to_tmp(monkeypatch, tmp_path):
    """Use a custom adapter pointed at tmp_path to verify apply() writes JSON."""
    target = tmp_path / "claude_desktop_config.json"

    class _TmpAdapter(ClaudeDesktopAdapter):
        def config_path(self) -> Path:  # type: ignore[override]
            return target

    adapter = _TmpAdapter()
    result = adapter.apply("python", ["-m", "cloudprice_mcp.server"], force=False, dry_run=False)
    assert result.action == clients.ACTION_WROTE_NEW
    assert target.exists()
    written = json.loads(target.read_text(encoding="utf-8"))
    assert ENTRY_NAME in written["mcpServers"]


def test_apply_dry_run_does_not_write(tmp_path):
    target = tmp_path / "config.json"

    class _TmpAdapter(ClaudeDesktopAdapter):
        def config_path(self) -> Path:  # type: ignore[override]
            return target

    adapter = _TmpAdapter()
    result = adapter.apply("python", [], force=False, dry_run=True)
    assert result.action == clients.ACTION_WOULD_WRITE_NEW
    assert not target.exists()


def test_apply_invalid_json_raises_with_clear_error(tmp_path):
    target = tmp_path / "config.json"
    target.write_text("{ this is not json", encoding="utf-8")

    class _TmpAdapter(ClaudeDesktopAdapter):
        def config_path(self) -> Path:  # type: ignore[override]
            return target

    adapter = _TmpAdapter()
    with pytest.raises(ValueError, match="not valid JSON"):
        adapter.apply("python", [], force=False, dry_run=False)


def test_apply_skips_when_identical_no_force(tmp_path):
    target = tmp_path / "config.json"
    target.write_text(
        json.dumps({"mcpServers": {ENTRY_NAME: {"command": "python", "args": []}}}),
        encoding="utf-8",
    )

    class _TmpAdapter(ClaudeDesktopAdapter):
        def config_path(self) -> Path:  # type: ignore[override]
            return target

    mtime_before = target.stat().st_mtime_ns
    adapter = _TmpAdapter()
    result = adapter.apply("python", [], force=False, dry_run=False)
    assert result.action == clients.ACTION_SKIPPED_IDENTICAL
    # File should NOT have been rewritten
    assert target.stat().st_mtime_ns == mtime_before
