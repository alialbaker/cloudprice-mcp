"""
`assess_migration` — the "should I move?" FinOps decision tool.

Given a workload inventory currently running on `source_cloud`, project the steady-
state monthly cost on every other cloud, the one-time exit cost (egress to leave
the source), and the payback period. Returns a structured dict consumable by
export.py renderers.

This is an orchestrator over the existing per-section comparators in compare.py:
  - compare_workload          (compute + block storage, with multi-AZ + commitment)
  - compare_object_storage    (S3 / Blob / Cloud Storage / Object Storage)
  - compare_postgres          (RDS / Azure DB / Cloud SQL / OCI DB with PostgreSQL)
  - compare_egress            (internet egress, tiered + free-tier-aware)
  - egress.inter_region_cost_for_gb  (cross-region within the same cloud)

No new pricing data — pure orchestration on top of v0.5's catalog.
"""
from __future__ import annotations

from typing import Literal

from ..caveats import evaluate as evaluate_caveats
from ..compare import (
    CLOUDS,
    ComputeRequest,
    EgressRequest,
    ObjectStorageRequest,
    PostgresRequest,
    StorageRequest,
    compare_egress,
    compare_object_storage,
    compare_postgres,
    compare_workload,
)
from ..inventory import (
    ComputeItem,
    DatabaseItem,
    EgressItem,
    ObjectStorageItem,
    StorageItem,
    WorkloadInventory,
)
from ..pricing import Cloud, PriceCatalog

# Map our extended commitment enum (6 values in inventory) onto the 3 v0.5 tiers
# until v0.6.x ships per-family RI rates. Conservative downcast: any 1-year option
# uses the 1yr_no_upfront discount; any 3-year option uses 3yr_partial_upfront.
_COMMITMENT_TO_V05_TIER: dict[str, Literal["none", "1yr_no_upfront", "3yr_partial_upfront"]] = {
    "none": "none",
    "1yr_no_upfront": "1yr_no_upfront",
    "1yr_all_upfront": "1yr_no_upfront",
    "3yr_no_upfront": "3yr_partial_upfront",
    "3yr_partial_upfront": "3yr_partial_upfront",
    "3yr_all_upfront": "3yr_partial_upfront",
}


