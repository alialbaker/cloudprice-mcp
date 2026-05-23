"""Tests for the token-price refresh orchestrator (scripts/refresh_tokens.py).

Verifies that:
  - The orchestrator collects every (model, provider=X) entry from the catalog
    and passes them to the X fetcher.
  - Returned entries are spliced back in-place (same model, same provider).
  - Per-provider fetcher failures are recorded as `skipped` and the catalog
    for that provider is preserved.
  - Diffs are computed by comparing old vs new for each price field.
"""
from __future__ import annotations

import sys
import types

import pytest

from scripts import refresh_tokens


def _toy_catalog() -> dict:
    return {
        "as_of": "2026-05-01",
        "models": [
            {
                "model_id": "claude-3-haiku",
                "providers": [
                    {"provider": "anthropic", "input_per_1m_usd": 0.25, "output_per_1m_usd": 1.25},
                    {"provider": "bedrock",   "input_per_1m_usd": 0.25, "output_per_1m_usd": 1.25},
                ],
            },
            {
                "model_id": "llama-3.1-8b",
                "providers": [
                    {"provider": "bedrock", "input_per_1m_usd": 0.22, "output_per_1m_usd": 0.22},
                ],
            },
        ],
    }


def _install_fake_fetcher(monkeypatch, provider: str, returns: dict | Exception):
    mod = types.ModuleType(f"scripts.token_fetchers.{provider}")

    def fake_fetch(known_models):
        if isinstance(returns, Exception):
            raise returns
        return returns

    mod.fetch_token_prices = fake_fetch  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, f"scripts.token_fetchers.{provider}", mod)


# --- collection + splice ---


def test_collects_only_target_provider_entries(monkeypatch):
    """The orchestrator must hand the bedrock fetcher only the bedrock-tagged
    entries, not the anthropic ones."""
    seen: dict = {}

    def fake_fetch(known_models):
        seen.update(known_models)
        return {}

    mod = types.ModuleType("scripts.token_fetchers.bedrock")
    mod.fetch_token_prices = fake_fetch  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "scripts.token_fetchers.bedrock", mod)

    catalog = _toy_catalog()
    summary = refresh_tokens.TokenRefreshSummary()
    refresh_tokens._refresh_provider("bedrock", catalog, summary)

    assert set(seen.keys()) == {"claude-3-haiku", "llama-3.1-8b"}
    # claude-3-haiku entry passed should be the BEDROCK one (price 0.25 in both
    # so we can't distinguish on price — confirm via provider field).
    assert seen["claude-3-haiku"]["provider"] == "bedrock"


def test_splices_returned_entries_back_into_catalog(monkeypatch):
    _install_fake_fetcher(monkeypatch, "bedrock", {
        "claude-3-haiku": {
            "provider": "bedrock",
            "input_per_1m_usd": 0.20,   # was 0.25, dropped 20%
            "output_per_1m_usd": 1.00,  # was 1.25
        },
    })

    catalog = _toy_catalog()
    summary = refresh_tokens.TokenRefreshSummary()
    refresh_tokens._refresh_provider("bedrock", catalog, summary)

    # Anthropic entry untouched
    anth = catalog["models"][0]["providers"][0]
    assert anth["provider"] == "anthropic"
    assert anth["input_per_1m_usd"] == 0.25

    # Bedrock entry updated
    bed = catalog["models"][0]["providers"][1]
    assert bed["provider"] == "bedrock"
    assert bed["input_per_1m_usd"] == 0.20
    assert bed["output_per_1m_usd"] == 1.00


def test_models_not_returned_keep_old_prices(monkeypatch):
    """If the fetcher returns claude-3-haiku but not llama-3.1-8b, the
    llama row in the catalog must be left exactly as it was."""
    _install_fake_fetcher(monkeypatch, "bedrock", {
        "claude-3-haiku": {
            "provider": "bedrock",
            "input_per_1m_usd": 0.20,
            "output_per_1m_usd": 1.00,
        },
    })

    catalog = _toy_catalog()
    summary = refresh_tokens.TokenRefreshSummary()
    refresh_tokens._refresh_provider("bedrock", catalog, summary)

    llama_bedrock = catalog["models"][1]["providers"][0]
    assert llama_bedrock["input_per_1m_usd"] == 0.22  # unchanged
    assert llama_bedrock["output_per_1m_usd"] == 0.22  # unchanged


