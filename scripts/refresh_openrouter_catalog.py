"""Refresh the OpenRouter-derived sibling of the extended LLM catalog.

OpenRouter is an aggregator/router that exposes 350+ models from underlying
providers (Anthropic, OpenAI, Google, Together, Fireworks, Groq, etc.) behind
one API. Their `/api/v1/models` is unauthenticated and publishes per-model
prompt + completion rates as USD-per-token strings.

Why we ingest it alongside LiteLLM:
    LiteLLM tells you what each upstream provider charges. OpenRouter tells
    you what THEIR routed price is (which includes their margin and any
    pass-through). For the same model:
      - LiteLLM `bedrock/anthropic.claude-3-haiku-20240307-v1:0` -> direct
        Bedrock retail price
      - OpenRouter `anthropic/claude-3-haiku` -> what OpenRouter charges to
        route to it
    Comparing the two answers a real FinOps question: "is going through
    OpenRouter cheaper than wiring up Bedrock directly?" — usually no for
    high volume, sometimes yes for spiky workloads where you'd otherwise
    overpay on min-commit. Both rows live in the same extended catalog
    tagged source='litellm' or source='openrouter'.

Run:
    python scripts/refresh_openrouter_catalog.py [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_FILE = REPO_ROOT / "src" / "cloudprice_mcp" / "data" / "llm_catalog_openrouter.json"

OPENROUTER_URL = "https://openrouter.ai/api/v1/models"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--source-url", default=OPENROUTER_URL)
    args = parser.parse_args(argv)

    raw = _download(args.source_url)
    rows = _transform(raw)
    catalog = {
        "as_of": dt.date.today().isoformat(),
        "source": "openrouter",
        "source_url": args.source_url,
        "license": "OpenRouter public catalog (no redistribution restriction noted, "
                   "but cite the source).",
        "currency": "USD",
        "schema_version": 1,
        "notes": (
            "Auto-ingested from OpenRouter's public /api/v1/models. Prices reflect "
            "OpenRouter's routed rate (includes their margin), NOT the underlying "
            "provider's direct retail price. For direct provider pricing on the "
            "same model use the litellm-sourced rows in llm_catalog_extended.json."
        ),
        "model_count": len(rows),
        "models": rows,
    }
    if args.dry_run:
        print(f"=== DRY RUN — {len(rows)} models would be written ===")
        for r in rows[:5]:
            print(f"  {r['openrouter_id']:50s} in=${r['input_per_1m_usd']:.4f} "
                  f"out=${r['output_per_1m_usd']:.4f} ctx={r.get('context_window_tokens', '?')}")
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
    """OpenRouter gives {data: [{id, pricing: {prompt, completion, ...}, ...}, ...]}."""
    rows: list[dict] = []
    for entry in raw.get("data", []):
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("id")
        pricing = entry.get("pricing") or {}
        prompt = pricing.get("prompt")
        completion = pricing.get("completion")
        if not model_id or prompt is None or completion is None:
            continue

        try:
            input_per_token = float(prompt)
            output_per_token = float(completion)
        except (TypeError, ValueError):
            continue

        # Skip free-tier entries (price 0/0) and entries with no pricing signal.
        if input_per_token <= 0 and output_per_token <= 0:
            continue

        row = {
            "openrouter_id": model_id,
            "provider": "openrouter",
            "provider_underlying": _vendor_from_id(model_id),
            "name": entry.get("name"),
            "mode": _mode_from_modality(entry.get("architecture") or {}),
            "input_per_1m_usd": round(input_per_token * 1_000_000, 4),
            "output_per_1m_usd": round(output_per_token * 1_000_000, 4),
        }

        cache_read = pricing.get("input_cache_read")
        if cache_read is not None:
            try:
                cr = float(cache_read)
            except (TypeError, ValueError):
                cr = 0.0
            if cr > 0:
                row["cache_read_per_1m_usd"] = round(cr * 1_000_000, 4)
        cache_write = pricing.get("input_cache_write")
        if cache_write is not None:
            try:
                cw = float(cache_write)
            except (TypeError, ValueError):
                cw = 0.0
            if cw > 0:
                row["cache_write_per_1m_usd"] = round(cw * 1_000_000, 4)

        ctx = entry.get("context_length")
        if ctx:
            row["context_window_tokens"] = int(ctx)

        top = entry.get("top_provider") or {}
        max_out = top.get("max_completion_tokens")
        if max_out:
            row["max_output_tokens"] = int(max_out)

        rows.append(row)

    rows.sort(key=lambda r: r["openrouter_id"])
    return rows


def _vendor_from_id(model_id: str) -> str:
    """OpenRouter IDs are 'vendor/model-name'. Pull the vendor prefix as a
    hint of the underlying provider."""
    return model_id.split("/", 1)[0] if "/" in model_id else "unknown"


def _mode_from_modality(arch: dict) -> str:
    """OpenRouter only publishes text/chat-style models in /v1/models — treat
    everything as 'chat'. Future: distinguish multimodal (text+image->text)."""
    modality = arch.get("modality") or ""
    if "->" in modality:
        return "chat"
    return "chat"


if __name__ == "__main__":
    sys.exit(main())
