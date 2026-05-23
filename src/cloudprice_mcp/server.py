import asyncio
import json
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from . import __version__
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
from .finops.commitment import ALL_SCENARIOS, optimize_commitment
from .finops.egress_arbitrage import find_egress_arbitrage
from .finops.migration import assess_migration
from .finops.carbon import compare_carbon_footprint
from .finops.anomaly import detect_price_anomalies
from .finops.gpu import compare_gpu_workload
from .finops.report import generate_decision_report
from .finops.sentinel import watch_workload
from .finops.spot import compare_spot
from .finops.tco import GrowthAssumptions, compare_total_cost_of_ownership
from .finops.extended_tokens import lookup_extended_model_pricing
from .finops.focus_export import export_focus
from .finops.tokens import compare_token_pricing
from .inventory import InventoryError, parse_dict
from .pricing import HOURS_PER_MONTH, Cloud, load_catalog

server: Server = Server("cloudprice-mcp")


def _list_skus(cloud: Cloud) -> list[str]:
    catalog = load_catalog()
    return sorted(i.sku for i in catalog.by_cloud(cloud))


# --- v0.6 FinOps inventory schema (shared across all 4 FinOps tools) ---


_FINOPS_COMPUTE_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Friendly label, e.g. 'api-tier'"},
        "vcpus": {"type": "integer", "minimum": 1},
        "memory_gb": {"type": "number", "minimum": 0.5},
        "quantity": {"type": "integer", "minimum": 1, "default": 1},
        "multi_az": {"type": "boolean", "default": False},
        "os_disk_gb": {"type": ["number", "null"], "minimum": 0},
        "os_disk_type": {"type": "string", "enum": ["ssd", "hdd"], "default": "ssd"},
        "snapshot_count": {"type": "integer", "minimum": 0, "default": 0},
        "snapshot_incremental_factor": {
            "type": "number", "minimum": 0, "maximum": 1, "default": 1.0,
            "description": "1.0 = upper-bound, 0.3 = typical real-world incremental, 0.0 = exclude.",
        },
    },
    "required": ["name", "vcpus", "memory_gb"],
    "additionalProperties": False,
}

_FINOPS_STORAGE_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "capacity_gb": {"type": "number", "minimum": 1},
        "disk_type": {"type": "string", "enum": ["ssd", "hdd"], "default": "ssd"},
        "quantity": {"type": "integer", "minimum": 1, "default": 1},
        "snapshot_count": {"type": "integer", "minimum": 0, "default": 0},
        "snapshot_incremental_factor": {
            "type": "number", "minimum": 0, "maximum": 1, "default": 1.0,
        },
    },
    "required": ["name", "capacity_gb"],
    "additionalProperties": False,
}

_FINOPS_OBJECT_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "capacity_gb": {"type": "number", "minimum": 1},
        "tier": {"type": "string", "enum": ["hot", "cool", "archive"], "default": "hot"},
        "quantity": {"type": "integer", "minimum": 1, "default": 1},
    },
    "required": ["name", "capacity_gb"],
    "additionalProperties": False,
}

_FINOPS_DATABASE_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "vcpus": {"type": "integer", "minimum": 1},
        "memory_gb": {"type": "number", "minimum": 0.5},
        "storage_gb": {"type": "number", "minimum": 0, "default": 0},
        "engine": {"type": "string", "enum": ["postgres"], "default": "postgres"},
        "quantity": {"type": "integer", "minimum": 1, "default": 1},
    },
    "required": ["name", "vcpus", "memory_gb"],
    "additionalProperties": False,
}

_FINOPS_EGRESS_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "gb_per_month": {"type": "number", "minimum": 0},
        "direction": {
            "type": "string",
            "enum": ["out_to_internet", "inter_region"],
            "default": "out_to_internet",
        },
    },
    "required": ["name", "gb_per_month"],
    "additionalProperties": False,
}

_FINOPS_ONE_TIME_SCHEMA = {
    "type": "object",
    "properties": {
        "data_to_migrate_gb": {"type": "number", "minimum": 0, "default": 0},
    },
    "additionalProperties": False,
}


def _finops_inventory_properties(*, include_source_cloud: bool = True, include_commitment: bool = True) -> dict:
    """Build the shared inventory properties dict reused across FinOps tool schemas."""
    props: dict = {
        "compute": {"type": "array", "items": _FINOPS_COMPUTE_ITEM_SCHEMA, "default": []},
        "storage": {"type": "array", "items": _FINOPS_STORAGE_ITEM_SCHEMA, "default": []},
        "object_storage": {"type": "array", "items": _FINOPS_OBJECT_ITEM_SCHEMA, "default": []},
        "databases": {"type": "array", "items": _FINOPS_DATABASE_ITEM_SCHEMA, "default": []},
        "egress": {"type": "array", "items": _FINOPS_EGRESS_ITEM_SCHEMA, "default": []},
        "multi_az": {"type": "boolean", "default": False},
        "one_time": _FINOPS_ONE_TIME_SCHEMA,
    }
    if include_source_cloud:
        props["source_cloud"] = {
            "type": "string",
            "enum": ["aws", "azure", "gcp", "oci"],
            "description": "Cloud the workload currently runs on.",
        }
    if include_commitment:
        props["commitment"] = {
            "type": "string",
            "enum": list(ALL_SCENARIOS),
            "default": "none",
        }
    return props


