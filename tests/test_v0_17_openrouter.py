"""Tests for v0.17.0 OpenRouter ingest + cross-source lookup.

Two surfaces:
  1. scripts/refresh_openrouter_catalog._transform — shape + filter correctness
     against an OpenRouter-style JSON.
  2. extended_tokens.lookup_extended_model_pricing — new `source` filter to
     route queries to litellm-only / openrouter-only / both. Uses a small
     in-memory fixture with rows from both sources.
"""
from __future__ import annotations

import pytest

from cloudprice_mcp.finops import extended_tokens
from scripts.refresh_openrouter_catalog import _transform, _vendor_from_id

# --- _transform() ---


def _or_response(*entries: dict) -> dict:
    """Wrap entries in OpenRouter's {data: [...]} envelope."""
    return {"data": list(entries)}


def _or_entry(*, id: str, prompt: str, completion: str,
              context_length: int | None = 200000,
              cache_read: str | None = None, cache_write: str | None = None,
              name: str | None = None) -> dict:
    pricing = {"prompt": prompt, "completion": completion}
    if cache_read is not None:
        pricing["input_cache_read"] = cache_read
    if cache_write is not None:
        pricing["input_cache_write"] = cache_write
    entry = {
        "id": id,
        "name": name or id,
        "pricing": pricing,
        "architecture": {"modality": "text->text"},
        "top_provider": {"max_completion_tokens": 4096},
    }
    if context_length is not None:
        entry["context_length"] = context_length
    return entry


def test_transform_converts_per_token_strings_to_per_1m_floats():
    raw = _or_response(_or_entry(id="anthropic/claude-3-haiku",
                                 prompt="0.00000025", completion="0.00000125"))
    rows = _transform(raw)
    assert len(rows) == 1
    assert rows[0]["input_per_1m_usd"] == pytest.approx(0.25)
    assert rows[0]["output_per_1m_usd"] == pytest.approx(1.25)
    assert rows[0]["provider"] == "openrouter"
    assert rows[0]["provider_underlying"] == "anthropic"


def test_transform_drops_free_models_with_zero_pricing():
    raw = _or_response(
        _or_entry(id="free/model",  prompt="0", completion="0"),
        _or_entry(id="paid/model", prompt="0.00000025", completion="0.00000125"),
    )
    ids = [r["openrouter_id"] for r in _transform(raw)]
    assert ids == ["paid/model"]


def test_transform_skips_entries_missing_required_pricing():
    raw = {"data": [
        {"id": "x/y", "pricing": {"prompt": None, "completion": None}},
        {"id": "no-id", "pricing": {"prompt": "0.000001", "completion": "0.000001"}},
    ]}
    # First entry lacks prices, second lacks id — both skipped.
    # The second one has "no-id" as id actually, let me re-think.
    # Actually we need id absent. Use a model with id missing.
    raw = {"data": [
        {"pricing": {"prompt": "0.000001", "completion": "0.000001"}},  # no id
        {"id": "x/y", "pricing": {"prompt": None, "completion": None}},  # null prices
    ]}
    assert _transform(raw) == []


def test_transform_handles_non_numeric_pricing_gracefully():
    raw = _or_response(
        {"id": "broken/model", "pricing": {"prompt": "not-a-number", "completion": "0.000001"}, "architecture": {"modality": "text->text"}},
        _or_entry(id="ok/model", prompt="0.000001", completion="0.000002"),
    )
    ids = [r["openrouter_id"] for r in _transform(raw)]
    assert ids == ["ok/model"]  # broken row silently dropped


def test_transform_populates_cache_rates_when_present():
    raw = _or_response(_or_entry(
        id="qwen/qwen-max", prompt="0.0000025", completion="0.0000075",
        cache_read="0.00000025", cache_write="0.000003125",
    ))
    row = _transform(raw)[0]
    assert row["cache_read_per_1m_usd"] == pytest.approx(0.25)
    assert row["cache_write_per_1m_usd"] == pytest.approx(3.125)


def test_transform_omits_cache_fields_when_zero_or_missing():
    raw = _or_response(
        _or_entry(id="no-cache/model", prompt="0.000001", completion="0.000002"),
        _or_entry(id="zero-cache/model", prompt="0.000001", completion="0.000002",
                  cache_read="0", cache_write="0"),
    )
    rows = {r["openrouter_id"]: r for r in _transform(raw)}
    assert "cache_read_per_1m_usd" not in rows["no-cache/model"]
    assert "cache_read_per_1m_usd" not in rows["zero-cache/model"]


