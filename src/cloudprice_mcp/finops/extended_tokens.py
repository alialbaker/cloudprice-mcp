"""lookup_extended_model_pricing — search the LiteLLM-derived extended catalog.

The hand-curated `compare_token_pricing` tool covers 19 vetted models with
careful provider mapping. This tool covers the OTHER ~1900+ model/provider
combinations that LiteLLM tracks: Together AI, Fireworks, Replicate, Groq,
Cerebras, Perplexity, Vercel AI Gateway, regional Bedrock/Azure variants,
older model versions, etc.

The two tools complement each other:
  - `compare_token_pricing` — "What's the cheapest provider for Claude 4
    Sonnet?" — for top-tier models you'd actually deploy. Quality > coverage.
  - `lookup_extended_model_pricing` — "Does Together AI host Llama 3.1 405B,
    and at what price?" — for any model, any provider. Coverage > vetting.

Returns ranked rows (cheapest output cost first) with provenance always
tagged `source: "litellm"` so callers know it's community-maintained, not
vendor-verified.
"""
from __future__ import annotations

import json
from importlib import resources
from typing import Any

_DATA_PACKAGE = "cloudprice_mcp.data"
_DATA_FILE = "llm_catalog_extended.json"

_cache: dict[str, Any] | None = None


def _load_catalog() -> dict[str, Any]:
    global _cache
    if _cache is None:
        text = resources.files(_DATA_PACKAGE).joinpath(_DATA_FILE).read_text(encoding="utf-8")
        _cache = json.loads(text)
    return _cache


def reset_extended_cache() -> None:
    """Test hook."""
    global _cache
    _cache = None


def lookup_extended_model_pricing(
    query: str | None = None,
    provider: str | None = None,
    mode: str | None = None,
    max_context_tokens: int | None = None,
    monthly_input_tokens: int | None = None,
    monthly_output_tokens: int | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    """Search the extended LLM catalog for matching model/provider combinations.

    Args:
        query: case-insensitive substring matched against `litellm_id`. E.g.
            "llama-3.1-70b" matches every Llama 3.1 70B variant on every host.
        provider: filter to one normalized provider name (e.g., "bedrock",
            "fireworks_ai", "together_ai", "groq", "perplexity").
        mode: filter to "chat" / "completion" / "responses". Default: any.
        max_context_tokens: skip models with smaller context. Useful for
            "what models support 200K+ context cheap?"
        monthly_input_tokens / monthly_output_tokens: when both provided,
            compute monthly_total_usd per row + rank by it.
        limit: cap returned rows (default 25). Catalog has ~2000 entries.
    """
    catalog = _load_catalog()
    q = query.lower() if query else None
    prov = provider.lower() if provider else None
    md = mode.lower() if mode else None

    matched: list[dict[str, Any]] = []
    for row in catalog.get("models", []):
        if q and q not in row["litellm_id"].lower():
            continue
        if prov and row["provider"].lower() != prov:
            continue
        if md and row["mode"].lower() != md:
            continue
        if max_context_tokens is not None:
            ctx = row.get("context_window_tokens")
            if ctx is None or ctx < max_context_tokens:
                continue

        enriched = dict(row)
        enriched["source"] = "litellm"
        enriched["blended_per_1m_usd_3to1_out_to_in"] = round(
            (enriched["output_per_1m_usd"] * 3 + enriched["input_per_1m_usd"]) / 4, 4,
        )

        if monthly_input_tokens is not None and monthly_output_tokens is not None:
            input_cost = monthly_input_tokens / 1_000_000 * enriched["input_per_1m_usd"]
            output_cost = monthly_output_tokens / 1_000_000 * enriched["output_per_1m_usd"]
            enriched["monthly_input_usd"] = round(input_cost, 2)
            enriched["monthly_output_usd"] = round(output_cost, 2)
            enriched["monthly_total_usd"] = round(input_cost + output_cost, 2)

        matched.append(enriched)

    if monthly_input_tokens is not None and monthly_output_tokens is not None:
        matched.sort(key=lambda r: r["monthly_total_usd"])
        ranked_by = "monthly_total_usd"
    else:
        matched.sort(key=lambda r: r["output_per_1m_usd"])
        ranked_by = "output_per_1m_usd"

    total_match = len(matched)
    rows = matched[:limit]

    return {
        "kind": "extended_model_pricing_lookup",
        "title": _build_title(query, provider, mode, monthly_input_tokens, monthly_output_tokens),
        "headline": _build_headline(rows, ranked_by, total_match, limit),
        "as_of": catalog.get("as_of"),
        "source": catalog.get("source", "litellm"),
        "source_url": catalog.get("source_url"),
        "filters": {
            "query": query, "provider": provider, "mode": mode,
            "max_context_tokens": max_context_tokens,
            "monthly_input_tokens": monthly_input_tokens,
            "monthly_output_tokens": monthly_output_tokens,
            "limit": limit,
        },
        "ranked_by": ranked_by,
        "total_matches": total_match,
        "returned_rows": len(rows),
        "rows": rows,
        "recommended": (
            {"litellm_id": rows[0]["litellm_id"], "provider": rows[0]["provider"]}
            if rows else None
        ),
        "honest_gaps": _honest_gaps(),
    }


def _build_title(query, provider, mode, in_t, out_t) -> str:
    parts = ["Extended LLM catalog lookup"]
    if query:
        parts.append(f"matching {query!r}")
    if provider:
        parts.append(f"on {provider}")
    if mode:
        parts.append(f"(mode={mode})")
    if in_t is not None and out_t is not None:
        parts.append(f"@ {in_t // 1_000_000}M in / {out_t // 1_000_000}M out tokens/mo")
    return " ".join(parts)


def _build_headline(rows, ranked_by, total, limit) -> str:
    if not rows:
        return "No models in the extended catalog match those filters."
    top = rows[0]
    truncated = " (showing top %d)" % limit if total > limit else ""
    if ranked_by == "monthly_total_usd":
        return (
            f"{top['litellm_id']} on {top['provider']} cheapest at "
            f"${top['monthly_total_usd']:,.2f}/mo across {total} matches{truncated}."
        )
    return (
        f"{top['litellm_id']} on {top['provider']} lowest output cost: "
        f"${top['output_per_1m_usd']:.4f}/1M across {total} matches{truncated}."
    )


def _honest_gaps() -> list[str]:
    return [
        "Source: LiteLLM's `model_prices_and_context_window.json` — community-maintained, MIT-licensed. Not vendor-verified the way the hand-curated `compare_token_pricing` catalog is.",
        "Prices are on-demand list rates. Batch-API discounts, committed-use rates, prompt-caching savings, and provisioned-throughput are NOT modeled.",
        "Regional variants (e.g. azure/eu/, bedrock/ap-northeast-1/) appear as separate rows — filter with `query` to narrow.",
        "Coverage: chat / completion / responses modes only. Image-gen, embedding, audio, video-gen, rerank entries from LiteLLM are filtered out in v0.16.0.",
        "For top-tier production models, prefer `compare_token_pricing` (hand-curated, vetted, narrower) over this tool.",
    ]