_COMPUTE_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Friendly label for this row, e.g. 'web-tier-1'"},
        "vcpus": {"type": "integer", "minimum": 1},
        "memory_gb": {"type": "number", "minimum": 0.5},
        "quantity": {"type": "integer", "minimum": 1, "default": 1},
        "hours_per_month": {"type": "integer", "minimum": 1, "default": HOURS_PER_MONTH},
        "tier": {"type": ["string", "null"], "description": "Optional grouping label (e.g. Web/App/DB)"},
        "group": {"type": ["string", "null"], "description": "Optional sub-grouping label"},
        "os_disk_gb": {"type": ["number", "null"], "minimum": 0},
        "os_disk_type": {"type": "string", "enum": ["ssd", "hdd"], "default": "ssd"},
        "os_disk_snapshot_count": {
            "type": "integer", "minimum": 0, "default": 0,
            "description": "Number of OS-disk snapshots retained. Each priced at the cloud's snapshot per-GB rate × disk size × instance quantity.",
        },
        "os_disk_snapshot_incremental_factor": {
            "type": "number", "minimum": 0, "maximum": 1, "default": 1.0,
            "description": "Multiplier on OS-disk snapshot upper-bound cost. 1.0 = full, 0.3 = typical incremental, 0.0 = exclude. Defaults to 1.0.",
        },
    },
    "required": ["name", "vcpus", "memory_gb"],
    "additionalProperties": False,
}

_EGRESS_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Friendly label, e.g. 'api-egress' or 'cdn-bandwidth'"},
        "gb_per_month": {"type": "number", "minimum": 0, "description": "Data transfer volume in GB/month"},
        "direction": {
            "type": "string",
            "enum": ["out_to_internet", "inter_region"],
            "default": "out_to_internet",
            "description": "out_to_internet: outbound to public internet (honors free tier — AWS/Azure 100 GB, OCI 10 TB). inter_region: cross-region transfer within the same cloud (no free tier, flat rate ~$0.02/GB on hyperscalers, $0.0085 on OCI).",
        },
        "tier_label": {"type": ["string", "null"]},
    },
    "required": ["name", "gb_per_month"],
    "additionalProperties": False,
}


_OBJECT_STORAGE_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Friendly label for this bucket/container, e.g. 'app-uploads'"},
        "capacity_gb": {"type": "number", "minimum": 1},
        "tier": {
            "type": "string",
            "enum": ["hot", "cool", "archive"],
            "default": "hot",
            "description": "Access tier: 'hot' = frequent (eg S3 Standard), 'cool' = infrequent, 'archive' = deep archive (eg Glacier)",
        },
        "quantity": {"type": "integer", "minimum": 1, "default": 1},
        "tier_label": {"type": ["string", "null"], "description": "Optional grouping label"},
    },
    "required": ["name", "capacity_gb"],
    "additionalProperties": False,
}


_POSTGRES_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Friendly label for this database, e.g. 'orders-prod'"},
        "vcpus": {"type": "integer", "minimum": 1},
        "memory_gb": {"type": "number", "minimum": 0.5},
        "storage_gb": {"type": "number", "minimum": 0, "default": 0, "description": "Persistent storage size in GB"},
        "quantity": {"type": "integer", "minimum": 1, "default": 1},
        "hours_per_month": {"type": "integer", "minimum": 1, "default": HOURS_PER_MONTH},
        "tier": {"type": ["string", "null"], "description": "Optional grouping label (e.g. Prod/Stage/Dev)"},
    },
    "required": ["name", "vcpus", "memory_gb"],
    "additionalProperties": False,
}


