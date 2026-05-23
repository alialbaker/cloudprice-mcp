"""generate_decision_report — CFO-grade FinOps markdown report.

The "trust" angle that Vantage / Cloudability / Apptio can't easily
replicate because they're SaaS-locked. Their reports live in their UI;
ours are markdown text the user owns and can paste anywhere.

A single call to this tool produces a structured report that combines:
  - Executive summary (the 1-2 sentence answer to "where should we run?")
  - Per-cloud cost comparison table (calls assess_migration internally)
  - Recommendation + reasoning (with explicit caveats)
  - Optional carbon comparison (if include_carbon=True)
  - Aggregated honest_gaps from every sub-tool that ran
  - Audit trail block: catalog version, package version, every input

This is what a FinOps practitioner hands to a CFO who needs a defensible
decision artifact. It's not "trust me, the cloud calculator said so." It's
"here's what we asked, what we got back, the assumptions, and the limits."
"""
from __future__ import annotations

from typing import Any

from .. import __version__
from ..inventory import WorkloadInventory
from ..pricing import Cloud, PriceCatalog
from .carbon import compare_carbon_footprint
from .migration import assess_migration

ALL_CLOUDS: tuple[Cloud, ...] = ("aws", "azure", "gcp", "oci")


def generate_decision_report(
    catalog: PriceCatalog,
    inventory: WorkloadInventory,
    targets: list[Cloud] | None = None,
    include_carbon: bool = True,
    requester: str | None = None,
) -> dict[str, Any]:
    """Run the standard migration assessment + optionally carbon, then
    fold everything into a single CFO-ready markdown document.

    Returns a dict with both the structured underlying data AND a
    `markdown` field containing the rendered report. The user gets the
    structured data for programmatic use AND the markdown for paste.

    Args:
        inventory: workload spec
        targets: optional cloud list (default all 4)
        include_carbon: also run compare_carbon_footprint and include a
            carbon section (default True)
        requester: optional name/email for the audit trail
    """
    migration = assess_migration(catalog, inventory, targets=targets)

    carbon = None
    if include_carbon and inventory.compute:
        first = inventory.compute[0]
        try:
            carbon = compare_carbon_footprint(
                catalog,
                vcpus=first.vcpus,
                memory_gb=first.memory_gb,
                quantity=first.quantity,
                targets=targets,
            )
        except ValueError:
            carbon = None

    markdown = _render_markdown(
        migration=migration, carbon=carbon, inventory=inventory,
        catalog=catalog, requester=requester,
    )

    return {
        "kind": "finops_decision_report",
        "title": migration.get("title", "FinOps decision report"),
        "headline": migration.get("headline", ""),
        "recommended_cloud": migration.get("recommended"),
        "markdown": markdown,
        "underlying": {
            "migration": migration,
            "carbon": carbon,
        },
        "audit": {
            "cloudprice_mcp_version": __version__,
            "catalog_as_of": catalog.as_of,
            "requester": requester,
            "include_carbon": include_carbon,
        },
    }


