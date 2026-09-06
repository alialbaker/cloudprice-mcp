"""AWS Bedrock token-price fetcher.

Uses the AWS Pricing API (`boto3.client('pricing')`) with service code
`AmazonBedrock`. Bedrock publishes per-token pricing per model and per
direction (input vs output), with units of `1K tokens` (we convert to
1M).

Authentication: standard AWS credential chain. The same OIDC role the
v0.7 per-cloud refresh uses already has `pricing:GetProducts`; we reuse
it.

Mapping strategy:
    Our catalog `model_id` (e.g. "claude-3-haiku") is the stable key we
    expose to MCP clients. Bedrock uses verbose, dated IDs like
    `anthropic.claude-3-haiku-20240307-v1:0`. We use a substring-match
    table (BEDROCK_ID_HINTS) so a Bedrock-side rename like
    `-20240307-v1:0` -> `-20240307-v1:1` still resolves. If multiple
    Bedrock SKUs match the same internal model_id we take the cheapest
    on-demand price (some models have both ON_DEMAND and BATCH; we want
    ON_DEMAND).

Per-SKU resilience:
    A missing model in the Pricing API response is logged and skipped —
    we preserve the caller's entry untouched. Only models we
    successfully refresh appear in the return dict.
"""
from __future__ import annotations

import json
from typing import Any

from scripts.token_fetchers.base import TokenFetchError, TokenProviderEntry

provider_name = "bedrock"
_PRICING_REGION = "us-east-1"
_TARGET_REGION_CODE = "USE1"  # Bedrock usagetype prefix for us-east-1


# Substring fragments that uniquely identify each internal model_id within a
# Bedrock model ARN/usagetype string. Order matters only when one fragment
# could match a longer one (e.g. claude-3 vs claude-3-5).
BEDROCK_ID_HINTS: dict[str, str] = {
    "claude-4-opus":    "claude-opus-4",
    "claude-4-sonnet":  "claude-sonnet-4",
    "claude-3-5-haiku": "claude-3-5-haiku",
    "claude-3-haiku":   "claude-3-haiku",
    "llama-3.1-405b":   "llama3-1-405b",
    "llama-3.1-70b":    "llama3-1-70b",
    "llama-3.1-8b":     "llama3-1-8b",
    "llama-3.3-70b":    "llama3-3-70b",
    "mistral-large-2":  "mistral-large-2407",
    "deepseek-r1":      "deepseek.r1",
}


def fetch_token_prices(
    known_models: dict[str, TokenProviderEntry],
) -> dict[str, TokenProviderEntry]:
    """Refresh prices for every model in `known_models` that we know how to
    resolve in Bedrock. Returns a dict of model_id -> updated entry. Models
    we couldn't find are omitted from the return (caller preserves the old
    entry)."""
    try:
        import boto3
    except ImportError as e:
        raise TokenFetchError(
            "Bedrock token fetcher requires boto3. Install with "
            "`pip install boto3` before running the refresh script."
        ) from e

    # Pricing API only runs in us-east-1 / ap-south-1; this is the endpoint
    # region, NOT the priced region. SonarLint S6262 doesn't apply.
    client = boto3.client("pricing", region_name=_PRICING_REGION)  # NOSONAR
    raw_prices = _scan_all_bedrock_products(client)

    out: dict[str, TokenProviderEntry] = {}
    for model_id, existing in known_models.items():
        hint = BEDROCK_ID_HINTS.get(model_id)
        if not hint:
            continue
        matched = _pick_cheapest_for(hint, raw_prices)
        if matched is None:
            continue
        input_per_1m = matched.get("input_per_1m_usd")
        output_per_1m = matched.get("output_per_1m_usd")
        if input_per_1m is None or output_per_1m is None:
            continue
        updated: TokenProviderEntry = dict(existing)  # type: ignore[assignment]
        updated["provider"] = provider_name
        updated["input_per_1m_usd"] = round(input_per_1m, 6)
        updated["output_per_1m_usd"] = round(output_per_1m, 6)
        out[model_id] = updated
    return out


