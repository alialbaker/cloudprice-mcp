"""Per-provider token-price fetchers (v0.15.0+).

Mirror of the v0.7 per-cloud fetchers pattern, but for LLM token pricing.
Each provider module exposes a `fetch_token_prices(known_models)` callable
that returns refreshed entries for the models we already track in
`data/token_prices.json`.

We never auto-discover new models — the catalog is the source of truth for
which (model_id, provider) pairs we care about. The fetcher only updates
prices for known entries.

Provider coverage as of v0.15.0:
  - bedrock: AWS Pricing API via boto3 (most-used hyperscaler-hosted models)
  - others: future patches. OpenAI / Anthropic direct don't publish a
    machine-readable pricing API; would require scraping. Vertex / Azure
    OpenAI route through their existing cloud pricing APIs.
"""
from __future__ import annotations
