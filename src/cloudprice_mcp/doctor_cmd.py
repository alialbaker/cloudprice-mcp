"""
`cloudprice-mcp doctor` — diagnose install + per-client config issues.

Now multi-client aware: iterates every adapter and reports per-client status.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from . import __version__ as cloudprice_version
from . import clients, path_fix
from .setup_cmd import build_cloudprice_args, detect_python_command

CHECK = "✓"
CROSS = "✗"
WARN = "⚠"
_ENTRY_LABEL = "    cloudprice entry"


def _ok(label: str, detail: str = "") -> None:
    detail_str = f" — {detail}" if detail else ""
    print(f"  {CHECK} {label}{detail_str}")


def _fail(label: str, detail: str) -> None:
    print(f"  {CROSS} {label} — {detail}")


def _warn(label: str, detail: str) -> None:
    print(f"  {WARN} {label} — {detail}")


def _check_python_version() -> bool:
    py = sys.version_info
    if py >= (3, 10):
        _ok("Python version", f"{py.major}.{py.minor}.{py.micro} (3.10+ required)")
        return True
    _fail(
        "Python version",
        f"{py.major}.{py.minor}.{py.micro} — cloudprice-mcp requires 3.10+",
    )
    return False


def _check_package_install() -> bool:
    _ok("cloudprice-mcp package", f"v{cloudprice_version}")
    return True


def _check_tool_registration() -> tuple[bool, int]:
    try:
        from .server import list_tools
        tools = asyncio.run(list_tools())
        _ok("MCP tools registered", f"{len(tools)} tools")
        return True, len(tools)
    except Exception as e:  # pragma: no cover
        _fail("MCP tools registered", f"{type(e).__name__}: {e}")
        return False, 0


def _check_one_client(adapter: clients.ClientAdapter) -> bool:
    """Return True if the client is fully OK (or absent — absence is not failure)."""
    print(f"\n  [{adapter.display_name}]")

    if not adapter.is_installed():
        print(f"    {WARN} not detected on this system (skipping)")
        return True

    config_path = adapter.config_path()
    print(f"    {CHECK} detected — config path: {config_path}")

    try:
        existing = adapter.read_existing_config()
    except ValueError as e:
        _fail("    config file", str(e))
        return False

    if existing is None:
        _fail(
            _ENTRY_LABEL,
            f"config file does not exist. Run `cloudprice-mcp setup --client {adapter.name}`.",
        )
        return False

    if not adapter.already_present(existing):
        _fail(
            _ENTRY_LABEL,
            f"missing — Run `cloudprice-mcp setup --client {adapter.name}`.",
        )
        return False

    expected_command = detect_python_command()
    expected_args = build_cloudprice_args()
    if adapter.existing_matches(existing, expected_command, expected_args):
        _ok(_ENTRY_LABEL, "present and up to date")
        return True

    # Entry present but doesn't match expected — could be dev install, or stale path.
    _warn(
        _ENTRY_LABEL,
        "present but command/args don't match this Python install. "
        f"Run `cloudprice-mcp setup --client {adapter.name} --force` to refresh.",
    )

    # Soft check: does the configured command path exist at all?
    cmd = _extract_command(adapter, existing)
    if cmd:
        cmd_path = Path(cmd)
        if cmd_path.is_absolute() and not cmd_path.exists():
            _fail(
                "    command path",
                f"absolute path '{cmd}' does NOT exist. Refresh with --force.",
            )
            return False
    return True  # mismatched but not strictly broken


def _extract_command(adapter: clients.ClientAdapter, existing: dict) -> str | None:
    """Pull out the `command` field from an existing config across all schemas we support."""
    name = adapter.name
    if name in {"claude", "cursor", "windsurf", "cline"}:
        return ((existing.get("mcpServers") or {}).get(clients.ENTRY_NAME) or {}).get("command")
    if name == "copilot":
        return ((existing.get("servers") or {}).get(clients.ENTRY_NAME) or {}).get("command")
    if name == "zed":
        return (
            (existing.get("context_servers") or {}).get(clients.ENTRY_NAME) or {}
        ).get("command")
    if name == "continue":
        # Per-server file — entry is the whole document.
        return existing.get("command")
    return None


def _check_shell_shim() -> None:
    """Hint-only: report whether cloudprice-mcp is callable as a bare shell command.

    Never returns a failure — even if the shim isn't on PATH, `python -m cloudprice_mcp.cli`
    works, and the configs setup writes use absolute Python paths regardless. This check
    exists purely to suggest `cloudprice-mcp fix-path` when it would help.
    """
    print("\nShell shim (optional — affects only your shell, not the MCP clients):")
    scripts_dir = path_fix.get_scripts_dir()

    if not path_fix.shim_exists():
        print(f"  {WARN} cloudprice-mcp shim not found in {scripts_dir}")
        print("       (Maybe you installed via `pip install -e .` in a different venv.)")
        return

    _ok("cloudprice-mcp shim", str(path_fix.shim_path()))

    on_current = path_fix.is_on_current_path(scripts_dir)
    if on_current:
        _ok(f"Scripts dir ({scripts_dir})", "on current shell PATH — bare `cloudprice-mcp` resolves")
        return

    if sys.platform == "win32":
        in_persistent = path_fix.is_in_user_path(scripts_dir)
        if in_persistent:
            print(
                f"  {WARN} Scripts dir is in persistent user PATH but not in this shell yet.\n"
                "       Open a fresh PowerShell window — then `cloudprice-mcp` will resolve."
            )
            return
        print(
            "  ℹ Scripts dir not on PATH — bare `cloudprice-mcp` won't resolve in your shell.\n"
            "       This does NOT affect MCP clients (their configs use absolute paths).\n"
            "       To fix:  cloudprice-mcp fix-path     (or keep using `python -m cloudprice_mcp.cli ...`)"
        )
        return

    print(
        f"  ℹ Scripts dir ({scripts_dir}) not on PATH — bare `cloudprice-mcp` won't resolve.\n"
        "       Add it to your shell rc, or keep using `python -m cloudprice_mcp.cli ...`."
    )


def run_doctor(_args: argparse.Namespace) -> int:
    print("🩺 cloudprice-mcp doctor — running checks\n")

    print("Install:")
    py_ok = _check_python_version()
    pkg_ok = _check_package_install()
    tools_ok, _ = _check_tool_registration()

    _check_shell_shim()

    print("\nClients:")
    detected_any = False
    all_clients_ok = True
    for adapter in clients.all_adapters():
        if adapter.is_installed():
            detected_any = True
        client_ok = _check_one_client(adapter)
        if adapter.is_installed():
            all_clients_ok = all_clients_ok and client_ok

    print()
    if not detected_any:
        print(
            "⚠ No MCP-compatible clients detected on this system.\n"
            "   Install Claude Desktop / VS Code (with Copilot Chat) / Cursor / Windsurf, "
            "then re-run: cloudprice-mcp setup"
        )
        return 1

    if py_ok and pkg_ok and tools_ok and all_clients_ok:
        print("✅ All checks passed.")
        print(
            "   If a client still doesn't show cloudprice, fully quit + reopen it.\n"
            "   Quick refresh: cloudprice-mcp setup --force"
        )
        return 0

    print("⚠ Some checks failed. See details above.")
    print("   Quick fix attempt: cloudprice-mcp setup --force")
    return 1
