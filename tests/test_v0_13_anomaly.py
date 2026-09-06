"""Tests for v0.13.0 detect_price_anomalies — statistical anomaly detection
on the bundled price-history dataset."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from cloudprice_mcp import history
from cloudprice_mcp.finops.anomaly import _SENSITIVITY_THRESHOLDS, detect_price_anomalies

# --- Module-level (uses real bundled snapshots) ---


def test_returns_anomaly_detection_kind():
    r = detect_price_anomalies()
    assert r["kind"] == "price_anomaly_detection"
    assert "anomalies" in r
    assert "method_used" in r
    assert "thresholds" in r


def test_bundled_history_detects_oci_corrections():
    """OCI E5.Flex and A2.Flex shapes dropped 72.78% between 2026-04-26
    (hand-curated) and 2026-05-12 (first auto-refresh). Default moderate
    sensitivity should flag all 9 of them."""
    r = detect_price_anomalies(sensitivity="moderate")
    oci_anomalies = [a for a in r["anomalies"] if a["cloud"] == "oci"]
    assert len(oci_anomalies) >= 9  # 5 E5 sizes + 4 A2 sizes
    # All should be ~73% drops
    for a in oci_anomalies:
        assert -75 <= a["change_pct"] <= -70
        assert a["direction"] == "down"


def test_strict_sensitivity_returns_fewer_anomalies_than_permissive():
    strict = detect_price_anomalies(sensitivity="strict")
    permissive = detect_price_anomalies(sensitivity="permissive")
    assert strict["anomaly_count"] <= permissive["anomaly_count"]


def test_cloud_filter():
    r = detect_price_anomalies(cloud="oci")
    for a in r["anomalies"]:
        assert a["cloud"] == "oci"


def test_limit_caps_results():
    r = detect_price_anomalies(sensitivity="permissive", limit=3)
    assert len(r["anomalies"]) <= 3


def test_anomalies_sorted_by_severity_desc():
    r = detect_price_anomalies(sensitivity="permissive")
    severities = [a["severity"] for a in r["anomalies"]]
    assert severities == sorted(severities, reverse=True)


def test_thresholds_returned_match_sensitivity():
    r = detect_price_anomalies(sensitivity="strict")
    pct, z = _SENSITIVITY_THRESHOLDS["strict"]
    assert r["thresholds"]["pct_change"] == pct
    assert r["thresholds"]["z_score"] == z


def test_honest_gaps_present():
    r = detect_price_anomalies()
    gaps = " ".join(r["honest_gaps"]).lower()
    assert "statistical" in gaps
    assert "z-score" in gaps or "z_score" in gaps


def test_headline_mentions_top_anomaly():
    r = detect_price_anomalies(sensitivity="moderate")
    if r["anomaly_count"] > 0:
        top = r["anomalies"][0]
        assert top["cloud"].upper() in r["headline"]
        assert top["sku"] in r["headline"]


# --- Synthetic snapshot fixture exercises z-score path ---


@pytest.fixture
def six_snapshots(monkeypatch, tmp_path):
    """6 synthetic weekly snapshots where one SKU spikes on week 6."""
    snap_dir = tmp_path / "prices"
    snap_dir.mkdir()

    base_catalog = {
        "as_of": "PLACEHOLDER",
        "currency": "USD",
        "aws": {
            "region": "us-east-1",
            "instances": [
                {"sku": "stable.large", "vcpus": 2, "memory_gb": 8, "hourly_usd": 0.10},
                {"sku": "spike.large", "vcpus": 2, "memory_gb": 8, "hourly_usd": 0.10},
            ],
            "storage": [],
        },
    }

    def write(date_str: str, spike_price: float):
        cat = json.loads(json.dumps(base_catalog))
        cat["as_of"] = date_str
        cat["aws"]["instances"][1]["hourly_usd"] = spike_price
        (snap_dir / f"{date_str}.json").write_text(json.dumps(cat), encoding="utf-8")

    # 5 weeks of stable pricing for spike.large, then a 30% jump in week 6
    for date_str, price in [
        ("2026-03-01", 0.10), ("2026-03-08", 0.10), ("2026-03-15", 0.10),
        ("2026-03-22", 0.10), ("2026-03-29", 0.10), ("2026-04-05", 0.13),
    ]:
        write(date_str, price)

    # Redirect history.* to read from our temp dir.
    class _FakeFiles:
        def __init__(self, root: Path):
            self.root = root
        def iterdir(self):
            return self.root.iterdir()
        def joinpath(self, name: str):
            return self.root / name

    fake_resources_files = lambda _pkg: _FakeFiles(snap_dir)
    monkeypatch.setattr(history, "resources", type("R", (), {"files": staticmethod(fake_resources_files)}))
    return snap_dir


def test_auto_method_switches_to_zscore_with_dense_history(six_snapshots):
    r = detect_price_anomalies(sensitivity="moderate")
    # 6 snapshots, median samples per SKU = 6, should auto-pick z_score
    assert r["method_used"] == "z_score"


def test_zscore_flags_spike_in_otherwise_stable_series(six_snapshots):
    r = detect_price_anomalies(sensitivity="moderate")
    spike_anomalies = [a for a in r["anomalies"] if a["sku"] == "spike.large"]
    assert len(spike_anomalies) == 1
    a = spike_anomalies[0]
    assert a["method"] == "z_score"
    assert a["direction"] == "up"
    assert a["change_pct"] == pytest.approx(30.0)
    assert "z_score" in a
    assert a["z_score"] > 2.0  # well above moderate threshold


def test_zscore_ignores_flat_series(six_snapshots):
    r = detect_price_anomalies(sensitivity="moderate")
    stable_anomalies = [a for a in r["anomalies"] if a["sku"] == "stable.large"]
    assert stable_anomalies == []


def test_explicit_method_overrides_auto(six_snapshots):
    r = detect_price_anomalies(sensitivity="moderate", method="percent_change")
    assert r["method_used"] == "percent_change"
    # spike.large at +30% should still flag via percent_change at moderate (5%)
    spike = [a for a in r["anomalies"] if a["sku"] == "spike.large"]
    assert len(spike) == 1
    assert spike[0]["method"] == "percent_change"