_STORAGE_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Friendly label for this volume, e.g. 'db-data-1'"},
        "capacity_gb": {"type": "number", "minimum": 1},
        "disk_type": {"type": "string", "enum": ["ssd", "hdd"], "default": "ssd"},
        "quantity": {"type": "integer", "minimum": 1, "default": 1},
        "tier": {"type": ["string", "null"]},
        "group": {"type": ["string", "null"]},
        "iops": {"type": ["integer", "null"], "minimum": 0, "description": "Carried as metadata; not used for SKU matching in v0.2"},
        "throughput_mbs": {"type": ["number", "null"], "minimum": 0, "description": "Carried as metadata; not used for SKU matching in v0.2"},
        "snapshot_count": {"type": "integer", "minimum": 0, "default": 0, "description": "Number of snapshots retained. Priced at the cloud's snapshot per-GB rate × capacity × volume quantity."},
        "snapshot_incremental_factor": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
            "default": 1.0,
            "description": "Multiplier on the upper-bound snapshot cost. 1.0 = full upper-bound (each snapshot full capacity). 0.3 = typical real-world incremental dedup (~30%). 0.0 = exclude snapshots from total. Defaults to 1.0 for backward compatibility.",
        },
    },
    "required": ["name", "capacity_gb"],
    "additionalProperties": False,
}


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="get_aws_price",
            description=(
                "Look up the on-demand Linux hourly + monthly price for an AWS EC2 "
                "instance type in us-east-1. Returns vCPUs, memory, hourly USD, and "
                "monthly USD (730 hours). For multi-cloud comparisons including OCI, "
                "Azure, and GCP, use compare_clouds instead."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "instance_type": {
                        "type": "string",
                        "description": f"EC2 instance type, e.g. 't3.medium'. Available: {', '.join(_list_skus('aws'))}",
                    }
                },
                "required": ["instance_type"],
            },
        ),
        Tool(
            name="get_azure_price",
            description=(
                "Look up the on-demand Linux hourly + monthly price for an Azure VM "
                "size in eastus. Returns vCPUs, memory, hourly USD, and monthly USD."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "vm_size": {
                        "type": "string",
                        "description": f"Azure VM size, e.g. 'D4s_v5'. Available: {', '.join(_list_skus('azure'))}",
                    }
                },
                "required": ["vm_size"],
            },
        ),
        Tool(
            name="get_gcp_price",
            description=(
                "Look up the on-demand Linux hourly + monthly price for a GCP Compute "
                "Engine machine type in us-east1. Returns vCPUs, memory, hourly USD, "
                "and monthly USD."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "machine_type": {
                        "type": "string",
                        "description": f"GCP machine type, e.g. 'e2-standard-4'. Available: {', '.join(_list_skus('gcp'))}",
                    }
                },
                "required": ["machine_type"],
            },
        ),
        Tool(
            name="compare_clouds",
            description=(
                "Find the cheapest equivalent VM across AWS, Azure, GCP, and OCI for "
                "a single target spec (vCPUs and memory). Returns the best-fit SKU "
                "per cloud sorted by monthly cost, plus the absolute and percent "
                "savings of the cheapest vs the most expensive option. OCI A1 Always "
                "Free is included — for specs that fit within 4 OCPU + 24 GB Arm, "
                "OCI returns $0/mo (real perpetual free tier, not a quirk)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "vcpus": {"type": "integer", "minimum": 1},
                    "memory_gb": {"type": "number", "minimum": 0.5},
                },
                "required": ["vcpus", "memory_gb"],
            },
        ),
        Tool(
            name="compare_compute_inventory",
            description=(
                "Bulk-compare a list of compute workloads across AWS, Azure, GCP, and "
                "OCI. Each row is independently sized to the cheapest VM that meets "
                "its vCPU/memory spec on each cloud, multiplied by quantity and "
                "hours_per_month. Optional os_disk_gb adds attached storage cost. "
                "Returns per-row matches, per-cloud totals, and the cheapest cloud "
                "overall. Useful for sizing-sheet style inputs."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "workloads": {
                        "type": "array",
                        "items": _COMPUTE_ITEM_SCHEMA,
                        "minItems": 1,
                    }
                },
                "required": ["workloads"],
            },
        ),
        Tool(
            name="compare_storage_inventory",
            description=(
                "Bulk-compare a list of block-storage volumes across AWS, Azure, GCP, "
                "and OCI. Each row picks the cheapest SKU matching its disk_type "
                "(ssd or hdd) on each cloud, then prices it at capacity_gb × quantity. "
                "Returns per-row matches, per-cloud totals, and cheapest cloud. IOPS "
                "and throughput are accepted but not used for SKU matching. Snapshot "
                "pricing is upper-bound (real-world incremental snapshots cost less)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "volumes": {
                        "type": "array",
                        "items": _STORAGE_ITEM_SCHEMA,
                        "minItems": 1,
                    }
                },
                "required": ["volumes"],
            },
        ),
        Tool(
            name="compare_egress",
            description=(
                "Compare data-transfer costs across AWS, Azure, GCP, and OCI for a "
                "given monthly volume. Two directions supported: 'out_to_internet' "
                "(tiered pricing with free-tier credits — AWS/Azure 100 GB, OCI 10 TB "
                "free) and 'inter_region' (flat rate for cross-region transfer within "
                "the same cloud). At 50 TB/mo of internet egress OCI is ~12× cheaper "
                "than the hyperscalers — a real competitive moat for content/CDN "
                "workloads. VPC peering is NOT yet modeled."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "transfers": {
                        "type": "array",
                        "items": _EGRESS_ITEM_SCHEMA,
                        "minItems": 1,
                    }
                },
                "required": ["transfers"],
            },
        ),
        Tool(
            name="compare_object_storage",
            description=(
                "Compare object-storage pricing across AWS S3, Azure Blob, GCP Cloud "
                "Storage, and OCI Object Storage. Each request specifies capacity_gb "
                "and access tier (hot/cool/archive); the tool picks the cheapest SKU "
                "per cloud at that tier. OCI offers 20 GB Always Free in the 'hot' "
                "tier — surfaced when capacity fits. NOTE: egress, request, and "
                "retrieval costs are not modeled (often the actual hidden killer). "
                "v0.3 preview — placeholder pricing, verify before relying on numbers."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "volumes": {
                        "type": "array",
                        "items": _OBJECT_STORAGE_ITEM_SCHEMA,
                        "minItems": 1,
                    }
                },
                "required": ["volumes"],
            },
        ),
        Tool(
            name="compare_postgres_database",
            description=(
                "Compare managed PostgreSQL pricing across AWS RDS, Azure Database for "
                "PostgreSQL, GCP Cloud SQL, and OCI Database with PostgreSQL. Each "
                "request specifies vCPUs, memory, and storage_gb; the tool picks the "
                "cheapest matching SKU per cloud and totals compute + storage. v0.3 "
                "preview — pricing is bundled placeholder data; verify against current "
                "cloud pricing pages before relying on numbers."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "databases": {
                        "type": "array",
                        "items": _POSTGRES_ITEM_SCHEMA,
                        "minItems": 1,
                    }
                },
                "required": ["databases"],
            },
        ),
        Tool(
            name="compare_workload",
            description=(
                "Combined compute + block-storage compare across AWS, Azure, GCP, "
                "and OCI. Pass a compute list and a storage list (either may be "
                "empty). Returns nested per-row breakdowns plus combined per-cloud "
                "totals and the overall cheapest cloud. Mirrors the structure of a "
                "two-sheet sizing workbook (compute BoM + storage BoM). Optional "
                "`commitment` parameter estimates 1-year or 3-year Reserved Instance "
                "/ Savings Plan / Committed Use discount on compute (storage stays "
                "at on-demand). For object storage, use compare_object_storage. "
                "For managed databases, use compare_postgres_database."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "compute": {
                        "type": "array",
                        "items": _COMPUTE_ITEM_SCHEMA,
                        "default": [],
                    },
                    "storage": {
                        "type": "array",
                        "items": _STORAGE_ITEM_SCHEMA,
                        "default": [],
                    },
                    "commitment": {
                        "type": "string",
                        "enum": ["none", "1yr_no_upfront", "3yr_partial_upfront"],
                        "default": "none",
                        "description": "Compute commitment tier. 'none' = on-demand only. '1yr_no_upfront' applies a representative 30% compute discount. '3yr_partial_upfront' applies 50%. Storage and snapshots are not discounted.",
                    },
                    "multi_az": {
                        "type": "boolean",
                        "default": False,
                        "description": "If true, double compute cost on every cloud to model Multi-AZ / HA deployments (sync replicas across two zones). Storage stays at 1x because object/block storage is usually cross-AZ at base price already.",
                    },
                },
            },
        ),
        # --- v0.6 FinOps decision tools ---
        Tool(
            name="assess_migration",
            description=(
                "Project cross-cloud cost + payback for moving a workload away from "
                "its source cloud. Inputs: source_cloud + workload inventory (compute / "
                "storage / object_storage / databases / egress) + optional one_time data "
                "to migrate. Returns per-target monthly cost, savings %, exit egress cost, "
                "payback months, ranked recommendation by 3-year TCO, and triggered caveats "
                "(e.g., 'OCI A1.Flex is ARM — verify your AMIs'). The kind of FinOps "
                "decision that normally lives in a half-built spreadsheet — now one tool call."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **_finops_inventory_properties(),
                    "targets": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["aws", "azure", "gcp", "oci"]},
                        "description": "Target clouds to evaluate. Default: all clouds except source_cloud.",
                    },
                },
                "required": ["source_cloud"],
            },
        ),
        Tool(
            name="optimize_commitment",
            description=(
                "Compute per-scenario cost / savings / payback for compute commitment "
                "options (none, 1yr_no_upfront, 1yr_all_upfront, 3yr_no_upfront, "
                "3yr_partial_upfront, 3yr_all_upfront). Returns each scenario's monthly "
                "cost, upfront, 3-year total, savings %, and payback months — plus the "
                "recommended scenario by lowest 3-year TCO. Compute-only (storage / "
                "database / object / egress are not discounted because most clouds don't "
                "offer meaningful commitments on these). Per-family RI tiers come in "
                "v0.6.x; v0.6.0 uses cloud-level conservative averages."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **_finops_inventory_properties(include_commitment=False),
                    "cloud": {
                        "type": "string",
                        "enum": ["aws", "azure", "gcp", "oci"],
                        "description": "Cloud to evaluate (default: source_cloud, then 'aws').",
                    },
                    "scenarios": {
                        "type": "array",
                        "items": {"type": "string", "enum": list(ALL_SCENARIOS)},
                        "description": "Subset of commitment scenarios to evaluate. Default: all 6.",
                    },
                },
                "required": ["compute"],
            },
        ),
        Tool(
            name="compare_total_cost_of_ownership",
            description=(
                "Project per-cloud per-year cost over a configurable horizon (default 3 "
                "years), with linear YoY growth assumptions for compute / storage / egress. "
                "Returns cumulative TCO per cloud, year-by-year breakdown by category, and "
                "sensitivity analysis identifying the most impactful growth variable. The "
                "kind of number that goes into board decks and budget conversations — now "
                "computed from a public catalog instead of a spreadsheet."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **_finops_inventory_properties(include_source_cloud=False),
                    "horizon_years": {
                        "type": "integer", "minimum": 1, "default": 3,
                        "description": "Years to project (default 3 — FinOps standard).",
                    },
                    "growth": {
                        "type": "object",
                        "properties": {
                            "compute_pct_yoy": {
                                "type": "number", "default": 0.0,
                                "description": "+0.20 means +20% YoY compute growth.",
                            },
                            "storage_pct_yoy": {"type": "number", "default": 0.0},
                            "egress_pct_yoy": {"type": "number", "default": 0.0},
                        },
                        "additionalProperties": False,
                    },
                    "targets": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["aws", "azure", "gcp", "oci"]},
                        "description": "Clouds to project. Default: all 4 clouds.",
                    },
                },
            },
        ),
        Tool(
            name="find_egress_arbitrage",
            description=(
                "Specialized assess_migration scoped to egress patterns. Useful when a "
                "team's largest cost line is data transfer (CDN workloads, video streaming, "
                "content distribution). Returns per-target egress cost, monthly + annual "
                "savings, payback months on any one-time exit cost, and recommendation. "
                "The OCI 12× moat is the headline finding: at 50 TB/month internet egress, "
                "OCI is roughly $340 vs $4,000+ on the hyperscalers because of OCI's "
                "10 TB/month free tier + $0.0085/GB beyond."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "source_cloud": {
                        "type": "string",
                        "enum": ["aws", "azure", "gcp", "oci"],
                    },
                    "egress": {
                        "type": "array",
                        "items": _FINOPS_EGRESS_ITEM_SCHEMA,
                        "minItems": 1,
                    },
                    "one_time": _FINOPS_ONE_TIME_SCHEMA,
                    "targets": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["aws", "azure", "gcp", "oci"]},
                    },
                },
                "required": ["source_cloud", "egress"],
            },
        ),
        # --- v0.7.1 price-history tools ---
        Tool(
            name="get_price_history",
            description=(
                "Return the multi-cloud price history for a specific (cloud, sku). "
                "cloudprice-mcp persists every weekly auto-refresh as a dated snapshot, "
                "so this is the only FinOps tool that can answer 'what did m5.xlarge "
                "cost in May?' — neither AWS Calculator nor GCP Estimator preserves "
                "historical pricing. Returns the timeseries, total change %, and the "
                "earliest/latest data points. Useful for: validating commitment timing, "
                "spotting quiet price increases, citing audit-traceable historical "
                "numbers to a CFO."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "cloud": {"type": "string", "enum": ["aws", "azure", "gcp", "oci"]},
                    "sku": {"type": "string", "description": "Instance type / SKU name, e.g. m5.xlarge, D2s_v5, e2-standard-2, VM.Standard.E5.Flex.1OCPU"},
                    "since": {
                        "type": ["string", "null"],
                        "description": "Optional ISO date (YYYY-MM-DD); restricts to snapshots on or after this date.",
                    },
                },
                "required": ["cloud", "sku"],
                "additionalProperties": False,
            },
        ),
        Tool(
            name="list_tracked_skus",
            description=(
                "List every (cloud, sku) pair present in the bundled price-history "
                "dataset, with how many weekly snapshots exist for each. Use this to "
                "discover which SKUs you can call get_price_history on, or to find "
                "the biggest-mover SKUs since a given date. Filter by cloud to scope."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "cloud": {
                        "type": ["string", "null"],
                        "enum": ["aws", "azure", "gcp", "oci", None],
                    },
                    "since": {
                        "type": ["string", "null"],
                        "description": "Optional ISO date; only consider snapshots on or after this date.",
                    },
                },
                "additionalProperties": False,
            },
        ),
        # --- v0.14.0 FinOps decision report (CFO-grade markdown) ---
        Tool(
            name="generate_decision_report",
            description=(
                "Generate a CFO-grade FinOps decision report in markdown. "
                "Combines assess_migration + compare_carbon_footprint into a "
                "single structured artifact the user can paste into a wiki / "
                "PR / Slack / email. Sections: executive summary, per-cloud "
                "cost table, carbon comparison (optional), the workload spec, "
                "aggregated honest_gaps, and an audit trail (package version, "
                "catalog as_of, requester, reproduce command). The 'trust' "
                "angle commercial FinOps tools can't easily replicate because "
                "their reports live in their UI; this one is markdown the "
                "user owns. Use when the question is 'we need a decision "
                "artifact a CFO can sign off on'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **_finops_inventory_properties(),
                    "targets": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["aws", "azure", "gcp", "oci"]},
                        "description": "Optional list of target clouds to compare against the source.",
                    },
                    "include_carbon": {"type": "boolean", "default": True},
                    "requester": {"type": "string", "description": "Optional name/email for the audit trail."},
                },
                "required": ["source_cloud"],
                "additionalProperties": False,
            },
        ),
        # --- v0.13.0 Price anomaly detection ---
        Tool(
            name="detect_price_anomalies",
            description=(
                "Statistical anomaly detection over the bundled price-history "
                "dataset. Compounds the v0.7.1 history archive: as snapshots "
                "accumulate weekly, this tool auto-flags unusual price moves "
                "without you having to manually inspect every SKU. Two methods, "
                "auto-selected by dataset density: percent_change (for <5 "
                "snapshots) and z_score (>=5). Sensitivities: strict (10% / "
                "z>=2.5), moderate (5% / z>=2.0, default), permissive (2% / z>=1.5). "
                "Use for 'what changed this week?' and 'anything weird across "
                "any cloud since {date}?' queries. Excellent source for "
                "weekly LinkedIn / blog content auto-generation."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "since": {"type": "string", "description": "ISO date — restrict to snapshots on or after this date. Optional."},
                    "cloud": {"type": "string", "enum": ["aws", "azure", "gcp", "oci"], "description": "Filter to one cloud. Optional."},
                    "sensitivity": {"type": "string", "enum": ["strict", "moderate", "permissive"], "default": "moderate"},
                    "method": {"type": "string", "enum": ["percent_change", "z_score", "auto"], "default": "auto"},
                    "limit": {"type": "integer", "minimum": 1, "default": 25},
                },
                "additionalProperties": False,
            },
        ),
        # --- v0.12.0 LLM token pricing ---
        Tool(
            name="compare_token_pricing",
            description=(
                "Cross-provider LLM token pricing comparison — the only FinOps tool "
                "that returns per-1M input/output rates AND monthly cost for the same "
                "model on each provider that hosts it (Claude on Anthropic vs Bedrock "
                "vs Vertex; GPT on OpenAI vs Azure OpenAI; Llama on Bedrock; Gemini "
                "on Google vs Vertex; Mistral / DeepSeek direct). Filter by model_family "
                "('claude', 'gpt', 'gemini', 'llama', 'mistral', 'deepseek'), exact "
                "model_id, or provider list. If monthly_input_tokens + monthly_output_tokens "
                "are provided, ranks by total monthly USD. Otherwise ranks by per-1M "
                "output cost (output tokens dominate most workloads). Surfaces cache_read "
                "/ cache_write rates when published — most workloads see 30-90% input cost "
                "reduction with proper prompt caching."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "model_family": {
                        "type": "string",
                        "enum": ["claude", "gpt", "gemini", "llama", "mistral", "deepseek"],
                        "description": "Filter to one model family. Optional.",
                    },
                    "model_id": {
                        "type": "string",
                        "description": "Exact model ID (e.g. 'claude-4-sonnet', 'gpt-4o', 'gemini-2.0-flash'). Optional.",
                    },
                    "providers": {
                        "type": "array",
                        "items": {"type": "string", "enum": [
                            "anthropic", "openai", "google", "deepseek", "mistral",
                            "bedrock", "vertex", "azure_openai",
                        ]},
                        "description": "Optional list of providers to include.",
                    },
                    "monthly_input_tokens": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Monthly input token volume. Required to get monthly_total_usd in result.",
                    },
                    "monthly_output_tokens": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Monthly output token volume. Required to get monthly_total_usd in result.",
                    },
                },
                "additionalProperties": False,
            },
        ),
        # --- v0.18.0 FOCUS 1.3 export ---
        Tool(
            name="export_focus",
            description=(
                "Emit FOCUS 1.3-shaped rows from any pricing query, ready to "
                "import into Vantage Custom Providers / Microsoft Cost Mgmt / "
                "OpenCost / Apptio / any FinOps tool that consumes the FinOps "
                "Foundation's open billing schema (ratified Dec 2025). The "
                "rows populate the list-price columns (ListCost, ListUnitPrice, "
                "PricingQuantity, etc.) and leave billed/contracted/effective "
                "columns NULL since cloudprice produces projections, not real "
                "bills — that's explicitly documented in the result's notes "
                "field. Use this when the user says 'export my comparison to "
                "FOCUS', 'send this to Vantage', 'plug this into our FinOps "
                "stack'. Supports query_kind: compute_workload (calls "
                "assess_migration), token_pricing (compare_token_pricing), "
                "extended_model_lookup (lookup_extended_model_pricing). Format "
                "is 'json' (default) or 'csv'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query_kind": {
                        "type": "string",
                        "enum": ["compute_workload", "token_pricing", "extended_model_lookup"],
                        "description": "Which underlying pricing tool to run before FOCUS-shaping.",
                    },
                    "format": {"type": "string", "enum": ["json", "csv"], "default": "json"},
                    "billing_period_start": {
                        "type": "string",
                        "description": "ISO date YYYY-MM-DD for the synthetic billing period start. Defaults to current month.",
                    },
                    # compute_workload args (use the standard inventory shape via source_cloud)
                    "source_cloud": {"type": "string", "enum": ["aws", "azure", "gcp", "oci"]},
                    "compute": {"type": "array"},
                    "storage": {"type": "array"},
                    "object_storage": {"type": "array"},
                    "egress": {"type": "array"},
                    "databases": {"type": "array"},
                    "commitment": {"type": "string"},
                    "multi_az": {"type": "boolean"},
                    "targets": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["aws", "azure", "gcp", "oci"]},
                    },
                    # token_pricing args
                    "model_family": {"type": "string", "enum": ["claude", "gpt", "gemini", "llama", "mistral", "deepseek"]},
                    "model_id": {"type": "string"},
                    "providers": {"type": "array", "items": {"type": "string"}},
                    "monthly_input_tokens": {"type": "integer", "minimum": 0},
                    "monthly_output_tokens": {"type": "integer", "minimum": 0},
                    # extended_model_lookup args
                    "query": {"type": "string"},
                    "provider": {"type": "string"},
                    "mode": {"type": "string", "enum": ["chat", "completion", "responses"]},
                    "source": {"type": "string", "enum": ["litellm", "openrouter"]},
                },
                "required": ["query_kind"],
            },
        ),
        # --- v0.16.0 / v0.17.0 extended LLM catalog (LiteLLM + OpenRouter) ---
        Tool(
            name="lookup_extended_model_pricing",
            description=(
                "Search the extended LLM catalog (~2300 model/provider combinations "
                "auto-ingested weekly from two upstreams: LiteLLM's public JSON "
                "(direct-provider retail prices for Together AI, Fireworks, Replicate, "
                "Groq, Cerebras, Perplexity, regional Bedrock/Azure variants, etc.) "
                "and OpenRouter's /api/v1/models (routed prices including OpenRouter's "
                "margin). Every row tagged source='litellm' or source='openrouter' "
                "so callers can compare direct vs routed pricing for the same model. "
                "Complements compare_token_pricing (hand-curated, 19 vetted models). "
                "Use this when the user asks about: (a) a model or provider "
                "compare_token_pricing doesn't know — 'cheapest place to host Llama "
                "3.1 405B', 'Together AI pricing', 'what Groq charges for Mixtral' — "
                "or (b) whether OpenRouter routing is cheaper or more expensive than "
                "going direct to Bedrock/Anthropic/OpenAI ('does OpenRouter add margin "
                "for Claude 3 Haiku?')."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Case-insensitive substring matched against the model ID. E.g. 'llama-3.1-70b' matches every Llama 3.1 70B variant on every host.",
                    },
                    "provider": {
                        "type": "string",
                        "description": "Filter to one provider: 'bedrock', 'fireworks_ai', 'together_ai', 'groq', 'perplexity', 'replicate', 'openrouter' (for routed prices), etc.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["chat", "completion", "responses"],
                        "description": "Mode filter. Default: any (catalog already excludes embedding/image/audio).",
                    },
                    "max_context_tokens": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Skip models with smaller context. Useful for 'cheapest models that support 200K+ context'.",
                    },
                    "monthly_input_tokens": {"type": "integer", "minimum": 0},
                    "monthly_output_tokens": {"type": "integer", "minimum": 0},
                    "source": {
                        "type": "string",
                        "enum": ["litellm", "openrouter"],
                        "description": "Filter to one upstream source. 'litellm' = direct-provider retail prices. 'openrouter' = OpenRouter routed prices (includes their margin). Default: both — useful for cross-source comparison.",
                    },
                    "limit": {"type": "integer", "minimum": 1, "default": 25},
                },
                "additionalProperties": False,
            },
        ),
        # --- v0.11.0 GPU pricing ---
        Tool(
            name="compare_gpu_workload",
            description=(
                "Cross-cloud GPU pricing comparison — finds the cheapest matching SKU "
                "per cloud for a (gpu_type, gpu_count) request. Supports NVIDIA T4 / "
                "A10 / A10G / L4 / L40S / V100 / A100 / H100 across AWS / Azure / GCP / "
                "OCI. Returns absolute hourly cost AND $/GPU/h so users can see both "
                "the absolute winner and the per-GPU packaging-efficiency winner "
                "(they're often different clouds). Surfaces over-provisioning when "
                "the only matching SKU has more GPUs than requested (e.g., asking for "
                "1 H100 but only bare-metal 8x SKUs are available). Use for AI/ML "
                "workload pricing decisions — training, inference, fine-tuning."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "gpu_type": {
                        "type": "string",
                        "description": "GPU family name, e.g. 'A100', 'H100', 'L4', 'L40S', 'T4', 'A10', 'A10G', 'V100'. Case-insensitive.",
                    },
                    "gpu_count": {"type": "integer", "minimum": 1, "default": 1},
                    "targets": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["aws", "azure", "gcp", "oci"]},
                        "description": "Optional list of clouds to compare; default all 4.",
                    },
                },
                "required": ["gpu_type"],
                "additionalProperties": False,
            },
        ),
        # --- v0.10.0 Carbon-aware FinOps ---
        Tool(
            name="compare_carbon_footprint",
            description=(
                "Multi-cloud carbon footprint comparison — the only FinOps tool that "
                "returns both USD cost AND kg CO2e/month side-by-side. For a given "
                "vCPU + memory shape (and optional quantity), returns per-cloud best-"
                "matching SKU + monthly cost + grid-based kg CO2e + market-based "
                "residual kg CO2e (after the cloud's published renewable matching) + "
                "power class (x86 vs ARM). Ranked cheapest-carbon-first. Surfaces "
                "tradeoffs (AWS/Azure 100% renewable matched, GCP ~64% CFE, OCI "
                "unmatched outside EU). Use when the user cares about sustainability, "
                "Green FinOps, or carbon-cost dual optimization."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "vcpus": {"type": "integer", "minimum": 1},
                    "memory_gb": {"type": "number", "minimum": 0.5},
                    "quantity": {"type": "integer", "minimum": 1, "default": 1},
                    "targets": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["aws", "azure", "gcp", "oci"]},
                        "description": "Optional list of clouds to compare; default all 4.",
                    },
                },
                "required": ["vcpus", "memory_gb"],
                "additionalProperties": False,
            },
        ),
        # --- v0.9.0 Cost Drift Sentinel ---
        Tool(
            name="watch_workload",
            description=(
                "Cost Drift Sentinel — turns cloudprice from query tool into agent capability. "
                "First call (no baseline): captures the current per-cloud monthly cost for the "
                "workload as a baseline JSON the caller persists. Subsequent calls (with the "
                "saved baseline): compute today's cost, compare to baseline, return a structured "
                "drift report with per-cloud delta + SKU-level attribution from the price-history "
                "dataset. Stateless — no database, no server-side storage. Drop the baseline JSON "
                "into a git repo or any durable location. Designed for scheduled agents (GitHub "
                "Actions cron, Claude Code's autonomous mode, etc.) to detect 'has this cost "
                "moved since I signed off?' between runs."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    **_finops_inventory_properties(),
                    "baseline": {
                        "type": ["object", "null"],
                        "description": (
                            "Previously returned baseline JSON. Pass null on first call to "
                            "capture a fresh baseline; pass it back on subsequent calls to "
                            "detect drift."
                        ),
                    },
                    "alert_threshold_pct": {
                        "type": "number",
                        "minimum": 0,
                        "default": 5.0,
                        "description": "Drift % that triggers an alert. Defaults to 5%.",
                    },
                },
                "required": ["source_cloud"],
                "additionalProperties": False,
            },
        ),
        # --- v0.8.1 spot pricing tool ---
        Tool(
            name="compare_spot",
            description=(
                "Compare spot / preemptible pricing for a vCPU + memory shape across "
                "AWS, Azure, GCP, and OCI. Returns per-cloud best-matching SKU + "
                "on-demand cost + spot cost + savings + eviction characteristics, "
                "ranked cheapest-first. The only multi-cloud spot comparison "
                "anyone publishes openly — AWS Spot vs Azure Spot vs GCP Spot VMs "
                "vs OCI Preemptible all use different pricing + eviction models, "
                "and this tool surfaces every one of them with their tradeoffs."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "vcpus": {"type": "integer", "minimum": 1},
                    "memory_gb": {"type": "number", "minimum": 0.5},
                    "targets": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["aws", "azure", "gcp", "oci"]},
                        "description": "Optional list of clouds to compare; default all 4.",
                    },
                },
                "required": ["vcpus", "memory_gb"],
                "additionalProperties": False,
            },
        ),
    ]