def assess_migration(
    catalog: PriceCatalog,
    inventory: WorkloadInventory,
    targets: list[Cloud] | None = None,
) -> dict:
    """Project cross-cloud cost + payback for moving `inventory` away from its source.

    Returns a result dict with the canonical FinOps output shape, suitable for
    rendering via export.py and direct return from MCP tools.
    """
    if not inventory.source_cloud:
        raise ValueError(
            "assess_migration: inventory.source_cloud must be set "
            "(e.g. 'aws' / 'azure' / 'gcp' / 'oci')."
        )
    if inventory.is_empty():
        raise ValueError(
            "assess_migration: inventory has no workload items — nothing to assess."
        )

    source = inventory.source_cloud
    target_clouds: list[Cloud] = list(targets) if targets else [
        c for c in CLOUDS if c != source
    ]
    if not target_clouds:
        raise ValueError(
            "assess_migration: no target clouds to evaluate (source equals all known clouds?)."
        )

    # 1. Source baseline
    source_monthly = _total_monthly_for_cloud(catalog, inventory, source)

    # 2. One-time exit cost
    exit_cost = _exit_cost(catalog, inventory)

    # 3. Per-target cost + payback + caveats
    per_target: dict[str, dict] = {}
    for target in target_clouds:
        target_monthly = _total_monthly_for_cloud(catalog, inventory, target)
        savings_pct = (
            round(100 * (source_monthly - target_monthly) / source_monthly)
            if source_monthly > 0
            else 0
        )
        monthly_savings = source_monthly - target_monthly
        if monthly_savings > 0 and exit_cost > 0:
            payback_months = round(exit_cost / monthly_savings, 1)
        elif monthly_savings > 0:
            payback_months = 0.0  # immediate payback (no exit cost)
        else:
            payback_months = None

        triggered = evaluate_caveats(
            inventory, target, ctx={"target_monthly": target_monthly}
        )

        per_target[target] = {
            "monthly_usd": round(target_monthly, 2),
            "savings_vs_source_pct": savings_pct,
            "payback_months": payback_months,
            "three_year_total_usd": round(target_monthly * 36 + exit_cost, 2),
            "caveats": [c.message for c in triggered if c.severity == "warn"],
            "blockers": [c.message for c in triggered if c.severity == "block"],
            "info": [c.message for c in triggered if c.severity == "info"],
        }

    # 4. Ranking — exclude blocked targets
    rankable = [
        (name, t) for name, t in per_target.items() if not t["blockers"]
    ]
    rankable.sort(key=lambda x: x[1]["three_year_total_usd"])
    ranking = [name for name, _ in rankable]
    recommended = ranking[0] if ranking else None

    headline = _build_headline(source, recommended, per_target, exit_cost)

    return {
        "kind": "migration_assessment",
        "title": f"Migration Assessment: {source.upper()}",
        "headline": headline,
        "source_cloud": source,
        "source_monthly_usd": round(source_monthly, 2),
        "targets": per_target,
        "one_time_exit_cost_usd": round(exit_cost, 2),
        "ranking_by_3yr_tco": ranking,
        "recommended": recommended,
        "honest_gaps": [
            "No workload discovery — relies on user-supplied inventory",
            "No license portability modeling (BYOL, SQL Server, RHEL)",
            "No CPU-utilization right-sizing (we trust supplied vCPU/RAM)",
            "Conservative flat commitment tiers (per-family RI rates land in v0.6.x)",
            "No performance / latency comparison (cost-only)",
        ],
    }


# --- Total cost computation ---


def _total_monthly_for_cloud(
    catalog: PriceCatalog, inv: WorkloadInventory, cloud: Cloud
) -> float:
    """Sum monthly cost on `cloud` across every workload section.

    Reuses the existing per-section comparators by extracting just this cloud's
    total from each result. Each section gets its own helper to keep this
    orchestrator linear and easy to follow.
    """
    return (
        _compute_storage_cost(catalog, inv, cloud)
        + _object_storage_cost(catalog, inv, cloud)
        + _database_cost(catalog, inv, cloud)
        + _internet_egress_cost(catalog, inv, cloud)
        + _inter_region_egress_cost(catalog, inv, cloud)
    )


def _compute_storage_cost(
    catalog: PriceCatalog, inv: WorkloadInventory, cloud: Cloud
) -> float:
    if not (inv.compute or inv.storage):
        return 0.0
    commitment = _COMMITMENT_TO_V05_TIER.get(inv.commitment, "none")
    compute_reqs = [_compute_to_request(c) for c in inv.compute]
    storage_reqs = [_storage_to_request(s) for s in inv.storage]
    result = compare_workload(
        catalog,
        compute_reqs,
        storage_reqs,
        commitment=commitment,
        multi_az=inv.multi_az,
    )
    if commitment != "none" and "commitment" in result:
        return result["commitment"]["totals_by_cloud"].get(cloud, 0)
    if result.get("combined"):
        return result["combined"]["totals_by_cloud"].get(cloud, 0)
    return 0.0


def _object_storage_cost(
    catalog: PriceCatalog, inv: WorkloadInventory, cloud: Cloud
) -> float:
    if not inv.object_storage:
        return 0.0
    reqs = [_object_to_request(o) for o in inv.object_storage]
    result = compare_object_storage(catalog, reqs)
    return result["totals_by_cloud"].get(cloud, 0)


def _database_cost(
    catalog: PriceCatalog, inv: WorkloadInventory, cloud: Cloud
) -> float:
    if not inv.databases:
        return 0.0
    reqs = [_database_to_postgres_request(d) for d in inv.databases]
    result = compare_postgres(catalog, reqs)
    return result["totals_by_cloud"].get(cloud, 0)


