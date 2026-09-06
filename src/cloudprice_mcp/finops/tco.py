"""
`compare_total_cost_of_ownership` — multi-year cross-cloud TCO with growth modeling.

Given a workload inventory, projects per-cloud per-year cost across a configurable
horizon (default 3 years), applying growth-rate assumptions for compute / storage /
egress. Returns cumulative TCO, year-by-year breakdown, and a sensitivity
indicator on the most impactful variable.

This is the "what does this cost over 3 years?" tool — the kind of number that
goes into board decks and budget conversations. Most teams compute this in a
spreadsheet by hand; cloudprice-mcp turns it into a single tool call.

Scope for v0.6.0:
- Linear year-over-year growth for compute / storage / egress (one rate each)
- 3-year horizon by default; configurable
- Source cloud included in projection if specified, plus all targets
- Sensitivity analysis: identifies which growth assumption matters most
- Reuses migration.py's per-section cost helpers (no duplicate work)

Out of scope (deferred):
- Non-linear growth curves (S-curves, seasonality)
- Per-workload-section growth (one rate per section is enough for v0.6)
- Discount-rate / NPV math (cumulative dollars, not present value)
- License costs / labor costs / migration project costs
"""
from __future__ import annotations

from dataclasses import dataclass

from ..compare import CLOUDS
from ..inventory import WorkloadInventory
from ..pricing import Cloud, PriceCatalog
from .migration import (
    _compute_storage_cost,
    _database_cost,
    _inter_region_egress_cost,
    _internet_egress_cost,
    _object_storage_cost,
)

DEFAULT_HORIZON_YEARS = 3
MONTHS_PER_YEAR = 12


@dataclass(frozen=True)
class GrowthAssumptions:
    """Year-over-year linear growth rates expressed as decimal multipliers.

    A value of 0.20 means +20% YoY (each year's cost is 1.2× the previous year).
    """
    compute_pct_yoy: float = 0.0
    storage_pct_yoy: float = 0.0
    egress_pct_yoy: float = 0.0

    def as_dict(self) -> dict:
        return {
            "compute_pct_yoy": self.compute_pct_yoy,
            "storage_pct_yoy": self.storage_pct_yoy,
            "egress_pct_yoy": self.egress_pct_yoy,
        }


def compare_total_cost_of_ownership(
    catalog: PriceCatalog,
    inventory: WorkloadInventory,
    horizon_years: int = DEFAULT_HORIZON_YEARS,
    growth: GrowthAssumptions | None = None,
    targets: list[Cloud] | None = None,
) -> dict:
    """Project per-cloud per-year cost over `horizon_years`.

    Args:
        catalog: pricing catalog (bundled or live)
        inventory: workload inventory (year 1 baseline; growth applied to subsequent years)
        horizon_years: how many years to project (default 3)
        growth: per-section growth assumptions (default 0% — flat workload)
        targets: clouds to evaluate (default: all 4 clouds)

    Returns:
        dict with `kind=total_cost_of_ownership`, per-cloud per-year matrix,
        cumulative TCO, ranking, sensitivity analysis.
    """
    if inventory.is_empty():
        raise ValueError(
            "compare_total_cost_of_ownership: inventory has no workload items."
        )
    if horizon_years < 1:
        raise ValueError(
            f"compare_total_cost_of_ownership: horizon_years must be >= 1, got {horizon_years}."
        )

    growth = growth or GrowthAssumptions()
    target_clouds: list[Cloud] = list(targets) if targets else list(CLOUDS)

    # 1. Year-1 cost baseline per cloud, broken down by section
    baseline = _baseline_costs_per_cloud(catalog, inventory, target_clouds)

    # 2. Apply growth to project per-year per-cloud
    per_cloud_per_year = _project_growth(baseline, growth, horizon_years)

    # 3. Cumulative TCO per cloud
    cumulative = {
        cloud: round(sum(year["total_usd"] for year in years), 2)
        for cloud, years in per_cloud_per_year.items()
    }

    # 4. Ranking
    ranking = sorted(cumulative.keys(), key=lambda c: cumulative[c])

    # 5. Sensitivity analysis on the dominant cost driver
    sensitivity = _build_sensitivity(baseline, growth, horizon_years, ranking)

    headline = _build_headline(ranking, cumulative, horizon_years)

    return {
        "kind": "total_cost_of_ownership",
        "title": f"Total Cost of Ownership: {horizon_years}-year projection",
        "headline": headline,
        "horizon_years": horizon_years,
        "growth_assumptions": growth.as_dict(),
        "per_cloud_per_year": per_cloud_per_year,
        "cumulative_tco_usd": cumulative,
        "ranking_by_tco": ranking,
        "recommended": ranking[0] if ranking else None,
        "sensitivity": sensitivity,
        "honest_gaps": [
            "Linear YoY growth only (no S-curves or seasonality)",
            "One growth rate per section (compute / storage / egress) — not per-workload-row",
            "Cumulative dollars, not NPV (no discount rate applied)",
            "License costs / labor / migration project costs not included",
            "List-price baseline; commitment discounts not applied (use optimize_commitment for that)",
        ],
    }


