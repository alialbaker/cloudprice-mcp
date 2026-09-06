"""Tests for v0.15.0 token-price refresh — Bedrock fetcher + orchestrator.

Same pattern as test_v0_7_fetchers.py for AWS: boto3 is a script-only dep, so
we inject a fake `boto3` module into sys.modules. The bedrock fetcher uses a
paginator, so the fake supports `client.get_paginator("get_products").paginate()`
returning an iterable of pages.
"""
from __future__ import annotations

import json
import sys
import types
from unittest.mock import MagicMock

import pytest

from scripts.token_fetchers.base import TokenFetchError

# --- helpers ---


def _bedrock_product(
    *, usagetype: str, model: str, unit: str, price: float,
    inference_type: str = "On-Demand", region_code: str = "us-east-1",
) -> dict:
    return {
        "product": {
            "attributes": {
                "usagetype": usagetype,
                "model": model,
                "inferenceType": inference_type,
                "regionCode": region_code,
            },
        },
        "terms": {
            "OnDemand": {
                "T1": {
                    "priceDimensions": {
                        "P1": {
                            "unit": unit,
                            "pricePerUnit": {"USD": f"{price:.10f}"},
                        }
                    }
                }
            }
        },
    }


def _haiku_input_output_pair(input_price_per_1k: float, output_price_per_1k: float) -> list[dict]:
    """Two Bedrock products: input + output for claude-3-haiku."""
    return [
        _bedrock_product(
            usagetype="USE1-anthropic.claude-3-haiku-20240307-v1:0-input-tokens",
            model="anthropic.claude-3-haiku-20240307-v1:0",
            unit="1K tokens",
            price=input_price_per_1k,
        ),
        _bedrock_product(
            usagetype="USE1-anthropic.claude-3-haiku-20240307-v1:0-output-tokens",
            model="anthropic.claude-3-haiku-20240307-v1:0",
            unit="1K tokens",
            price=output_price_per_1k,
        ),
    ]


@pytest.fixture
def fake_boto3(monkeypatch):
    """Install a fake `boto3` module that returns a paginator over the given
    list of pages (each page is a list of product dicts)."""

    def factory(pages: list[list[dict]]):
        client = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {"PriceList": [json.dumps(p) for p in page]} for page in pages
        ]
        client.get_paginator.return_value = paginator

        boto3_mod = types.ModuleType("boto3")
        boto3_mod.client = MagicMock(return_value=client)
        monkeypatch.setitem(sys.modules, "boto3", boto3_mod)
        return client

    return factory


# --- Bedrock fetcher: happy path ---


def test_refreshes_known_claude_3_haiku(fake_boto3):
    fake_boto3([_haiku_input_output_pair(0.00025, 0.00125)])
    from scripts.token_fetchers import bedrock

    known = {
        "claude-3-haiku": {
            "provider": "bedrock",
            "input_per_1m_usd": 999.0,
            "output_per_1m_usd": 999.0,
        }
    }
    out = bedrock.fetch_token_prices(known)
    assert "claude-3-haiku" in out
    # 0.00025/1K * 1000 = 0.25/1M
    assert out["claude-3-haiku"]["input_per_1m_usd"] == pytest.approx(0.25)
    assert out["claude-3-haiku"]["output_per_1m_usd"] == pytest.approx(1.25)
    assert out["claude-3-haiku"]["provider"] == "bedrock"


def test_refreshes_multiple_models_in_one_call(fake_boto3):
    products = [
        *_haiku_input_output_pair(0.00025, 0.00125),
        _bedrock_product(
            usagetype="USE1-meta.llama3-1-8b-instruct-v1:0-input-tokens",
            model="meta.llama3-1-8b-instruct-v1:0",
            unit="1K tokens", price=0.00022,
        ),
        _bedrock_product(
            usagetype="USE1-meta.llama3-1-8b-instruct-v1:0-output-tokens",
            model="meta.llama3-1-8b-instruct-v1:0",
            unit="1K tokens", price=0.00022,
        ),
    ]
    fake_boto3([products])
    from scripts.token_fetchers import bedrock

    known = {
        "claude-3-haiku": {"provider": "bedrock"},
        "llama-3.1-8b":   {"provider": "bedrock"},
    }
    out = bedrock.fetch_token_prices(known)
    assert out["claude-3-haiku"]["input_per_1m_usd"] == pytest.approx(0.25)
    assert out["llama-3.1-8b"]["input_per_1m_usd"] == pytest.approx(0.22)


