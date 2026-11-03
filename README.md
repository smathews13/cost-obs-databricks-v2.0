# **cost-obs** app for Databricks

[![Deploy to Databricks](https://img.shields.io/badge/Deploy%20to-Databricks-FF3621?style=for-the-badge&logo=databricks&logoColor=white)](https://accounts.cloud.databricks.com/select-workspace?destination_url=/apps/install?repo_url=https://github.com/smathews13/cost-obs-databricks-v2.0)

> **🧪 Release status: 2.1 Beta — select customers**
> cost-obs 2.1 is in beta testing with a select group of customers. It is feature-complete and deployed to production-like workspaces, but expect fixes to land frequently. Coordinate with your Databricks account team before rolling it out broadly, and report issues through them. For the generally available release, use [cost-obs 1.x](https://github.com/smathews13/cost-obs-databricks).

> **⚠️ Not Official Databricks Software**
> This application is built and maintained by the Databricks field engineering team and is **not an official Databricks product**. It is not covered by Databricks Support SLAs. Your Databricks account team can help you deploy, configure, and troubleshoot this app as part of your engagement.

> **🔧 Customization Notice**
> You are welcome to modify and customize this application's source code to fit your organization's requirements. However, be aware that local customizations may conflict with future upstream updates. We recommend tracking your changes in a fork and reviewing diffs carefully before pulling upstream updates.

---

A full-stack Databricks App for account-level compute cost visibility, chargeback, and anomaly detection across your entire Databricks platform.

Built on FastAPI + React, deployed as a [Databricks App](https://docs.databricks.com/en/dev-tools/databricks-apps/index.html) with service principal authentication and serverless compute built in. Supports **multi-cloud deployment** across AWS, Azure, and GCP with automatic cloud detection.

---

## What's new in 2.1 Beta (2026-10-23)

2.1 moves the app onto Lakebase, deploys everything through a Databricks Asset Bundle, and adds Genie and Unity Gateway cost visibility.

**Lakebase-first serving.** Dashboard tabs read pre-aggregated gold tables from a Lakebase (Postgres) copy instead of scanning system tables on a SQL warehouse. Gold tables are built in Unity Catalog and loaded into Lakebase by the app. If Lakebase is empty or unavailable, reads fall back to warehouse gold, so tabs keep working. App state (settings, user roles, pricing, alerts, webhooks, cloud connections, refresh logs and the response cache) also lives in Lakebase. The database sleeps after 5 idle minutes.

**Bundle-owned deployment.** One `databricks bundle deploy` provisions the app, Lakebase project, setup job, and optional Genie space. The setup job builds gold tables, publishes setup notebooks, and fails loudly when Lakebase or catalog access is missing. Fresh installs and 1.x → 2.1 upgrades are detected automatically.

**Unity Gateway tab (formerly AI Gateway).** Combines AI/ML product spend (the former AI/ML tab, now the Cost sub-tab) with governed model traffic from `system.ai_gateway.usage`: requests, tokens, latency, error rate, by model, by service, and top users. A Catalog sub-tab lists model and MCP services through the Unity Gateway SDK. Supports both the current and legacy usage-table column schemas.

**Genie tab.** Genie spend by surface (Genie Code, Genie One, Genie Agents), charged vs free tier, by workspace and over time. A Spaces sub-tab lists Genie spaces and recent conversations through the Genie SDK. The **Ask Genie** drawer answers cost questions against a bundled Genie space.

**Consistent tabs.** Every tab uses the same page header (date range and workspace chips), a methodology panel explaining where its numbers come from, shared KPI cards, and clear "unavailable" notices instead of fake `$0` values.

**Other 2.1 changes**
- Shared gold sources: point the app at gold tables shared from another workspace, with source filters and per-source capability notices.
- Account prices on Cloud Costs, matching v1.
- Region-scoped SQL and warehouse detail is flagged instead of mislabeled.
- Settings revamp ported from v1 (Access, Resources, Config, Permissions).
- `databricks-sdk` 0.155.0.

### Field-hardening ported from v1 1.1

Ports the v1 1.1 field-hardening pass into v2's `app/backend/` + `app/frontend/` package layout.

**Apps bundle materialized view.** New `daily_apps_summary` pre-aggregation at (usage_date, workspace_id, app_id, sku_name) grain filtered to `billing_origin_product = 'APPS'`. Powers all six APPS query slots (summary, per-app breakdown, timeseries, filtered averages, avg-cost-per-app, sku breakdown) via an MV fast path with automatic fallback to raw scans when the MV is unavailable.

**Tagging bundle fast paths.** `daily_tag_summary` MV now backs the tag statistics KPIs and coverage summary. The tagging bundle previously timed out around 75–90 s on large accounts on cold-warehouse first loads; the MV path is sub-second once warm.

**Reliability**
- **Concurrent MV rebuild lock.** All uvicorn workers now share `/tmp/cost-obs-mv-refresh.lock`. The startup refresh path previously fired `CREATE OR REPLACE TABLE AS SELECT` concurrently from every worker in the pod, producing `DELTA_METADATA_CHANGED` and `DELTA_CONCURRENT_APPEND` errors on the core MV tables. Startup and the nightly scheduler now mutex against each other on the same lock.
- **No more `$0` lockouts.** When a critical Apps bundle query timed out on a cold warehouse, the resulting zero-value response was being cached for the full 30 min TTL — even after the warehouse warmed up, users saw `$0` until the cache expired. Timed-out or all-zero responses now short-cache to 60 s so the next request recovers automatically.
- **Apps workspace-filter passthrough.** The Apps tab client-side workspace filter treated the "all workspaces selected" state as an active filter, which dropped registry apps that had no billing (empty `workspace_names`). Fixed to pass through when the selection equals `availableWorkspaces.length` or is empty, and to include apps without workspace data in partial-selection mode.

**Performance**
- **Uvicorn workers dropped from 4 to 2.** This app is nearly 100 % I/O bound; four workers on the Databricks Apps small tier were oversubscribing CPU and quadrupling per-worker `TTLCache` duplication + SDK client footprint for no gain. Each worker still uses its `ThreadPoolExecutor(10)` for per-request query parallelism.
- **Optimizer prefetches gated to tab visits.** `warehouse-health` and `warehouse-idle-time` were firing on Dashboard mount and starving concurrent bundles on cold start. They now fire only when `activeTab === "optimizer"`.
- **Chart flicker eliminated.** `isAnimationActive={false}` on Bar/Line/Area/Pie primitives across 14+ chart consumers removes the label flicker that appeared during Recharts' initial-render animation.

**UI polish**
- **WCAG AA contrast.** All non-placeholder `text-gray-400` usages promoted to `text-gray-500`.
- **Palette drift cleanup.** `sky-*`, `teal-*`, and `emerald-*` classes replaced with `blue-*` / `cyan-*` / `green-*` to align with the Databricks brand palette.

### Upgrading from 2.0

Redeploy the bundle: `databricks bundle deploy -t <target>` then `databricks bundle run cost_obs_setup -t <target>` if you want the setup job to verify. Setup is idempotent — each task checks `setup_state` before executing, so re-running is safe. The two new materialized views (`daily_apps_summary`, `daily_tag_summary`) auto-build on first startup when the app detects data older than 26 hours; the raw-scan fallbacks keep the Apps and Tagging tabs working the whole time. No back-fill, no manual SQL, no config change.

If you ran `databricks bundle destroy` first, the Lakebase project is only soft-deleted and keeps its name for about 7 days. Redeploy fails until you purge it, for example `databricks postgres delete-project projects/cost-obs-db --purge` using the target's Lakebase project name.

If your app used `COST_OBS_WORKSPACES` or any other setting from 2.0, that's preserved — 2.1 introduces no new required env vars or bundle variables.

### At-a-glance changelog

| Area | Change | Impact |
|---|---|---|
| Apps tab | `daily_apps_summary` MV fast path (6 slots) | Sub-second cold loads once MV is warm |
| Tagging tab | `daily_tag_summary` MV fast path | Fixes the 75–90 s timeout on large accounts |
| Cache | 60 s TTL on degraded/empty Apps bundle | No more 30-min `$0` lockouts after a cold-warehouse timeout |
| Reliability | Startup MV rebuild wraps `/tmp/cost-obs-mv-refresh.lock` | Kills `DELTA_METADATA_CHANGED` on pod restart |
| Apps tab | Client-side workspace filter pass-through | Registry apps show when "all workspaces" is selected |
| Runtime | Uvicorn workers 4 → 2 | Better cache hit rate, less CPU contention |
| Runtime | Optimize tab prefetches gated to activeTab | Faster first paint of Dashboard |
| UI | `isAnimationActive={false}` on Recharts primitives | No more label flicker on chart mount |
| UI | `text-gray-400` → `text-gray-500`, palette drift cleanup | WCAG AA + brand palette compliance |

---

## What's changed in the new deployment model

- **Setup is simpler.** The SQL warehouse is bound as an Apps resource and injected as `DATABRICKS_WAREHOUSE_ID`. Workspace scoping is controlled with `COST_OBS_WORKSPACES`. Customers no longer need to select a warehouse or change workspace scope inside the app UI.

- **Fake zeros are gone.** If a required grant is missing, the app shows that the affected metric is unavailable and points to the fix — instead of rendering a misleading $0.00 value.

- **Setup experience is consistent across tabs.** Permissions, config, and readiness share the same underlying state so customers do not see one tab report healthy while another shows stale errors after a grant or rebuild.

- **Diagnostics surface actionable next steps.** Instead of raw error details, the app maps failures to specific actions — **Grant SQL**, **Rebuild**, or **Configure** — so customers can move directly to the fix.

**Deploy from Git** is the recommended deployment path. The app's first-run flow is built around an environment check, permissions check, and table creation step.

---

## Deployment

### Setup Requirements

#### Required

| Requirement | Notes |
|---|---|
| **Databricks workspace with Apps enabled** | Deploy from Git is available in supported workspaces with Databricks Apps enabled |
| **Unity Catalog enabled** | Required — the app reads from UC system tables and stores app-managed tables in UC |
| **Access to core system tables** | At minimum: `system.billing.usage` and `system.billing.list_prices` |
| **SQL warehouse** | A warehouse must be bound to the app. The recommended path is a SQL warehouse resource bound as `sql-warehouse` so the app receives `DATABRICKS_WAREHOUSE_ID` automatically |
| **Workspace Admin role** | Needed to create the app and, if required, remediate warehouse access |
| **Metastore Admin role, or access to one** | Needed to run the required Unity Catalog system table grants during setup |

#### Recommended warehouse

Use a **Serverless SQL warehouse**. The app is designed to read through a bound warehouse resource — add a `sql-warehouse` resource in app configuration rather than relying on manual runtime switching.

A **Medium** warehouse is a sensible starting point for initial table creation and normal dashboard usage. If you use Pro or Classic instead, expect slower startup and rebuild times.

#### Optional system tables

These are not required for the initial deployment, but they unlock additional parts of the app:

| Optional access | What it enables |
|---|---|
| `system.query.history` | SQL Warehousing views, query attribution, and warehouse rightsizing |
| `system.compute.clusters` | Cluster metadata and compute-oriented breakdowns |
| `system.lakeflow.*` | Job and pipeline name resolution and workflow KPIs |
| `system.serving.served_entities` | Model serving and AI/ML enrichment |
| `system.access.workspaces_latest` | Human-readable workspace names |

---

### Step 1 — Create the app from Git

> **Workspace preview required:** If you do not see a **Git repository** option when creating an app, enable it first: go to **Settings → Workspace Previews**, find **"Deploy Databricks apps from Git repositories (Beta)"**, and toggle it **ON**. This is a per-workspace setting and requires workspace admin access.

1. In your Databricks workspace, open **Apps**
2. Click **Create app**
3. Select **Git repository** as the source
4. Enter the repository URL: `https://github.com/smathews13/cost-obs-databricks`
5. Use the `main` branch
6. Give the app a name such as `cost-observability`
7. Click **Create**

---

### Step 2 — Bind the SQL warehouse resource and add SQL scope

Bind a SQL warehouse resource in the Apps UI so the warehouse ID is injected automatically. This is the recommended pattern for Databricks Apps and avoids manual warehouse wiring.

1. Open the app's **Settings** page (three-dot menu on the app card)
2. In **Resources**, click **Add resource**
3. Choose **SQL warehouse**
4. Select the warehouse you want the app to use
5. Set the permission to **Can use**
6. Leave the resource key as `sql-warehouse`
7. Under **User authorization**, click **Add scope** and add the `sql` scope
8. Save the configuration

---

### Step 3 — Review environment variables

The app keeps the initial deployment as simple as possible. For most customers, only these three variables are relevant. They are set at deploy time and are not intended to be changed inside the app UI.

> The SQL warehouse, workspace scope, and table location are configured once and held constant. If you need to change them, delete the app and recreate it.

| Variable | Default | Change if… |
|---|---|---|
| `COST_OBS_CATALOG` | Set by setup wizard | You want to pre-configure the catalog instead of choosing it in the setup wizard |
| `COST_OBS_SCHEMA` | Set by setup wizard | You want to pre-configure the schema name |
| `COST_OBS_WORKSPACES` | All workspaces | You want to scope the app to a comma-separated list of workspace IDs |

**Lakebase resource added in the Apps UI:** the resource injects `PGHOST` but not `ENDPOINT_NAME`. The app looks the endpoint up from `PGHOST` on startup; if the app's service principal can't see the project, set `ENDPOINT_NAME` = `projects/<project>/branches/production/endpoints/primary` yourself. Bundle deploys set it automatically.

Keep the first deployment minimal. Add optional cloud-cost or advanced integrations only after the base app is healthy.

<details>
<summary>All environment variable overrides</summary>

| Variable | Default | Description |
|---|---|---|
| `DATABRICKS_HOST` | Auto-detected | Override the workspace URL if not picked up automatically |
| `DATABRICKS_HTTP_PATH` | Auto-detected from resource binding | Point to an existing warehouse, or omit to use the bound resource |
| `COST_OBS_CATALOG` | Set by setup wizard | Unity Catalog catalog for app-managed tables. When set, takes precedence over the wizard value |
| `COST_OBS_SCHEMA` | Set by setup wizard | Schema name for app-managed tables. When set, takes precedence over the wizard value |
| `COST_OBS_WORKSPACES` | All workspaces | Comma-separated workspace IDs to scope the dashboard |
| `AZURE_SUBSCRIPTION_ID` | — | Azure subscription ID (shown in account banner on Azure) |
| `SMTP_HOST` / `SMTP_*` | — | Email alert configuration |
| `AWS_COST_CATALOG` / `AWS_COST_SCHEMA` | `billing` / `aws` | AWS CUR actual cost tables |
| `AZURE_COST_CATALOG` / `AZURE_COST_SCHEMA` | `billing` / `azure` | Azure cost export tables |
| `DATABRICKS_TOKEN` | — | Service principal token override; not needed when deployed as a Databricks App |

</details>

---

### Step 4 — Deploy

1. Click **Deploy**
2. Wait for the app status to show **Running**
3. Open the app URL

The deployment installs dependencies, starts the backend, and serves the frontend. Once the app is running, the first-run setup flow begins.

---

### Step 5 — Complete first-run setup

On first open, the app checks the environment, validates permissions, and creates the app-managed tables.

#### 5a — Confirm readiness

The app shows whether the warehouse, core system tables, and optional feature dependencies are ready. If the app reports a missing dependency, remediate it before proceeding.

#### 5b — Complete the Setup Wizard

The built-in Setup Wizard handles grants and table creation on first run. You do not need to navigate into Settings manually for initial setup.

- **Grants:** The wizard attempts the required grants automatically using the permissions of the logged-in user. Click the grant button, then click **Re-check** to confirm they applied. If the automatic grant fails due to insufficient privileges, the wizard displays the SQL to copy and run manually as the appropriate admin.

- **Table build:** Once grants pass, the wizard prompts you to build the pre-aggregated tables used by the dashboard. Click **Build**. This runs in the background on your warehouse and typically takes 3–8 minutes.

- **Workspace filter:** If `COST_OBS_WORKSPACES` was not set at deploy time, the wizard prompts you to choose workspace scope before completion. If workspace IDs were set as an environment variable, this step is skipped.

If you need to re-apply grants or rebuild tables later, use **Settings → Permissions** and **Settings → Config**. These are the ongoing management surfaces after initial setup.

---

### Step 6 — Verify the deployment

After setup completes, confirm:

- The main billing views load
- Readiness is green for core dependencies
- Optional areas only show as unavailable if their supporting system tables were not granted
- No dashboard tile is blocked by a missing warehouse or system table grant

If anything is degraded, go to **Settings → Permissions** or **Settings → Config** to identify whether the issue is warehouse access, missing system table grants, missing app-managed tables, or a schema mismatch.

---

### Minimum access for end users

This deployment path uses the **app service principal** for SQL execution. End users do not need any additional authentication for normal app usage.

Use **Settings → Permissions** to manage who can administer or view the app.

---

### Troubleshooting

| Symptom | Likely cause | Recommended action |
|---|---|---|
| Warehouse access failure after deploy | Warehouse resource missing, wrong warehouse selected, or access drift | Verify the bound SQL warehouse resource; rerun the warehouse-related remediation SQL if prompted |
| Billing tabs show no data | Core system table grants not applied | Run the required runtime grants from **Settings → Permissions**, then click **Re-check** |
| Optional tabs are unavailable | Optional system tables (`system.query.history`, `system.compute.clusters`, etc.) were not granted | Grant the optional dependencies you want to enable, then re-check readiness |
| Rebuild required or schema mismatch | App-managed tables are missing or out of sync | Rebuild from **Settings → Config** |
| Deploy from Git option not visible | Workspace preview not enabled | Enable the Git deployment preview in **Settings → Workspace Previews** |
| Data looks stale | App-managed tables have not been refreshed recently | Refresh from **Settings → Config → Rebuild** |

---

## What It Does

### $DBU Spend
| Feature | Description |
|---|---|
| **Spend Over Time** | Daily spend timeseries by product category |
| **Spend by Product** | Horizontal bar chart with workspace filter — SQL, ETL, Interactive, Model Serving, AI Search, Fine-Tuning, AI Functions, Serverless |
| **Spend by SKU** | Top 10 SKUs with workspace filter |
| **Spend by User** | Top spenders by DBU cost |
| **Workspace Table** | Per-workspace cost breakdown with top products/users |
| **Interactive Compute** | All-purpose cluster usage by user, cluster, or notebook with historical toggle |
| **ETL Breakdown** | Jobs and SDP pipeline spend with type filters, pagination, and historical toggle |
| **Account Prices Toggle** | Apply a blended negotiated-rate discount (from `system.billing.account_prices`, private preview) to Overview and infrastructure spend. See [Account discounts](#account-discounts-and-negotiated-pricing) |

### SQL
| Feature | Description |
|---|---|
| **Query Spend by Source** | Daily cost timeseries by query source type (DBSQL, Genie, Dashboard, etc.) |
| **Warehouse Spend by Type** | Daily spend area chart segmented by Serverless/Pro/Classic |
| **Warehouses by Size** | Distribution of warehouses by size with workspace filter |
| **Top Users** | Highest-cost SQL users |
| **Query Source Breakdown** | Drill-down table by source type |
| **Most Expensive Queries** | Top queries with historical toggle, pagination, and query profile links |
| **Warehouse Rightsizing** | Automated recommendations to right-size overprovisioned warehouses based on `system.query.history` utilization heuristics |

### Unity Gateway
| Feature | Description |
|---|---|
| **Cost** | AI/ML product spend (see AI/ML features below) |
| **Gateway Overview** | Requests, tokens, latency, and error rate over time from `system.ai_gateway.usage` |
| **Models / Services / Top Users** | Traffic and estimated cost by destination model, gateway service, and requester |
| **Catalog** | Model and MCP services registered in Unity Gateway |

Requires `AI_GATEWAY_USAGE_TABLE` (usually `system.ai_gateway.usage`) for the traffic sub-tabs; the Cost sub-tab works without it.

### Genie
| Feature | Description |
|---|---|
| **Overview** | Genie spend by surface (Genie Code, Genie One, Genie Agents), charged vs free tier, and daily trend |
| **Workspaces** | Per-workspace Genie spend |
| **Spaces** | Genie spaces and recent conversations |
| **Ask Genie** | Natural-language cost questions answered by a bundled Genie space (`GENIE_SPACE_ID`) |

### AI/ML (Unity Gateway → Cost)
| Feature | Description |
|---|---|
| **AI/ML Spend Over Time** | Stacked area chart by AI/ML category |
| **Cost by Category** | Donut chart of spend distribution |
| **Top Serverless Endpoints** | Highest-cost inference endpoints |
| **ML Runtime Clusters** | Clusters running ML/GPU runtimes with hyperlinks, pagination, and historical toggle |
| **Agent Bricks** | Knowledge Assistants and other agent types with type filters, pagination, and historical toggle |

### Apps
| Feature | Description |
|---|---|
| **App Cost Dashboard** | Per-app spend with SKU breakdown drill-down |
| **Connected Artifacts** | Serving endpoints, SQL warehouses, and other resources used by apps |

### Tagging Hub
| Feature | Description |
|---|---|
| **Tag Coverage** | Tagged vs untagged spend ratio |
| **Spend by Tag** | Cost attribution by tag key/value pairs |
| **Spend by Key** | Horizontal bar chart of top tag keys |
| **Untagged Resources** | Clusters, jobs, pipelines, warehouses, and endpoints missing tags — with dynamic suggested tags per resource type, historical toggle, and pagination |

### Users
| Feature | Description |
|---|---|
| **Users by Spend** | Ranked list of users by total DBU cost across all products |
| **Spend Over Time per User** | Daily timeseries for any selected user |
| **Product Breakdown** | Cost split by product category per user |
| **User Growth Trend** | Active user count over time |

### KPIs & Trends
| Feature | Description |
|---|---|
| **Platform KPIs** | Total spend, DBUs, successful runs, active clusters, workspaces, models served |
| **KPI Drill-Downs** | Click any KPI to see daily/monthly trend lines in a modal |
| **Spend Anomalies** | Largest day-over-day spend changes with date search and AI analysis |

### Cloud Costs
| Feature | Description |
|---|---|
| **Multi-Cloud Support** | Auto-detects AWS, Azure, or GCP from workspace URL; displays cloud-specific logos, instance types, pricing links, and setup guides |
| **Infrastructure KPIs** | Total cloud cost, DBU hours, avg active clusters/day, avg cluster cost — all derived from billing data |
| **Cost Over Time** | Area chart of estimated infrastructure costs with instance family filter bubbles |
| **Instance Family Usage** | DBU hours by EC2 (AWS), VM series (Azure), or machine type (GCP) instance family |
| **Cluster Table** | Per-cluster cost attribution with instance types, pricing links, pagination, and historical toggle |
| **Actual Costs Integration** | Toggle between estimated and actual costs when AWS CUR 2.0, Azure Cost Management Export, or GCP Billing Export is configured |
| **Cloud Integration Wizard** | In-app 5-step setup guide for AWS, Azure, and GCP actual cost integration |
| **2025 Pricing** | Updated EC2 and Azure VM pricing covering: AWS m7i, r7i, c7i, i4i, g6; Azure Dv6, Ev5/v6, NC A100 v4, ND A100 v4, NVadsA10 v5 |

### Settings
| Feature | Description |
|---|---|
| **General** | Date range selection, display preferences, and automatic refresh schedule |
| **Configuration** | Table status and rebuild |
| **Permissions** | System table access checks and service principal grant management |
| **User Permissions** | Admin-only management of who has admin vs. read-only access to the app |
| **Account Pricing** | Toggle between standard list prices and negotiated account prices |

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Databricks App                        │
│                                                         │
│  ┌──────────────┐          ┌──────────────────────────┐ │
│  │  React + TS  │◄────────►│  FastAPI (4 workers)     │ │
│  │  Vite + TW   │  REST    │  18 routers              │ │
│  └──────────────┘          └──────────┬───────────────┘ │
│                                       │                 │
└───────────────────────────────────────┼─────────────────┘
                                        │ Databricks SDK
                      ┌─────────────────┼──────────────────┐
                      │                 ▼                  │
                      │   ┌──────────────────────────────┐ │
                      │   │  SQL Warehouse (Serverless)  │ │
                      │   └─────────────┬────────────────┘ │
                      │                 │                  │
                      │   ┌─────────────▼────────────────┐ │
                      │   │  system.billing.usage        │ │
                      │   │  system.billing.list_prices  │ │
                      │   │  system.billing.account_prices│ │
                      │   │  system.query.history        │ │
                      │   │  system.compute.*            │ │
                      │   │  system.lakeflow.*           │ │
                      │   │  system.serving.*            │ │
                      │   │  system.access.*             │ │
                      │   │  6 app-managed Delta tables  │ │
                      │   └──────────────────────────────┘ │
                      │         Databricks                 │
                      └────────────────────────────────────┘
                                        │
                      ┌─────────────────▼──────────────────┐
                      │  Delta tables (app-managed)         │
                      │  App state: alerts, permissions,    │
                      │  settings, user preferences         │
                      └────────────────────────────────────┘
```

### Authentication

All dashboard queries run as the app's **service principal** (SP). The SP is automatically granted access to the required system tables during the setup wizard's Permissions step. End users do not need any additional authentication for normal app usage.

The catalog and schema created during setup are owned by the SP. The installing user receives `USE CATALOG`, `USE SCHEMA`, `SELECT`, and `MANAGE` grants automatically — giving them full visibility in the Unity Catalog browser and the ability to re-grant the SP on future redeploys.

### Data Sources

All billing and compute data is **account-level** — queries run against Unity Catalog system tables which span all workspaces in the account.

| System Table | Usage |
|---|---|
| `system.billing.usage` | Core spend/DBU data for all products |
| `system.billing.list_prices` | Standard SKU pricing for cost calculation |
| `system.billing.account_prices` | Negotiated/discounted account-specific prices (private preview) |
| `system.query.history` | SQL query attribution, source tracking, and rightsizing signals |
| `system.compute.clusters` | Cluster metadata, names, owners, ML runtime detection |
| `system.compute.warehouses` | Warehouse names, types, sizes |
| `system.lakeflow.pipelines` | SDP pipeline name resolution |
| `system.lakeflow.jobs` | Job name resolution |
| `system.lakeflow.job_run_timeline` | Job success/failure tracking for KPIs |
| `system.serving.served_entities` | ML endpoint metadata |
| `system.access.workspaces_latest` | Workspace name resolution |

### Account discounts and negotiated pricing

**By default, every dollar figure is at list price**: usage from `system.billing.usage` multiplied by the current rate in `system.billing.list_prices`. Discounts linked to your Databricks account are not reflected unless you turn on account pricing.

To apply them, open **Settings → Experimental → Account Pricing** and enable **Use account prices**. The app then:

- reads your negotiated rates from `system.billing.account_prices` (private preview; the app's service principal needs `SELECT` on it),
- computes one usage-weighted blended ratio, `Σ usage × account price / Σ usage × list price`, over the selected date range and workspaces. SKUs without a negotiated rate count at list price,
- scales the **Overview** spend figures (summary, products, workspaces, trend) and **infrastructure** spend by that ratio. A banner shows the active discount.

Limits to be aware of:

- Other tabs (SQL, Unity Gateway / AI, Apps, Genie, Users & Groups, KPIs, Optimize, Tagging), budgets and alerts still show list prices.
- The ratio is blended, so per-product splits on the Overview assume the same discount on every SKU. If your discounts differ a lot by SKU, the total is right for the selected range but individual products are approximate.
- Only discounts expressed as per-SKU rates in `system.billing.account_prices` are captured. Commit-level credits, prepaid balances or invoice adjustments that don't appear there are not.
- If the table is unavailable or holds no rates for the SKUs you used, the banner says so and list prices are shown.

### App-Managed Tables

The setup wizard creates **6 pre-aggregated Delta tables** in the Unity Catalog location you configure. These are the only persistent objects the app creates in your environment.

| Table | What it stores | Rows (est.) |
|---|---|---|
| `daily_usage_summary` | Total DBUs + spend per day | ~365 |
| `daily_product_breakdown` | DBUs + spend per day × product category | ~3,600 |
| `daily_workspace_breakdown` | DBUs + spend per day × workspace | ~3,600–36,000 |
| `sql_tool_attribution` | Genie vs DBSQL spend split per day × warehouse | ~730–7,000 |
| `daily_query_stats` | Query count, rows read, compute time per day | ~365 |
| `dbsql_cost_per_query` | Per-query cost attribution for the last 90 days | ~90k–900k |

Tables are built automatically when the setup wizard completes. The dashboard works immediately using direct system table queries while the background build runs (typically 3–8 minutes), then switches to the pre-aggregated tables automatically.

### Keeping Tables Fresh

Tables are automatically refreshed on a nightly schedule (default: 05:00 UTC). The scheduler runs incremental updates using MERGE INTO, so only new data is processed after the initial full build — refresh times after the first run are typically under a minute for most deployments.

The refresh frequency and scheduled time are configurable under **Settings → General**. Options include nightly (default) and every 6 hours.

To rebuild on demand, go to **Settings → Config → Rebuild**. This triggers a full rebuild of all 6 tables from the latest `system.*` data and typically takes 3–8 minutes. Progress is shown in real time.

Tables can be dropped and recreated at any time with no data loss — all source data lives in `system.*` tables managed by Databricks.

### Performance Optimizations

| Optimization | Detail |
|---|---|
| **Pre-aggregated Tables** | 6 Delta tables for sub-second dashboard loads |
| **Parallel Query Execution** | `ThreadPoolExecutor` (10 workers) runs 6–8 queries concurrently per bundle endpoint |
| **4-Hour Query Cache** | `TTLCache` with 500 entries — cost data changes at most once per day |
| **SDK Call Caching** | Pipeline names, group membership, and app registry cached for 1 hour |
| **Bundle Endpoints** | Single API call returns all data for a tab (reduces HTTP round-trips) |
| **React Query** | 30-minute stale time, 1-hour GC — prevents redundant refetches |
| **Lazy-Loaded Chunks** | Each heavy tab (Cloud Costs, AI/ML, Tagging, etc.) is a separate JS chunk loaded on first visit |

---

## Cloud Cost Integration

The Cloud Costs tab displays estimated infrastructure costs out of the box. It can also show **actual** AWS, Azure, or GCP billing data when configured. Full step-by-step setup instructions are built into the app — open the Cloud Costs tab and click **Set Up Actual Costs** to launch the in-app wizard.

### AWS (CUR 2.0)

The app reads from `billing.aws.actuals_gold`. Setup steps are available in the in-app wizard, and the table location can be overridden via `AWS_COST_CATALOG` / `AWS_COST_SCHEMA`.

### Azure (Cost Management Export)

The app reads from `billing.azure.actuals_gold`. Setup steps are available in the in-app wizard, and the table location can be overridden via `AZURE_COST_CATALOG` / `AZURE_COST_SCHEMA`.

### GCP (BigQuery Billing Export)

The app reads directly from your GCP billing export synced into a Databricks catalog via [BigQuery data sharing](https://docs.databricks.com/aws/en/delta-sharing/share-data-databricks). The default table is `billing.gcp.gcp_billing_export_v1` — override via environment variables:

| Variable | Default | Description |
|---|---|---|
| `GCP_COST_CATALOG` | `billing` (or `COST_OBS_CATALOG`) | Catalog containing the GCP billing table |
| `GCP_COST_SCHEMA` | `gcp` | Schema containing the GCP billing table |
| `GCP_COST_TABLE` | `gcp_billing_export_v1` | Table name — use `gcp_billing_export_resource_v1` for resource-level detail, or `actuals_gold` if you have a pre-aggregated gold table |

Setup steps are available in the in-app wizard under Cloud Costs → Set Up Actual Costs → GCP.

---

## Security

- All dashboard API endpoints are authenticated by the Databricks Apps platform
- The `X-Forwarded-Email` header is used to identify the requesting user
- Settings mutation endpoints (cloud connections, webhook config, user permissions) require **admin role** — enforced server-side before any state change
- Webhook URLs are masked in API responses (never returned in plaintext after save)

---

## Project Structure

```
cost-obs-databricks/
├── server/                      # FastAPI backend
│   ├── app.py                   # Entry point, startup tasks, router registration
│   ├── db.py                    # SQL connector, 4h TTL query cache, connection pool
│   ├── materialized_views.py    # MV creation, refresh, and query templates
│   ├── alerting.py              # Spike detection logic
│   ├── alert_manager.py         # Alert persistence and delivery
│   ├── cloud_pricing.py         # EC2 / Azure VM pricing for cost estimates
│   ├── queries/
│   │   └── __init__.py          # Core billing SQL
│   └── routers/                 # 18 API route handlers
│       ├── billing.py           # Core spend, KPIs, user/product breakdowns
│       ├── dbsql.py             # SQL tab bundle
│       ├── warehouse_health.py  # Warehouse utilization and rightsizing
│       ├── aiml.py              # AI/ML cost center
│       ├── apps.py              # Databricks Apps cost tracking
│       ├── tagging.py           # Tag coverage and untagged resource surfacing
│       ├── aws_actual.py        # AWS CUR actual cost queries
│       ├── azure_actual.py      # Azure actual cost queries
│       ├── alerts.py            # Threshold alerts and notifications
│       ├── use_cases.py         # Deprecated use case tracking (not mounted)
│       ├── users_groups.py      # User spend analytics
│       ├── settings.py          # App config, cloud connections, user permissions
│       └── setup.py             # First-run setup wizard
│
├── client/                      # React frontend
│   └── src/
│       ├── App.tsx              # Main dashboard (12 tabs, lazy-loaded chunks)
│       └── components/          # 30+ components
│
├── static/                      # Pre-built frontend assets (committed for git deployments)
├── app.yaml                     # Databricks Apps config with environment variables
├── app.yaml.example             # Environment variable template
├── pyproject.toml               # Python dependencies
└── docs/                        # Setup guides and architecture docs
```

---

## API Overview

The backend exposes a REST API at `/api/`. Key endpoints:

| Endpoint | Description |
|---|---|
| `GET /api/billing/dashboard-bundle-fast` | All DBU overview data in one parallel call |
| `GET /api/billing/by-product` | Spend by product category with workspace filter |
| `GET /api/billing/sku-breakdown` | Top SKUs with workspace filter |
| `GET /api/billing/spend-by-user-group` | Top users by spend |
| `GET /api/billing/infra-bundle` | Cloud cost estimates with billing-derived KPIs |
| `GET /api/dbsql/dashboard-bundle` | SQL tab data (sources, users, warehouses, queries) |
| `GET /api/warehouse-health/recommendations` | Rightsizing recommendations |
| `GET /api/aws-actual/dashboard-bundle` | AWS CUR actual cost data bundle |
| `GET /api/azure-actual/dashboard-bundle` | Azure actual cost data bundle |
| `GET /api/aiml/dashboard-bundle` | AI/ML cost center data |
| `GET /api/apps/dashboard-bundle` | Apps cost data |
| `GET /api/tagging/dashboard-bundle` | Tagging hub data |
| `GET /api/billing/platform-kpis-bundle` | Platform KPIs and anomalies |
| `GET /api/users-groups/bundle` | User spend analytics |
| `GET /api/health` | Health check |

Full interactive API docs at `http://localhost:8000/docs` (FastAPI Swagger UI).

---

## Tech Stack

| Layer | Technology |
|---|---|
| Frontend | React 19, TypeScript, Vite, Tailwind CSS 4, Recharts, TanStack Query v5 |
| Backend | Python 3.11+, FastAPI, Databricks SQL Connector, Databricks SDK 0.81+ |
| Data | Databricks system tables (account-level), Unity Catalog, Delta materialized views |
| Persistence | Delta materialized views (app-managed), TTL in-memory cache |
| Deployment | Databricks Apps (service principal auth, serverless compute), multi-cloud (AWS + Azure) |
| Caching | TTLCache (4h query cache, 1h SDK cache), React Query (30min stale time) |
