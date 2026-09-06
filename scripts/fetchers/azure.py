"""Azure Retail Prices API fetcher.

Unauthenticated public REST API:
    https://prices.azure.com/api/retail/prices

No API key required. OData-style $filter. Pages of up to 1000 items each.
We query per-SKU rather than fetching everything in the region, which keeps the
filter precise and the response small.

Linux on-demand pricing only. Spot, low-priority, reserved, and Windows variants
are filtered out client-side because Azure's `meterName` distinguishes them
inconsistently (sometimes via meterName, sometimes via productName).
"""
from __future__ import annotations

import time

import httpx

from scripts.fetchers.base import FetchError, InstanceSku, MissingPriceError

cloud_name = "azure"
region = "eastus"
_API_BASE = "https://prices.azure.com/api/retail/prices"
_API_VERSION = "2023-01-01-preview"

# Azure Retail Prices API is rate-limited. Per-SKU sequential calls work fine
# at low SKU counts but hit 429s once the catalog grows past ~15 SKUs in a
# tight loop. A 250ms gap between calls keeps us under the threshold while
# still completing a 30-SKU refresh in ~8 seconds.
_INTER_CALL_DELAY_SECONDS = 0.25
_RETRY_MAX_ATTEMPTS = 3
_RETRY_INITIAL_BACKOFF_SECONDS = 2.0


def fetch_instance_prices(skus: list[InstanceSku]) -> list[InstanceSku]:
    refreshed: list[InstanceSku] = []
    with httpx.Client(timeout=20.0) as client:
        for entry in skus:
            sku = entry["sku"]
            arm_name = sku if sku.startswith("Standard_") else f"Standard_{sku}"
            entry_out = dict(entry)
            try:
                entry_out["hourly_usd"] = _lookup_one(client, arm_name, spot=False)
                spot = _lookup_one(client, arm_name, spot=True, allow_missing=True)
                if spot is not None:
                    entry_out["spot_hourly_usd"] = spot
            except FetchError:
                # If Azure rate-limits us mid-refresh, preserve the catalog value
                # for this SKU rather than fail the whole cloud. Better to ship
                # 80% refreshed than 0% refreshed. Operators see the skip via the
                # orchestrator's per-cloud summary.
                pass
            refreshed.append(entry_out)  # type: ignore[arg-type]
            time.sleep(_INTER_CALL_DELAY_SECONDS)
    return refreshed


def fetch_storage_prices(skus):
    # Azure storage has many tiers per type (P10, P15, P20...). The current
    # catalog uses single representative entries ("Standard SSD") rather than
    # listing every tier. Refreshing storage requires matching specific tier
    # SKUs not present in the input. Deferred to v0.7.1+; for now return input
    # unchanged so the orchestrator can still produce a snapshot.
    return list(skus)


def _lookup_one(
    client: httpx.Client,
    arm_sku_name: str,
    *,
    spot: bool = False,
    allow_missing: bool = False,
) -> float | None:
    """Find the per-hour Linux on-demand (or Spot) price for an Azure VM SKU.

    `spot=False` returns the regular Consumption rate (raises MissingPriceError
    if absent). `spot=True` returns the Spot rate when one is published; pair
    with `allow_missing=True` to return None instead of raising (not every Azure
    SKU has a Spot variant — e.g., B-series burstable).
    """
    filter_q = (
        f"serviceName eq 'Virtual Machines' "
        f"and armRegionName eq '{region}' "
        f"and priceType eq 'Consumption' "
        f"and armSkuName eq '{arm_sku_name}'"
    )
    # Retry loop with exponential backoff on 429s. Other HTTP errors fail fast.
    resp = None
    for attempt in range(_RETRY_MAX_ATTEMPTS):
        try:
            resp = client.get(
                _API_BASE,
                params={"$filter": filter_q, "api-version": _API_VERSION},
            )
            resp.raise_for_status()
            break
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429 and attempt < _RETRY_MAX_ATTEMPTS - 1:
                time.sleep(_RETRY_INITIAL_BACKOFF_SECONDS * (2 ** attempt))
                continue
            raise FetchError(f"Azure Retail Prices API error for {arm_sku_name}: {e}") from e
        except httpx.HTTPError as e:
            raise FetchError(f"Azure Retail Prices API error for {arm_sku_name}: {e}") from e
    assert resp is not None

    items = resp.json().get("Items", [])

    def is_windows(i): return "Windows" in (i.get("productName") or "")
    def meter(i): return i.get("meterName") or ""

    if spot:
        matches = [
            i for i in items
            if "Spot" in meter(i)
            and "Low Priority" not in meter(i)
            and not is_windows(i)
        ]
    else:
        matches = [
            i for i in items
            if "Spot" not in meter(i)
            and "Low Priority" not in meter(i)
            and not is_windows(i)
        ]

    if not matches:
        if allow_missing:
            return None
        raise MissingPriceError(
            f"Azure: no Linux {'spot' if spot else 'on-demand'} price found for "
            f"{arm_sku_name} (API returned {len(items)} rows). SKU may have been retired."
        )
    cheapest = min(matches, key=lambda i: i["unitPrice"])
    return float(cheapest["unitPrice"])
