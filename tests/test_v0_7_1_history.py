"""Tests for v0.7.1 price-history feature.

The bundled package only ships one snapshot to start (2026-05-12). These
tests inject additional fake snapshots into the package's prices directory
via a tmp_path + monkeypatch trick so we can exercise multi-point timeseries
behavior without waiting for real refreshes.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from cloudprice_mcp import history
from cloudprice_mcp.history import (
    PricePoint,
    all_changes_since,
    history_window,
    list_snapshot_dates,
    load_history,
    load_snapshot,
)

# --- Module-level (uses bundled snapshots only — currently just 2026-05-12) ---


def test_list_snapshot_dates_returns_iso_dates():
    dates = list_snapshot_dates()
    assert len(dates) >= 1
    for d in dates:
        # Each entry parseable as ISO date
        assert len(d) == 10
        assert d[4] == "-" and d[7] == "-"


def test_load_snapshot_returns_full_catalog():
    dates = list_snapshot_dates()
    catalog = load_snapshot(dates[0])
    assert catalog["as_of"] == dates[0]
    assert "aws" in catalog
    assert "azure" in catalog
    assert "instances" in catalog["aws"]


def test_load_history_filters_by_cloud():
    aws_pts = load_history(cloud="aws")
    azure_pts = load_history(cloud="azure")
    assert len(aws_pts) > 0
    assert len(azure_pts) > 0
    assert all(p.cloud == "aws" for p in aws_pts)
    assert all(p.cloud == "azure" for p in azure_pts)


def test_load_history_filters_by_sku():
    pts = load_history(sku="m5.xlarge")
    assert len(pts) >= 1
    assert all(p.sku == "m5.xlarge" for p in pts)
    assert all(p.cloud == "aws" for p in pts)  # m5.xlarge only exists on AWS


def test_history_window_returns_none_for_unknown_sku():
    assert history_window("aws", "this-sku-does-not-exist") is None


def test_history_window_works_for_known_sku():
    w = history_window("aws", "m5.xlarge")
    assert w is not None
    assert w.cloud == "aws"
    assert w.sku == "m5.xlarge"
    assert w.region == "us-east-1"
    assert len(w.points) >= 1
    assert w.latest.hourly_usd > 0


def test_single_point_window_reports_zero_change():
    """With one data point, change must be 0.0 (no baseline to compare against)."""
    w = history_window("aws", "m5.xlarge")
    if w and len(w.points) == 1:
        assert w.total_change_pct == 0.0
        assert w.total_change_usd == 0.0


# --- Multi-point timeseries (synthetic snapshots injected via monkeypatch) ---


@pytest.fixture
def synthetic_snapshots(monkeypatch, tmp_path):
    """Inject 3 synthetic snapshots so multi-point math can be exercised."""
    snap_dir = tmp_path / "prices"
    snap_dir.mkdir()

    base_catalog = {
        "as_of": "PLACEHOLDER",
        "currency": "USD",
        "aws": {
            "region": "us-east-1",
            "instances": [{"sku": "m5.xlarge", "vcpus": 4, "memory_gb": 16, "hourly_usd": 0.0}],
            "storage": [],
        },
    }

    def write(date_str: str, price: float):
        cat = json.loads(json.dumps(base_catalog))
        cat["as_of"] = date_str
        cat["aws"]["instances"][0]["hourly_usd"] = price
        (snap_dir / f"{date_str}.json").write_text(json.dumps(cat), encoding="utf-8")

    write("2026-01-01", 0.200)
    write("2026-03-01", 0.196)
    write("2026-05-01", 0.192)
    # Add an irrelevant file that must be ignored
    (snap_dir / "README.md").write_text("not a snapshot")

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


def test_multi_point_window_computes_change(synthetic_snapshots):
    w = history_window("aws", "m5.xlarge")
    assert w is not None
    assert len(w.points) == 3
    # 0.200 -> 0.192 = -4.0%
    assert w.total_change_pct == pytest.approx(-4.0)
    assert w.total_change_usd == pytest.approx(-0.008)
    # Points should be sorted oldest first
    assert w.points[0].as_of == "2026-01-01"
    assert w.points[-1].as_of == "2026-05-01"


def test_since_filter_drops_old_snapshots(synthetic_snapshots):
    w = history_window("aws", "m5.xlarge", since="2026-03-01")
    assert w is not None
    assert len(w.points) == 2
    assert w.points[0].as_of == "2026-03-01"


def test_all_changes_since_finds_movers(synthetic_snapshots):
    changes = all_changes_since("2026-01-01")
    # m5.xlarge moved from 0.200 -> 0.192
    assert len(changes) == 1
    cloud, sku, old, new = changes[0]
    assert cloud == "aws"
    assert sku == "m5.xlarge"
    assert old == pytest.approx(0.200)
    assert new == pytest.approx(0.192)


def test_all_changes_excludes_unchanged_skus(synthetic_snapshots, tmp_path):
    # Add a flat-price SKU
    snap_dir = tmp_path / "prices"
    for date_str in ("2026-01-01", "2026-03-01", "2026-05-01"):
        cat = json.loads((snap_dir / f"{date_str}.json").read_text())
        cat["aws"]["instances"].append({
            "sku": "flat.large", "vcpus": 2, "memory_gb": 8, "hourly_usd": 0.10
        })
        (snap_dir / f"{date_str}.json").write_text(json.dumps(cat))
    changes = all_changes_since("2026-01-01")
    skus_changed = {sku for _, sku, _, _ in changes}
    assert "flat.large" not in skus_changed


def test_pricepoint_to_dict():
    p = PricePoint(
        as_of="2026-05-12", cloud="aws", sku="m5.xlarge",
        region="us-east-1", hourly_usd=0.192,
    )
    d = p.to_dict()
    assert d == {
        "as_of": "2026-05-12", "cloud": "aws", "sku": "m5.xlarge",
        "region": "us-east-1", "hourly_usd": 0.192,
    }


def test_historywindow_to_dict_has_full_series(synthetic_snapshots):
    w = history_window("aws", "m5.xlarge")
    d = w.to_dict()
    assert d["cloud"] == "aws"
    assert d["sku"] == "m5.xlarge"
    assert d["data_points"] == 3
    assert d["earliest_as_of"] == "2026-01-01"
    assert d["latest_as_of"] == "2026-05-01"
    assert len(d["series"]) == 3
    assert d["series"][0]["hourly_usd"] == pytest.approx(0.200)
