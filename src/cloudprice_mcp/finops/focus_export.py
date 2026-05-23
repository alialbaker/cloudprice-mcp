"""export_focus — emit FOCUS 1.3-shaped rows from any pricing query.

FOCUS (FinOps Open Cost & Usage Specification) is the FinOps Foundation's
open billing schema, version 1.3 ratified December 2025. Major FinOps
tools — Vantage, Apptio, Cloudability, Microsoft Cost Management,
OpenCost, Komiser — all consume it. A cloudprice-mcp output that emits
FOCUS-conformant rows plugs straight into the enterprise FinOps stack.

What we map vs what we leave blank:
    cloudprice-mcp produces *list prices and projections*, not billed
    usage. So our FOCUS rows populate:
      - ListCost / ListUnitPrice (from our catalog)
      - PricingQuantity / PricingUnit / ConsumedQuantity / ConsumedUnit
        (synthesized from monthly hours or token volumes when provided)
      - ProviderName / PublisherName / ServiceName / ServiceCategory /
        RegionId / SkuId (from the catalog row)
      - ChargeCategory = "Usage", ChargeFrequency = "Recurring",
        PricingCategory = "Standard" (overridable when modeling commitments)
      - BillingPeriodStart/End synthesized from the query (default: current month)
    We DO NOT populate (left None/blank) — these only exist on real bills:
      - BilledCost, EffectiveCost, ContractedCost, ContractedUnitPrice
      - BillingAccountId/Name, ResourceId/Name (synthetic)
      - Tags (caller can layer on)
    This is documented explicitly in the result's `notes` field so
    downstream FinOps tools know which columns are projections vs blanks.

Supported query_kind values:
    "compute_workload"        — calls assess_migration internally
    "token_pricing"           — calls compare_token_pricing internally
    "extended_model_lookup"   — calls lookup_extended_model_pricing internally

Output format: "json" (default) or "csv".
"""
from __future__ import annotations

import csv
import datetime as dt
import io
from typing import Any

from ..inventory import WorkloadInventory
from ..pricing import PriceCatalog
from .extended_tokens import lookup_extended_model_pricing
from .migration import assess_migration
from .tokens import compare_token_pricing

FOCUS_VERSION = "1.3"

# FOCUS 1.3 ProviderName canonical values for the major hyperscalers + the
# common LLM publishers. Unknown vendors pass through unchanged.
_PROVIDER_NAME = {
    "aws": "AWS",
    "azure": "Microsoft Azure",
    "gcp": "Google Cloud",
    "oci": "Oracle Cloud",
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "google": "Google",
    "deepseek": "DeepSeek",
    "mistral": "Mistral AI",
    "bedrock": "AWS",
    "vertex": "Google Cloud",
    "azure_openai": "Microsoft Azure",
    "openrouter": "OpenRouter",
    "together_ai": "Together AI",
    "fireworks_ai": "Fireworks AI",
    "groq": "Groq",
    "perplexity": "Perplexity",
    "replicate": "Replicate",
}

_HOURS_PER_MONTH = 730  # FinOps convention; aligns with FOCUS examples


def export_focus(
    query_kind: str,
    catalog: PriceCatalog | None = None,
    inventory: WorkloadInventory | None = None,
    targets: list[str] | None = None,
    # token_pricing kwargs
    model_family: str | None = None,
    model_id: str | None = None,
    providers: list[str] | None = None,
    monthly_input_tokens: int | None = None,
    monthly_output_tokens: int | None = None,
    # extended_model_lookup kwargs
    query: str | None = None,
    provider: str | None = None,
    mode: str | None = None,
    source: str | None = None,
    # output controls
    format: str = "json",
    billing_period_start: str | None = None,
) -> dict[str, Any]:
    """Run the underlying pricing query and emit FOCUS 1.3-shaped rows.

    Args:
        query_kind: one of "compute_workload", "token_pricing",
            "extended_model_lookup".
        catalog/inventory/targets: required for compute_workload.
        model_family/model_id/providers/monthly_input_tokens/monthly_output_tokens:
            forwarded to compare_token_pricing.
        query/provider/mode/source: forwarded to lookup_extended_model_pricing.
        format: "json" (default) or "csv".
        billing_period_start: ISO date "YYYY-MM-01" for the synthetic billing
            period. Defaults to current month start.
    """
    fmt = format.lower()
    if fmt not in {"json", "csv"}:
        raise ValueError(f"Unknown format: {format}. Use 'json' or 'csv'.")

    period_start = _resolve_period_start(billing_period_start)
    period_end = _next_month_start(period_start)

    if query_kind == "compute_workload":
        rows = _from_compute_workload(catalog, inventory, targets, period_start, period_end)
    elif query_kind == "token_pricing":
        rows = _from_token_pricing(
            model_family, model_id, providers,
            monthly_input_tokens, monthly_output_tokens, period_start, period_end,
        )
    elif query_kind == "extended_model_lookup":
        rows = _from_extended_lookup(
            query, provider, mode, source,
            monthly_input_tokens, monthly_output_tokens, period_start, period_end,
        )
    else:
        raise ValueError(
            f"Unknown query_kind: {query_kind!r}. "
            f"Supported: compute_workload, token_pricing, extended_model_lookup."
        )

    result: dict[str, Any] = {
        "kind": "focus_export",
        "focus_version": FOCUS_VERSION,
        "billing_period_start": period_start.isoformat(),
        "billing_period_end": period_end.isoformat(),
        "row_count": len(rows),
        "rows": rows,
        "format": fmt,
        "notes": [
            f"FOCUS {FOCUS_VERSION} list-price projection — NOT a real bill.",
            "Populated columns: ListCost, ListUnitPrice, PricingQuantity, PricingUnit, "
            "ConsumedQuantity, ConsumedUnit, ProviderName, PublisherName, ServiceName, "
            "ServiceCategory, RegionId, SkuId, ChargeCategory, ChargeFrequency, PricingCategory.",
            "BLANK columns (only populated on real bills): BilledCost, EffectiveCost, "
            "ContractedCost, ContractedUnitPrice, BillingAccountId/Name, ResourceId/Name, Tags.",
            "Compatible with: Vantage Custom Providers, Microsoft Cost Management "
            "FOCUS imports, OpenCost FOCUS exports.",
        ],
    }
    if fmt == "csv":
        result["csv"] = _rows_to_csv(rows)
    return result


