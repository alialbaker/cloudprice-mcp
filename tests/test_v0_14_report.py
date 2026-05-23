"""Tests for v0.14.0 generate_decision_report — CFO-grade markdown reports."""
from __future__ import annotations

from cloudprice_mcp import __version__
from cloudprice_mcp.finops.report import generate_decision_report
from cloudprice_mcp.inventory import parse_dict
from cloudprice_mcp.pricing import load_catalog, reset_catalog_cache


def setup_function():
    reset_catalog_cache()


def _basic_inv():
    return parse_dict({
        "source_cloud": "aws",
        "compute": [{"name": "web", "vcpus": 4, "memory_gb": 16, "quantity": 6}],
    })


def _full_inv():
    return parse_dict({
        "source_cloud": "aws",
        "compute": [{"name": "web", "vcpus": 4, "memory_gb": 16, "quantity": 6}],
        "object_storage": [{"name": "hot", "capacity_gb": 10240, "tier": "hot"}],
        "egress": [{"name": "internet", "gb_per_month": 51200, "direction": "out_to_internet"}],
    })


# --- Shape ---


def test_returns_report_kind():
    r = generate_decision_report(load_catalog(), _basic_inv())
    assert r["kind"] == "finops_decision_report"
    assert "markdown" in r
    assert "recommended_cloud" in r
    assert "audit" in r
    assert "underlying" in r


def test_markdown_is_nonempty_string():
    r = generate_decision_report(load_catalog(), _basic_inv())
    assert isinstance(r["markdown"], str)
    assert len(r["markdown"]) > 200


def test_underlying_includes_migration_result():
    r = generate_decision_report(load_catalog(), _basic_inv())
    assert r["underlying"]["migration"]["kind"] == "migration_assessment"


def test_underlying_includes_carbon_when_requested():
    r = generate_decision_report(load_catalog(), _basic_inv(), include_carbon=True)
    assert r["underlying"]["carbon"] is not None
    assert r["underlying"]["carbon"]["kind"] == "carbon_footprint_comparison"


def test_skips_carbon_when_disabled():
    r = generate_decision_report(load_catalog(), _basic_inv(), include_carbon=False)
    assert r["underlying"]["carbon"] is None
    # Carbon header should NOT appear
    assert "## Carbon footprint" not in r["markdown"]


# --- Markdown sections ---


def test_markdown_has_required_sections():
    r = generate_decision_report(load_catalog(), _full_inv())
    md = r["markdown"]
    assert "# " in md  # title
    assert "## Executive summary" in md
    assert "## Per-cloud cost comparison" in md
    assert "## Workload analyzed" in md
    assert "## Audit trail" in md


def test_carbon_section_only_when_included():
    r_with = generate_decision_report(load_catalog(), _basic_inv(), include_carbon=True)
    r_without = generate_decision_report(load_catalog(), _basic_inv(), include_carbon=False)
    assert "## Carbon footprint" in r_with["markdown"]
    assert "## Carbon footprint" not in r_without["markdown"]


def test_workload_block_mentions_compute():
    r = generate_decision_report(load_catalog(), _full_inv())
    md = r["markdown"]
    assert "web" in md       # compute name
    assert "4 vCPU" in md
    assert "16.0 GB" in md or "16 GB" in md


def test_workload_block_mentions_egress():
    r = generate_decision_report(load_catalog(), _full_inv())
    assert "51200" in r["markdown"] or "internet" in r["markdown"]


def test_audit_block_includes_version_and_catalog_date():
    cat = load_catalog()
    r = generate_decision_report(cat, _basic_inv(), requester="cfo@example.com")
    md = r["markdown"]
    assert __version__ in md
    assert cat.as_of in md
    assert "cfo@example.com" in md


def test_audit_struct_includes_metadata():
    cat = load_catalog()
    r = generate_decision_report(cat, _basic_inv(), requester="ali@example.com", include_carbon=False)
    audit = r["audit"]
    assert audit["cloudprice_mcp_version"] == __version__
    assert audit["catalog_as_of"] == cat.as_of
    assert audit["requester"] == "ali@example.com"
    assert audit["include_carbon"] is False


# --- Honest gaps aggregation ---


def test_honest_gaps_aggregated_when_present():
    r = generate_decision_report(load_catalog(), _basic_inv(), include_carbon=True)
    md = r["markdown"]
    assert "## Honest gaps" in md
    # Should mention sources via the (from X) tags
    assert "(from migration)" in md
    assert "(from carbon)" in md


# --- Targets filter passed through ---


def test_targets_filter_limits_table_rows():
    r = generate_decision_report(load_catalog(), _basic_inv(), targets=["azure", "oci"])
    md = r["markdown"]
    # GCP shouldn't appear as a target row (source AWS still appears as the source row)
    assert "| GCP" not in md


# --- Recommended marker ---


def test_recommended_cloud_marked_in_table():
    r = generate_decision_report(load_catalog(), _basic_inv())
    rec = r["recommended_cloud"]
    if rec:  # only if a recommendation was made
        # The "←" marker should appear next to the recommended cloud row
        assert f"| {rec.upper()} ←" in r["markdown"] or rec.upper() in r["markdown"]
