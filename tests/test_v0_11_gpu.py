"""Tests for v0.11.0 compare_gpu_workload — cross-cloud GPU pricing."""
from __future__ import annotations

import pytest

from cloudprice_mcp.finops.gpu import compare_gpu_workload
from cloudprice_mcp.pricing import (
    Instance,
    PriceCatalog,
    load_catalog,
    reset_catalog_cache,
)


def setup_function():
    reset_catalog_cache()


def _make_catalog(instances: list[Instance]) -> PriceCatalog:
    return PriceCatalog(as_of="2026-05-16", currency="USD", instances=tuple(instances), storage=())


# --- Basic shape ---


def test_returns_carbon_kind_and_per_cloud_rows():
    cat = load_catalog()
    r = compare_gpu_workload(cat, gpu_type="H100", gpu_count=8)
    assert r["kind"] == "gpu_workload_comparison"
    assert len(r["per_cloud"]) >= 1
    for row in r["per_cloud"]:
        assert "hourly_usd" in row
        assert "hourly_usd_per_gpu" in row
        assert row["gpu_count_in_sku"] >= row["gpu_count_requested"]


def test_h100_8gpu_returns_cheapest_first():
    """Whoever has the cheapest 8x H100 in the catalog wins. Originally OCI's
    BM.GPU.H100.8 at $80/h beat AWS p5.48xlarge at $98.32/h; in May 2026 AWS
    cut H100 pricing 44% and now ranks first. We test the BEHAVIOR (winner is
    actually cheapest) instead of asserting a specific provider — which was
    brittle and broke on the first real auto-refresh."""
    cat = load_catalog()
    r = compare_gpu_workload(cat, gpu_type="H100", gpu_count=8)
    assert r["per_cloud"], "expected at least one H100 SKU in catalog"
    # Recommended must equal the row with lowest hourly_usd
    cheapest_row = min(r["per_cloud"], key=lambda row: row["hourly_usd"])
    assert r["recommended"] == cheapest_row["cloud"]
    assert r["per_cloud"][0]["cloud"] == cheapest_row["cloud"]
    # Per-cloud rows must be sorted cheapest-first
    hourly_prices = [row["hourly_usd"] for row in r["per_cloud"]]
    assert hourly_prices == sorted(hourly_prices)


def test_a100_1gpu_per_gpu_winner_distinct_from_absolute():
    """For 1x A100: absolute winner may differ from per-GPU winner depending
    on how each cloud packages the SKU (bare-metal 8x vs single-GPU). Test
    that the per_gpu_hourly_winner field exists and correctly identifies the
    lowest $/GPU/h — without hard-coding which provider that is."""
    cat = load_catalog()
    r = compare_gpu_workload(cat, gpu_type="A100", gpu_count=1)
    assert r["per_cloud"], "expected at least one A100 SKU in catalog"
    assert r["recommended"] is not None
    assert r["per_gpu_hourly_winner"] is not None
    # The per-GPU winner should genuinely have the lowest hourly_usd_per_gpu
    by_per_gpu = sorted(r["per_cloud"], key=lambda row: row["hourly_usd_per_gpu"])
    assert r["per_gpu_hourly_winner"] == by_per_gpu[0]["cloud"]


def test_over_provisioned_flagged_when_only_bigger_sku_available():
    """Asking for 1x A100 in AWS — only p4d.24xlarge (8x A100) is in our
    catalog, so AWS row should be flagged over_provisioned."""
    cat = load_catalog()
    r = compare_gpu_workload(cat, gpu_type="A100", gpu_count=1, targets=["aws"])
    assert len(r["per_cloud"]) == 1
    assert r["per_cloud"][0]["over_provisioned"] is True
    assert r["per_cloud"][0]["gpu_count_in_sku"] == 8


def test_gpu_type_case_insensitive():
    cat = load_catalog()
    r_upper = compare_gpu_workload(cat, gpu_type="A100", gpu_count=1)
    r_lower = compare_gpu_workload(cat, gpu_type="a100", gpu_count=1)
    assert [r["cloud"] for r in r_upper["per_cloud"]] == [r["cloud"] for r in r_lower["per_cloud"]]


def test_no_match_for_unknown_gpu_type():
    cat = load_catalog()
    r = compare_gpu_workload(cat, gpu_type="MI300X", gpu_count=1)
    assert r["per_cloud"] == []
    assert set(r["clouds_without_match"]) == {"aws", "azure", "gcp", "oci"}
    assert r["recommended"] is None
    assert "No cloud" in r["headline"]


def test_targets_filter_works():
    cat = load_catalog()
    r = compare_gpu_workload(cat, gpu_type="H100", gpu_count=8, targets=["aws", "oci"])
    clouds = {row["cloud"] for row in r["per_cloud"]}
    assert clouds == {"aws", "oci"}


def test_validation_raises_on_zero_gpu_count():
    cat = _make_catalog([])
    with pytest.raises(ValueError, match="gpu_count"):
        compare_gpu_workload(cat, gpu_type="A100", gpu_count=0)


def test_l4_skips_clouds_without_l4_skus():
    """Only AWS + GCP publish L4 SKUs in our catalog right now."""
    cat = load_catalog()
    r = compare_gpu_workload(cat, gpu_type="L4", gpu_count=1)
    matched_clouds = {row["cloud"] for row in r["per_cloud"]}
    no_match = set(r["clouds_without_match"])
    assert "azure" in no_match
    assert "oci" in no_match
    assert "gcp" in matched_clouds


def test_headline_mentions_recommended_sku_and_price():
    cat = load_catalog()
    r = compare_gpu_workload(cat, gpu_type="H100", gpu_count=8)
    headline = r["headline"]
    # Catalog-drift-resilient: assert headline includes whichever SKU + price
    # actually won, not a hard-coded specific cloud.
    winner = r["per_cloud"][0]
    assert winner["sku"] in headline
    # Headline formats hourly as $XX.XXXX/h — check the integer dollars portion.
    assert f"${int(winner['hourly_usd'])}" in headline or f"{winner['hourly_usd']:.4f}" in headline


def test_per_cloud_row_has_gpu_memory():
    cat = load_catalog()
    r = compare_gpu_workload(cat, gpu_type="A100", gpu_count=1)
    for row in r["per_cloud"]:
        assert "gpu_memory_gb_each" in row
        # A100 comes in 40 GB or 80 GB
        assert row["gpu_memory_gb_each"] in (40, 80)


def test_honest_gaps_include_spot_and_provisioning_disclosure():
    cat = load_catalog()
    r = compare_gpu_workload(cat, gpu_type="A100", gpu_count=1)
    gaps = " ".join(r["honest_gaps"]).lower()
    assert "spot" in gaps
    assert "bare-metal" in gaps or "over_provisioned" in gaps or "over-provisioned" in gaps


def test_synthetic_catalog_ranks_by_hourly_then_gpu_count():
    """Tie-break: when two SKUs have same hourly, prefer smaller gpu_count
    (less over-provisioning)."""
    cat = _make_catalog([
        Instance(cloud="aws", sku="big-cheap", vcpus=96, memory_gb=1000, hourly_usd=10.0,
                 region="us-east-1", gpu_type="A100", gpu_count=8, gpu_memory_gb_each=40),
        Instance(cloud="aws", sku="small-cheap", vcpus=12, memory_gb=64, hourly_usd=10.0,
                 region="us-east-1", gpu_type="A100", gpu_count=2, gpu_memory_gb_each=40),
    ])
    r = compare_gpu_workload(cat, gpu_type="A100", gpu_count=1, targets=["aws"])
    assert r["per_cloud"][0]["sku"] == "small-cheap"
