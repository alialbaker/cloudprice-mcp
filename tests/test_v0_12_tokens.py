"""Tests for v0.12.0 compare_token_pricing — cross-provider LLM token pricing."""
from __future__ import annotations

import pytest

from cloudprice_mcp.finops.tokens import compare_token_pricing, reset_catalog_cache


def setup_function():
    reset_catalog_cache()


# --- Basic shape ---


def test_returns_token_pricing_kind():
    r = compare_token_pricing()
    assert r["kind"] == "token_pricing_comparison"
    assert len(r["rows"]) > 0
    for row in r["rows"]:
        assert "input_per_1m_usd" in row
        assert "output_per_1m_usd" in row
        assert "model_id" in row
        assert "provider" in row


def test_unfiltered_returns_all_models_x_providers():
    r = compare_token_pricing()
    # We ship 19 models, each on 1-3 providers — expect at least 20 rows total
    assert len(r["rows"]) >= 20


def test_blended_metric_computed():
    """blended_per_1m = (output*3 + input) / 4 — the typical chat workload ratio."""
    r = compare_token_pricing(model_id="claude-4-sonnet")
    for row in r["rows"]:
        expected = (row["output_per_1m_usd"] * 3 + row["input_per_1m_usd"]) / 4
        assert row["blended_per_1m_usd_3to1_out_to_in"] == pytest.approx(round(expected, 4))


# --- Filtering ---


def test_filter_by_family_claude():
    r = compare_token_pricing(model_family="claude")
    assert len(r["rows"]) > 0
    for row in r["rows"]:
        assert row["family"] == "claude"


def test_filter_by_model_id_returns_only_that_model():
    r = compare_token_pricing(model_id="gpt-4o")
    assert len(r["rows"]) > 0
    for row in r["rows"]:
        assert row["model_id"] == "gpt-4o"


def test_filter_by_providers():
    r = compare_token_pricing(providers=["anthropic", "openai"])
    seen_providers = {row["provider"] for row in r["rows"]}
    assert seen_providers <= {"anthropic", "openai"}
    assert len(r["rows"]) > 0


def test_combined_family_and_provider_filter():
    r = compare_token_pricing(model_family="gpt", providers=["openai"])
    for row in r["rows"]:
        assert row["family"] == "gpt"
        assert row["provider"] == "openai"


def test_unknown_filter_returns_empty():
    r = compare_token_pricing(model_family="nonexistent")
    assert r["rows"] == []
    assert r["recommended"] is None
    assert "No models match" in r["headline"]


# --- Monthly cost calculation + ranking ---


def test_monthly_volumes_compute_total_cost():
    r = compare_token_pricing(
        model_id="claude-4-sonnet",
        monthly_input_tokens=10_000_000,
        monthly_output_tokens=2_000_000,
    )
    for row in r["rows"]:
        # 10M input * $3/1M = $30; 2M output * $15/1M = $30; total $60
        assert row["monthly_input_usd"] == pytest.approx(30.0)
        assert row["monthly_output_usd"] == pytest.approx(30.0)
        assert row["monthly_total_usd"] == pytest.approx(60.0)


def test_monthly_volumes_rank_by_total_cost():
    """When volumes are given, results should sort by monthly_total_usd."""
    r = compare_token_pricing(
        monthly_input_tokens=50_000_000,
        monthly_output_tokens=10_000_000,
    )
    totals = [row["monthly_total_usd"] for row in r["rows"]]
    assert totals == sorted(totals)
    assert r["ranked_by"] == "monthly_total_usd"


def test_no_volumes_rank_by_output_cost():
    """Without volumes, results sort by per-1M output cost."""
    r = compare_token_pricing()
    outputs = [row["output_per_1m_usd"] for row in r["rows"]]
    assert outputs == sorted(outputs)
    assert r["ranked_by"] == "output_per_1m_usd"


# --- Recommended + metadata ---


def test_recommended_is_first_row():
    r = compare_token_pricing(model_family="claude")
    assert r["recommended"]["model_id"] == r["rows"][0]["model_id"]
    assert r["recommended"]["provider"] == r["rows"][0]["provider"]


def test_cache_fields_included_when_published():
    """Anthropic publishes cache_read/write for Claude 4 Sonnet; Bedrock doesn't."""
    r = compare_token_pricing(model_id="claude-4-sonnet")
    anthropic_row = next(row for row in r["rows"] if row["provider"] == "anthropic")
    bedrock_row = next(row for row in r["rows"] if row["provider"] == "bedrock")
    assert anthropic_row["cache_read_per_1m_usd"] is not None
    assert anthropic_row["cache_write_per_1m_usd"] is not None
    assert bedrock_row["cache_read_per_1m_usd"] is None


def test_context_window_included():
    r = compare_token_pricing(model_id="gpt-5")
    for row in r["rows"]:
        assert row["context_window_tokens"] == 400000


def test_provider_label_resolved():
    r = compare_token_pricing(model_id="claude-4-sonnet", providers=["bedrock"])
    assert r["rows"][0]["provider_label"] == "AWS Bedrock"
    assert r["rows"][0]["provider_kind"] == "hyperscaler"


def test_honest_gaps_disclose_batch_caching_and_curation():
    r = compare_token_pricing()
    gaps = " ".join(r["honest_gaps"]).lower()
    assert "batch" in gaps
    assert "cach" in gaps
    assert "hand-curated" in gaps or "curated" in gaps


# --- Provider parity discovery ---


def test_claude_4_sonnet_priced_on_three_providers():
    """The catalog ships Claude 4 Sonnet on Anthropic + Bedrock + Vertex."""
    r = compare_token_pricing(model_id="claude-4-sonnet")
    providers = {row["provider"] for row in r["rows"]}
    assert providers == {"anthropic", "bedrock", "vertex"}


def test_gpt_4o_priced_on_two_providers():
    """GPT-4o ships on OpenAI direct + Azure OpenAI."""
    r = compare_token_pricing(model_id="gpt-4o")
    providers = {row["provider"] for row in r["rows"]}
    assert providers == {"openai", "azure_openai"}
