"""Tests for v0.16.0 extended LLM catalog — LiteLLM ingest + lookup tool.

Two test surfaces:
  1. The transform function in scripts/refresh_extended_catalog.py — given a
     LiteLLM-shaped dict, does it produce well-formed rows?
  2. The lookup tool in cloudprice_mcp.finops.extended_tokens — given a
     known fixture catalog, do query/provider/mode filters work + does
     ranking obey the documented order?

The lookup tests patch the module cache directly with a small fixture so we
don't depend on the bundled real catalog (which drifts as LiteLLM updates).
"""
from __future__ import annotations

import pytest

from cloudprice_mcp.finops import extended_tokens
from scripts.refresh_extended_catalog import _transform


# --- transform() — given LiteLLM-shaped input ---


def test_transform_filters_to_chat_completion_responses_modes():
    raw = {
        "sample_spec": {"mode": "chat", "input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6, "litellm_provider": "x"},
        "good-chat":   {"mode": "chat",       "input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6, "litellm_provider": "openai"},
        "good-comp":   {"mode": "completion", "input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6, "litellm_provider": "openai"},
        "good-resp":   {"mode": "responses",  "input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6, "litellm_provider": "openai"},
        "skip-image":  {"mode": "image_generation", "input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6, "litellm_provider": "openai"},
        "skip-embed":  {"mode": "embedding",        "input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6, "litellm_provider": "openai"},
        "skip-audio":  {"mode": "audio_transcription", "input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6, "litellm_provider": "openai"},
    }
    rows = _transform(raw)
    ids = {r["litellm_id"] for r in rows}
    # sample_spec excluded by name; image/embed/audio excluded by mode
    assert ids == {"good-chat", "good-comp", "good-resp"}


def test_transform_drops_entries_with_missing_prices():
    raw = {
        "no-prices": {"mode": "chat", "litellm_provider": "openai"},  # no costs
        "null-in":   {"mode": "chat", "input_cost_per_token": None, "output_cost_per_token": 2e-6, "litellm_provider": "openai"},
        "ok":        {"mode": "chat", "input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6, "litellm_provider": "openai"},
    }
    rows = _transform(raw)
    assert [r["litellm_id"] for r in rows] == ["ok"]


def test_transform_converts_per_token_to_per_1m():
    raw = {
        "ok": {"mode": "chat", "input_cost_per_token": 2.5e-7, "output_cost_per_token": 1.25e-6, "litellm_provider": "anthropic"},
    }
    rows = _transform(raw)
    # 2.5e-7 USD/token * 1_000_000 = 0.25 USD/1M
    assert rows[0]["input_per_1m_usd"] == pytest.approx(0.25)
    assert rows[0]["output_per_1m_usd"] == pytest.approx(1.25)


def test_transform_normalizes_verbose_provider_names():
    raw = {
        "claude-bedrock": {"mode": "chat", "input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6, "litellm_provider": "bedrock_converse"},
        "claude-vertex":  {"mode": "chat", "input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6, "litellm_provider": "vertex_ai-anthropic_models"},
        "gpt-azure":      {"mode": "chat", "input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6, "litellm_provider": "azure"},
        "gemini-direct":  {"mode": "chat", "input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6, "litellm_provider": "gemini"},
    }
    rows = {r["litellm_id"]: r for r in _transform(raw)}
    assert rows["claude-bedrock"]["provider"] == "bedrock"
    assert rows["claude-vertex"]["provider"] == "vertex"
    assert rows["gpt-azure"]["provider"] == "azure_openai"
    assert rows["gemini-direct"]["provider"] == "google"
    # Provenance preserved
    assert rows["claude-bedrock"]["provider_raw"] == "bedrock_converse"


def test_transform_propagates_optional_fields():
    raw = {
        "rich": {
            "mode": "chat",
            "input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6,
            "cache_read_input_token_cost": 1e-7,
            "cache_creation_input_token_cost": 3e-6,
            "max_input_tokens": 200000,
            "max_output_tokens": 4096,
            "litellm_provider": "anthropic",
            "supports_vision": True,
            "supports_function_calling": True,
        },
    }
    row = _transform(raw)[0]
    assert row["cache_read_per_1m_usd"] == pytest.approx(0.1)
    assert row["cache_write_per_1m_usd"] == pytest.approx(3.0)
    assert row["context_window_tokens"] == 200000
    assert row["max_output_tokens"] == 4096
    assert row["supports_vision"] is True
    assert row["supports_function_calling"] is True


def test_transform_stable_sort_by_provider_then_id():
    raw = {
        "z-model": {"mode": "chat", "input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6, "litellm_provider": "openai"},
        "a-model": {"mode": "chat", "input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6, "litellm_provider": "anthropic"},
        "m-model": {"mode": "chat", "input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6, "litellm_provider": "openai"},
    }
    rows = _transform(raw)
    assert [r["litellm_id"] for r in rows] == ["a-model", "m-model", "z-model"]


# --- lookup_extended_model_pricing — query/filter/rank ---


def _install_fixture_catalog(monkeypatch):
    catalog = {
        "as_of": "2026-05-23",
        "source": "litellm",
        "source_url": "https://example.invalid/fixture.json",
        "models": [
            {"litellm_id": "claude-3-haiku-20240307", "provider": "anthropic", "mode": "chat",
             "input_per_1m_usd": 0.25, "output_per_1m_usd": 1.25, "context_window_tokens": 200000},
            {"litellm_id": "bedrock/us-east-1/anthropic.claude-3-haiku-20240307-v1:0",
             "provider": "bedrock", "mode": "chat",
             "input_per_1m_usd": 0.25, "output_per_1m_usd": 1.25, "context_window_tokens": 200000},
            {"litellm_id": "together_ai/meta-llama/Llama-3.1-70B-Instruct",
             "provider": "together_ai", "mode": "chat",
             "input_per_1m_usd": 0.88, "output_per_1m_usd": 0.88, "context_window_tokens": 128000},
            {"litellm_id": "groq/llama-3.1-70b-versatile",
             "provider": "groq", "mode": "chat",
             "input_per_1m_usd": 0.59, "output_per_1m_usd": 0.79, "context_window_tokens": 32768},
            {"litellm_id": "fireworks_ai/llama-v3p1-70b-instruct",
             "provider": "fireworks_ai", "mode": "chat",
             "input_per_1m_usd": 0.90, "output_per_1m_usd": 0.90, "context_window_tokens": 128000},
        ],
    }
    monkeypatch.setattr(extended_tokens, "_cache", catalog)
    return catalog


def test_query_substring_match_case_insensitive(monkeypatch):
    _install_fixture_catalog(monkeypatch)
    out = extended_tokens.lookup_extended_model_pricing(query="llama-3.1-70B")
    ids = {r["litellm_id"] for r in out["rows"]}
    # Fireworks entry uses "v3p1" not "3.1" — should NOT match.
    assert ids == {
        "together_ai/meta-llama/Llama-3.1-70B-Instruct",
        "groq/llama-3.1-70b-versatile",
    }


def test_provider_filter(monkeypatch):
    _install_fixture_catalog(monkeypatch)
    out = extended_tokens.lookup_extended_model_pricing(provider="groq")
    assert out["total_matches"] == 1
    assert out["rows"][0]["provider"] == "groq"


def test_context_filter_excludes_smaller_models(monkeypatch):
    _install_fixture_catalog(monkeypatch)
    out = extended_tokens.lookup_extended_model_pricing(max_context_tokens=100000)
    # groq has 32K, should be excluded; the rest should pass
    ids = {r["litellm_id"] for r in out["rows"]}
    assert "groq/llama-3.1-70b-versatile" not in ids
    assert len(ids) == 4


def test_ranks_by_output_cost_when_no_volumes(monkeypatch):
    _install_fixture_catalog(monkeypatch)
    out = extended_tokens.lookup_extended_model_pricing(query="llama-3.1-70b")
    outputs = [r["output_per_1m_usd"] for r in out["rows"]]
    assert outputs == sorted(outputs)


def test_ranks_by_monthly_total_when_volumes_provided(monkeypatch):
    _install_fixture_catalog(monkeypatch)
    out = extended_tokens.lookup_extended_model_pricing(
        query="llama-3.1-70b",
        monthly_input_tokens=10_000_000,
        monthly_output_tokens=2_000_000,
    )
    totals = [r["monthly_total_usd"] for r in out["rows"]]
    assert totals == sorted(totals)
    # All rows should now have the monthly columns
    for r in out["rows"]:
        assert "monthly_input_usd" in r
        assert "monthly_output_usd" in r
        assert "monthly_total_usd" in r


def test_limit_truncates_results(monkeypatch):
    _install_fixture_catalog(monkeypatch)
    out = extended_tokens.lookup_extended_model_pricing(query="claude", limit=1)
    assert out["total_matches"] == 2
    assert out["returned_rows"] == 1
    assert len(out["rows"]) == 1


def test_returns_source_provenance_per_row(monkeypatch):
    _install_fixture_catalog(monkeypatch)
    out = extended_tokens.lookup_extended_model_pricing(query="claude")
    for row in out["rows"]:
        assert row["source"] == "litellm"


def test_headline_when_no_matches(monkeypatch):
    _install_fixture_catalog(monkeypatch)
    out = extended_tokens.lookup_extended_model_pricing(query="nonexistent-model")
    assert out["total_matches"] == 0
    assert out["returned_rows"] == 0
    assert "No models" in out["headline"]


def test_blended_proxy_present(monkeypatch):
    _install_fixture_catalog(monkeypatch)
    out = extended_tokens.lookup_extended_model_pricing(provider="groq")
    row = out["rows"][0]
    # blended weights output by 3x: three parts of 0.79 plus one part 0.59, then /4 = 0.74
    assert row["blended_per_1m_usd_3to1_out_to_in"] == pytest.approx(0.74)
