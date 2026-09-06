"""
Output renderers for v0.6 FinOps tool results.

Each FinOps tool (`assess_migration`, `optimize_commitment`, `compare_tco`,
`find_egress_arbitrage`) returns a structured dict. These renderers turn that dict
into the format the consumer wants:

    markdown  — Slack / docs / LLM context (default)
    json      — programmatic / piping
    text      — plain ASCII for terminals or chat

CSV / HTML are deferred to v0.7+ pending demand.

Design choice — generic over specific:
  Each tool's output dict contains a "kind" discriminator (e.g.
  "kind": "migration_assessment") so the renderer knows what shape to expect
  and can produce a tool-appropriate table. Adding a new tool = add a new render
  function + register it in RENDERERS_BY_KIND.
"""
from __future__ import annotations

import io
import json
from collections.abc import Callable
from typing import Any

# --- Public API ---


def to_markdown(result: dict) -> str:
    """Render a FinOps tool result as Markdown (default consumer format)."""
    renderer = _renderer_for(result, format="markdown")
    return renderer(result)


def to_json(result: dict) -> str:
    """Render a FinOps tool result as pretty-printed JSON."""
    return json.dumps(result, indent=2, sort_keys=False, default=_json_default)


def to_text(result: dict) -> str:
    """Render a FinOps tool result as plain ASCII (for terminals / chat / Slack)."""
    renderer = _renderer_for(result, format="text")
    return renderer(result)


# --- Renderer dispatch ---


def _renderer_for(result: dict, format: str) -> Callable[[dict], str]:
    kind = (result or {}).get("kind", "_generic")
    table = _RENDERERS_BY_KIND.get((kind, format), _RENDERERS_BY_KIND[("_generic", format)])
    return table


# --- Generic fallback (works for any dict) ---


def _generic_markdown(result: dict) -> str:
    """Best-effort markdown for any FinOps result. Used when no specialized renderer is registered."""
    buf = io.StringIO()
    title = result.get("title") or "FinOps result"
    buf.write(f"## {title}\n\n")
    headline = result.get("headline")
    if headline:
        buf.write(f"**{headline}**\n\n")

    # If there's a `targets` block (assess-migration / egress-arbitrage shape), render as a table.
    targets = result.get("targets")
    if isinstance(targets, dict) and targets:
        buf.write("| Target | Monthly $ | Savings % | Payback (mo) | Notes |\n")
        buf.write("|---|---|---|---|---|\n")
        for cloud, t in targets.items():
            monthly = _fmt_currency(t.get("monthly_usd"))
            savings = _fmt_pct(t.get("savings_vs_source_pct") or t.get("savings_pct"))
            payback = _fmt_payback(t.get("payback_months"))
            notes = ", ".join(t.get("caveats", []) or []) or "—"
            buf.write(f"| {cloud} | {monthly} | {savings} | {payback} | {notes} |\n")
        buf.write("\n")

    # If there's a `scenarios` block (optimize-commitment shape), render as a table.
    scenarios = result.get("scenarios")
    if isinstance(scenarios, dict) and scenarios:
        buf.write("| Scenario | Monthly $ | Savings % | Upfront $ | 3yr Total $ | Payback (mo) |\n")
        buf.write("|---|---|---|---|---|---|\n")
        for name, s in scenarios.items():
            monthly = _fmt_currency(s.get("monthly_usd"))
            savings = _fmt_pct(s.get("savings_vs_ondemand_pct"))
            upfront = _fmt_currency(s.get("upfront_usd"))
            tyr = _fmt_currency(s.get("3yr_total_usd"))
            payback = _fmt_payback(s.get("payback_months"))
            buf.write(f"| {name} | {monthly} | {savings} | {upfront} | {tyr} | {payback} |\n")
        buf.write("\n")

    # Top-level summary fields
    for key in ("source_monthly_usd", "one_time_exit_cost_usd", "recommended", "recommendation_reason"):
        if key in result:
            label = key.replace("_", " ").title()
            value = result[key]
            if isinstance(value, (int, float)):
                value = _fmt_currency(value) if "usd" in key else value
            buf.write(f"- **{label}:** {value}\n")

    notes = result.get("notes") or result.get("honest_gaps") or []
    if notes:
        buf.write("\n### Notes\n\n")
        for n in notes:
            buf.write(f"- {n}\n")

    return buf.getvalue().rstrip() + "\n"


def _generic_text(result: dict) -> str:
    """Best-effort plain ASCII for any FinOps result."""
    buf = io.StringIO()
    title = result.get("title") or "FinOps result"
    buf.write(title + "\n")
    buf.write("=" * len(title) + "\n\n")
    headline = result.get("headline")
    if headline:
        buf.write(headline + "\n\n")

    targets = result.get("targets")
    if isinstance(targets, dict) and targets:
        for cloud, t in targets.items():
            monthly = _fmt_currency(t.get("monthly_usd"))
            savings = _fmt_pct(t.get("savings_vs_source_pct") or t.get("savings_pct"))
            payback = _fmt_payback(t.get("payback_months"))
            buf.write(f"  {cloud:<8} {monthly:>14}  ({savings})  payback {payback}\n")
        buf.write("\n")

    scenarios = result.get("scenarios")
    if isinstance(scenarios, dict) and scenarios:
        for name, s in scenarios.items():
            monthly = _fmt_currency(s.get("monthly_usd"))
            savings = _fmt_pct(s.get("savings_vs_ondemand_pct"))
            payback = _fmt_payback(s.get("payback_months"))
            buf.write(f"  {name:<24} {monthly:>14}  ({savings})  payback {payback}\n")
        buf.write("\n")

    for key in ("source_monthly_usd", "one_time_exit_cost_usd"):
        if key in result:
            label = key.replace("_", " ").capitalize()
            buf.write(f"{label}: {_fmt_currency(result[key])}\n")
    if result.get("recommended"):
        buf.write(f"Recommended: {result['recommended']}\n")
    if result.get("recommendation_reason"):
        buf.write(f"  -> {result['recommendation_reason']}\n")

    notes = result.get("notes") or result.get("honest_gaps") or []
    if notes:
        buf.write("\nNotes:\n")
        for n in notes:
            buf.write(f"  - {n}\n")

    return buf.getvalue().rstrip() + "\n"


# --- Formatting helpers ---


def _fmt_currency(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, (int, float)):
        return f"${value:,.0f}" if value >= 100 else f"${value:,.2f}"
    return str(value)


def _fmt_pct(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, (int, float)):
        sign = "+" if value > 0 else ""
        return f"{sign}{value:.0f}%"
    return str(value)


def _fmt_payback(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, (int, float)):
        return f"{value:.1f} mo"
    return str(value)


def _json_default(o: Any) -> Any:
    """Fallback for json.dumps when it sees a Path / dataclass / set / etc."""
    if hasattr(o, "to_dict"):
        return o.to_dict()
    if hasattr(o, "__dict__"):
        return o.__dict__
    if isinstance(o, set):
        return sorted(o)
    return str(o)


# --- Renderer registry ---
# Per-tool specialized renderers can be added later by registering (kind, format) keys.
# The generic renderer handles common shapes (targets / scenarios / headline) for now.

_RENDERERS_BY_KIND: dict[tuple[str, str], Callable[[dict], str]] = {
    ("_generic", "markdown"): _generic_markdown,
    ("_generic", "text"): _generic_text,
}
