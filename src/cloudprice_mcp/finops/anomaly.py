"""detect_price_anomalies — statistical detection of unusual price moves.

Compounds the v0.7.1 price-history dataset moat with content discovery:
"this week's biggest movers" becomes auto-detectable once we have 3+
snapshots. The more snapshots accumulate, the better detection gets — a
nice flywheel for the public history archive.

Two detection methods, chosen by sensitivity:

1. PERCENT-CHANGE (default for small datasets):
   - For each (cloud, sku), compute % change between the earliest and latest
     snapshot in the window.
   - Flag any SKU whose absolute % change exceeds a threshold (default 5%).
   - Works with as few as 2 snapshots.

2. Z-SCORE (auto-selected when len(snapshots) >= 5):
   - For each (cloud, sku), compute z-score of the most recent point
     against the historical mean ± stdev.
   - Flag any SKU whose |z| >= 2 (default).
   - Detects single-week shocks even within a noisy trend.

The honest disclosure: with only 3-4 snapshots, statistical "anomaly" is
mostly the v0.7.0 -> v0.8.0 corrections we already know about (e.g., OCI
-72.78%). The tool's real value compounds as the dataset grows past 12
weeks; by then it auto-surfaces genuinely unusual moves nobody manually
noticed.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any, Literal

from ..history import list_snapshot_dates, load_history
from ..pricing import Cloud

Sensitivity = Literal["strict", "moderate", "permissive"]
Method = Literal["percent_change", "z_score", "auto"]

# Defaults per sensitivity. Tuned conservatively — false positives at lower
# sensitivities feed straight into "weekly LinkedIn anomaly post" content.
_SENSITIVITY_THRESHOLDS: dict[Sensitivity, tuple[float, float]] = {
    # (pct_change_threshold, z_score_threshold)
    "strict":     (10.0, 2.5),   # only flag dramatic moves
    "moderate":   (5.0, 2.0),    # default — catches real moves, few false positives
    "permissive": (2.0, 1.5),    # surface gentle drifts too
}


@dataclass(frozen=True)
class Anomaly:
    cloud: str
    sku: str
    method: Method
    severity: float            # absolute % change OR |z-score|
    change_pct: float          # signed % change earliest -> latest in window
    direction: Literal["up", "down"]
    earliest_as_of: str
    latest_as_of: str
    earliest_hourly_usd: float
    latest_hourly_usd: float
    data_points: int
    z_score: float | None = None  # populated when method=z_score

    def to_dict(self) -> dict[str, Any]:
        out = {
            "cloud": self.cloud,
            "sku": self.sku,
            "method": self.method,
            "severity": round(self.severity, 3),
            "change_pct": round(self.change_pct, 2),
            "direction": self.direction,
            "earliest_as_of": self.earliest_as_of,
            "latest_as_of": self.latest_as_of,
            "earliest_hourly_usd": self.earliest_hourly_usd,
            "latest_hourly_usd": self.latest_hourly_usd,
            "data_points": self.data_points,
        }
        if self.z_score is not None:
            out["z_score"] = round(self.z_score, 3)
        return out


def detect_price_anomalies(
    since: str | None = None,
    cloud: Cloud | None = None,
    sensitivity: Sensitivity = "moderate",
    method: Method = "auto",
    limit: int = 25,
) -> dict[str, Any]:
    """Detect unusual price moves in the bundled price-history dataset.

    Args:
        since: ISO date — restrict to snapshots on or after this date. None = all history.
        cloud: filter to one cloud. None = all 4.
        sensitivity: "strict" / "moderate" / "permissive" — threshold preset.
        method: "percent_change" / "z_score" / "auto". Auto picks z-score
            when there are >=5 snapshots in the window, otherwise percent.
        limit: max anomalies returned (sorted by severity descending).
    """
    snapshots = list_snapshot_dates()
    if not snapshots:
        return _empty_result(sensitivity, method, since, cloud,
                             reason="No price snapshots bundled with this build.")

    # Bucket points by (cloud, sku)
    points = load_history(cloud=cloud, since=since)
    by_key: dict[tuple[str, str], list] = {}
    for p in points:
        by_key.setdefault((p.cloud, p.sku), []).append(p)

    if not by_key:
        return _empty_result(sensitivity, method, since, cloud,
                             reason="No price history matches the filters.")

    pct_threshold, z_threshold = _SENSITIVITY_THRESHOLDS[sensitivity]

    # Auto-select method based on dataset density. We use the per-SKU sample
    # size, not total snapshots — different SKUs may have different histories.
    sample_sizes = [len(pts) for pts in by_key.values()]
    median_samples = statistics.median(sample_sizes) if sample_sizes else 0
    effective_method: Method = method
    if method == "auto":
        effective_method = "z_score" if median_samples >= 5 else "percent_change"

    anomalies: list[Anomaly] = []
    for (c, sku), pts in by_key.items():
        anomaly = _classify_sku(
            cloud=c, sku=sku, pts=pts,
            method=effective_method,
            pct_threshold=pct_threshold,
            z_threshold=z_threshold,
        )
        if anomaly is not None:
            anomalies.append(anomaly)

    anomalies.sort(key=lambda a: a.severity, reverse=True)
    anomalies = anomalies[:limit]

    return {
        "kind": "price_anomaly_detection",
        "as_of_window": {"earliest": min(snapshots), "latest": max(snapshots)},
        "snapshots_count": len(snapshots),
        "median_samples_per_sku": int(median_samples),
        "sensitivity": sensitivity,
        "method_requested": method,
        "method_used": effective_method,
        "thresholds": {"pct_change": pct_threshold, "z_score": z_threshold},
        "filters": {"since": since, "cloud": cloud},
        "headline": _build_headline(anomalies, effective_method, len(snapshots)),
        "anomaly_count": len(anomalies),
        "anomalies": [a.to_dict() for a in anomalies],
        "honest_gaps": _honest_gaps(len(snapshots)),
    }


def _classify_sku(
    *, cloud: str, sku: str, pts: list,
    method: Method, pct_threshold: float, z_threshold: float,
) -> Anomaly | None:
    """Decide whether a single (cloud, sku) series qualifies as an anomaly.

    Returns the Anomaly when one of the configured methods flags the series,
    or None otherwise. Method dispatch:
      - z_score path runs first when method=='z_score' AND len(pts) >= 3
      - falls through to percent_change when z-score isn't computable
        (single historical point) OR when percent_change is the chosen method
    """
    pts_sorted = sorted(pts, key=lambda p: p.as_of)
    if len(pts_sorted) < 2:
        return None
    earliest = pts_sorted[0]
    latest = pts_sorted[-1]
    if earliest.hourly_usd <= 0:
        return None

    change_pct = (latest.hourly_usd - earliest.hourly_usd) / earliest.hourly_usd * 100
    abs_pct = abs(change_pct)
    base_kwargs = {
        "cloud": cloud, "sku": sku,
        "change_pct": round(change_pct, 2),
        "direction": "up" if change_pct > 0 else "down",
        "earliest_as_of": earliest.as_of,
        "latest_as_of": latest.as_of,
        "earliest_hourly_usd": earliest.hourly_usd,
        "latest_hourly_usd": latest.hourly_usd,
        "data_points": len(pts_sorted),
    }

    if method == "z_score" and len(pts_sorted) >= 3:
        z_anomaly = _try_zscore_anomaly(
            pts_sorted, base_kwargs, z_threshold, pct_threshold, abs_pct,
        )
        if z_anomaly is not None:
            return z_anomaly
        # Otherwise fall through to percent_change (zero-stdev + tiny diff, or
        # not enough historical points yet)

    if abs_pct >= pct_threshold:
        return Anomaly(method="percent_change", severity=abs_pct, **base_kwargs)
    return None


def _try_zscore_anomaly(
    pts_sorted: list, base_kwargs: dict, z_threshold: float,
    pct_threshold: float, abs_pct: float,
) -> Anomaly | None:
    """Z-score branch of the classifier. Returns Anomaly when |z|>=threshold,
    or when historical stdev is 0 but the latest point moved (sentinel z=999
    so it ranks above any real-stdev anomaly). Returns None when z-score
    isn't applicable (caller falls through to percent_change)."""
    historical = [p.hourly_usd for p in pts_sorted[:-1]]
    if len(historical) < 2:
        return None
    latest = pts_sorted[-1]
    mean = statistics.mean(historical)
    stdev = statistics.stdev(historical)
    if stdev > 0:
        z = (latest.hourly_usd - mean) / stdev
        if abs(z) >= z_threshold:
            return Anomaly(method="z_score", severity=abs(z), z_score=z, **base_kwargs)
        # Below z-threshold: real anomaly evaluation done; do NOT fall back to
        # percent_change (would double-flag SKUs with mild moves on noisy series).
        return None
    # stdev == 0: flat-lined series. Flag when latest differs AND moves enough
    # to clear pct_threshold (avoids floating-point dust).
    if latest.hourly_usd != mean and abs_pct >= pct_threshold:
        return Anomaly(method="z_score", severity=999.0, z_score=999.0, **base_kwargs)
    return None


