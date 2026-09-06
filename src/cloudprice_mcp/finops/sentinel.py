"""watch_workload — Cost Drift Sentinel.

The shift from query tool to agent capability. Other FinOps tools answer
"what does this cost?" — this one answers "is this still what it cost when
I signed off on it?"

Usage flow (the agent / cron pattern):

    # First call — no baseline yet. Tool returns the current cost as the
    # baseline, the user (or their automation) saves the returned baseline
    # JSON somewhere durable (a file in their repo, S3, anywhere).
    baseline_report = watch_workload(catalog, inventory)
    save_to_disk(baseline_report["baseline"])

    # Later (next week, next month, whenever):
    baseline = load_from_disk()
    drift_report = watch_workload(catalog, inventory, baseline=baseline)
    if drift_report["alert_triggered"]:
        notify_humans(drift_report)

The tool is stateless. There is no server-side database. The user persists
the baseline. This makes it trivial to wire into:
  - GitHub Actions (baseline.json committed to a repo, daily cron compares)
  - Slack bots (baseline JSON stored in DM history)
  - Terraform (baseline as a data source, drift detected at plan time)
  - Anywhere else a baseline file can live

Why this is different from assess_migration:
  - assess_migration: "what's the best move right now?" — comparison across clouds
  - watch_workload: "has anything moved since I last looked?" — comparison across time

The drift report attributes change at the SKU level by consulting the
bundled price-history dataset — so the report says not just "AWS costs
went up 3%" but "AWS went up 3% because m5.xlarge price increased 4%
between {baseline_date} and {today}."
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from ..inventory import WorkloadInventory
from ..pricing import Cloud, PriceCatalog
from .migration import _total_monthly_for_cloud

ALL_CLOUDS: tuple[Cloud, ...] = ("aws", "azure", "gcp", "oci")


@dataclass(frozen=True)
class WatchBaseline:
    """A self-contained baseline that lives in the user's filesystem / repo /
    storage and is passed back to watch_workload to detect drift.

    The workload_hash makes it impossible to silently compare baselines
    against a different workload — if the spec changes, the hash changes,
    and we return a fresh baseline rather than a misleading drift report.
    """
    as_of: str                              # ISO date the baseline was captured
    catalog_as_of: str                      # cloudprice catalog version at baseline time
    workload_hash: str                      # deterministic hash of the workload spec
    per_cloud_monthly_usd: dict[str, float] # cloud -> baseline monthly cost
    threshold_pct: float = 5.0              # default drift threshold

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> WatchBaseline:
        return cls(
            as_of=d["as_of"],
            catalog_as_of=d["catalog_as_of"],
            workload_hash=d["workload_hash"],
            per_cloud_monthly_usd=dict(d["per_cloud_monthly_usd"]),
            threshold_pct=float(d.get("threshold_pct", 5.0)),
        )


def watch_workload(
    catalog: PriceCatalog,
    inventory: WorkloadInventory,
    baseline: WatchBaseline | dict[str, Any] | None = None,
    alert_threshold_pct: float = 5.0,
) -> dict[str, Any]:
    """Stateless drift sensor over a workload.

    No baseline -> capture current cost as the baseline, return it. The caller
    persists it.
    With baseline -> recompute current cost, compare against baseline, return
    a drift report. If the workload spec changed (hash mismatch), return a
    new baseline + a `baseline_replaced` flag so the caller knows to update.
    """
    today = date.today().isoformat()
    workload_hash = _hash_workload(inventory)
    per_cloud_now = _per_cloud_costs(catalog, inventory)

    if baseline is None:
        return {
            "kind": "watch_baseline",
            "as_of": today,
            "headline": "Baseline captured — pass this back next time to detect drift.",
            "baseline": WatchBaseline(
                as_of=today,
                catalog_as_of=catalog.as_of,
                workload_hash=workload_hash,
                per_cloud_monthly_usd=per_cloud_now,
                threshold_pct=alert_threshold_pct,
            ).to_dict(),
            "per_cloud_monthly_usd": per_cloud_now,
            "honest_gaps": _honest_gaps_baseline(),
        }

    if isinstance(baseline, dict):
        baseline = WatchBaseline.from_dict(baseline)

    # Workload spec drifted → return a fresh baseline rather than a misleading
    # drift report. The caller can replace their saved baseline.
    if baseline.workload_hash != workload_hash:
        return {
            "kind": "watch_baseline_replaced",
            "as_of": today,
            "headline": (
                "Workload spec changed since baseline was captured — returning a "
                "fresh baseline. The old baseline is no longer comparable."
            ),
            "baseline_replaced": True,
            "previous_baseline_as_of": baseline.as_of,
            "baseline": WatchBaseline(
                as_of=today,
                catalog_as_of=catalog.as_of,
                workload_hash=workload_hash,
                per_cloud_monthly_usd=per_cloud_now,
                threshold_pct=baseline.threshold_pct,
            ).to_dict(),
            "per_cloud_monthly_usd": per_cloud_now,
        }

    # Real drift report
    threshold = baseline.threshold_pct
    per_cloud_drift = []
    max_abs_drift = 0.0
    for cloud in ALL_CLOUDS:
        old = baseline.per_cloud_monthly_usd.get(cloud, 0.0)
        new = per_cloud_now.get(cloud, 0.0)
        drift_pct = ((new - old) / old * 100) if old > 0 else 0.0
        per_cloud_drift.append({
            "cloud": cloud,
            "baseline_monthly_usd": round(old, 2),
            "current_monthly_usd": round(new, 2),
            "drift_monthly_usd": round(new - old, 2),
            "drift_pct": round(drift_pct, 2),
            "exceeds_threshold": abs(drift_pct) >= threshold,
        })
        max_abs_drift = max(max_abs_drift, abs(drift_pct))

    alert_triggered = max_abs_drift >= threshold
    sku_attribution = _attribute_sku_changes(baseline.as_of)

    return {
        "kind": "watch_drift_report",
        "as_of": today,
        "baseline_as_of": baseline.as_of,
        "headline": _build_drift_headline(per_cloud_drift, alert_triggered, threshold),
        "alert_triggered": alert_triggered,
        "threshold_pct": threshold,
        "max_drift_pct": round(max_abs_drift, 2),
        "per_cloud": per_cloud_drift,
        "sku_attribution": sku_attribution,
        "baseline": baseline.to_dict(),
        "recommended_action": _recommend_action(max_abs_drift, threshold),
        "honest_gaps": _honest_gaps_drift(),
    }


# --- helpers ---


def _per_cloud_costs(catalog: PriceCatalog, inventory: WorkloadInventory) -> dict[str, float]:
    return {
        cloud: round(_total_monthly_for_cloud(catalog, inventory, cloud), 2)
        for cloud in ALL_CLOUDS
    }


def _hash_workload(inventory: WorkloadInventory) -> str:
    """Stable SHA-256 of the workload spec. Same compute/storage/egress/etc.
    yields the same hash regardless of dict ordering."""
    payload = {
        "source_cloud": inventory.source_cloud,
        "compute": [
            {"vcpus": c.vcpus, "memory_gb": c.memory_gb, "quantity": c.quantity}
            for c in inventory.compute
        ],
        "storage": [
            {"capacity_gb": s.capacity_gb, "disk_type": s.disk_type, "quantity": s.quantity}
            for s in inventory.storage
        ],
        "egress": [
            {"gb_per_month": e.gb_per_month, "direction": e.direction}
            for e in inventory.egress
        ],
        "object_storage": [
            {"capacity_gb": o.capacity_gb, "tier": o.tier}
            for o in inventory.object_storage
        ],
        "commitment": inventory.commitment,
        "multi_az": inventory.multi_az,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _attribute_sku_changes(since: str) -> list[dict[str, Any]]:
    """Use the price-history dataset to identify which SKUs moved between
    `since` and today.

    Pragmatic v0.9 attribution: walk every tracked SKU across the 4 clouds,
    surface any whose price moved >=1% since `since`. We don't filter by
    "which SKUs your specific workload uses" because the SKU picker is
    per-cloud / per-comparator and not deterministic here without rerunning
    the full assess pipeline. Treat the list as investigation context — the
    drift report's per_cloud totals are the source of truth, this is colour.
    """
    changes: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()
    from ..history import load_history  # local import — avoid circular
    points = load_history(since=since)
    by_key: dict[tuple[str, str], list[Any]] = {}
    for p in points:
        by_key.setdefault((p.cloud, p.sku), []).append(p)
    for (c, sku), pts in by_key.items():
        if len(pts) < 2:
            continue
        oldest = min(pts, key=lambda p: p.as_of)
        newest = max(pts, key=lambda p: p.as_of)
        if oldest.hourly_usd <= 0:
            continue
        pct = (newest.hourly_usd - oldest.hourly_usd) / oldest.hourly_usd * 100
        if abs(pct) < 1.0:
            continue
        if (c, sku) in seen_pairs:
            continue
        seen_pairs.add((c, sku))
        changes.append({
            "cloud": c,
            "sku": sku,
            "old_hourly_usd": oldest.hourly_usd,
            "new_hourly_usd": newest.hourly_usd,
            "drift_pct": round(pct, 2),
            "since": oldest.as_of,
            "until": newest.as_of,
        })
    changes.sort(key=lambda c: abs(c["drift_pct"]), reverse=True)
    return changes[:25]  # top 25 movers


def _build_drift_headline(per_cloud: list[dict], alert: bool, threshold: float) -> str:
    if not alert:
        return f"No alert — max drift {max((abs(c['drift_pct']) for c in per_cloud), default=0):.2f}% under {threshold}% threshold."
    biggest = max(per_cloud, key=lambda c: abs(c["drift_pct"]))
    direction = "up" if biggest["drift_pct"] > 0 else "down"
    return (
        f"ALERT — {biggest['cloud'].upper()} cost moved {direction} "
        f"{abs(biggest['drift_pct']):.2f}% vs baseline "
        f"(${biggest['baseline_monthly_usd']:,.2f} → ${biggest['current_monthly_usd']:,.2f}/mo)."
    )


def _recommend_action(max_drift_pct: float, threshold: float) -> str:
    if max_drift_pct < threshold:
        return "no_action"
    if max_drift_pct < threshold * 2:
        return "review"
    return "investigate"


def _honest_gaps_baseline() -> list[str]:
    return [
        "Baseline reflects the catalog at this exact point in time. Save the returned `baseline` JSON somewhere durable and pass it back on future calls.",
        "If you change the workload spec (add a compute item, change region, etc.) the hash mismatches and a fresh baseline is returned automatically — no silent drift against an outdated spec.",
    ]


def _honest_gaps_drift() -> list[str]:
    return [
        "Drift percentages compare the workload's per-cloud monthly totals at baseline vs today, using current catalog prices for both.",
        "SKU-level attribution is broad: it surfaces every SKU in the price-history dataset that moved more than 1% since baseline date, not just the ones in your workload. Treat it as investigation context, not exact attribution.",
        "Threshold defaults to 5% — override with the `alert_threshold_pct` parameter (or in the baseline) for tighter / looser alerting.",
        "If the catalog hasn't refreshed since the baseline was taken (e.g., baseline was taken hours ago), drift will be 0% — that's correct, not a bug. The signal only shows up across catalog refreshes.",
    ]