# --- Baseline computation ---


def _baseline_costs_per_cloud(
    catalog: PriceCatalog,
    inv: WorkloadInventory,
    clouds: list[Cloud],
) -> dict[Cloud, dict[str, float]]:
    """Year-1 monthly cost per cloud, broken down by category.

    Returns: {cloud: {"compute_storage": $, "object_storage": $, "database": $, "egress": $}}
    """
    out: dict[Cloud, dict[str, float]] = {}
    for cloud in clouds:
        out[cloud] = {
            "compute_storage": _compute_storage_cost(catalog, inv, cloud),
            "object_storage": _object_storage_cost(catalog, inv, cloud),
            "database": _database_cost(catalog, inv, cloud),
            "egress": (
                _internet_egress_cost(catalog, inv, cloud)
                + _inter_region_egress_cost(catalog, inv, cloud)
            ),
        }
    return out


# --- Growth projection ---


def _project_growth(
    baseline: dict[Cloud, dict[str, float]],
    growth: GrowthAssumptions,
    horizon_years: int,
) -> dict[Cloud, list[dict]]:
    """Apply linear YoY growth to project per-year totals.

    Year 1 is the baseline (multiplier 1.0). Year N's multiplier is (1 + rate)^(N-1).
    The rate is applied per category — compute_storage uses compute_pct_yoy, etc.
    """
    per_cloud: dict[Cloud, list[dict]] = {}
    for cloud, sections in baseline.items():
        years = []
        for year_idx in range(horizon_years):
            compute_mult = (1 + growth.compute_pct_yoy) ** year_idx
            storage_mult = (1 + growth.storage_pct_yoy) ** year_idx
            egress_mult = (1 + growth.egress_pct_yoy) ** year_idx

            compute_storage_year = sections["compute_storage"] * compute_mult * MONTHS_PER_YEAR
            object_storage_year = sections["object_storage"] * storage_mult * MONTHS_PER_YEAR
            database_year = sections["database"] * compute_mult * MONTHS_PER_YEAR
            egress_year = sections["egress"] * egress_mult * MONTHS_PER_YEAR

            total_year = (
                compute_storage_year + object_storage_year + database_year + egress_year
            )
            years.append({
                "year": year_idx + 1,
                "compute_storage_usd": round(compute_storage_year, 2),
                "object_storage_usd": round(object_storage_year, 2),
                "database_usd": round(database_year, 2),
                "egress_usd": round(egress_year, 2),
                "total_usd": round(total_year, 2),
            })
        per_cloud[cloud] = years
    return per_cloud


# --- Sensitivity analysis ---