def _empty_result(sensitivity, method, since, cloud, *, reason: str) -> dict[str, Any]:
    return {
        "kind": "price_anomaly_detection",
        "headline": reason,
        "anomaly_count": 0,
        "anomalies": [],
        "filters": {"since": since, "cloud": cloud},
        "sensitivity": sensitivity,
        "method_requested": method,
        "honest_gaps": _honest_gaps(0),
    }


def _build_headline(anomalies: list[Anomaly], method: Method, snapshots: int) -> str:
    if not anomalies:
        return f"No anomalies detected across the bundled {snapshots} price snapshot(s)."
    top = anomalies[0]
    method_label = "z-score" if method == "z_score" else "% change"
    if top.method == "z_score" and top.z_score is not None:
        sev_label = f"|z|={abs(top.z_score):.2f}"
    else:
        sev_label = f"{top.severity:.2f}%"
    return (
        f"{top.cloud.upper()} {top.sku} flagged — {top.change_pct:+.2f}% "
        f"({sev_label}, {top.direction}) between {top.earliest_as_of} and {top.latest_as_of}. "
        f"{len(anomalies)} anomalies total via {method_label}."
    )


def _honest_gaps(snapshot_count: int) -> list[str]:
    gaps = [
        "Anomalies are statistical — they flag unusual moves, not necessarily VALUABLE moves. A 50% drop on a SKU you don't use isn't relevant; cross-reference with your workload.",
        "With few snapshots (<5), z-score is unreliable and the tool defaults to percent-change. Detection quality compounds as the dataset grows past 12 weeks.",
        "Sensitivity 'permissive' (2% threshold) generates frequent false positives — noise in vendor pricing rounds, currency conversions, etc. Use 'moderate' (5%) or 'strict' (10%) for newsworthy moves.",
        "The earliest snapshot may pre-date when cloudprice corrected a hand-curated inaccuracy (e.g., OCI E5.Flex -72.78% in v0.7.0). Those one-time corrections look like anomalies but aren't market moves.",
    ]
    if snapshot_count < 5:
        gaps.append(
            f"Current bundled dataset has only {snapshot_count} snapshot(s). z-score detection "
            "kicks in at >=5 per SKU; until then, percent_change is used. Genuine "
            "anomalies become detectable after ~12 weekly refreshes."
        )
    return gaps
