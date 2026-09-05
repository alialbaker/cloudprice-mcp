"""Protocol-free tool dispatch for cloudprice-mcp.

This module holds the tool implementations and knows nothing about MCP,
transports or servers: `call()` takes a tool name and a plain dict of
arguments and returns plain JSON-serialisable data.

That separation exists so the engine can be driven by more than one front
end. `server.py` wraps this for the stdio MCP server; an AWS Lambda behind
Amazon Bedrock AgentCore Gateway calls `call()` directly and therefore does
not need the MCP SDK installed at all.
"""

from __future__ import annotations

from typing import Any

from .compare import (
    ComputeRequest,
    EgressRequest,
    ObjectStorageRequest,
    PostgresRequest,
    StorageRequest,
    bulk_compare_compute,
    bulk_compare_storage,
    compare_all_clouds,
    compare_egress,
    compare_object_storage,
    compare_postgres,
    compare_workload,
)
from .finops.anomaly import detect_price_anomalies
from .finops.carbon import compare_carbon_footprint
from .finops.commitment import optimize_commitment
from .finops.egress_arbitrage import find_egress_arbitrage
from .finops.extended_tokens import lookup_extended_model_pricing
from .finops.focus_export import export_focus
from .finops.gpu import compare_gpu_workload
from .finops.migration import assess_migration
from .finops.report import generate_decision_report
from .finops.sentinel import watch_workload
from .finops.spot import compare_spot
from .finops.tco import GrowthAssumptions, compare_total_cost_of_ownership
from .finops.tokens import compare_token_pricing
from .inventory import InventoryError, parse_dict
from .pricing import HOURS_PER_MONTH, Cloud, load_catalog


def list_skus(cloud: Cloud) -> list[str]:
    """Every SKU this catalog knows about for one cloud, sorted."""
    catalog = load_catalog()
    return sorted(i.sku for i in catalog.by_cloud(cloud))


def _ok(payload: dict | list) -> dict | list:
    return payload


def _err(message: str) -> dict:
    return {"error": message}


def _lookup(cloud: Cloud, sku_field: str, sku: str) -> dict:
    catalog = load_catalog()
    instance = catalog.find(cloud, sku)
    if instance is None:
        return _err(
            f"Unknown {cloud.upper()} {sku_field} '{sku}'. "
            f"Available: {', '.join(list_skus(cloud))}"
        )
    return _ok({"as_of": catalog.as_of, **instance.to_dict()})


def _build_compute_requests(items: list[dict[str, Any]]) -> list[ComputeRequest]:
    return [
        ComputeRequest(
            name=item["name"],
            vcpus=int(item["vcpus"]),
            memory_gb=float(item["memory_gb"]),
            quantity=int(item.get("quantity", 1)),
            hours_per_month=int(item.get("hours_per_month", HOURS_PER_MONTH)),
            tier=item.get("tier"),
            group=item.get("group"),
            os_disk_gb=float(item["os_disk_gb"]) if item.get("os_disk_gb") else None,
            os_disk_type=item.get("os_disk_type", "ssd"),
            os_disk_snapshot_count=int(item.get("os_disk_snapshot_count", 0)),
            os_disk_snapshot_incremental_factor=float(
                item.get("os_disk_snapshot_incremental_factor", 1.0)
            ),
        )
        for item in items
    ]


def _build_storage_requests(items: list[dict[str, Any]]) -> list[StorageRequest]:
    return [
        StorageRequest(
            name=item["name"],
            capacity_gb=float(item["capacity_gb"]),
            disk_type=item.get("disk_type", "ssd"),
            quantity=int(item.get("quantity", 1)),
            tier=item.get("tier"),
            group=item.get("group"),
            iops=int(item["iops"]) if item.get("iops") is not None else None,
            throughput_mbs=float(item["throughput_mbs"]) if item.get("throughput_mbs") is not None else None,
            snapshot_count=int(item.get("snapshot_count", 0)),
            snapshot_incremental_factor=float(item.get("snapshot_incremental_factor", 1.0)),
        )
        for item in items
    ]


# --- Per-tool handlers — keeps call_tool simple via a dispatch table ---


def _handle_get_aws_price(catalog, args):
    return _lookup("aws", "instance_type", args["instance_type"])


def _handle_get_azure_price(catalog, args):
    return _lookup("azure", "vm_size", args["vm_size"])


def _handle_get_gcp_price(catalog, args):
    return _lookup("gcp", "machine_type", args["machine_type"])