def _scan_all_bedrock_products(client) -> list[dict[str, Any]]:
    """Page through every Bedrock SKU and return a list of normalized rows.

    Each row: {model_hint, direction, inference_type, price_per_1k_usd}.
    `model_hint` is the lowercased usagetype/model string we'll substring-
    match against BEDROCK_ID_HINTS.
    """
    rows: list[dict[str, Any]] = []
    paginator = client.get_paginator("get_products")
    try:
        pages = paginator.paginate(ServiceCode="AmazonBedrock")
        for page in pages:
            for raw in page.get("PriceList", []):
                product = json.loads(raw)
                normalized = _normalize_product(product)
                if normalized is not None:
                    rows.append(normalized)
    except Exception as e:
        raise TokenFetchError(f"AWS Pricing API error for Bedrock: {e}") from e
    return rows


def _normalize_product(product: dict) -> dict[str, Any] | None:
    """Pull the bits we care about out of one Pricing API product."""
    attrs = (product.get("product") or {}).get("attributes") or {}
    usagetype = (attrs.get("usagetype") or "").lower()
    model = (attrs.get("model") or "").lower()
    inference = (attrs.get("inferenceType") or attrs.get("inferencetype") or "").lower()

    # Only us-east-1 on-demand inference rows interest us. Bedrock publishes
    # batch + provisioned-throughput SKUs in the same service; skip those.
    region_code = (attrs.get("regionCode") or "").lower()
    if region_code:
        if region_code != "us-east-1":
            return None
    elif _TARGET_REGION_CODE.lower() not in usagetype:
        return None
    if inference and "demand" not in inference:
        return None

    direction = _direction_from_usagetype(usagetype)
    if direction is None:
        return None

    price_per_1k = _on_demand_price_per_1k(product)
    if price_per_1k is None:
        return None

    return {
        "model_hint": f"{usagetype} {model}",
        "direction": direction,
        "price_per_1k_usd": price_per_1k,
    }


def _direction_from_usagetype(usagetype: str) -> str | None:
    if "input-tokens" in usagetype or "inputtokens" in usagetype:
        return "input"
    if "output-tokens" in usagetype or "outputtokens" in usagetype:
        return "output"
    return None


def _on_demand_price_per_1k(product: dict) -> float | None:
    """Walk OnDemand -> priceDimensions, return USD per 1K tokens."""
    on_demand = product.get("terms", {}).get("OnDemand", {})
    for term in on_demand.values():
        for pd in (term.get("priceDimensions") or {}).values():
            price = _price_per_1k_from_dimension(pd)
            if price is not None:
                return price
    return None


def _price_per_1k_from_dimension(pd: dict) -> float | None:
    """Bedrock uses units like '1K tokens', 'tokens', or '1k input tokens'.
    Returns USD per 1K tokens, or None if the dimension isn't a token rate."""
    unit = (pd.get("unit") or "").lower()
    if "token" not in unit:
        return None
    usd = (pd.get("pricePerUnit") or {}).get("USD")
    if usd is None:
        return None
    value = float(usd)
    if value <= 0:
        return None
    if "1k" in unit or "1,000" in unit or "1000" in unit:
        return value
    # Per-single-token — scale up to /1K.
    return value * 1000.0


def _pick_cheapest_for(
    hint: str, rows: list[dict[str, Any]],
) -> dict[str, float] | None:
    """Find the cheapest input + output prices for any row whose
    model_hint contains `hint`. Returns {input_per_1m_usd, output_per_1m_usd}
    or None if we couldn't find both directions."""
    input_prices: list[float] = []
    output_prices: list[float] = []
    for r in rows:
        if hint not in r["model_hint"]:
            continue
        per_1m = r["price_per_1k_usd"] * 1000.0
        if r["direction"] == "input":
            input_prices.append(per_1m)
        else:
            output_prices.append(per_1m)
    if not input_prices or not output_prices:
        return None
    return {
        "input_per_1m_usd": min(input_prices),
        "output_per_1m_usd": min(output_prices),
    }