# --- failure handling ---


def test_provider_fetcher_failure_preserves_catalog(monkeypatch):
    _install_fake_fetcher(monkeypatch, "bedrock", RuntimeError("AWS API down"))

    catalog = _toy_catalog()
    before = {
        "claude_input": catalog["models"][0]["providers"][1]["input_per_1m_usd"],
        "llama_input":  catalog["models"][1]["providers"][0]["input_per_1m_usd"],
    }

    summary = refresh_tokens.TokenRefreshSummary()
    refresh_tokens._refresh_provider("bedrock", catalog, summary)

    # Nothing refreshed
    assert "bedrock" not in summary.refreshed
    assert summary.skipped
    assert "RuntimeError" in summary.skipped[0][1]

    # Catalog untouched
    assert catalog["models"][0]["providers"][1]["input_per_1m_usd"] == before["claude_input"]
    assert catalog["models"][1]["providers"][0]["input_per_1m_usd"] == before["llama_input"]


def test_provider_with_no_known_models_is_skipped():
    """If we ask to refresh a provider that has zero entries in the catalog,
    we get a clear skip with reason — no fetcher is called."""
    catalog = {"as_of": "2026-05-01", "models": []}
    summary = refresh_tokens.TokenRefreshSummary()
    refresh_tokens._refresh_provider("bedrock", catalog, summary)

    assert summary.skipped == [("bedrock", "no models tagged with this provider in catalog")]
    assert "bedrock" not in summary.refreshed


# --- summary diffs ---


def test_summary_records_per_field_diffs(monkeypatch):
    _install_fake_fetcher(monkeypatch, "bedrock", {
        "claude-3-haiku": {
            "provider": "bedrock",
            "input_per_1m_usd": 0.20,
            "output_per_1m_usd": 1.00,
        },
    })

    catalog = _toy_catalog()
    summary = refresh_tokens.TokenRefreshSummary()
    refresh_tokens._refresh_provider("bedrock", catalog, summary)

    diffs = summary.diffs["bedrock"]
    fields = {(model, field): (old, new) for model, field, old, new in diffs}
    assert fields[("claude-3-haiku", "input_per_1m_usd")] == (0.25, 0.20)
    assert fields[("claude-3-haiku", "output_per_1m_usd")] == (1.25, 1.00)


def test_summary_skips_zero_change(monkeypatch):
    """If a price comes back identical, it should NOT appear in the diff."""
    _install_fake_fetcher(monkeypatch, "bedrock", {
        "claude-3-haiku": {
            "provider": "bedrock",
            "input_per_1m_usd": 0.25,   # identical
            "output_per_1m_usd": 1.25,  # identical
        },
    })

    catalog = _toy_catalog()
    summary = refresh_tokens.TokenRefreshSummary()
    refresh_tokens._refresh_provider("bedrock", catalog, summary)

    assert "bedrock" in summary.refreshed
    assert "bedrock" not in summary.diffs


def test_markdown_summary_has_useful_sections(monkeypatch):
    _install_fake_fetcher(monkeypatch, "bedrock", {
        "claude-3-haiku": {
            "provider": "bedrock", "input_per_1m_usd": 0.20, "output_per_1m_usd": 1.00,
        },
    })

    catalog = _toy_catalog()
    summary = refresh_tokens.TokenRefreshSummary()
    refresh_tokens._refresh_provider("bedrock", catalog, summary)
    md = summary.as_markdown()

    assert "bedrock" in md
    assert "claude-3-haiku" in md
    assert "input_per_1m_usd" in md
    assert "-20.00%" in md or "-20" in md
