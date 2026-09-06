"""
Caveats library for v0.6+ FinOps decision tools.

A caveat is a known cross-cloud risk that a tool should surface alongside its
numeric output — e.g., "OCI A1.Flex is ARM, verify your AMIs," "3-year RI not
portable, pilot first," "High egress benefits from Direct Connect, not modeled."

Schema (in caveats.json):
  - id          stable identifier for cross-references
  - cloud       "any" or a specific cloud filter
  - trigger_id  maps to a Python function below in TRIGGERS
  - message     human-readable text shown in tool output
  - severity    "info" (advisory) | "warn" (real risk) | "block" (skip target)

Adding a new caveat = add to caveats.json + (if needed) add a trigger function here.

Block-severity caveats prevent a target from being recommended in `assess_migration`
ranking. Warn-severity caveats flag a real risk. Info-severity caveats are
context for the user but don't change recommendations.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources

from .inventory import WorkloadInventory

DATA_PACKAGE = "cloudprice_mcp.data"

Severity = str  # "info" | "warn" | "block"


@dataclass(frozen=True)
class Caveat:
    id: str
    cloud: str  # "any" or "aws" / "azure" / "gcp" / "oci"
    trigger_id: str
    message: str
    severity: Severity


# --- Trigger functions ---
# Each trigger evaluates whether the caveat applies to (inventory, target_cloud, ctx).
# ctx is a dict with optional info from the caller (e.g., the per-target result dict).

TriggerFn = Callable[[WorkloadInventory, str, dict], bool]


def _source_equals_target(inv: WorkloadInventory, target: str, _ctx: dict) -> bool:
    return inv.source_cloud is not None and inv.source_cloud == target


def _compute_fits_oci_a1_flex(inv: WorkloadInventory, _target: str, _ctx: dict) -> bool:
    """OCI A1.Flex Always Free covers up to 4 OCPU + 24 GB. ARM-only.

    If any compute item fits within those limits, OCI's pricing will use A1.Flex
    (the cheapest option) — and the user needs to know it's ARM.
    """
    return any(c.vcpus <= 4 and c.memory_gb <= 24 for c in inv.compute)


def _commitment_is_3yr(inv: WorkloadInventory, _target: str, _ctx: dict) -> bool:
    return inv.commitment.startswith("3yr_")


def _internet_egress_over_100tb_per_month(
    inv: WorkloadInventory, _target: str, _ctx: dict
) -> bool:
    total_internet = sum(
        e.gb_per_month for e in inv.egress if e.direction == "out_to_internet"
    )
    return total_internet > 100_000  # 100 TB in GB


def _multi_az_enabled(inv: WorkloadInventory, _target: str, _ctx: dict) -> bool:
    return inv.multi_az


def _compute_empty(inv: WorkloadInventory, _target: str, _ctx: dict) -> bool:
    return not inv.compute and not inv.is_empty()


TRIGGERS: dict[str, TriggerFn] = {
    "source_equals_target": _source_equals_target,
    "compute_fits_oci_a1_flex": _compute_fits_oci_a1_flex,
    "commitment_is_3yr": _commitment_is_3yr,
    "internet_egress_over_100tb_per_month": _internet_egress_over_100tb_per_month,
    "multi_az_enabled": _multi_az_enabled,
    "compute_empty": _compute_empty,
}


# --- Library loading + evaluation ---


_caveat_cache: list[Caveat] | None = None


def load_caveats() -> list[Caveat]:
    """Load caveats from the bundled JSON file. Cached after first call."""
    global _caveat_cache
    if _caveat_cache is not None:
        return _caveat_cache
    text = resources.files(DATA_PACKAGE).joinpath("caveats.json").read_text(encoding="utf-8")
    raw = json.loads(text)
    _caveat_cache = [
        Caveat(
            id=entry["id"],
            cloud=entry["cloud"],
            trigger_id=entry["trigger_id"],
            message=entry["message"],
            severity=entry["severity"],
        )
        for entry in raw["caveats"]
    ]
    return _caveat_cache


def reset_caveat_cache() -> None:
    """Test helper — drop the singleton."""
    global _caveat_cache
    _caveat_cache = None


def evaluate(
    inventory: WorkloadInventory,
    target_cloud: str,
    ctx: dict | None = None,
) -> list[Caveat]:
    """Return all caveats triggered for this (inventory, target_cloud, ctx) tuple.

    Order matters for display: blocks first, then warns, then infos.
    """
    ctx = ctx or {}
    triggered: list[Caveat] = []
    for caveat in load_caveats():
        if caveat.cloud != "any" and caveat.cloud != target_cloud:
            continue
        trigger = TRIGGERS.get(caveat.trigger_id)
        if trigger is None:
            continue  # unknown trigger ID — skip gracefully (forward-compat)
        if trigger(inventory, target_cloud, ctx):
            triggered.append(caveat)
    severity_order = {"block": 0, "warn": 1, "info": 2}
    triggered.sort(key=lambda c: severity_order.get(c.severity, 99))
    return triggered


def split_by_severity(caveats: list[Caveat]) -> dict[str, list[Caveat]]:
    """Convenience: group triggered caveats by severity for tool output."""
    out: dict[str, list[Caveat]] = {"block": [], "warn": [], "info": []}
    for c in caveats:
        out.setdefault(c.severity, []).append(c)
    return out


def has_blocker(caveats: list[Caveat]) -> bool:
    """True if any caveat is severity=block (target should not be recommended)."""
    return any(c.severity == "block" for c in caveats)