def _handle_compare_clouds(catalog, args):
    vcpus = int(args["vcpus"])
    memory_gb = float(args["memory_gb"])
    matches = compare_all_clouds(catalog, vcpus, memory_gb)
    if not matches:
        return _err("No matches found in catalog.")
    cheapest = matches[0]
    priciest = matches[-1]
    spread = priciest.instance.monthly_usd - cheapest.instance.monthly_usd
    pct = (spread / priciest.instance.monthly_usd * 100) if priciest.instance.monthly_usd else 0
    return _ok(
        {
            "as_of": catalog.as_of,
            "request": {"vcpus": vcpus, "memory_gb": memory_gb},
            "matches": [m.to_dict() for m in matches],
            "summary": {
                "cheapest_cloud": cheapest.cloud,
                "monthly_savings_usd": round(spread, 2),
                "monthly_savings_pct": round(pct, 1),
            },
        }
    )


def _handle_compare_compute_inventory(catalog, args):
    workloads = _build_compute_requests(args["workloads"])
    result = bulk_compare_compute(catalog, workloads)
    return _ok({"as_of": catalog.as_of, **result})


def _handle_compare_storage_inventory(catalog, args):
    volumes = _build_storage_requests(args["volumes"])
    result = bulk_compare_storage(catalog, volumes)
    return _ok({"as_of": catalog.as_of, **result})


def _handle_compare_egress(catalog, args):
    requests = [
        EgressRequest(
            name=item["name"],
            gb_per_month=float(item["gb_per_month"]),
            direction=item.get("direction", "out_to_internet"),
            tier_label=item.get("tier_label"),
        )
        for item in args["transfers"]
    ]
    result = compare_egress(catalog, requests)
    return _ok({"as_of": catalog.as_of, **result})


def _handle_compare_object_storage(catalog, args):
    requests = [
        ObjectStorageRequest(
            name=item["name"],
            capacity_gb=float(item["capacity_gb"]),
            tier=item.get("tier", "hot"),
            quantity=int(item.get("quantity", 1)),
            tier_label=item.get("tier_label"),
        )
        for item in args["volumes"]
    ]
    result = compare_object_storage(catalog, requests)
    return _ok({"as_of": catalog.as_of, **result})


def _handle_compare_postgres_database(catalog, args):
    requests = [
        PostgresRequest(
            name=item["name"],
            vcpus=int(item["vcpus"]),
            memory_gb=float(item["memory_gb"]),
            storage_gb=float(item.get("storage_gb", 0)),
            quantity=int(item.get("quantity", 1)),
            hours_per_month=int(item.get("hours_per_month", HOURS_PER_MONTH)),
            tier=item.get("tier"),
        )
        for item in args["databases"]
    ]
    result = compare_postgres(catalog, requests)
    return _ok({"as_of": catalog.as_of, **result})


def _handle_compare_workload(catalog, args):
    compute = _build_compute_requests(args.get("compute", []))
    storage = _build_storage_requests(args.get("storage", []))
    if not compute and not storage:
        return _err("compare_workload needs at least one of compute or storage to be non-empty.")
    commitment = args.get("commitment", "none")
    multi_az = bool(args.get("multi_az", False))
    result = compare_workload(catalog, compute, storage, commitment=commitment, multi_az=multi_az)
    return _ok({"as_of": catalog.as_of, **result})


# --- v0.6 FinOps decision tool handlers ---


def _handle_assess_migration(catalog, args):
    try:
        inv = parse_dict(args)
    except InventoryError as e:
        return _err(f"assess_migration: {e}")
    try:
        result = assess_migration(catalog, inv, targets=args.get("targets"))
    except ValueError as e:
        return _err(f"assess_migration: {e}")
    return _ok({"as_of": catalog.as_of, **result})


def _handle_optimize_commitment(catalog, args):
    try:
        inv = parse_dict(args)
    except InventoryError as e:
        return _err(f"optimize_commitment: {e}")
    try:
        result = optimize_commitment(
            catalog,
            inv,
            cloud=args.get("cloud"),
            scenarios=args.get("scenarios"),
        )
    except ValueError as e:
        return _err(f"optimize_commitment: {e}")
    return _ok({"as_of": catalog.as_of, **result})


def _handle_compare_total_cost_of_ownership(catalog, args):
    try:
        inv = parse_dict(args)
    except InventoryError as e:
        return _err(f"compare_total_cost_of_ownership: {e}")
    growth_args = args.get("growth") or {}
    growth = GrowthAssumptions(
        compute_pct_yoy=float(growth_args.get("compute_pct_yoy", 0.0)),
        storage_pct_yoy=float(growth_args.get("storage_pct_yoy", 0.0)),
        egress_pct_yoy=float(growth_args.get("egress_pct_yoy", 0.0)),
    )
    try:
        result = compare_total_cost_of_ownership(
            catalog,
            inv,
            horizon_years=int(args.get("horizon_years", 3)),
            growth=growth,
            targets=args.get("targets"),
        )
    except ValueError as e:
        return _err(f"compare_total_cost_of_ownership: {e}")
    return _ok({"as_of": catalog.as_of, **result})


