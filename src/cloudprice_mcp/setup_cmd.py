"""
`cloudprice-mcp setup` — auto-configure cloudprice-mcp in any installed MCP-compatible client.

Trust spectrum:
  cloudprice-mcp setup                       # interactive — detect + show + ask Y/N for the batch
  cloudprice-mcp setup --yes                 # skip prompt, configure all detected
  cloudprice-mcp setup --client copilot      # configure just one client (repeatable)
  cloudprice-mcp setup --all                 # configure ALL known clients (creates dirs)
  cloudprice-mcp setup --force               # overwrite existing cloudprice entry without merging
  cloudprice-mcp setup --dry-run             # show diffs, write nothing
  cloudprice-mcp setup --print-config        # emit per-client JSON to stdout
  cloudprice-mcp setup --list-clients        # show detection table

Supports: Claude Desktop, GitHub Copilot Agent Mode (VS Code), Cursor, Windsurf, Cline, Continue.dev, Zed.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from . import clients

# -------- Backward-compat exports (used by doctor_cmd before the v0.5.1 refactor) --------


def detect_python_command() -> str:
    """Absolute path to the current Python interpreter.

    Why absolute: macOS / Linux MCP clients launch with a minimal PATH that often
    doesn't include where `python3` lives. sys.executable is bulletproof.
    """
    return sys.executable


def detect_config_path() -> tuple[Path, str]:
    """Backward-compat shim. Returns Claude Desktop's config path + a label."""
    adapter = clients.ClaudeDesktopAdapter()
    path = adapter.config_path()
    if sys.platform == "win32":
        kind = "Microsoft Store" if "Packages" in str(path) else "direct .exe"
        if not path.exists():
            return path, f"Windows ({kind}, config dir not yet created)"
        return path, f"Windows ({kind})"
    if sys.platform == "darwin":
        return path, "macOS"
    return path, "Linux"


def build_cloudprice_args() -> list[str]:
    """The args list for invoking the MCP server module."""
    return ["-m", "cloudprice_mcp.server"]


def kill_cached_subprocesses() -> int:
    """Kill any lingering cloudprice-mcp subprocesses so clients respawn fresh."""
    killed = 0
    if sys.platform == "win32":
        try:
            cmd = (
                'Get-Process | Where-Object { $_.Path -like "*cloudprice-mcp*" } '
                "| ForEach-Object { Stop-Process -Id $_.Id -Force; 1 }"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", cmd],
                capture_output=True,
                text=True,
                timeout=10,
            )
            killed = len([line for line in result.stdout.splitlines() if line.strip() == "1"])
        except (subprocess.SubprocessError, FileNotFoundError):
            pass
    else:
        for pattern in ("cloudprice-mcp", "cloudprice_mcp"):
            try:
                result = subprocess.run(
                    ["pkill", "-f", pattern], capture_output=True, timeout=5
                )
                if result.returncode == 0:
                    killed += 1
            except (subprocess.SubprocessError, FileNotFoundError):
                pass
    return killed


# -------- Helpers --------


