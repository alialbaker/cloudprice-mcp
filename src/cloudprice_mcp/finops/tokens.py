"""compare_token_pricing — cross-provider LLM token pricing comparison.

The fastest-growing FinOps cost category in 2026 isn't EC2 — it's LLM tokens.
Every team building AI-assisted features is bleeding budget across OpenAI,
Anthropic, Google, AWS Bedrock, Azure OpenAI, Mistral, DeepSeek, etc. Same
model is often available on multiple providers at different prices; the
cheapest provider for Claude 3.5 Sonnet, GPT-4o, Llama 3.1 etc. is a real
FinOps question with no canonical public comparison.

This tool answers:
    "What does Claude 4 Sonnet cost on each provider that offers it?"
    "Cheapest model that handles 200K context for output-heavy chat?"
    "If I'm pushing 50M input + 10M output per month, what's my bill on
     each option?"

Inputs:
    model_family  — optional filter: "claude" | "gpt" | "gemini" | "llama" | "mistral" | "deepseek"
    model_id      — optional exact filter: "claude-4-sonnet", "gpt-4o", etc.
    providers     — optional provider filter
    monthly_input_tokens   — for monthly-cost calculation
    monthly_output_tokens  — for monthly-cost calculation

Returns per-model, per-provider rows ranked by total monthly cost when
volumes are provided, or by per-1M output cost otherwise (since output
tokens dominate most chat/inference workloads).
"""
from __future__ import annotations

import json
from importlib import resources
from typing import Any

_DATA_PACKAGE = "cloudprice_mcp.data"
_DATA_FILE = "token_prices.json"

_catalog_cache: dict[str, Any] | None = None


def _load_catalog() -> dict[str, Any]:
    global _catalog_cache
    if _catalog_cache is None:
        text = resources.files(_DATA_PACKAGE).joinpath(_DATA_FILE).read_text(encoding="utf-8")
        _catalog_cache = json.loads(text)
    return _catalog_cache


def reset_catalog_cache() -> None:
    """Used by tests to inject patched catalogs."""
    global _catalog_cache
    _catalog_cache = None


def compare_token_pricing(
    model_family: str | None = None,
    model_id: str | None = None,
    providers: list[str] | None = None,
    monthly_input_tokens: int | None = None,
    monthly_output_tokens: int | None = None,
) -> dict[str, Any]:
    """Cross-provider LLM token pricing comparison.

    Filters compose AND-wise: if you pass `model_family="claude"` and
    `providers=["bedrock", "vertex"]`, you'll get Claude models priced on
    those two hyperscalers only.

    If both monthly volumes are provided, also computes per-row monthly USD
    cost and ranks by that. Otherwise ranks by per-1M output cost (output
    tokens dominate most workloads — usually 1:3 to 1:10 input:output).
    """
    catalog = _load_catalog()
    family_filter = model_family.lower() if model_family else None
    id_filter = model_id.lower() if model_id else None
    provider_filter = {p.lower() for p in providers} if providers else None

    rows: list[dict[str, Any]] = []
    matched_models: set[str] = set()
    seen_providers: set[str] = set()

    for model in catalog.get("models", []):
        if family_filter and model["family"].lower() != family_filter:
            continue
        if id_filter and model["model_id"].lower() != id_filter:
            continue

        for provider_entry in model.get("providers", []):
            provider = provider_entry["provider"]
            if provider_filter and provider not in provider_filter:
                continue

            input_rate = float(provider_entry["input_per_1m_usd"])
            output_rate = float(provider_entry["output_per_1m_usd"])
            cache_read = provider_entry.get("cache_read_per_1m_usd")
            cache_write = provider_entry.get("cache_write_per_1m_usd")

            row = {
                "model_id": model["model_id"],
                "family": model["family"],
                "vendor": model["vendor"],
                "context_window_tokens": model["context_window_tokens"],
                "provider": provider,
                "provider_label": catalog["providers"].get(provider, {}).get("label", provider),
                "provider_kind": catalog["providers"].get(provider, {}).get("kind", "unknown"),
                "input_per_1m_usd": input_rate,
                "output_per_1m_usd": output_rate,
                "cache_read_per_1m_usd": float(cache_read) if cache_read is not None else None,
                "cache_write_per_1m_usd": float(cache_write) if cache_write is not None else None,
                "blended_per_1m_usd_3to1_out_to_in": round((output_rate * 3 + input_rate) / 4, 4),
            }

            if monthly_input_tokens is not None and monthly_output_tokens is not None:
                input_cost = monthly_input_tokens / 1_000_000 * input_rate
                output_cost = monthly_output_tokens / 1_000_000 * output_rate
                row["monthly_input_usd"] = round(input_cost, 2)
                row["monthly_output_usd"] = round(output_cost, 2)
                row["monthly_total_usd"] = round(input_cost + output_cost, 2)

            rows.append(row)
            matched_models.add(model["model_id"])
            seen_providers.add(provider)

    # Ranking
    if monthly_input_tokens is not None and monthly_output_tokens is not None:
        rows.sort(key=lambda r: r["monthly_total_usd"])
        ranking_field = "monthly_total_usd"
    else:
        rows.sort(key=lambda r: r["output_per_1m_usd"])
        ranking_field = "output_per_1m_usd"

    recommended = rows[0] if rows else None

    return {
        "kind": "token_pricing_comparison",
        "title": _build_title(model_family, model_id, providers, monthly_input_tokens, monthly_output_tokens),
        "headline": _build_headline(rows, ranking_field, monthly_input_tokens, monthly_output_tokens),
        "as_of": catalog.get("as_of"),
        "filters": {
            "model_family": model_family,
            "model_id": model_id,
            "providers": providers,
            "monthly_input_tokens": monthly_input_tokens,
            "monthly_output_tokens": monthly_output_tokens,
        },
        "ranked_by": ranking_field,
        "rows": rows,
        "recommended": {
            "model_id": recommended["model_id"],
            "provider": recommended["provider"],
        } if recommended else None,
        "matched_models": sorted(matched_models),
        "matched_providers": sorted(seen_providers),
        "honest_gaps": _honest_gaps(),
    }