def _handle_find_egress_arbitrage(catalog, args):
    try:
        inv = parse_dict(args)
    except InventoryError as e:
        return _err(f"find_egress_arbitrage: {e}")
    try:
        result = find_egress_arbitrage(catalog, inv, targets=args.get("targets"))
    except ValueError as e:
        return _err(f"find_egress_arbitrage: {e}")
    return _ok({"as_of": catalog.as_of, **result})


# Tool name → handler dispatch table. Adding a new tool = add one entry.
def _handle_get_price_history(catalog, args):
    from .history import history_window
    cloud = args.get("cloud")
    sku = args.get("sku")
    since = args.get("since")
    if not cloud or not sku:
        return _err("get_price_history: cloud and sku are required")
    window = history_window(cloud, sku, since=since)
    if window is None:
        msg = f"No history for {cloud}/{sku}"
        if since:
            msg += f" since {since}"
        return _err(msg)
    return _ok(window.to_dict())


def _handle_compare_spot(catalog, args):
    vcpus = int(args["vcpus"])
    memory_gb = float(args["memory_gb"])
    targets = args.get("targets")
    result = compare_spot(catalog, vcpus=vcpus, memory_gb=memory_gb, targets=targets)
    return _ok({"as_of": catalog.as_of, **result})


def _handle_generate_decision_report(catalog, args):
    try:
        inv = parse_dict(args)
    except InventoryError as e:
        return _err(f"generate_decision_report: {e}")
    try:
        result = generate_decision_report(
            catalog,
            inv,
            targets=args.get("targets"),
            include_carbon=bool(args.get("include_carbon", True)),
            requester=args.get("requester"),
        )
    except ValueError as e:
        return _err(f"generate_decision_report: {e}")
    return _ok({"as_of": catalog.as_of, **result})


def _handle_detect_price_anomalies(catalog, args):
    try:
        result = detect_price_anomalies(
            since=args.get("since"),
            cloud=args.get("cloud"),
            sensitivity=args.get("sensitivity", "moderate"),
            method=args.get("method", "auto"),
            limit=int(args.get("limit", 25)),
        )
    except ValueError as e:
        return _err(f"detect_price_anomalies: {e}")
    return _ok(result)


def _handle_compare_token_pricing(catalog, args):
    try:
        result = compare_token_pricing(
            model_family=args.get("model_family"),
            model_id=args.get("model_id"),
            providers=args.get("providers"),
            monthly_input_tokens=args.get("monthly_input_tokens"),
            monthly_output_tokens=args.get("monthly_output_tokens"),
        )
    except ValueError as e:
        return _err(f"compare_token_pricing: {e}")
    return _ok(result)


def _handle_export_focus(catalog, args):
    query_kind = args.get("query_kind")
    inv = None
    if query_kind == "compute_workload":
        try:
            inv = parse_dict(args)
        except InventoryError as e:
            return _err(f"export_focus: {e}")
    try:
        result = export_focus(
            query_kind=query_kind,
            catalog=catalog,
            inventory=inv,
            targets=args.get("targets"),
            model_family=args.get("model_family"),
            model_id=args.get("model_id"),
            providers=args.get("providers"),
            monthly_input_tokens=args.get("monthly_input_tokens"),
            monthly_output_tokens=args.get("monthly_output_tokens"),
            query=args.get("query"),
            provider=args.get("provider"),
            mode=args.get("mode"),
            source=args.get("source"),
            format=args.get("format", "json"),
            billing_period_start=args.get("billing_period_start"),
        )
    except ValueError as e:
        return _err(f"export_focus: {e}")
    return _ok(result)


def _handle_lookup_extended_model_pricing(catalog, args):
    try:
        result = lookup_extended_model_pricing(
            query=args.get("query"),
            provider=args.get("provider"),
            mode=args.get("mode"),
            max_context_tokens=args.get("max_context_tokens"),
            monthly_input_tokens=args.get("monthly_input_tokens"),
            monthly_output_tokens=args.get("monthly_output_tokens"),
            source=args.get("source"),
            limit=int(args.get("limit", 25)),
        )
    except ValueError as e:
        return _err(f"lookup_extended_model_pricing: {e}")
    return _ok(result)


def _handle_compare_gpu_workload(catalog, args):
    gpu_type = args.get("gpu_type", "")
    gpu_count = int(args.get("gpu_count", 1))
    targets = args.get("targets")
    try:
        result = compare_gpu_workload(
            catalog, gpu_type=gpu_type, gpu_count=gpu_count, targets=targets,
        )
    except ValueError as e:
        return _err(f"compare_gpu_workload: {e}")
    return _ok({"as_of": catalog.as_of, **result})