# --- skips: things the fetcher should NOT refresh ---


def test_skips_models_not_in_hints_table(fake_boto3):
    """An obscure model that isn't in BEDROCK_ID_HINTS shouldn't show up in
    the output even if AWS publishes its price."""
    fake_boto3([_haiku_input_output_pair(0.00025, 0.00125)])
    from scripts.token_fetchers import bedrock

    known = {"some-future-model": {"provider": "bedrock"}}
    out = bedrock.fetch_token_prices(known)
    assert out == {}


def test_skips_batch_inference_skus(fake_boto3):
    """Batch SKUs (cheaper, async) shouldn't be picked up — we want on-demand."""
    fake_boto3([[
        _bedrock_product(
            usagetype="USE1-anthropic.claude-3-haiku-20240307-v1:0-input-tokens-batch",
            model="anthropic.claude-3-haiku-20240307-v1:0",
            unit="1K tokens", price=0.000125,
            inference_type="Batch",
        ),
        _bedrock_product(
            usagetype="USE1-anthropic.claude-3-haiku-20240307-v1:0-output-tokens-batch",
            model="anthropic.claude-3-haiku-20240307-v1:0",
            unit="1K tokens", price=0.000625,
            inference_type="Batch",
        ),
    ]])
    from scripts.token_fetchers import bedrock

    out = bedrock.fetch_token_prices({"claude-3-haiku": {"provider": "bedrock"}})
    # Both inputs+outputs were batch, so we can't form a complete pair — skipped.
    assert out == {}


def test_skips_other_regions(fake_boto3):
    """eu-west-1 SKUs should be filtered out — we target us-east-1."""
    fake_boto3([[
        _bedrock_product(
            usagetype="EUW1-anthropic.claude-3-haiku-20240307-v1:0-input-tokens",
            model="anthropic.claude-3-haiku-20240307-v1:0",
            unit="1K tokens", price=0.0003,
            region_code="eu-west-1",
        ),
        _bedrock_product(
            usagetype="EUW1-anthropic.claude-3-haiku-20240307-v1:0-output-tokens",
            model="anthropic.claude-3-haiku-20240307-v1:0",
            unit="1K tokens", price=0.0015,
            region_code="eu-west-1",
        ),
    ]])
    from scripts.token_fetchers import bedrock

    out = bedrock.fetch_token_prices({"claude-3-haiku": {"provider": "bedrock"}})
    assert out == {}


def test_skips_when_only_input_or_only_output_present(fake_boto3):
    """A row with only input (and no output) shouldn't half-refresh — we
    want both directions or skip the model entirely."""
    fake_boto3([[
        _bedrock_product(
            usagetype="USE1-anthropic.claude-3-haiku-20240307-v1:0-input-tokens",
            model="anthropic.claude-3-haiku-20240307-v1:0",
            unit="1K tokens", price=0.00025,
        ),
    ]])
    from scripts.token_fetchers import bedrock

    out = bedrock.fetch_token_prices({"claude-3-haiku": {"provider": "bedrock"}})
    assert out == {}


# --- correctness: picking + unit normalization ---


def test_picks_cheapest_when_multiple_inputs(fake_boto3):
    """If two on-demand inputs exist for the same model (e.g. different
    region aliases), take the cheapest."""
    fake_boto3([[
        _bedrock_product(
            usagetype="USE1-anthropic.claude-3-haiku-20240307-v1:0-input-tokens",
            model="anthropic.claude-3-haiku-20240307-v1:0",
            unit="1K tokens", price=0.00050,
        ),
        _bedrock_product(
            usagetype="USE1-anthropic.claude-3-haiku-20240307-v1:0-input-tokens-cheap",
            model="anthropic.claude-3-haiku-20240307-v1:0",
            unit="1K tokens", price=0.00025,
        ),
        _bedrock_product(
            usagetype="USE1-anthropic.claude-3-haiku-20240307-v1:0-output-tokens",
            model="anthropic.claude-3-haiku-20240307-v1:0",
            unit="1K tokens", price=0.00125,
        ),
    ]])
    from scripts.token_fetchers import bedrock

    out = bedrock.fetch_token_prices({"claude-3-haiku": {"provider": "bedrock"}})
    assert out["claude-3-haiku"]["input_per_1m_usd"] == pytest.approx(0.25)