def _build_title(family, model_id, providers, in_tokens, out_tokens) -> str:
    parts = ["Token pricing"]
    if model_id:
        parts.append(f"for {model_id}")
    elif family:
        parts.append(f"for {family} family")
    if providers:
        parts.append(f"on {', '.join(providers)}")
    if in_tokens is not None and out_tokens is not None:
        parts.append(f"@ {in_tokens // 1_000_000}M in / {out_tokens // 1_000_000}M out tokens/mo")
    return " ".join(parts)


def _build_headline(rows, ranked_by, in_tokens, out_tokens) -> str:
    if not rows:
        return "No models match the requested filters."
    top = rows[0]
    if ranked_by == "monthly_total_usd":
        return (
            f"{top['model_id']} on {top['provider']} is cheapest at "
            f"${top['monthly_total_usd']:,.2f}/mo for {in_tokens // 1_000_000}M in / "
            f"{out_tokens // 1_000_000}M out tokens."
        )
    return (
        f"{top['model_id']} on {top['provider']} has the lowest output cost: "
        f"${top['output_per_1m_usd']:.4f}/1M output (input ${top['input_per_1m_usd']:.4f}/1M)."
    )


def _honest_gaps() -> list[str]:
    return [
        "Prices are vendor list rates. Enterprise agreements, committed-use discounts, batch-API 50% discounts, and provisioned-throughput hourly rates are NOT modeled — only on-demand per-token pricing.",
        "Cache pricing (when published) is included as separate fields. Most workloads see 30-90% input cost reduction with proper prompt caching — not factored into the monthly_total calculation.",
        "Context-window pricing tiers (e.g. Gemini 1.5 Pro >128K input gets 2x rate) are NOT modeled. Within-tier prices only.",
        "Vendor parity assumed where Anthropic / OpenAI models are offered on hyperscalers (Bedrock, Vertex, Azure OpenAI). Provider-level regional pricing variation is NOT modeled.",
        "blended_per_1m_usd_3to1_out_to_in is a single-number proxy assuming a typical chat workload (3 output tokens per 1 input). Adjust for your actual traffic mix.",
        "Catalog is hand-curated as of the `as_of` date. Auto-refresh from vendor pricing pages is planned for v0.12.x.",
    ]
