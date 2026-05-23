"""Shared types + errors for token-price fetchers."""
from __future__ import annotations

from typing import Protocol, TypedDict


class TokenProviderEntry(TypedDict, total=False):
    """One (model_id, provider) price row in the catalog."""
    provider: str
    input_per_1m_usd: float
    output_per_1m_usd: float
    cache_read_per_1m_usd: float
    cache_write_per_1m_usd: float


class TokenFetchError(RuntimeError):
    """Network / upstream-API failure. Orchestrator should NOT overwrite the
    catalog row for this entry."""


class TokenProviderFetcher(Protocol):
    """One provider module = one fetcher.

    Given the catalog's existing list of (model_id -> TokenProviderEntry)
    pairs for THIS provider, returns refreshed prices. We preserve the
    caller's structure; only the price fields change.

    Returns dict mapped by model_id so the orchestrator can splice updates
    into the larger catalog without ambiguity.
    """

    provider_name: str

    def fetch_token_prices(
        self, known_models: dict[str, TokenProviderEntry]
    ) -> dict[str, TokenProviderEntry]: ...