def _ok(payload: dict | list) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(payload, indent=2))]


def _err(message: str) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"error": message}, indent=2))]


def _lookup(cloud: Cloud, sku_field: str, sku: str) -> list[TextContent]:
    catalog = load_catalog()
    instance = catalog.find(cloud, sku)
    if instance is None:
        return _err(
            f"Unknown {cloud.upper()} {sku_field} '{sku}'. "
            f"Available: {', '.join(_list_skus(cloud))}"
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


def _handle_get_aws_price(catalog, args):  # noqa: ARG001 (catalog passed for uniformity)
    return _lookup("aws", "instance_type", args["instance_type"])


def _handle_get_azure_price(catalog, args):  # noqa: ARG001
    return _lookup("azure", "vm_size", args["vm_size"])


def _handle_get_gcp_price(catalog, args):  # noqa: ARG001
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


def _handle_detect_price_anomalies(catalog, args):  # noqa: ARG001 — history loads its own data
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


def _handle_compare_token_pricing(catalog, args):  # noqa: ARG001 — catalog unused; token catalog is separate
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


def _handle_lookup_extended_model_pricing(catalog, args):  # noqa: ARG001 — extended catalog is separate file
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


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    handler = _TOOL_HANDLERS.get(name)
    if handler is None:
        return _err(f"Unknown tool: {name}")
    catalog = load_catalog()
    return handler(catalog, arguments)


async def _run() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main() -> None:
    """Console entry point. Runs the MCP server over stdio."""
    _ = __version__
    asyncio.run(_run())


if __name__ == "__main__":
    main()
