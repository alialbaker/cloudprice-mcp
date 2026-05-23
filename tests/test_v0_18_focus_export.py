"""Tests for v0.18.0 FOCUS 1.3 export tool.

Three surfaces:
  1. compute_workload dispatcher — wraps assess_migration with the real catalog.
  2. token_pricing dispatcher — wraps compare_token_pricing with the bundled
     hand-curated catalog (19 models).
  3. CSV serialization round-trip.

We assert FOCUS 1.3 conformance at the *column-name* level (every row must
have the canonical columns) and at the value level for a couple of must-be-
correct cases (ListCost > 0 for paid SKUs; ProviderName mapped to the
FOCUS canonical strings).
"""
from __future__ import annotations

import csv
import datetime as dt
import io

import pytest

from cloudprice_mcp.finops.focus_export import (
    FOCUS_VERSION,
    _next_month_start,
    _resolve_period_start,
    export_focus,
)
from cloudprice_mcp.inventory import parse_dict
from cloudprice_mcp.pricing import load_catalog, reset_catalog_cache


def setup_function():
    reset_catalog_cache()


# --- columns required for FOCUS 1.3 list-price export ---


REQUIRED_COLUMNS = {
    "ProviderName", "PublisherName", "ServiceName", "ServiceCategory",
    "RegionId", "SkuId", "ChargeCategory", "ChargeClass", "ChargeFrequency",
    "ChargeDescription", "PricingCategory", "PricingQuantity", "PricingUnit",
    "ConsumedQuantity", "ConsumedUnit", "ListUnitPrice", "ListCost",
    "ContractedUnitPrice", "ContractedCost", "EffectiveCost", "BilledCost",
    "BillingCurrency", "BillingPeriodStart", "BillingPeriodEnd",
    "ChargePeriodStart", "ChargePeriodEnd",
    "BillingAccountId", "BillingAccountName", "ResourceId", "ResourceName", "Tags",
}


# --- compute_workload ---


def _basic_workload():
    return parse_dict({
        "source_cloud": "aws",
        "compute": [{"name": "web", "vcpus": 4, "memory_gb": 16, "quantity": 6}],
    })


def test_compute_workload_returns_focus_envelope():
    out = export_focus("compute_workload", catalog=load_catalog(), inventory=_basic_workload())
    assert out["kind"] == "focus_export"
    assert out["focus_version"] == FOCUS_VERSION
    assert isinstance(out["rows"], list)
    assert out["row_count"] == len(out["rows"])


def test_compute_workload_row_has_all_required_focus_columns():
    out = export_focus("compute_workload", catalog=load_catalog(), inventory=_basic_workload())
    assert out["rows"]
    row = out["rows"][0]
    assert REQUIRED_COLUMNS.issubset(row.keys())


def test_compute_workload_emits_one_row_per_cloud_plus_source():
    out = export_focus(
        "compute_workload", catalog=load_catalog(), inventory=_basic_workload(),
        targets=["azure", "oci"],
    )
    providers = [r["ProviderName"] for r in out["rows"]]
    # Source AWS first, then Azure + OCI targets
    assert providers[0] == "AWS"
    assert "Microsoft Azure" in providers
    assert "Oracle Cloud" in providers
    # GCP excluded by targets filter
    assert "Google Cloud" not in providers


def test_compute_workload_unit_price_matches_total():
    out = export_focus("compute_workload", catalog=load_catalog(), inventory=_basic_workload())
    for row in out["rows"]:
        # ListCost can be 0 (free tier — OCI Always Free covers this workload).
        assert row["ListCost"] >= 0
        # Math relationship holds regardless of cost.
        derived = row["PricingQuantity"] * row["ListUnitPrice"]
        assert derived == pytest.approx(row["ListCost"], abs=0.01)
        assert row["PricingUnit"] == "Hour"
        assert row["ServiceCategory"] == "Compute"
        assert row["ChargeCategory"] == "Usage"
        assert row["ChargeFrequency"] == "Recurring"
        assert row["PricingCategory"] == "Standard"


def test_compute_workload_some_clouds_have_positive_cost():
    """At least the major hyperscalers should produce non-zero cost rows for
    a 6x (4 vCPU, 16 GB) workload — guards against the dispatcher silently
    returning all-zero rows."""
    out = export_focus("compute_workload", catalog=load_catalog(), inventory=_basic_workload())
    positives = [r for r in out["rows"] if r["ListCost"] > 0]
    assert len(positives) >= 2  # source + at least 1 target


def test_compute_workload_billed_cost_left_null():
    """List-price exports MUST NOT populate BilledCost/EffectiveCost/etc.
    Documented in the result's notes; downstream FinOps tools depend on this."""
    out = export_focus("compute_workload", catalog=load_catalog(), inventory=_basic_workload())
    for row in out["rows"]:
        assert row["BilledCost"] is None
        assert row["EffectiveCost"] is None
        assert row["ContractedCost"] is None
        assert row["ContractedUnitPrice"] is None
        assert row["BillingAccountId"] is None
        assert row["ResourceId"] is None