# --- query_kind dispatchers ---


def _from_compute_workload(
    catalog: PriceCatalog | None,
    inventory: WorkloadInventory | None,
    targets: list[str] | None,
    period_start: dt.date,
    period_end: dt.date,
) -> list[dict[str, Any]]:
    if catalog is None or inventory is None:
        raise ValueError("compute_workload requires both catalog and inventory.")
    assessment = assess_migration(catalog, inventory, targets=targets)
    rows: list[dict[str, Any]] = []
    source_cloud = assessment.get("source_cloud")
    targets_map = assessment.get("targets") or {}
    # One synthetic row per target cloud at the monthly granularity. Caller can
    # break out per-SKU rows downstream from the structured target data.
    for cloud, t in targets_map.items():
        monthly = t.get("monthly_usd")
        if monthly is None:
            continue
        rows.append(_focus_row(
            provider_key=cloud,
            service_name=f"{_PROVIDER_NAME.get(cloud, cloud).upper()} compute workload",
            service_category="Compute",
            sku_id=f"workload:{inventory.source_cloud}->{cloud}",
            region_id=t.get("region", "default"),
            pricing_quantity=_HOURS_PER_MONTH,
            pricing_unit="Hour",
            list_unit_price=round(monthly / _HOURS_PER_MONTH, 8),
            list_cost=monthly,
            period_start=period_start,
            period_end=period_end,
            charge_description=(
                f"Migration assessment: {inventory.source_cloud} -> {cloud} monthly run-rate"
            ),
        ))
    if source_cloud and assessment.get("source_monthly_usd") is not None:
        rows.insert(0, _focus_row(
            provider_key=source_cloud,
            service_name=f"{_PROVIDER_NAME.get(source_cloud, source_cloud).upper()} compute workload",
            service_category="Compute",
            sku_id=f"workload:{source_cloud}:current",
            region_id="default",
            pricing_quantity=_HOURS_PER_MONTH,
            pricing_unit="Hour",
            list_unit_price=round(assessment["source_monthly_usd"] / _HOURS_PER_MONTH, 8),
            list_cost=assessment["source_monthly_usd"],
            period_start=period_start,
            period_end=period_end,
            charge_description=f"Current source cloud baseline ({source_cloud})",
        ))
    return rows


def _from_token_pricing(
    model_family, model_id, providers,
    monthly_input_tokens, monthly_output_tokens,
    period_start, period_end,
) -> list[dict[str, Any]]:
    result = compare_token_pricing(
        model_family=model_family, model_id=model_id, providers=providers,
        monthly_input_tokens=monthly_input_tokens,
        monthly_output_tokens=monthly_output_tokens,
    )
    return [
        _token_row(row, period_start, period_end, monthly_input_tokens, monthly_output_tokens)
        for row in result.get("rows", [])
    ]


def _from_extended_lookup(
    query, provider, mode, source,
    monthly_input_tokens, monthly_output_tokens,
    period_start, period_end,
) -> list[dict[str, Any]]:
    result = lookup_extended_model_pricing(
        query=query, provider=provider, mode=mode, source=source,
        monthly_input_tokens=monthly_input_tokens,
        monthly_output_tokens=monthly_output_tokens,
        limit=1000,  # FOCUS export should be comprehensive; downstream filters as needed
    )
    return [
        _token_row(row, period_start, period_end, monthly_input_tokens, monthly_output_tokens)
        for row in result.get("rows", [])
    ]


# --- row builders ---