def test_normalizes_per_token_unit_to_per_1m(fake_boto3):
    """A row priced per single token (rare) should be scaled up correctly."""
    fake_boto3([[
        _bedrock_product(
            usagetype="USE1-anthropic.claude-3-haiku-20240307-v1:0-input-tokens",
            model="anthropic.claude-3-haiku-20240307-v1:0",
            unit="tokens", price=0.00000025,  # $0.25 per 1M
        ),
        _bedrock_product(
            usagetype="USE1-anthropic.claude-3-haiku-20240307-v1:0-output-tokens",
            model="anthropic.claude-3-haiku-20240307-v1:0",
            unit="tokens", price=0.00000125,
        ),
    ]])
    from scripts.token_fetchers import bedrock

    out = bedrock.fetch_token_prices({"claude-3-haiku": {"provider": "bedrock"}})
    assert out["claude-3-haiku"]["input_per_1m_usd"] == pytest.approx(0.25)
    assert out["claude-3-haiku"]["output_per_1m_usd"] == pytest.approx(1.25)


# --- error paths ---


def test_raises_fetch_error_when_pricing_api_throws(fake_boto3, monkeypatch):
    """A boto3 ClientError should bubble up as TokenFetchError so the
    orchestrator preserves the catalog for this provider."""
    client = fake_boto3([])
    paginator = MagicMock()
    paginator.paginate.side_effect = RuntimeError("AccessDenied: pricing:GetProducts")
    client.get_paginator.return_value = paginator
    from scripts.token_fetchers import bedrock

    with pytest.raises(TokenFetchError):
        bedrock.fetch_token_prices({"claude-3-haiku": {"provider": "bedrock"}})


def test_disambiguates_claude_3_haiku_from_3_5_haiku(fake_boto3):
    """The hints table must not let claude-3-haiku match claude-3-5-haiku
    (longer fragment); verify both can refresh independently."""
    fake_boto3([[
        _bedrock_product(
            usagetype="USE1-anthropic.claude-3-haiku-20240307-v1:0-input-tokens",
            model="anthropic.claude-3-haiku-20240307-v1:0",
            unit="1K tokens", price=0.00025,
        ),
        _bedrock_product(
            usagetype="USE1-anthropic.claude-3-haiku-20240307-v1:0-output-tokens",
            model="anthropic.claude-3-haiku-20240307-v1:0",
            unit="1K tokens", price=0.00125,
        ),
        _bedrock_product(
            usagetype="USE1-anthropic.claude-3-5-haiku-20241022-v1:0-input-tokens",
            model="anthropic.claude-3-5-haiku-20241022-v1:0",
            unit="1K tokens", price=0.00080,
        ),
        _bedrock_product(
            usagetype="USE1-anthropic.claude-3-5-haiku-20241022-v1:0-output-tokens",
            model="anthropic.claude-3-5-haiku-20241022-v1:0",
            unit="1K tokens", price=0.00400,
        ),
    ]])
    from scripts.token_fetchers import bedrock

    out = bedrock.fetch_token_prices({
        "claude-3-haiku":   {"provider": "bedrock"},
        "claude-3-5-haiku": {"provider": "bedrock"},
    })
    assert out["claude-3-haiku"]["input_per_1m_usd"] == pytest.approx(0.25)
    assert out["claude-3-5-haiku"]["input_per_1m_usd"] == pytest.approx(0.80)
    # CRITICAL: 3-haiku must NOT pick up 3-5-haiku's price (would be 0.80 not 0.25)
    assert out["claude-3-haiku"]["input_per_1m_usd"] != pytest.approx(0.80)