def _confirm(prompt: str) -> bool:
    try:
        answer = input(f"{prompt} [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in ("", "y", "yes")


def _action_label(action: str) -> str:
    return {
        clients.ACTION_WROTE_NEW: "wrote new config file",
        clients.ACTION_MERGED: "merged into existing config",
        clients.ACTION_REFRESHED: "refreshed existing cloudprice entry",
        clients.ACTION_SKIPPED_IDENTICAL: "skipped (already up to date)",
        clients.ACTION_WOULD_WRITE_NEW: "would write new config file",
        clients.ACTION_WOULD_MERGE: "would merge into existing config",
        clients.ACTION_WOULD_REFRESH: "would refresh existing cloudprice entry",
        clients.ACTION_WOULD_SKIP_IDENTICAL: "would skip (already up to date)",
    }.get(action, action)


def _is_dry_run_action(action: str) -> bool:
    return action.startswith("would_")


# -------- Target selection --------


def _select_targets(args: argparse.Namespace) -> list[clients.ClientAdapter]:
    """Decide which adapters to configure based on flags + detection."""
    if args.client:
        out: list[clients.ClientAdapter] = []
        for name in args.client:
            adapter = clients.adapter_by_name(name)
            if adapter is None:
                known = ", ".join(clients.known_client_names())
                print(f"⚠ Unknown client '{name}'. Known: {known}", file=sys.stderr)
                continue
            out.append(adapter)
        return out
    if args.all:
        return clients.all_adapters()
    return clients.detect_installed()


# -------- Subcommand handlers --------


def _list_clients_command() -> int:
    print("Known MCP-compatible clients (✓ = appears installed on this system):\n")
    print(f"  {'name':<10} {'installed':<10} {'config path'}")
    print(f"  {'----':<10} {'---------':<10} {'-----------'}")
    for adapter in clients.all_adapters():
        installed = "✓" if adapter.is_installed() else " "
        print(f"  {adapter.name:<10} {installed:<10} {adapter.config_path()}")
    print(
        "\nRun `cloudprice-mcp setup` to configure all detected clients, "
        "or `cloudprice-mcp setup --client <name>` to pick one."
    )
    return 0


def _print_config_command(args: argparse.Namespace) -> int:
    """Emit per-client JSON to stdout. No prompts, no detection chatter."""
    targets = _select_targets(args)
    if not targets:
        print(
            "⚠ No clients selected. Pass --client <name>, --all, or run from a system with a client installed.",
            file=sys.stderr,
        )
        return 1
    command = detect_python_command()
    args_list = build_cloudprice_args()
    out = {}
    for adapter in targets:
        try:
            existing = adapter.read_existing_config()
        except ValueError:
            existing = None
        merged = adapter.merge_entry(existing, command, args_list)
        out[adapter.name] = {
            "config_path": str(adapter.config_path()),
            "config": merged,
        }
    print(json.dumps(out, indent=2))
    return 0


# -------- Main orchestrator --------


def run_setup(args: argparse.Namespace) -> int:
    if getattr(args, "list_clients", False):
        return _list_clients_command()
    if args.print_config:
        return _print_config_command(args)

    targets = _select_targets(args)
    if not targets:
        print(
            "⚠ No clients detected. Pass --client <name> or --all to override.\n"
            "   Run `cloudprice-mcp setup --list-clients` to see what's known.",
            file=sys.stderr,
        )
        return 1

    command = detect_python_command()
    args_list = build_cloudprice_args()

    print("🔍 cloudprice-mcp setup\n")
    print(f"  Python interpreter: {command}")
    print(f"  Args:               {args_list}")
    print(f"  Targets:            {len(targets)} client(s)\n")

    # Plan + print
    plans: list[tuple[clients.ClientAdapter, dict | None, str]] = []
    for adapter in targets:
        try:
            existing = adapter.read_existing_config()
        except ValueError as e:
            print(f"  ⚠ {adapter.display_name}: {e}", file=sys.stderr)
            continue
        action = adapter.plan_action(existing, command, args_list, args.force, dry_run=True)
        plans.append((adapter, existing, action))

    if not plans:
        print("⚠ Nothing to do (all targets had unreadable configs).", file=sys.stderr)
        return 1

    print("📋 Plan:\n")
    for adapter, _, action in plans:
        print(f"  • {adapter.display_name}")
        print(f"      path:   {adapter.config_path()}")
        print(f"      action: {_action_label(action)}\n")

    if args.dry_run:
        print("--dry-run set: no files written.")
        return 0

    # Confirm
    interactive_skip = args.yes or args.force
    if not interactive_skip:
        if not _confirm("Proceed with all of the above?"):
            print("Aborted. No changes made.")
            return 1

    # Apply
    print()
    results: list[clients.WriteResult] = []
    for adapter, _, _ in plans:
        try:
            result = adapter.apply(command, args_list, force=args.force, dry_run=False)
        except ValueError as e:
            print(f"  ✗ {adapter.display_name}: {e}", file=sys.stderr)
            continue
        symbol = "○" if result.action == clients.ACTION_SKIPPED_IDENTICAL else "✓"
        print(f"  {symbol} {result.display_name}: {_action_label(result.action)}")
        print(f"      → {result.config_path}")
        results.append(result)

    # Kill cached subprocesses if Claude Desktop is in the mix (other clients respawn cleanly).
    if any(r.client_name == "claude" for r in results):
        killed = kill_cached_subprocesses()
        if killed:
            print(f"\n✓ Killed {killed} cached cloudprice-mcp subprocess(es).")

    # Restart instructions per client
    if results:
        print("\n🎯 Restart each configured client to pick up the change:\n")
        for r in results:
            adapter = clients.adapter_by_name(r.client_name)
            if adapter is None:
                continue
            print(f"  • {r.display_name}")
            print(f"      {adapter.restart_instructions()}")
            print(f"      Verify: {adapter.verify_hint()}\n")

    print("💡 Run `cloudprice-mcp doctor` if anything looks wrong.")
    return 0


# -------- argparse hookup (called from cli.py) --------


def add_setup_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip the interactive Y/N confirmation.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Overwrite existing cloudprice entries without merging — useful for path refresh on upgrade.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be written without modifying any files.",
    )
    parser.add_argument(
        "--print-config", action="store_true",
        help="Print per-client JSON to stdout for users who prefer to paste it manually.",
    )
    parser.add_argument(
        "--client", action="append", default=None, metavar="NAME",
        help=(
            "Configure a specific client (repeatable). "
            f"Known: {', '.join(clients.known_client_names())}."
        ),
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Configure all known clients, even ones that don't appear installed.",
    )
    parser.add_argument(
        "--list-clients", action="store_true",
        help="Show which clients are known and which appear installed; exit.",
    )