def _handle_compare_carbon_footprint(catalog, args):
    vcpus = int(args["vcpus"])
    memory_gb = float(args["memory_gb"])
    quantity = int(args.get("quantity", 1))
    targets = args.get("targets")
    try:
        result = compare_carbon_footprint(
            catalog, vcpus=vcpus, memory_gb=memory_gb, quantity=quantity, targets=targets,
        )
    except ValueError as e:
        return _err(f"compare_carbon_footprint: {e}")
    return _ok({"as_of": catalog.as_of, **result})


def _handle_watch_workload(catalog, args):
    try:
        inv = parse_dict(args)
    except InventoryError as e:
        return _err(f"watch_workload: {e}")
    baseline = args.get("baseline")
    threshold = float(args.get("alert_threshold_pct", 5.0))
    try:
        result = watch_workload(catalog, inv, baseline=baseline, alert_threshold_pct=threshold)
    except ValueError as e:
        return _err(f"watch_workload: {e}")
    return _ok({"as_of": catalog.as_of, **result})


def _handle_list_tracked_skus(catalog, args):
    from .history import list_snapshot_dates, load_history
    cloud = args.get("cloud")
    since = args.get("since")
    snapshots = list_snapshot_dates()
    points = load_history(cloud=cloud, since=since)

    # Aggregate by (cloud, sku)
    by_key: dict[tuple[str, str], list] = {}
    for p in points:
        by_key.setdefault((p.cloud, p.sku), []).append(p)

    skus = []
    for (c, sku), pts in sorted(by_key.items()):
        pts_sorted = sorted(pts, key=lambda x: x.as_of)
        latest = pts_sorted[-1]
        oldest = pts_sorted[0]
        change_pct = ((latest.hourly_usd - oldest.hourly_usd) / oldest.hourly_usd * 100) if oldest.hourly_usd > 0 and len(pts_sorted) > 1 else 0.0
        skus.append({
            "cloud": c,
            "sku": sku,
            "region": latest.region,
            "data_points": len(pts_sorted),
            "latest_hourly_usd": latest.hourly_usd,
            "latest_as_of": latest.as_of,
            "total_change_pct": round(change_pct, 2),
        })

    return _ok({
        "snapshots": snapshots,
        "filters": {"cloud": cloud, "since": since},
        "skus": skus,
    })


_TOOL_HANDLERS = {
    "get_aws_price": _handle_get_aws_price,
    "get_azure_price": _handle_get_azure_price,
    "get_gcp_price": _handle_get_gcp_price,
    "compare_clouds": _handle_compare_clouds,
    "compare_compute_inventory": _handle_compare_compute_inventory,
    "compare_storage_inventory": _handle_compare_storage_inventory,
    "compare_egress": _handle_compare_egress,
    "compare_object_storage": _handle_compare_object_storage,
    "compare_postgres_database": _handle_compare_postgres_database,
    "compare_workload": _handle_compare_workload,
    # v0.6 FinOps decision tools
    "assess_migration": _handle_assess_migration,
    "optimize_commitment": _handle_optimize_commitment,
    "compare_total_cost_of_ownership": _handle_compare_total_cost_of_ownership,
    "find_egress_arbitrage": _handle_find_egress_arbitrage,
    # v0.7.1 price-history tools
    "get_price_history": _handle_get_price_history,
    "list_tracked_skus": _handle_list_tracked_skus,
    # v0.8.1 spot pricing tool
    "compare_spot": _handle_compare_spot,
    # v0.9.0 cost drift sentinel
    "watch_workload": _handle_watch_workload,
    # v0.10.0 carbon-aware FinOps
    "compare_carbon_footprint": _handle_compare_carbon_footprint,
    # v0.11.0 GPU pricing
    "compare_gpu_workload": _handle_compare_gpu_workload,
    # v0.12.0 LLM token pricing
    "compare_token_pricing": _handle_compare_token_pricing,
    "lookup_extended_model_pricing": _handle_lookup_extended_model_pricing,
    "export_focus": _handle_export_focus,
    # v0.13.0 anomaly detection
    "detect_price_anomalies": _handle_detect_price_anomalies,
    # v0.14.0 FinOps decision report
    "generate_decision_report": _handle_generate_decision_report,
}


def call(name: str, arguments: dict[str, Any] | None = None) -> Any:
    """Run one tool by name and return plain data.

    Unknown tools return an ``{"error": ...}`` payload rather than raising,
    so a caller can surface the problem to a model instead of a stack trace.
    """
    handler = _TOOL_HANDLERS.get(name)
    if handler is None:
        return _err(f"Unknown tool: {name}")
    return handler(load_catalog(), arguments or {})


def tool_names() -> list[str]:
    """Every tool name `call()` accepts."""
    return sorted(_TOOL_HANDLERS)