def _render_markdown(
    *, migration: dict, carbon: dict | None, inventory: WorkloadInventory,
    catalog: PriceCatalog, requester: str | None,
) -> str:
    lines: list[str] = []

    # --- Header ---
    title = migration.get("title", "FinOps decision report")
    lines.append(f"# {title}")
    lines.append("")
    if migration.get("headline"):
        lines.append(f"**{migration['headline']}**")
        lines.append("")

    # --- Executive summary ---
    lines.append("## Executive summary")
    lines.append("")
    rec = migration.get("recommended")
    source = migration.get("source_cloud", "?")
    source_monthly = migration.get("source_monthly_usd")
    if rec and source_monthly is not None:
        targets = migration.get("targets") or {}
        if rec in targets:
            target_monthly = targets[rec].get("monthly_usd")
            savings_pct = targets[rec].get("savings_vs_source_pct")
            payback = targets[rec].get("payback_months")
            sav_str = f"{savings_pct}%" if savings_pct is not None else "an unknown amount"
            pay_str = (
                f"{payback:.1f} months" if isinstance(payback, (int, float)) and payback >= 0
                else "n/a (no exit cost modeled)"
            )
            lines.append(
                f"- **Recommendation**: move to **{rec.upper()}** "
                f"(${target_monthly:,.0f}/mo vs ${source_monthly:,.0f}/mo on {source.upper()}). "
                f"Savings: {sav_str}. Payback: {pay_str}."
            )
        else:
            lines.append(f"- **Recommendation**: stay on {source.upper()}. Headline: {migration.get('headline')}")
    else:
        lines.append("- No migration recommendation could be computed for this workload.")
    lines.append("")

    # --- Cost comparison table ---
    lines.append("## Per-cloud cost comparison")
    lines.append("")
    lines.append("| Cloud | Monthly USD | Savings vs source | Payback | 3-yr total USD |")
    lines.append("|---|---|---|---|---|")
    if source_monthly is not None:
        lines.append(f"| **{source.upper()}** (source) | ${source_monthly:,.0f} | — | — | — |")
    ranking = migration.get("ranking_by_3yr_tco") or list((migration.get("targets") or {}).keys())
    for cloud in ranking:
        t = (migration.get("targets") or {}).get(cloud)
        if not t:
            continue
        savings = t.get("savings_vs_source_pct")
        payback = t.get("payback_months")
        sav_cell = f"{savings:+d}%" if isinstance(savings, int) else (f"{savings}" if savings is not None else "—")
        pay_cell = (
            f"{payback:.1f} mo" if isinstance(payback, (int, float)) and payback >= 0
            else "n/a"
        )
        marker = " ←" if cloud == rec else ""
        lines.append(
            f"| {cloud.upper()}{marker} | ${t.get('monthly_usd', 0):,.0f} | {sav_cell} | "
            f"{pay_cell} | ${t.get('three_year_total_usd', 0):,.0f} |"
        )
    lines.append("")

    # --- One-time exit cost (if any) ---
    exit_cost = migration.get("one_time_exit_cost_usd")
    if exit_cost and exit_cost > 0:
        lines.append(f"**One-time exit cost (egress out of {source.upper()})**: ${exit_cost:,.2f}")
        lines.append("")

    # --- Carbon section (optional) ---
    if carbon and carbon.get("per_cloud"):
        lines.append("## Carbon footprint")
        lines.append("")
        if carbon.get("headline"):
            lines.append(f"_{carbon['headline']}_")
            lines.append("")
        lines.append("| Cloud | Monthly kg CO2e (grid) | Residual (after renewable matching) | Power class |")
        lines.append("|---|---|---|---|")
        for row in carbon["per_cloud"]:
            cloud = row["cloud"]
            grid = row.get("monthly_kg_CO2e_grid", 0)
            res = row.get("monthly_kg_CO2e_residual", 0)
            power = row.get("power_class", "?")
            marker = " ←" if cloud == carbon.get("recommended") else ""
            lines.append(f"| {cloud.upper()}{marker} | {grid:,.2f} | {res:,.2f} | {power} |")
        lines.append("")

    # --- Workload spec ---
    lines.append("## Workload analyzed")
    lines.append("")
    lines.append("```")
    if inventory.compute:
        for c in inventory.compute:
            lines.append(f"compute  : {c.name} — {c.quantity}x ({c.vcpus} vCPU, {c.memory_gb} GB)")
    if inventory.storage:
        for s in inventory.storage:
            lines.append(f"storage  : {s.name} — {s.quantity}x {s.capacity_gb} GB {s.disk_type}")
    if inventory.object_storage:
        for o in inventory.object_storage:
            lines.append(f"object   : {o.name} — {o.capacity_gb} GB {o.tier}")
    if inventory.egress:
        for e in inventory.egress:
            lines.append(f"egress   : {e.name} — {e.gb_per_month} GB/mo {e.direction}")
    if inventory.databases:
        for d in inventory.databases:
            lines.append(f"database : {d.name} — {d.engine} {d.size_label} {d.storage_gb} GB")
    if inventory.commitment and inventory.commitment != "none":
        lines.append(f"commit   : {inventory.commitment}")
    if inventory.multi_az:
        lines.append("multi_az : true")
    lines.append("```")
    lines.append("")

    # --- Honest gaps (aggregated) ---
    all_gaps: list[str] = []
    for source_name, gaps in (("migration", migration.get("honest_gaps")), ("carbon", carbon.get("honest_gaps") if carbon else None)):
        if not gaps:
            continue
        for g in gaps:
            all_gaps.append(f"_(from {source_name})_ {g}")
    if all_gaps:
        lines.append("## Honest gaps — what this report does NOT model")
        lines.append("")
        for g in all_gaps:
            lines.append(f"- {g}")
        lines.append("")

    # --- Audit trail (the trust angle) ---
    lines.append("## Audit trail")
    lines.append("")
    lines.append(f"- **Generated by**: cloudprice-mcp v{__version__}")
    lines.append(f"- **Catalog as_of**: {catalog.as_of}")
    if requester:
        lines.append(f"- **Requested by**: {requester}")
    lines.append("- **Method**: per-cloud monthly + 3-year totals via `assess_migration`. "
                 "Carbon via `compare_carbon_footprint` when included. List-price catalog; "
                 "no enterprise / committed-use discounts applied.")
    lines.append("- **Reproduce**: `pip install \"cloudprice-mcp==" + __version__ + "\"` and pass the "
                 "Workload analyzed block above as the input to `generate_decision_report`.")
    lines.append("")

    return "\n".join(lines)