def _internet_egress_cost(
    catalog: PriceCatalog, inv: WorkloadInventory, cloud: Cloud
) -> float:
    internet = [e for e in inv.egress if e.direction == "out_to_internet"]
    if not internet:
        return 0.0
    reqs = [_egress_to_request(e) for e in internet]
    result = compare_egress(catalog, reqs)
    return result["totals_by_cloud"].get(cloud, 0)


def _inter_region_egress_cost(
    catalog: PriceCatalog, inv: WorkloadInventory, cloud: Cloud
) -> float:
    inter_region = [e for e in inv.egress if e.direction == "inter_region"]
    if not inter_region:
        return 0.0
    sku = catalog.egress_for(cloud)
    if sku is None:
        return 0.0
    return sum(sku.inter_region_cost_for_gb(e.gb_per_month) for e in inter_region)


def _exit_cost(catalog: PriceCatalog, inv: WorkloadInventory) -> float:
    """One-time egress cost to leave the source cloud for `data_to_migrate_gb`."""
    if inv.one_time.data_to_migrate_gb <= 0:
        return 0.0
    if inv.source_cloud is None:
        return 0.0
    sku = catalog.egress_for(inv.source_cloud)
    if sku is None:
        return 0.0
    return sku.cost_for_gb(inv.one_time.data_to_migrate_gb)


# --- Inventory item → existing request dataclass adapters ---


def _compute_to_request(c: ComputeItem) -> ComputeRequest:
    return ComputeRequest(
        name=c.name,
        vcpus=c.vcpus,
        memory_gb=c.memory_gb,
        quantity=c.quantity,
        os_disk_gb=c.os_disk_gb,
        os_disk_type=c.os_disk_type,
        os_disk_snapshot_count=c.snapshot_count,
        os_disk_snapshot_incremental_factor=c.snapshot_incremental_factor,
    )


def _storage_to_request(s: StorageItem) -> StorageRequest:
    return StorageRequest(
        name=s.name,
        capacity_gb=s.capacity_gb,
        disk_type=s.disk_type,
        quantity=s.quantity,
        snapshot_count=s.snapshot_count,
        snapshot_incremental_factor=s.snapshot_incremental_factor,
    )


def _object_to_request(o: ObjectStorageItem) -> ObjectStorageRequest:
    return ObjectStorageRequest(
        name=o.name,
        capacity_gb=o.capacity_gb,
        tier=o.tier,
        quantity=o.quantity,
    )


def _database_to_postgres_request(d: DatabaseItem) -> PostgresRequest:
    return PostgresRequest(
        name=d.name,
        vcpus=d.vcpus,
        memory_gb=d.memory_gb,
        storage_gb=d.storage_gb,
        quantity=d.quantity,
    )


def _egress_to_request(e: EgressItem) -> EgressRequest:
    return EgressRequest(
        name=e.name,
        gb_per_month=e.gb_per_month,
        direction=e.direction,
    )


# --- Headline builder ---


def _build_headline(
    source: str,
    recommended: str | None,
    per_target: dict[str, dict],
    exit_cost: float,
) -> str:
    if recommended is None:
        return f"No viable target found for {source.upper()} workload (all targets blocked)."
    rec = per_target[recommended]
    savings_pct = rec.get("savings_vs_source_pct", 0)
    payback = rec.get("payback_months")

    if savings_pct > 0:
        if payback is not None and payback > 0:
            return (
                f"{recommended.upper()} is cheapest by {savings_pct}%; "
                f"payback {payback} months on $${exit_cost:,.0f} exit cost"
            ).replace("$$", "$")
        return f"{recommended.upper()} is cheapest by {savings_pct}% (no exit cost)"
    if savings_pct == 0:
        return f"{recommended.upper()} ties with {source.upper()} on monthly cost"
    return (
        f"{recommended.upper()} is the cheapest target but more expensive than "
        f"staying on {source.upper()}; consider not moving."
    )
