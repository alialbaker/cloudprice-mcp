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
from dataclasses import dataclass
from importlib import resources
from typing import Any

_DATA_PACKAGE = "cloudprice_mcp.data"
_LITELLM_FILE = "llm_catalog_extended.json"
_OPENROUTER_FILE = "llm_catalog_openrouter.json"

_cache: dict[str, Any] | None = None


def _load_catalog() -> dict[str, Any]:
    """Merge the LiteLLM + OpenRouter catalogs into one row list. Each row
    keeps a `source` tag ('litellm' / 'openrouter') so callers can filter.
    The lookup tool normalizes both schemas to the same shape: every row
    has `model_id`, `provider`, `input_per_1m_usd`, `output_per_1m_usd`.
    """
    global _cache
    if _cache is not None:
        return _cache

    models: list[dict] = []
    as_of_dates: list[str] = []
    sources_loaded: list[str] = []

    litellm_blob = _read_optional(_LITELLM_FILE)
    if litellm_blob:
        for row in litellm_blob.get("models", []):
            models.append({
                **row,
                "model_id": row.get("litellm_id"),
                "source": "litellm",
            })
        if litellm_blob.get("as_of"):
            as_of_dates.append(litellm_blob["as_of"])
        sources_loaded.append("litellm")

    openrouter_blob = _read_optional(_OPENROUTER_FILE)
    if openrouter_blob:
        for row in openrouter_blob.get("models", []):
            models.append({
                **row,
                "model_id": row.get("openrouter_id"),
                "source": "openrouter",
            })
        if openrouter_blob.get("as_of"):
            as_of_dates.append(openrouter_blob["as_of"])
        sources_loaded.append("openrouter")

    _cache = {
        "as_of": max(as_of_dates) if as_of_dates else None,
        "sources_loaded": sources_loaded,
        "models": models,
    }
    return _cache


def _read_optional(filename: str) -> dict | None:
    """Return parsed JSON or None if the data file isn't bundled."""
    try:
        text = resources.files(_DATA_PACKAGE).joinpath(filename).read_text(encoding="utf-8")
    except OSError:
        return None
    return json.loads(text)


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
    source: str | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    """Search the extended LLM catalog for matching model/provider combinations.

    Args:
        query: case-insensitive substring matched against `model_id`. E.g.
            "llama-3.1-70b" matches every Llama 3.1 70B variant on every host.
        provider: filter to one normalized provider name (e.g., "bedrock",
            "fireworks_ai", "together_ai", "groq", "perplexity", "openrouter").
        mode: filter to "chat" / "completion" / "responses". Default: any.
        max_context_tokens: skip models with smaller context. Useful for
            "what models support 200K+ context cheap?"
        monthly_input_tokens / monthly_output_tokens: when both provided,
            compute monthly_total_usd per row + rank by it.
        source: filter to one upstream source: "litellm" (direct-provider
            retail prices) or "openrouter" (OpenRouter routed prices). Default:
            both. Useful for cross-source comparison.
        limit: cap returned rows (default 25). Catalog has ~2000 entries.
    """
    catalog = _load_catalog()
    filters = _Filters(
        q=query.lower() if query else None,
        prov=provider.lower() if provider else None,
        md=mode.lower() if mode else None,
        src=source.lower() if source else None,
        ctx_min=max_context_tokens,
    )

    matched: list[dict[str, Any]] = []
    for row in catalog.get("models", []):
        if not _row_matches(row, filters):
            continue
        matched.append(_enrich_row(row, monthly_input_tokens, monthly_output_tokens))

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
        "title": _build_title(query, provider, mode, source,
                              monthly_input_tokens, monthly_output_tokens),
        "headline": _build_headline(rows, ranked_by, total_match, limit),
        "as_of": catalog.get("as_of"),
        "sources_loaded": catalog.get("sources_loaded", []),
        "filters": {
            "query": query, "provider": provider, "mode": mode, "source": source,
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
            {"model_id": rows[0]["model_id"], "provider": rows[0]["provider"],
             "source": rows[0]["source"]}
            if rows else None
        ),
        "honest_gaps": _honest_gaps(),
    }


@dataclass(frozen=True)
class _Filters:
    q: str | None
    prov: str | None
    md: str | None
    src: str | None
    ctx_min: int | None


def _row_matches(row: dict, f: _Filters) -> bool:
    model_id = (row.get("model_id") or "").lower()
    if f.q and f.q not in model_id:
        return False
    if f.prov and row.get("provider", "").lower() != f.prov:
        return False
    if f.md and row.get("mode", "").lower() != f.md:
        return False
    if f.src and row.get("source", "").lower() != f.src:
        return False
    if f.ctx_min is not None:
        ctx = row.get("context_window_tokens")
        if ctx is None or ctx < f.ctx_min:
            return False
    return True


def _enrich_row(row: dict, in_tokens: int | None, out_tokens: int | None) -> dict:
    enriched = dict(row)
    output_rate = enriched["output_per_1m_usd"]
    input_rate = enriched["input_per_1m_usd"]
    enriched["blended_per_1m_usd_3to1_out_to_in"] = round(
        (output_rate * 3 + input_rate) / 4, 4,
    )
    if in_tokens is not None and out_tokens is not None:
        enriched["monthly_input_usd"] = round(in_tokens / 1_000_000 * input_rate, 2)
        enriched["monthly_output_usd"] = round(out_tokens / 1_000_000 * output_rate, 2)
        enriched["monthly_total_usd"] = round(
            enriched["monthly_input_usd"] + enriched["monthly_output_usd"], 2,
        )
    return enriched


def _build_title(query, provider, mode, source, in_t, out_t) -> str:
    parts = ["Extended LLM catalog lookup"]
    if query:
        parts.append(f"matching {query!r}")
    if provider:
        parts.append(f"on {provider}")
    if mode:
        parts.append(f"(mode={mode})")
    if source:
        parts.append(f"[source={source}]")
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
            f"{top['model_id']} on {top['provider']} ({top['source']}) cheapest at "
            f"${top['monthly_total_usd']:,.2f}/mo across {total} matches{truncated}."
        )
    return (
        f"{top['model_id']} on {top['provider']} ({top['source']}) lowest output cost: "
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