def test_transform_carries_context_window_and_max_output():
    raw = _or_response(_or_entry(
        id="model/big", prompt="0.000001", completion="0.000002",
        context_length=1_000_000,
    ))
    row = _transform(raw)[0]
    assert row["context_window_tokens"] == 1_000_000
    assert row["max_output_tokens"] == 4096


def test_vendor_from_id_extracts_prefix():
    assert _vendor_from_id("anthropic/claude-3-haiku") == "anthropic"
    assert _vendor_from_id("meta-llama/llama-3.1-70b") == "meta-llama"
    assert _vendor_from_id("no-slash-here") == "unknown"


# --- lookup_extended_model_pricing source filter ---


def _install_two_source_fixture(monkeypatch):
    catalog = {
        "as_of": "2026-05-23",
        "sources_loaded": ["litellm", "openrouter"],
        "models": [
            # Same canonical model, two sources, different prices —
            # the cross-source comparison scenario.
            {"model_id": "claude-3-haiku-20240307", "source": "litellm",
             "provider": "anthropic", "mode": "chat",
             "input_per_1m_usd": 0.25, "output_per_1m_usd": 1.25,
             "context_window_tokens": 200000},
            {"model_id": "anthropic/claude-3-haiku", "source": "openrouter",
             "provider": "openrouter", "mode": "chat",
             "input_per_1m_usd": 0.30, "output_per_1m_usd": 1.50,
             "context_window_tokens": 200000},
            {"model_id": "groq/llama-3.1-70b", "source": "litellm",
             "provider": "groq", "mode": "chat",
             "input_per_1m_usd": 0.59, "output_per_1m_usd": 0.79,
             "context_window_tokens": 32768},
        ],
    }
    monkeypatch.setattr(extended_tokens, "_cache", catalog)
    return catalog


def test_source_filter_litellm_only(monkeypatch):
    _install_two_source_fixture(monkeypatch)
    out = extended_tokens.lookup_extended_model_pricing(query="claude", source="litellm")
    sources = {r["source"] for r in out["rows"]}
    assert sources == {"litellm"}
    assert out["total_matches"] == 1


def test_source_filter_openrouter_only(monkeypatch):
    _install_two_source_fixture(monkeypatch)
    out = extended_tokens.lookup_extended_model_pricing(query="claude", source="openrouter")
    sources = {r["source"] for r in out["rows"]}
    assert sources == {"openrouter"}
    assert out["total_matches"] == 1


def test_no_source_filter_returns_both(monkeypatch):
    _install_two_source_fixture(monkeypatch)
    out = extended_tokens.lookup_extended_model_pricing(query="claude")
    sources = {r["source"] for r in out["rows"]}
    assert sources == {"litellm", "openrouter"}
    assert out["total_matches"] == 2


def test_source_filter_case_insensitive(monkeypatch):
    _install_two_source_fixture(monkeypatch)
    out = extended_tokens.lookup_extended_model_pricing(query="claude", source="LITELLM")
    assert out["total_matches"] == 1


def test_litellm_cheaper_than_openrouter_for_same_model(monkeypatch):
    """The real FinOps insight: same Claude 3 Haiku, OpenRouter charges 20% more
    (routed price includes their margin). The output cost ranking should put the
    litellm/anthropic row first."""
    _install_two_source_fixture(monkeypatch)
    out = extended_tokens.lookup_extended_model_pricing(query="claude")
    assert out["rows"][0]["source"] == "litellm"
    assert out["rows"][0]["output_per_1m_usd"] == pytest.approx(1.25)
    assert out["rows"][1]["source"] == "openrouter"
    assert out["rows"][1]["output_per_1m_usd"] == pytest.approx(1.50)


def test_sources_loaded_reflected_in_result(monkeypatch):
    _install_two_source_fixture(monkeypatch)
    out = extended_tokens.lookup_extended_model_pricing(query="claude")
    assert out["sources_loaded"] == ["litellm", "openrouter"]


def test_recommended_includes_source(monkeypatch):
    _install_two_source_fixture(monkeypatch)
    out = extended_tokens.lookup_extended_model_pricing(query="claude")
    assert out["recommended"] == {
        "model_id": "claude-3-haiku-20240307",
        "provider": "anthropic",
        "source": "litellm",
    }
