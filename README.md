# cloudprice-mcp

<!-- mcp-name: io.github.alialbaker/cloudprice-mcp -->

[![PyPI version](https://img.shields.io/pypi/v/cloudprice-mcp.svg)](https://pypi.org/project/cloudprice-mcp/)
[![Python versions](https://img.shields.io/pypi/pyversions/cloudprice-mcp.svg)](https://pypi.org/project/cloudprice-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![alialbaker/cloudprice-mcp MCP server](https://glama.ai/mcp/servers/alialbaker/cloudprice-mcp/badges/score.svg)](https://glama.ai/mcp/servers/alialbaker/cloudprice-mcp)

**The FinOps MCP server.** Gives Claude, GitHub Copilot, Cursor, Windsurf, Cline, Continue, Zed — or any MCP-compatible AI — structured pricing data and analysis primitives across **AWS, Azure, GCP, and OCI**. AI clients use cloudprice-mcp to compute Reserved Instance break-even, multi-cloud workload TCO, exit-cost migration analyses, snapshot cost modeling, and egress arbitrage — the kind of FinOps decisions that normally live in three browser tabs and a half-built spreadsheet.

**25 tools** covering compute, block storage, object storage, managed Postgres, **egress** (internet + inter-region with OCI's 10 TB free tier surfaced explicitly), Multi-AZ workloads, snapshots with realistic incremental modeling, Reserved Instance / Savings Plan discounts, FinOps decision suite (migration, commitment, TCO, egress arbitrage), **multi-cloud spot pricing** with eviction tradeoffs, **multi-cloud price history** (the only public weekly-refreshed dataset of its kind), a **stateless cost drift sentinel** for scheduled agents, **multi-cloud carbon footprint** ($ AND kg CO2e on the same query), **multi-cloud GPU pricing** (T4 / A10 / L4 / L40S / V100 / A100 / H100 across all 4 clouds), **cross-provider LLM token pricing** (Claude / GPT / Gemini / Llama / Mistral / DeepSeek across Anthropic / OpenAI / Bedrock / Vertex / Azure OpenAI), **statistical price-anomaly detection** over the weekly snapshot archive (z-score + percent-change, auto-selected by dataset density), and **CFO-grade FinOps decision report generation** (markdown reports with executive summary, cost table, carbon, audit trail, honest gaps). OCI Always Free tier (4 OCPU compute, 20 GB object storage, 10 TB egress) surfaced as $0 line items where it applies.

**One-line install configures every AI client you have:** `pip install cloudprice-mcp && cloudprice-mcp setup` — auto-detects Claude Desktop, GitHub Copilot Agent Mode, Cursor, Windsurf, Cline, Continue.dev, and Zed, then asks Y/N before writing each config.

![demo](demo.gif)

## What does FinOps look like with cloudprice-mcp?

Real questions teams actually ask. Paste any of these into Claude / Copilot / Cursor with cloudprice-mcp loaded:

> ***"I have 6× t3.2xlarge running on AWS. Compare the 3-year total cost on-demand vs 1-year Savings Plan vs 3-year RI partial upfront. What's the break-even month?"***
> → AI calls `compare_workload`, pulls list-price baseline, layers AWS's published RI rates, returns dollar break-even. ~7-month payback typical.

> ***"I'm thinking about offloading 5 TB of cold-tier object storage from AWS S3 to a cheaper provider. Compare archive-tier cost across all 4 clouds, factor in AWS exit egress, and tell me the payback period."***
> → AI calls `compare_object_storage` + `compare_egress`, computes one-time exit cost vs ongoing savings. Often surfaces "don't move — AWS Glacier Deep Archive is already tied for cheapest".

> ***"At 50 TB/month internet egress, where am I cheapest? Show the 3-year savings of moving."***
> → `compare_egress` → OCI ~$340/mo, AWS/Azure/GCP ~$4,000/mo. The 12× difference is OCI's 10 TB free tier — a real moat for content/CDN workloads.

> ***"Size a 3-tier SaaS workload: 8 web (4/16), 12 app (8/32), 4 DB (16/64), 5 TB shared SSD, 50 TB HDD bulk, 10 TB/month egress. Compare full-stack monthly cost across all 4 clouds with multi-AZ and 1-year commitment."***
> → AI chains `compare_workload` + `compare_egress`, applies multi-AZ multiplier (×2 compute) + commitment discount.

What you get back: dollar numbers traceable to a public catalog, AI-explained tradeoffs, payback periods, and the kind of "don't do that" recommendation that kills bad migrations before they happen. **No console-clicking. No tab-switching between three pricing calculators. No FinOps spreadsheet that goes stale the moment a new SKU drops.**

---

## Install

**Recommended (auto-config):**

```bash
pip install cloudprice-mcp
cloudprice-mcp setup     # auto-configures every detected MCP client, asks Y/N before writing
```

Then fully restart whichever clients were configured. **10 tools appear** in each. Done.

**Trust spectrum:**

| Command | When to use |
|---|---|
| `cloudprice-mcp setup` | Default — detects every installed client, shows the plan, asks Y/N once |
| `cloudprice-mcp setup --yes` | Skip prompt (CI / scripts) |
| `cloudprice-mcp setup --client copilot` | Configure a specific client (repeatable: `--client copilot --client cursor`) |
| `cloudprice-mcp setup --all` | Configure every known client even if not detected |
| `cloudprice-mcp setup --force` | Refresh existing entries — useful after upgrade or moving Python |
| `cloudprice-mcp setup --dry-run` | Show per-client diffs without writing |
| `cloudprice-mcp setup --print-config` | Emit per-client JSON to stdout for manual paste |
| `cloudprice-mcp setup --list-clients` | Detection table — which clients are known + installed on this system |
| Manual edit | Don't trust running new tools — see [INSTALL.md](INSTALL.md) per-client sections |

If something doesn't work, run:

```bash
cloudprice-mcp doctor
```

It tells you exactly what's broken (Python version, install path, config location, tool registration, command path validity).

Python 3.10+ required.

For step-by-step manual install (Windows / macOS / Linux), see **[INSTALL.md](INSTALL.md)**.

## Tools exposed

### Single-spec lookups (v0.1)

| Tool | What it does |
|---|---|
| `get_aws_price` | Look up an EC2 instance type → vCPUs, memory, hourly + monthly USD (us-east-1) |
| `get_azure_price` | Look up an Azure VM size → vCPUs, memory, hourly + monthly USD (eastus) |
| `get_gcp_price` | Look up a GCP Compute Engine machine type → vCPUs, memory, hourly + monthly USD (us-east1) |
| `compare_clouds` | Given a target spec (vCPUs + GB), return the cheapest matching SKU across **AWS / Azure / GCP / OCI**, sorted by monthly cost, with savings summary |

### Bulk + workload compare (v0.2)

| Tool | What it does |
|---|---|
| `compare_compute_inventory` | Bulk-compare a list of compute workloads (each with vCPUs / memory / quantity / hours / optional OS disk) across all 4 clouds. Returns per-row matches, per-cloud totals, cheapest cloud. |
| `compare_storage_inventory` | Bulk-compare a list of block-storage volumes (each with capacity / disk type / quantity) across all 4 clouds. |
| `compare_workload` | Combined compute + block storage in one call. Mirrors a two-sheet sizing workbook (compute BoM + storage BoM). Optional `commitment` overlay applies 1-year (30%) or 3-year (50%) compute discount. |

### Object storage + managed Postgres (v0.3)

| Tool | What it does |
|---|---|
| `compare_object_storage` | Bulk-compare object-storage buckets across **AWS S3 / Azure Blob / GCP Cloud Storage / OCI Object Storage**. Each row specifies capacity_gb + tier (`hot` / `cool` / `archive`). **OCI Always Free 20 GB tier surfaced explicitly** — capacity ≤ 20 GB on OCI hot tier returns $0/mo. |
| `compare_postgres_database` | Bulk-compare managed PostgreSQL pricing across **AWS RDS / Azure Database for PostgreSQL / GCP Cloud SQL / OCI Database with PostgreSQL**. Each row specifies vCPUs / memory / storage_gb. Storage cost is calculated separately from compute. |

### FinOps decision suite (v0.6, NEW)

Four named tools that turn cross-cloud pricing into FinOps decisions in one call instead of letting the AI chain three+ tools. All four consume a structured workload inventory (compute / storage / object_storage / databases / egress) plus tool-specific options.

| Tool | What it does |
|---|---|
| `assess_migration` | "Should I move?" — projects per-target cloud cost, savings %, **one-time exit egress cost**, payback months. Returns a ranked recommendation by 3-year TCO with triggered caveats (e.g., "OCI A1.Flex is ARM — verify your AMIs"). |
| `optimize_commitment` | "When does my RI / SP / CUD pay back?" — six commitment scenarios (`none` / `1yr_no_upfront` / `1yr_all_upfront` / `3yr_no_upfront` / `3yr_partial_upfront` / `3yr_all_upfront`) with per-scenario monthly cost, upfront, 3-year total, savings %, payback months. Recommends the lowest 3-year TCO option. |
| `compare_total_cost_of_ownership` | "What's my 3-year cost across clouds?" — multi-year projection with linear YoY growth assumptions for compute / storage / egress. Returns cumulative TCO per cloud, year-by-year breakdown, sensitivity analysis on the dominant variable. The kind of number that goes into board decks. |
| `find_egress_arbitrage` | "Where do I save on data transfer?" — specialized assess_migration scoped to egress only. Surfaces the OCI 12× moat: at 50 TB/month internet egress, OCI is ~$340 vs $4,000+ on the hyperscalers. |

All four tools accept a `WorkloadInventory` shape that mirrors a 4-section sizing sheet (compute / storage / object_storage / databases / egress) plus optional `commitment`, `multi_az`, and `one_time.data_to_migrate_gb` fields. Output includes `honest_gaps` — explicit list of what each tool does NOT model — to prevent over-trust.

### Egress + Multi-AZ + better snapshots (v0.5, NEW)

| Tool / Feature | What it does |
|---|---|
| `compare_egress` | Compare data-transfer costs across all 4 clouds. Two directions: `out_to_internet` (tiered pricing with free-tier credits — AWS/Azure 100 GB, **OCI 10 TB**) and `inter_region` (cross-region within the same cloud). At 50 TB/month internet egress, **OCI is ~12× cheaper than the hyperscalers** — a real moat for content/CDN workloads. |
| `compare_workload` `multi_az: true` | New flag doubles compute totals on every cloud to model Multi-AZ / HA deployments (sync replicas across two zones). Storage stays at 1× because object/block storage is usually cross-AZ at base price. |
| `snapshot_incremental_factor` | New per-row field on storage and OS-disk snapshots. Default `1.0` keeps the v0.2 upper-bound estimate. Set to `0.3` for typical real-world incremental dedup, or `0.0` to exclude snapshots from the total. |

### Example: compare_workload input shape

```json
{
  "compute": [
    { "name": "web", "tier": "Web", "vcpus": 4, "memory_gb": 16, "quantity": 8,  "os_disk_gb": 100, "os_disk_type": "ssd" },
    { "name": "app", "tier": "App", "vcpus": 8, "memory_gb": 32, "quantity": 12, "os_disk_gb": 200, "os_disk_type": "ssd" },
    { "name": "db",  "tier": "DB",  "vcpus": 16, "memory_gb": 64, "quantity": 4, "os_disk_gb": 500, "os_disk_type": "ssd" }
  ],
  "storage": [
    { "name": "shared-fast", "tier": "DB",  "capacity_gb": 5000,  "disk_type": "ssd" },
    { "name": "shared-bulk", "tier": "App", "capacity_gb": 50000, "disk_type": "hdd" }
  ]
}
```

### Snapshots (v0.2.1)

`snapshot_count` on storage rows and `os_disk_snapshot_count` on compute rows **are now priced**. Snapshot rates per cloud per disk type are bundled (~$0.05/GB-mo for AWS/Azure, ~$0.026/GB-mo for GCP).

**Caveat — upper-bound estimate:** snapshots are priced as `snapshot_per_gb_month × full_capacity × quantity × snapshot_count`. Real-world snapshots are **incremental** (only changed blocks), so actual cost is typically 20-50% of this model's number. If snapshots dominate your total, ask the cloud's calculator for a tighter estimate.

`iops` and `throughput_mbs` on storage rows are still accepted as metadata only — not used for SKU matching in this release.

### Reserved Instance / Savings Plan estimator (v0.2.1)

`compare_workload` accepts an optional `commitment` parameter:

| Value | Compute discount | Use case |
|---|---|---|
| `none` (default) | 0% | On-demand only |
| `1yr_no_upfront` | 30% | 1-year AWS Savings Plan / Azure RI / GCP CUD (no upfront) |
| `3yr_partial_upfront` | 50% | 3-year, partial upfront — typical "we know our baseline" deals |

Storage and snapshots are not discounted (most clouds don't offer meaningful storage commitments). Discount tiers are conservative averages — your actual rate depends on instance family, payment option, and region.

## Pricing data

Prices are bundled as a curated dataset of common SKUs across **4 clouds**:
- **Compute** (~50 VM SKUs across AWS / Azure / GCP / OCI, including OCI A1 Always Free + A2 Arm Ampere + E5 Flex)
- **Block storage** (SSD + HDD per cloud)
- **Object storage** (Hot / Cool / Archive tiers per cloud, including OCI Always Free 20 GB)
- **Managed PostgreSQL** (RDS / Azure DB / Cloud SQL / OCI Database with PostgreSQL)

### Auto-refreshed weekly (v0.7+)

The bundled catalog is refreshed every Sunday by a GitHub Action that hits each cloud's public pricing API:

- **AWS** — [Pricing API](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/price-changes.html) (via boto3, OIDC-authenticated)
- **Azure** — [Retail Prices API](https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices) (public, no auth)
- **OCI** — [Public pricing API](https://apexapps.oracle.com/pls/apex/cetools/api/v1/products/) (public, no auth)
- **GCP** — [Cloud Billing Catalog API](https://cloud.google.com/billing/docs/reference/rest/v1/services.skus) (via API key — `GCP_API_KEY` env var). Added in v0.8.0
- **Bedrock LLM token prices** — [AWS Pricing API](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/billing-getting-started.html) `AmazonBedrock` service code (via boto3, same OIDC role). Added in v0.15.0 — refreshes input/output token rates for every Bedrock-hosted model we track (Claude 4 Opus/Sonnet, Claude 3/3.5 Haiku, Llama 3.1/3.3, Mistral Large 2, DeepSeek R1)
- **Extended LLM catalog** — [LiteLLM's `model_prices_and_context_window.json`](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json) (MIT-licensed, community-maintained, unauthenticated). Added in v0.16.0 — ingests ~2000 (model, provider) combinations covering Together AI, Fireworks, Groq, Replicate, Perplexity, regional Bedrock/Azure variants, older model versions. Powers the `lookup_extended_model_pricing` tool. Curated hand-vetted prices (`compare_token_pricing`) and extended (`lookup_extended_model_pricing`) are deliberately separate so users always know which catalog they're hitting.

Each refresh writes a **dated snapshot** to `src/cloudprice_mcp/data/prices/YYYY-MM-DD.json` and `src/cloudprice_mcp/data/token_prices/YYYY-MM-DD.json` — every JSON ever published lives in the repo. The history archive is MIT-licensed and grows with every release.

Every tool result includes the catalog's `as_of` field so you know exactly which prices were used.

### Public price history dataset (v0.7.1+)

cloudprice-mcp is the only FinOps tool we know of that **preserves every weekly snapshot**. You can query *"what did m5.xlarge cost in May?"* — neither AWS Calculator nor GCP Estimator can answer that because their pages always show today.

**Query the history from the CLI:**

```bash
cloudprice-mcp history --cloud oci --sku VM.Standard.E5.Flex.4OCPU
# oci/VM.Standard.E5.Flex.4OCPU (us-ashburn-1) — 2 data point(s)
#
# AS_OF          HOURLY USD
# --------------------------
# 2026-04-26   $    0.67600
# 2026-05-12   $    0.18400
#
# Change: -72.78% ($-0.49200/h)
```

The -72.78% drop is the v0.7.0 auto-refresh **fixing a hand-curated inaccuracy** in the prior OCI catalog — proof that the auto-refresh story works.

**Query the history from AI assistants** via two new MCP tools:

- `get_price_history(cloud, sku, since?)` — full timeseries + change stats
- `list_tracked_skus(cloud?, since?)` — every (cloud, sku) pair we have history for

Real questions this unlocks:

> *"Has AWS m5.xlarge changed price in the last quarter?"*
> → AI calls `get_price_history`, returns timeseries with start/end prices and % change.

> *"Show me every multi-cloud price mover since January."*
> → AI calls `list_tracked_skus(since="2026-01-01")`, returns every SKU + its latest price + change.

### Cross-provider LLM token pricing (v0.12.0+)

Token costs are the fastest-growing FinOps line item in 2026 — and **nobody compares them cross-provider openly**. The same model is often available on multiple providers at different prices (Claude on Anthropic / Bedrock / Vertex; GPT on OpenAI / Azure OpenAI; Llama on Bedrock).

```python
from cloudprice_mcp.finops.tokens import compare_token_pricing

# Cheapest model overall for a 50M-in / 10M-out monthly workload
r = compare_token_pricing(
    monthly_input_tokens=50_000_000,
    monthly_output_tokens=10_000_000,
)
# gemini-1.5-flash on google is cheapest at $6.75/mo for 50M in / 10M out tokens.
#   gemini-1.5-flash       on google        $   6.75/mo
#   gemini-1.5-flash       on vertex        $   6.75/mo
#   gemini-2.0-flash       on google        $   9.00/mo
#   llama-3.1-8b           on bedrock       $  13.20/mo
#   gpt-4o-mini            on openai        $  13.50/mo
#   deepseek-v3            on deepseek      $  24.50/mo
```

```python
# Same model across all hosts — proves Claude 4 Sonnet provider parity
# (and surfaces that only Anthropic API publishes the 90%-off cache_read rate)
r = compare_token_pricing(model_id="claude-4-sonnet")
#   anthropic   in=$3/1M  out=$15/1M  cache_read=$0.30/1M  cache_write=$3.75/1M
#   bedrock     in=$3/1M  out=$15/1M
#   vertex      in=$3/1M  out=$15/1M
```

Covers 19 models across 8 providers: Claude (4 Opus / 4 Sonnet / 3.5 Haiku / 3 Haiku), GPT (5, 5 mini, 4o, 4o-mini, o1), Gemini (2.0 Flash, 1.5 Pro/Flash), Llama (3.1 8B/70B/405B, 3.3 70B), Mistral Large 2, DeepSeek V3/R1.

Real questions this unlocks:

> *"Cheapest model that handles 200K context for output-heavy chat at 10M/mo output volume?"*
> → AI calls `compare_token_pricing` with the volume + an optional model_family filter, returns ranked monthly cost across every viable model+provider combo.

> *"Should I use Anthropic API or Bedrock for Claude?"*
> → `compare_token_pricing(model_id="claude-4-sonnet")` shows price parity on per-token rates, but Anthropic API exposes a 10x cheaper cache_read rate that Bedrock doesn't publish. For caching-heavy workloads, Anthropic wins.

### Multi-cloud GPU pricing (v0.11.0+)

The fastest-growing cloud cost category — and nobody compares it cross-cloud openly.

```python
from cloudprice_mcp.finops.gpu import compare_gpu_workload
from cloudprice_mcp.pricing import load_catalog

r = compare_gpu_workload(load_catalog(), gpu_type="H100", gpu_count=8)
# OCI BM.GPU.H100.8 is cheapest at $80.0000/h for 8x H100.
#   oci    BM.GPU.H100.8                  $ 80.0000/h  $10.0000/GPU/h
#   gcp    a3-highgpu-8g                  $ 84.4000/h  $10.5500/GPU/h
#   aws    p5.48xlarge                    $ 98.3200/h  $12.2900/GPU/h
#   azure  ND96isr_H100_v5                $ 98.3200/h  $12.2900/GPU/h
```

Covers NVIDIA T4, A10, A10G, L4, L40S, V100, A100, H100 across all 4 clouds. Returns:

- **Absolute hourly winner** — the cheapest matching SKU per cloud
- **Per-GPU efficiency winner** — sometimes a different cloud (e.g., OCI's BM.GPU4.8 is cheapest *per GPU* but only sold as 8x, so for `gpu_count=1` Azure/GCP win the absolute ranking)
- **Over-provisioning flags** — when the only matching SKU bundles more GPUs than asked for
- **GPU memory** — differentiates A100 40GB vs 80GB (same `gpu_type` field)

The OCI H100 finding is real: at 8x H100 it's ~19% cheaper than AWS/Azure for identical hardware.

### Cost Drift Sentinel (v0.9.0+)

The shift from query tool to **agent capability**. Most FinOps tools answer *"what does this cost?"* — this one answers *"is this still what it cost when I signed off on it?"*

```python
from cloudprice_mcp.finops.sentinel import watch_workload
from cloudprice_mcp.inventory import parse_dict
from cloudprice_mcp.pricing import load_catalog

# First call — captures a baseline. Persist the returned baseline JSON.
result = watch_workload(load_catalog(), parse_dict(workload_spec))
save(result["baseline"])

# Later — pass the baseline back to detect drift.
report = watch_workload(load_catalog(), parse_dict(workload_spec), baseline=load_baseline())
if report["alert_triggered"]:
    notify_humans(report["headline"])
```

**Stateless by design** — no server, no database. The baseline JSON lives wherever you want: a file in your IaC repo, S3, Slack DM, anywhere. Each call is a pure function of `(catalog, workload, baseline)`.

Key properties:
- **Workload-hash protected** — if you change the workload spec, the hash mismatches and you get a fresh baseline rather than a misleading drift report
- **SKU-level attribution** — the drift report consults the price-history dataset and surfaces which SKUs moved the most between baseline and now
- **Configurable threshold** — default 5%; pass `alert_threshold_pct=N` to tune

**Plug-and-play GitHub Action template** at [`examples/cloudprice-watch.yml`](examples/cloudprice-watch.yml) — drop it in any IaC repo with a `workload.json`, get auto-opened GitHub issues when costs drift. Baseline is committed to your repo so the history is auditable.

### Carbon-aware FinOps (v0.10.0+) — kg CO2e per workload, alongside USD

The only FinOps MCP tool that returns **both cost AND carbon footprint** on the same query. AWS / Azure / GCP each publish their own customer dashboards (Customer Carbon Footprint Tool, Emissions Impact Dashboard, Carbon Footprint) — but none compare across providers. cloudprice does.

```python
from cloudprice_mcp.finops.carbon import compare_carbon_footprint
from cloudprice_mcp.pricing import load_catalog

result = compare_carbon_footprint(load_catalog(), vcpus=8, memory_gb=32, quantity=6)
# Returns per-cloud SKU + cost + power class (x86/ARM) + monthly kWh +
# grid-based kg CO2e + market-based residual kg CO2e (after renewable matching),
# ranked cheapest-carbon-first.
```

What's modeled (and disclosed in every response):
- **PUE per cloud** from each provider's public sustainability report
- **Grid carbon intensity per region** from public emissions data (EPA eGRID for US, similar baselines elsewhere)
- **Renewable matching** per provider (AWS/Azure 100%, GCP ~64% CFE, OCI unmatched outside EU)
- **ARM vs x86 power class** — ARM SKUs (Graviton, Ampere, Axion) modeled at ~30% lower per-vCPU power
- **Two carbon numbers per cloud**: location-based (grid) AND market-based (residual after renewable matching) — both surfaced so auditors can see both perspectives

What's NOT modeled (always disclosed via `honest_gaps[]`):
- Embodied carbon (server manufacturing) — operational only
- GPU / network / storage power — compute + memory only
- Time-of-use grid variation — annual averages only
- Real-time 24/7 CFE matching — GCP publishes annual CFE %; cloudprice uses that

Real questions this unlocks:

> *"What's the lowest-carbon cloud for 4 vCPU / 16 GB at 6 instances?"*
> → AI calls `compare_carbon_footprint`, returns per-cloud kg CO2e/mo ranked.

> *"How much carbon do I save running on ARM vs x86?"*
> → AI calls it twice with the same shape but different target SKUs.

### What's NOT modeled (real-world TCO killers)
- ✅ ~~Egress / data transfer~~ — **modeled in v0.5** (`compare_egress`)
- ✅ ~~Multi-AZ / HA replicas~~ — **modeled in v0.5** (`multi_az: true` on `compare_workload`)
- ✅ ~~Snapshots upper-bound only~~ — **fixed in v0.5** (`snapshot_incremental_factor`)
- **Reserved/Savings Plan SKU detail** (we apply a flat tier discount, not per-region/per-family detail) — roadmap
- **Multi-region pricing** (currently us-east only; us-west / eu-west planned for v0.5.1) — roadmap
- **IOPS-based storage matching** (capacity-only) — roadmap
- **Backup storage charges** (some clouds free, others billed) — roadmap
- **Request costs** (PUT/GET pricing for object storage) — roadmap
- **Retrieval costs** for archive tiers (Glacier-style retrieval can be 10× the storage cost) — roadmap
- **VPC peering / interconnect costs** — roadmap

These are tracked roadmap items. **Use cloudprice-mcp for the on-demand list-price baseline; do final TCO analysis with each cloud's own calculator before relying on numbers for big decisions.**

**Live runtime pricing (not just weekly refresh)** is being considered for v0.8 — would fetch prices directly at MCP tool invocation time instead of from the bundled catalog. Trade-offs: slower (network call per tool use), adds GCP auth requirement, breaks offline mode. The v0.7 weekly auto-refresh covers ~95% of the credibility win at zero runtime cost; live mode is opt-in territory.

## Develop locally

```bash
git clone https://github.com/Albaker-Group/cloudprice-mcp.git
cd cloudprice-mcp
pip install -e ".[dev]"
pytest
```

To point Claude Desktop at your dev copy, swap the `command` in the config:

```json
{
  "mcpServers": {
    "cloudprice": {
      "command": "python",
      "args": ["-m", "cloudprice_mcp.server"]
    }
  }
}
```

## License

MIT — see [LICENSE](LICENSE).

## Credits

Built by [Ali Albaker](https://cloud.albaker.info), multi-cloud architect — runs a live three-cloud portfolio at ~$1.80/month across AWS, Azure, and GCP, with OCI joining as the 4th cloud in 2026.
