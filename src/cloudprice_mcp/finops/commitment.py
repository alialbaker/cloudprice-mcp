"""
`optimize_commitment` — the "when does my RI / SP / CUD pay back?" FinOps tool.

Given a workload inventory, computes the cost / savings / payback for every
commitment scenario (`none`, `1yr_no_upfront`, `1yr_all_upfront`, `3yr_no_upfront`,
`3yr_partial_upfront`, `3yr_all_upfront`) on a chosen cloud, and recommends the
scenario with the lowest 3-year TCO (subject to break-even sanity).

Scope for v0.6.0:
- Cloud-level commitment rates (loaded from data/ri_tiers.json) — conservative
  averages across instance families.
- Compute-only — storage / database / object / egress are NOT discounted because
  most clouds don't offer meaningful storage commitments.
- 1-year commitments: discount applies for year 1, on-demand for years 2-3
  (no renewal modeling — assumes user re-decides at year 1).
- 3-year commitments: discount applies for the full 3-year horizon.

Per-family RI tiers (e.g., compute-optimized vs general-purpose vs memory-optimized)
are deferred to v0.6.x — the morning's RI break-even queries showed that flat tiers
are a useful approximation for most workloads, and per-family precision is best
delivered after we see what families users actually run.
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from importlib import resources

from ..compare import (
    CLOUDS,
    ComputeRequest,
    bulk_compare_compute,
)
from ..inventory import ComputeItem, WorkloadInventory
from ..pricing import Cloud, PriceCatalog

DATA_PACKAGE = "cloudprice_mcp.data"
HORIZON_MONTHS = 36  # 3-year analysis window (FinOps standard)

ALL_SCENARIOS: tuple[str, ...] = (
    "none",
    "1yr_no_upfront",
    "1yr_all_upfront",
    "3yr_no_upfront",
    "3yr_partial_upfront",
    "3yr_all_upfront",
)


# --- RI tier data loader (cached) ---


_ri_tier_cache: dict | None = None


def load_ri_tiers() -> dict:
    """Load data/ri_tiers.json (cached). Returns the full structure including scenarios + rates."""
    global _ri_tier_cache
    if _ri_tier_cache is not None:
        return _ri_tier_cache
    text = resources.files(DATA_PACKAGE).joinpath("ri_tiers.json").read_text(encoding="utf-8")
    _ri_tier_cache = json.loads(text)
    return _ri_tier_cache


def reset_ri_tier_cache() -> None:
    """Test helper — drop the singleton."""
    global _ri_tier_cache
    _ri_tier_cache = None


# --- Public entry point ---


def optimize_commitment(
    catalog: PriceCatalog,
    inventory: WorkloadInventory,
    cloud: Cloud | None = None,
    scenarios: Iterable[str] | None = None,
) -> dict:
    """Project per-scenario cost / savings / payback for compute commitment options.

    Args:
        catalog: bundled or live pricing catalog
        inventory: workload inventory (only `compute` items are used by this tool)
        cloud: cloud to evaluate against (default: inventory.source_cloud, then "aws")
        scenarios: subset of commitment scenarios to evaluate (default: all 6)

    Returns:
        dict with `kind=commitment_optimization`, per-scenario breakdown, recommendation.
    """
    if not inventory.compute:
        raise ValueError(
            "optimize_commitment: inventory.compute must have at least one item."
        )

    target_cloud: Cloud = cloud or inventory.source_cloud or "aws"
    if target_cloud not in CLOUDS:
        raise ValueError(
            f"optimize_commitment: unknown cloud '{target_cloud}'. "
            f"Must be one of {CLOUDS}."
        )

    scenarios = list(scenarios) if scenarios is not None else list(ALL_SCENARIOS)
    _validate_scenarios(scenarios)

    on_demand_monthly = _on_demand_monthly_compute(catalog, inventory.compute, target_cloud)
    if on_demand_monthly <= 0:
        # OCI A1.Flex Always Free covers the workload — every scenario is $0
        return _all_free_result(target_cloud, scenarios)

    tiers = load_ri_tiers()
    rates = tiers["rates_by_cloud"][target_cloud]
    scenario_meta = tiers["scenarios"]

    per_scenario: dict[str, dict] = {}
    for scenario in scenarios:
        per_scenario[scenario] = _compute_scenario(
            scenario,
            on_demand_monthly,
            rates,
            scenario_meta,
        )

    recommended = _pick_recommendation(per_scenario, scenarios)
    headline = _build_headline(target_cloud, on_demand_monthly, recommended, per_scenario)
    reason = _build_reason(recommended, per_scenario) if recommended else ""

    return {
        "kind": "commitment_optimization",
        "title": f"Commitment Optimization: {target_cloud.upper()}",
        "headline": headline,
        "cloud": target_cloud,
        "on_demand_monthly_usd": round(on_demand_monthly, 2),
        "horizon_months": HORIZON_MONTHS,
        "scenarios": per_scenario,
        "recommended": recommended,
        "recommendation_reason": reason,
        "honest_gaps": [
            "Cloud-level rates only — per-family tiers (compute-opt / memory-opt) come in v0.6.x",
            "Storage / database / object / egress are NOT discounted (most clouds don't offer meaningful commitments on these)",
            "1-year scenarios assume on-demand for years 2-3 (no renewal modeling)",
            "Conservative averages from published calculators; verify against your cloud's own RI/SP/CUD calculator before signing",
        ],
    }


# --- Per-scenario math ---


def _compute_scenario(
    scenario: str,
    on_demand_monthly: float,
    rates: dict,
    scenario_meta: dict,
) -> dict:
    """Compute the per-scenario breakdown.

    For scenario == "none": straight on-demand with $0 upfront.
    For 3-year scenarios: discount applies to all 36 months.
    For 1-year scenarios: discount applies to first 12 months, on-demand for next 24.
    """
    if scenario == "none":
        on_demand_total = on_demand_monthly * HORIZON_MONTHS
        return {
            "label": scenario_meta["none"]["label"],
            "monthly_usd": round(on_demand_monthly, 2),
            "monthly_usd_during_term": round(on_demand_monthly, 2),
            "monthly_usd_after_term": round(on_demand_monthly, 2),
            "upfront_usd": 0.0,
            "term_months": 0,
            "three_year_total_usd": round(on_demand_total, 2),
            "savings_vs_ondemand_usd": 0.0,
            "savings_vs_ondemand_pct": 0,
            "payback_months": None,
        }

    info = scenario_meta[scenario]
    term_months: int = info["term_months"]
    upfront_fraction: float = info["upfront_fraction"]
    discount: float = rates[scenario]

    # Cost during the commitment term
    discounted_monthly_full = on_demand_monthly * (1 - discount)
    term_total = discounted_monthly_full * term_months
    upfront = term_total * upfront_fraction
    monthly_during_term = (term_total - upfront) / term_months if term_months > 0 else 0.0

    # Cost during remainder of 3-year horizon (on-demand)
    remaining_months = HORIZON_MONTHS - term_months
    remaining_total = on_demand_monthly * remaining_months
    monthly_after_term = on_demand_monthly if remaining_months > 0 else monthly_during_term

    # 3-year total
    three_year_total = upfront + monthly_during_term * term_months + remaining_total
    on_demand_3yr = on_demand_monthly * HORIZON_MONTHS
    savings_usd = on_demand_3yr - three_year_total
    savings_pct = round(100 * savings_usd / on_demand_3yr) if on_demand_3yr > 0 else 0

    # Payback period: upfront ÷ monthly savings vs on-demand (during term)
    monthly_savings_during_term = on_demand_monthly - monthly_during_term
    if monthly_savings_during_term > 0 and upfront > 0:
        payback = round(upfront / monthly_savings_during_term, 1)
    elif monthly_savings_during_term > 0:
        payback = 0.0  # immediate payback (no upfront to recover)
    else:
        payback = None

    return {
        "label": info["label"],
        "monthly_usd": round(monthly_during_term, 2),
        "monthly_usd_during_term": round(monthly_during_term, 2),
        "monthly_usd_after_term": round(monthly_after_term, 2),
        "upfront_usd": round(upfront, 2),
        "term_months": term_months,
        "three_year_total_usd": round(three_year_total, 2),
        "savings_vs_ondemand_usd": round(savings_usd, 2),
        "savings_vs_ondemand_pct": savings_pct,
        "payback_months": payback,
        "discount_pct": round(discount * 100),
    }


def _on_demand_monthly_compute(
    catalog: PriceCatalog,
    items: list[ComputeItem],
    cloud: Cloud,
) -> float:
    """Total on-demand monthly compute cost for `items` on `cloud`.

    Reuses bulk_compare_compute to do the SKU matching, then extracts this cloud's
    total. Storage / OS-disk costs are NOT included — commitment optimization is
    compute-only (most clouds don't offer meaningful storage commitments).
    """
    requests = [_to_compute_request(c) for c in items]
    result = bulk_compare_compute(catalog, requests)
    return result["totals_by_cloud"].get(cloud, 0.0)


def _to_compute_request(c: ComputeItem) -> ComputeRequest:
    """Map ComputeItem → ComputeRequest (drops snapshot / OS-disk for commitment math)."""
    return ComputeRequest(
        name=c.name,
        vcpus=c.vcpus,
        memory_gb=c.memory_gb,
        quantity=c.quantity,
    )


def _all_free_result(cloud: Cloud, scenarios: list[str]) -> dict:
    """When the workload runs entirely on a free tier (e.g. OCI A1.Flex), every scenario is $0."""
    free_scenario = {
        "label": "$0 (free tier)",
        "monthly_usd": 0.0,
        "monthly_usd_during_term": 0.0,
        "monthly_usd_after_term": 0.0,
        "upfront_usd": 0.0,
        "term_months": 0,
        "three_year_total_usd": 0.0,
        "savings_vs_ondemand_usd": 0.0,
        "savings_vs_ondemand_pct": 0,
        "payback_months": None,
    }
    return {
        "kind": "commitment_optimization",
        "title": f"Commitment Optimization: {cloud.upper()}",
        "headline": (
            f"Workload fits {cloud.upper()}'s Always Free tier — no commitment "
            "savings to consider."
        ),
        "cloud": cloud,
        "on_demand_monthly_usd": 0.0,
        "horizon_months": HORIZON_MONTHS,
        "scenarios": {s: dict(free_scenario) for s in scenarios},
        "recommended": "none",
        "recommendation_reason": (
            "Already free at on-demand. No commitment upgrade gives further savings."
        ),
        "honest_gaps": [
            "OCI A1.Flex Always Free covers up to 4 OCPU + 24 GB across all instances on the tenancy",
            "If your workload outgrows the free tier, re-run optimize_commitment with the larger spec",
        ],
    }


# --- Recommendation logic ---


def _pick_recommendation(per_scenario: dict[str, dict], scenarios: list[str]) -> str | None:
    """Pick the scenario with the lowest 3-year total cost.

    All scenarios are evaluated; we just take the cheapest 3yr_total. This biases
    toward 3-year all-upfront when the cloud has aggressive upfront discounts —
    which is the right answer when total cost is the metric.
    """
    if not scenarios:
        return None
    candidates = [(s, per_scenario[s]) for s in scenarios if s in per_scenario]
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[1]["three_year_total_usd"])
    return candidates[0][0]


def _build_headline(
    cloud: Cloud,
    on_demand_monthly: float,
    recommended: str | None,
    per_scenario: dict[str, dict],
) -> str:
    if recommended is None or recommended == "none":
        return (
            f"On-demand on {cloud.upper()} (${on_demand_monthly:,.0f}/mo) "
            "is the recommended option — no commitment scenario beats it."
        )
    rec = per_scenario[recommended]
    savings_pct = rec.get("savings_vs_ondemand_pct", 0)
    payback = rec.get("payback_months")
    if payback is not None and payback > 0:
        return (
            f"{rec['label']} on {cloud.upper()} saves {savings_pct}% over 3 years; "
            f"pays back in {payback} months."
        )
    return f"{rec['label']} on {cloud.upper()} saves {savings_pct}% over 3 years."


def _build_reason(recommended: str, per_scenario: dict[str, dict]) -> str:
    rec = per_scenario[recommended]
    upfront = rec.get("upfront_usd", 0)
    savings_usd = rec.get("savings_vs_ondemand_usd", 0)
    if upfront > 0 and savings_usd > 0:
        return (
            f"3-year total ${rec['three_year_total_usd']:,.0f} vs on-demand "
            f"${rec['three_year_total_usd'] + savings_usd:,.0f}; "
            f"${savings_usd:,.0f} saved with ${upfront:,.0f} upfront."
        )
    if savings_usd > 0:
        return (
            f"${savings_usd:,.0f} saved over 3 years with no upfront cost."
        )
    return "No commitment scenario produces savings — stay on-demand."


# --- Validation ---


def _validate_scenarios(scenarios: list[str]) -> None:
    unknown = [s for s in scenarios if s not in ALL_SCENARIOS]
    if unknown:
        raise ValueError(
            f"optimize_commitment: unknown scenario(s) {unknown}. "
            f"Valid: {list(ALL_SCENARIOS)}"
        )
