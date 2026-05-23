"""Refresh the extended LLM catalog from LiteLLM's public price feed.

Different shape from `refresh_tokens.py` — that one splices per-(model, provider)
prices INTO our hand-curated catalog. This one ingests LiteLLM's
`model_prices_and_context_window.json` (2700+ entries) wholesale into a separate
data file `src/cloudprice_mcp/data/llm_catalog_extended.json`.

Why a separate catalog:
    The hand-curated catalog is 19 vetted models, source of truth for the
    `compare_token_pricing` tool — we own correctness there. LiteLLM is
    community-maintained and covers ~2700 (model, provider, region) combinations
    we'd never hand-curate. Keeping them separate means:
      - Existing tool behavior unchanged (`source: "manual"` rows only)
      - Power users opt into the extended catalog via the new
        `lookup_extended_model_pricing` tool
      - Provenance is always clear: every extended row says
        `source: "litellm"` so users know what they're getting

Filter policy:
    LiteLLM has image-gen, embedding, audio, video-gen entries too. We keep
    only `mode in {"chat", "completion", "responses"}` for v0.16.0 — covering
    embeddings/TTS/image-gen is a future patch (FOCUS-aligned categories).

Run:
    python scripts/refresh_extended_catalog.py [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_FILE = REPO_ROOT / "src" / "cloudprice_mcp" / "data" / "llm_catalog_extended.json"

LITELLM_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)

# Modes we ingest in v0.16.0. The rest (image_generation, embedding, audio_*,
# video_generation, rerank, etc.) are out-of-scope and dropped.
TARGET_MODES = {"chat", "completion", "responses"}

# LiteLLM provider names -> our normalized provider labels. Anything not in
# this map passes through unchanged.
PROVIDER_NORMALIZATION = {
    "vertex_ai-anthropic_models": "vertex",
    "vertex_ai-language-models": "vertex",
    "vertex_ai-llama_models": "vertex",
    "vertex_ai-mistral_models": "vertex",
    "vertex_ai-ai21_models": "vertex",
    "vertex_ai-image-models": "vertex",
    "vertex_ai-vision-models": "vertex",
    "vertex_ai-chat-models": "vertex",
    "vertex_ai-embedding-models": "vertex",
    "vertex_ai-anthropic-models": "vertex",
    "azure": "azure_openai",
    "azure_ai": "azure_ai",
    "bedrock_converse": "bedrock",
    "gemini": "google",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--source-url", default=LITELLM_URL,
                        help="Override URL (useful for tests against a local mirror).")
    args = parser.parse_args(argv)

    raw = _download(args.source_url)
    rows = _transform(raw)
    catalog = {
        "as_of": dt.date.today().isoformat(),
        "source": "litellm",
        "source_url": args.source_url,
        "license": "MIT (from BerriAI/litellm)",
        "currency": "USD",
        "schema_version": 1,
        "notes": (
            "Auto-ingested from LiteLLM's public price feed. Models filtered to "
            "chat/completion/responses modes. Prices normalized from per-token to "
            "per-1M tokens. Provider names normalized where LiteLLM uses verbose IDs "
            "(e.g., 'vertex_ai-anthropic_models' -> 'vertex'). For curated, vetted "
            "pricing on the most-popular 19 models see token_prices.json + the "
            "compare_token_pricing tool."
        ),
        "model_count": len(rows),
        "models": rows,
    }
    if args.dry_run:
        print(f"=== DRY RUN — {len(rows)} models would be written ===")
        print("Sample (first 5):")
        for r in rows[:5]:
            print(f"  {r['litellm_id']:60s} provider={r['provider']:18s} "
                  f"in=${r['input_per_1m_usd']:.4f} out=${r['output_per_1m_usd']:.4f}")
        return 0

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(
        json.dumps(catalog, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {OUTPUT_FILE.relative_to(REPO_ROOT)} — {len(rows)} models")
    return 0


def _download(url: str) -> dict:
    resp = httpx.get(url, timeout=30, follow_redirects=True)
    resp.raise_for_status()
    return resp.json()


def _transform(raw: dict) -> list[dict]:
    """LiteLLM gives {model_id: {fields}}; we flatten + filter + normalize prices."""
    rows: list[dict] = []
    for litellm_id, attrs in raw.items():
        row = _build_row(litellm_id, attrs)
        if row is not None:
            rows.append(row)
    rows.sort(key=lambda r: (r["provider"], r["litellm_id"]))
    return rows


def _build_row(litellm_id: str, attrs) -> dict | None:
    if not isinstance(attrs, dict) or litellm_id == "sample_spec":
        return None
    mode = attrs.get("mode")
    if mode not in TARGET_MODES:
        return None
    input_cost = attrs.get("input_cost_per_token")
    output_cost = attrs.get("output_cost_per_token")
    if input_cost is None or output_cost is None:
        return None
    if input_cost <= 0 and output_cost <= 0:
        return None

    provider_raw = attrs.get("litellm_provider") or "unknown"
    provider = PROVIDER_NORMALIZATION.get(provider_raw, provider_raw)
    row = {
        "litellm_id": litellm_id,
        "provider": provider,
        "provider_raw": provider_raw,
        "mode": mode,
        "input_per_1m_usd": round(float(input_cost) * 1_000_000, 4),
        "output_per_1m_usd": round(float(output_cost) * 1_000_000, 4),
    }
    _attach_cache_rates(row, attrs)
    _attach_context(row, attrs)
    _attach_capability_flags(row, attrs)
    return row


def _attach_cache_rates(row: dict, attrs: dict) -> None:
    cache_read = attrs.get("cache_read_input_token_cost")
    if cache_read is not None and cache_read > 0:
        row["cache_read_per_1m_usd"] = round(float(cache_read) * 1_000_000, 4)
    cache_create = attrs.get("cache_creation_input_token_cost")
    if cache_create is not None and cache_create > 0:
        row["cache_write_per_1m_usd"] = round(float(cache_create) * 1_000_000, 4)


def _attach_context(row: dict, attrs: dict) -> None:
    ctx = attrs.get("max_input_tokens") or attrs.get("max_tokens")
    if ctx:
        row["context_window_tokens"] = int(ctx)
    max_out = attrs.get("max_output_tokens")
    if max_out:
        row["max_output_tokens"] = int(max_out)


def _attach_capability_flags(row: dict, attrs: dict) -> None:
    for flag in ("supports_vision", "supports_function_calling",
                 "supports_prompt_caching", "supports_response_schema"):
        if attrs.get(flag):
            row[flag] = True


if __name__ == "__main__":
    sys.exit(main())