def _build_sensitivity(
    baseline: dict[Cloud, dict[str, float]],
    growth: GrowthAssumptions,
    horizon_years: int,
    ranking: list[Cloud],
) -> dict:
    """Identify which growth assumption matters most for the recommendation.

    Method: for each growth dimension, recompute the cumulative TCO with that
    rate doubled. Whichever dimension changes the recommended cloud's TCO most
    is the most sensitive variable.
    """
    if not ranking:
        return {"dominant_variable": None, "rationale": "no clouds evaluated"}

    cheapest = ranking[0]
    base_cumulative = _cumulative_for_cloud(baseline, growth, horizon_years, cheapest)

    # Bump each growth rate up by 10 percentage points and measure impact
    impacts: list[tuple[str, float]] = []
    for name, base_rate in [
        ("compute_pct_yoy", growth.compute_pct_yoy),
        ("storage_pct_yoy", growth.storage_pct_yoy),
        ("egress_pct_yoy", growth.egress_pct_yoy),
    ]:
        bumped_growth = GrowthAssumptions(
            compute_pct_yoy=growth.compute_pct_yoy + (0.10 if name == "compute_pct_yoy" else 0),
            storage_pct_yoy=growth.storage_pct_yoy + (0.10 if name == "storage_pct_yoy" else 0),
            egress_pct_yoy=growth.egress_pct_yoy + (0.10 if name == "egress_pct_yoy" else 0),
        )
        bumped = _cumulative_for_cloud(baseline, bumped_growth, horizon_years, cheapest)
        delta = bumped - base_cumulative
        impacts.append((name, delta))

    impacts.sort(key=lambda x: abs(x[1]), reverse=True)
    dominant_name, dominant_delta = impacts[0]

    return {
        "dominant_variable": dominant_name,
        "delta_per_10pct_bump_usd": round(dominant_delta, 2),
        "rationale": (
            f"Bumping {dominant_name} by +10pp YoY changes {cheapest.upper()}'s "
            f"{horizon_years}-year TCO by ${dominant_delta:,.0f}. "
            f"This is the variable to scrutinize most carefully."
        ),
    }


def _cumulative_for_cloud(
    baseline: dict[Cloud, dict[str, float]],
    growth: GrowthAssumptions,
    horizon_years: int,
    cloud: Cloud,
) -> float:
    """Helper for sensitivity — cumulative TCO for one cloud."""
    sections = baseline[cloud]
    total = 0.0
    for year_idx in range(horizon_years):
        compute_mult = (1 + growth.compute_pct_yoy) ** year_idx
        storage_mult = (1 + growth.storage_pct_yoy) ** year_idx
        egress_mult = (1 + growth.egress_pct_yoy) ** year_idx
        total += (
            sections["compute_storage"] * compute_mult * MONTHS_PER_YEAR
            + sections["object_storage"] * storage_mult * MONTHS_PER_YEAR
            + sections["database"] * compute_mult * MONTHS_PER_YEAR
            + sections["egress"] * egress_mult * MONTHS_PER_YEAR
        )
    return total


# --- Headline ---


def _build_headline(
    ranking: list[Cloud],
    cumulative: dict[Cloud, float],
    horizon_years: int,
) -> str:
    if not ranking:
        return "No clouds evaluated."
    cheapest = ranking[0]
    if len(ranking) == 1:
        return (
            f"{cheapest.upper()}: ${cumulative[cheapest]:,.0f} over {horizon_years} years."
        )
    runner_up = ranking[1]
    savings = cumulative[runner_up] - cumulative[cheapest]
    if savings > 0:
        return (
            f"{cheapest.upper()} is cheapest at ${cumulative[cheapest]:,.0f} over "
            f"{horizon_years} years — ${savings:,.0f} less than the runner-up "
            f"({runner_up.upper()})."
        )
    return f"{cheapest.upper()} ties for cheapest over {horizon_years} years."
