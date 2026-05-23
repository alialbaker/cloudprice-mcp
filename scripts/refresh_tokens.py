"""Token-price refresh orchestrator.

Mirror of `scripts/refresh_prices.py` but for `data/token_prices.json`.
Runs each token-fetcher in turn, splices the returned per-(model,provider)
prices back into the catalog, and writes a dated snapshot to
`src/cloudprice_mcp/data/token_prices/YYYY-MM-DD.json`.

Why dated snapshots:
    Same moat angle as the per-cloud history — after N weeks we have a
    public, MIT-licensed multi-provider LLM-token price-history dataset.
    Vantage / Cloudability / Apptio don't publish this; OpenAI/Anthropic
    pricing pages have no archive. The dated dir is the durable artifact.

Partial-refresh policy:
    Provider fetcher failures preserve the existing entries for that
    provider. The snapshot is still written. Per-model misses inside a
    provider are also non-fatal — they just don't appear in the diff.

Usage:
    python scripts/refresh_tokens.py [--dry-run] [--providers bedrock,...]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from importlib import import_module
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
TOKENS_FILE = REPO_ROOT / "src" / "cloudprice_mcp" / "data" / "token_prices.json"
SNAPSHOT_DIR = REPO_ROOT / "src" / "cloudprice_mcp" / "data" / "token_prices"

ALL_PROVIDERS = ("bedrock",)


class TokenRefreshSummary:
    """Tracks changes for the PR body."""

    def __init__(self) -> None:
        self.refreshed: list[str] = []
        self.skipped: list[tuple[str, str]] = []  # (provider, reason)
        # provider -> [(model_id, field, old, new)]
        self.diffs: dict[str, list[tuple[str, str, float, float]]] = {}

    def add_diff(self, provider: str, model_id: str, field: str, old: float, new: float) -> None:
        if abs(old - new) < 1e-9:
            return
        self.diffs.setdefault(provider, []).append((model_id, field, old, new))

    def as_markdown(self) -> str:
        lines: list[str] = []
        if self.refreshed:
            lines.append(f"Token-price refresh: {', '.join(self.refreshed)}")
        if self.skipped:
            for provider, reason in self.skipped:
                lines.append(f"Skipped {provider}: {reason}")
        for provider, changes in self.diffs.items():
            lines.append(f"\n### {provider} ({len(changes)} prices changed)")
            for model_id, field, old, new in changes:
                pct = ((new - old) / old * 100) if old else 0.0
                lines.append(f"- `{model_id}` {field}: ${old:.4f} -> ${new:.4f} ({pct:+.2f}%)")
        return "\n".join(lines) if lines else "No token-price changes."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--providers", default=",".join(ALL_PROVIDERS),
        help=f"Comma-separated subset of {ALL_PROVIDERS}. Default: all.",
    )
    parser.add_argument("--output-summary", type=Path, default=None)
    args = parser.parse_args(argv)

    selected = tuple(p.strip() for p in args.providers.split(",") if p.strip())
    invalid = [p for p in selected if p not in ALL_PROVIDERS]
    if invalid:
        parser.error(f"Unknown providers: {invalid}. Valid: {ALL_PROVIDERS}")

    catalog = _load_token_file()
    summary = TokenRefreshSummary()

    for provider in selected:
        _refresh_provider(provider, catalog, summary)

    _bump_as_of(catalog)

    if args.dry_run:
        print("=== DRY RUN — no files written ===")
        print(summary.as_markdown())
        return 0 if summary.refreshed else 2

    if not summary.refreshed:
        print("ERROR: no providers refreshed — refusing to write snapshot.")
        print(summary.as_markdown())
        return 2

    _write_snapshot(catalog)
    _write_current(catalog)
    print(summary.as_markdown())
    if args.output_summary:
        args.output_summary.write_text(summary.as_markdown(), encoding="utf-8")
    return 0


def _load_token_file() -> dict:
    return json.loads(TOKENS_FILE.read_text(encoding="utf-8"))


def _refresh_provider(provider: str, catalog: dict, summary: TokenRefreshSummary) -> None:
    """For one provider, collect every (model_id, entry) where entry.provider
    matches, ask the fetcher to refresh, then splice updates back in-place."""
    known: dict[str, dict] = {}
    # Track the index of the provider entry inside each model's providers list
    # so we can mutate without re-searching.
    positions: dict[str, tuple[int, int]] = {}  # model_id -> (model_idx, provider_idx)
    for mi, model in enumerate(catalog.get("models", [])):
        for pi, entry in enumerate(model.get("providers", [])):
            if entry.get("provider") == provider:
                known[model["model_id"]] = dict(entry)
                positions[model["model_id"]] = (mi, pi)

    if not known:
        summary.skipped.append((provider, "no models tagged with this provider in catalog"))
        return

    try:
        fetcher = import_module(f"scripts.token_fetchers.{provider}")
        refreshed = fetcher.fetch_token_prices(known)
    except Exception as e:  # noqa: BLE001
        summary.skipped.append((provider, f"{type(e).__name__}: {e}"))
        return

    for model_id, new_entry in refreshed.items():
        if model_id not in positions:
            continue  # fetcher returned something we didn't ask for; skip
        mi, pi = positions[model_id]
        old_entry = catalog["models"][mi]["providers"][pi]
        for field in ("input_per_1m_usd", "output_per_1m_usd",
                      "cache_read_per_1m_usd", "cache_write_per_1m_usd"):
            old_val = old_entry.get(field)
            new_val = new_entry.get(field)
            if old_val is not None and new_val is not None:
                summary.add_diff(provider, model_id, field, old_val, new_val)
        catalog["models"][mi]["providers"][pi] = new_entry
    summary.refreshed.append(provider)


def _bump_as_of(catalog: dict) -> None:
    catalog["as_of"] = dt.date.today().isoformat()


def _write_snapshot(catalog: dict) -> None:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    snapshot_path = SNAPSHOT_DIR / f"{catalog['as_of']}.json"
    snapshot_path.write_text(
        json.dumps(catalog, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote snapshot {snapshot_path.relative_to(REPO_ROOT)}")


def _write_current(catalog: dict) -> None:
    TOKENS_FILE.write_text(
        json.dumps(catalog, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    print(f"Updated {TOKENS_FILE.relative_to(REPO_ROOT)} (as_of={catalog['as_of']})")


if __name__ == "__main__":
    sys.exit(main())