def test_compute_workload_requires_catalog_and_inventory():
    with pytest.raises(ValueError, match="catalog and inventory"):
        export_focus("compute_workload")


# --- token_pricing ---


def test_token_pricing_produces_ai_ml_category_rows():
    out = export_focus("token_pricing", model_id="claude-3-haiku")
    assert out["rows"]
    for row in out["rows"]:
        assert row["ServiceCategory"] == "AI and Machine Learning"
        assert REQUIRED_COLUMNS.issubset(row.keys())


def test_token_pricing_without_volumes_uses_per_1m_output_pricing():
    out = export_focus("token_pricing", model_id="claude-3-haiku")
    for row in out["rows"]:
        assert row["PricingUnit"] == "1M output tokens"
        assert row["PricingQuantity"] == pytest.approx(1.0)


def test_token_pricing_with_volumes_uses_blended_unit():
    out = export_focus(
        "token_pricing", model_id="claude-3-haiku",
        monthly_input_tokens=10_000_000, monthly_output_tokens=2_000_000,
    )
    for row in out["rows"]:
        assert "blended" in row["PricingUnit"]
        # Pricing quantity = 12M / 1M = 12.0
        assert row["PricingQuantity"] == pytest.approx(12.0)
        assert row["ListCost"] > 0


def test_token_pricing_provider_names_canonical():
    """Anthropic / OpenAI / Google etc. must map to their FOCUS canonical names."""
    out = export_focus("token_pricing", model_id="claude-3-haiku")
    provider_names = {r["ProviderName"] for r in out["rows"]}
    # claude-3-haiku is published on Anthropic + Bedrock (Anthropic + AWS provider names)
    assert "Anthropic" in provider_names or "AWS" in provider_names


# --- extended_model_lookup ---


def test_extended_lookup_dispatches_through(monkeypatch):
    """Smoke test — the extended catalog exists in the bundled package, but
    we don't want this test to depend on real LiteLLM content. Just verify
    the dispatcher runs without error and returns the FOCUS envelope."""
    out = export_focus("extended_model_lookup", query="claude", source="litellm")
    assert out["kind"] == "focus_export"
    assert out["focus_version"] == FOCUS_VERSION
    # rows may be empty if the bundled catalog has no matches — that's fine
    assert isinstance(out["rows"], list)


# --- CSV ---


def test_csv_output_has_header_and_data_rows():
    out = export_focus(
        "token_pricing", model_id="claude-3-haiku",
        monthly_input_tokens=10_000_000, monthly_output_tokens=2_000_000,
        format="csv",
    )
    assert "csv" in out
    reader = csv.DictReader(io.StringIO(out["csv"]))
    parsed_rows = list(reader)
    assert len(parsed_rows) == out["row_count"]
    # Header reflects the FOCUS shape
    assert REQUIRED_COLUMNS.issubset(set(reader.fieldnames or []))
    # Nulls round-trip as empty strings (FOCUS convention)
    for row in parsed_rows:
        assert row["BilledCost"] == ""


def test_csv_unknown_format_raises():
    with pytest.raises(ValueError, match="Unknown format"):
        export_focus("token_pricing", model_id="claude-3-haiku", format="xml")


# --- billing period ---


def test_default_billing_period_is_current_month():
    out = export_focus("token_pricing", model_id="claude-3-haiku")
    today = dt.date.today()
    expected_start = today.replace(day=1).isoformat()
    assert out["billing_period_start"] == expected_start


def test_custom_billing_period_honored():
    out = export_focus(
        "token_pricing", model_id="claude-3-haiku",
        billing_period_start="2026-03-01",
    )
    assert out["billing_period_start"] == "2026-03-01"
    assert out["billing_period_end"] == "2026-04-01"


def test_invalid_billing_period_raises():
    with pytest.raises(ValueError, match="ISO date"):
        export_focus("token_pricing", model_id="claude-3-haiku",
                     billing_period_start="not-a-date")


def test_next_month_start_wraps_december():
    assert _next_month_start(dt.date(2026, 12, 1)) == dt.date(2027, 1, 1)
    assert _next_month_start(dt.date(2026, 3, 1)) == dt.date(2026, 4, 1)


def test_resolve_period_start_with_none_returns_first_of_current_month():
    today = dt.date.today()
    assert _resolve_period_start(None) == today.replace(day=1)


# --- query_kind validation ---


def test_unknown_query_kind_raises():
    with pytest.raises(ValueError, match="Unknown query_kind"):
        export_focus("not_a_real_kind")