def _focus_row(
    *, provider_key: str, service_name: str, service_category: str,
    sku_id: str, region_id: str,
    pricing_quantity: float, pricing_unit: str,
    list_unit_price: float, list_cost: float,
    period_start: dt.date, period_end: dt.date,
    charge_description: str,
    charge_category: str = "Usage",
    charge_frequency: str = "Recurring",
    pricing_category: str = "Standard",
) -> dict[str, Any]:
    provider_name = _PROVIDER_NAME.get(provider_key, provider_key)
    return {
        # Provider / service
        "ProviderName": provider_name,
        "PublisherName": provider_name,
        "ServiceName": service_name,
        "ServiceCategory": service_category,
        "RegionId": region_id,
        # SKU / charge
        "SkuId": sku_id,
        "ChargeCategory": charge_category,
        "ChargeClass": None,
        "ChargeFrequency": charge_frequency,
        "ChargeDescription": charge_description,
        # Pricing
        "PricingCategory": pricing_category,
        "PricingQuantity": pricing_quantity,
        "PricingUnit": pricing_unit,
        "ConsumedQuantity": pricing_quantity,
        "ConsumedUnit": pricing_unit,
        "ListUnitPrice": list_unit_price,
        "ListCost": list_cost,
        "ContractedUnitPrice": None,
        "ContractedCost": None,
        "EffectiveCost": None,
        "BilledCost": None,
        # Billing period
        "BillingCurrency": "USD",
        "BillingPeriodStart": period_start.isoformat(),
        "BillingPeriodEnd": period_end.isoformat(),
        "ChargePeriodStart": period_start.isoformat(),
        "ChargePeriodEnd": period_end.isoformat(),
        # Left blank — only known on real bills
        "BillingAccountId": None,
        "BillingAccountName": None,
        "ResourceId": None,
        "ResourceName": None,
        "Tags": None,
    }


def _token_row(
    src: dict, period_start: dt.date, period_end: dt.date,
    monthly_input_tokens: int | None, monthly_output_tokens: int | None,
) -> dict[str, Any]:
    """Convert one compare_token_pricing / lookup row to a FOCUS row.

    We emit ONE row per (model, provider) that bundles input + output as the
    blended monthly cost. The PricingUnit reflects this bundling so users see
    a single defensible monthly line per model+provider rather than 2 rows.
    """
    provider_key = src.get("provider", "unknown")
    model_id_value = src.get("model_id") or src.get("litellm_id") or "unknown-model"
    in_rate = src.get("input_per_1m_usd", 0.0)
    out_rate = src.get("output_per_1m_usd", 0.0)

    if monthly_input_tokens is not None and monthly_output_tokens is not None:
        list_cost = src.get("monthly_total_usd")
        if list_cost is None:
            list_cost = (monthly_input_tokens * in_rate + monthly_output_tokens * out_rate) / 1_000_000
        pricing_qty = (monthly_input_tokens + monthly_output_tokens) / 1_000_000
        unit = "1M tokens (blended in+out)"
        unit_price = round(list_cost / pricing_qty, 6) if pricing_qty else 0.0
        description = (
            f"{model_id_value} via {provider_key}: "
            f"{monthly_input_tokens:,} input + {monthly_output_tokens:,} output tokens/mo"
        )
    else:
        # No monthly volumes — emit per-1M-output-token line as the canonical
        # rate (output dominates). Caller can re-derive input cost from rate.
        list_cost = out_rate
        pricing_qty = 1.0
        unit = "1M output tokens"
        unit_price = out_rate
        description = (
            f"{model_id_value} via {provider_key}: list rate per 1M output tokens"
        )

    return _focus_row(
        provider_key=provider_key,
        service_name=f"{_PROVIDER_NAME.get(provider_key, provider_key)} LLM inference",
        service_category="AI and Machine Learning",
        sku_id=f"{provider_key}:{model_id_value}",
        region_id="default",
        pricing_quantity=round(pricing_qty, 6),
        pricing_unit=unit,
        list_unit_price=unit_price,
        list_cost=round(list_cost, 4),
        period_start=period_start,
        period_end=period_end,
        charge_description=description,
    )


# --- helpers ---


def _resolve_period_start(arg: str | None) -> dt.date:
    if arg:
        try:
            return dt.date.fromisoformat(arg)
        except ValueError as e:
            raise ValueError(
                f"billing_period_start must be ISO date YYYY-MM-DD, got {arg!r}"
            ) from e
    today = dt.date.today()
    return today.replace(day=1)


def _next_month_start(d: dt.date) -> dt.date:
    if d.month == 12:
        return d.replace(year=d.year + 1, month=1, day=1)
    return d.replace(month=d.month + 1, day=1)


def _rows_to_csv(rows: list[dict]) -> str:
    if not rows:
        return ""
    buf = io.StringIO()
    # Stable column order = FOCUS 1.3 commonly-used columns first.
    fieldnames = list(rows[0].keys())
    writer = csv.DictWriter(buf, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: ("" if v is None else v) for k, v in row.items()})
    return buf.getvalue()
