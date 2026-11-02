"""Gold tables for remaining viewer surfaces (warehouse-built, Lakebase-served)."""

from __future__ import annotations

import os


def _merge(table: str, pk: list[str], src_sql: str) -> str:
    parts = []
    for c in pk:
        if c in ("usage_date", "snapshot_date"):
            parts.append(f"tgt.{c} = src.{c}")
        else:
            parts.append(f"COALESCE(CAST(tgt.{c} AS STRING), '') = COALESCE(CAST(src.{c} AS STRING), '')")
    on = "\n  AND ".join(parts)
    return f"""MERGE INTO `{{catalog}}`.`{{schema}}`.`{table}` AS tgt
USING (
{src_sql}
) AS src
ON {on}
WHEN MATCHED THEN UPDATE SET *
WHEN NOT MATCHED BY TARGET THEN INSERT *
"""


def _idle_body(date_start: str) -> str:
    return f"""
  WITH date_spine AS (
    SELECT explode(sequence({date_start}, CURRENT_DATE(), INTERVAL 1 DAY)) AS usage_date
  ),
  all_events AS (
    SELECT warehouse_id, event_time, cluster_count
    FROM system.compute.warehouse_events
    WHERE event_time >= {date_start} - INTERVAL 7 DAYS
      AND event_time < CURRENT_DATE() + INTERVAL 1 DAY
  ),
  events_with_next AS (
    SELECT warehouse_id, event_time, cluster_count,
      LEAD(event_time) OVER (PARTITION BY warehouse_id ORDER BY event_time) AS next_event_time
    FROM all_events
  ),
  running_windows AS (
    SELECT warehouse_id, event_time AS window_start,
      COALESCE(next_event_time, CURRENT_DATE() + INTERVAL 1 DAY) AS window_end
    FROM events_with_next
    WHERE cluster_count > 0
  ),
  daily_running AS (
    SELECT w.warehouse_id, d.usage_date,
      GREATEST(w.window_start, CAST(d.usage_date AS TIMESTAMP)) AS window_start,
      LEAST(w.window_end, CAST(d.usage_date AS TIMESTAMP) + INTERVAL 1 DAY) AS window_end
    FROM running_windows w
    JOIN date_spine d
      ON w.window_end > CAST(d.usage_date AS TIMESTAMP)
     AND w.window_start < CAST(d.usage_date AS TIMESTAMP) + INTERVAL 1 DAY
    WHERE GREATEST(w.window_start, CAST(d.usage_date AS TIMESTAMP))
        < LEAST(w.window_end, CAST(d.usage_date AS TIMESTAMP) + INTERVAL 1 DAY)
  ),
  warehouse_uptime AS (
    SELECT warehouse_id, usage_date,
      SUM((UNIX_TIMESTAMP(window_end) - UNIX_TIMESTAMP(window_start)) / 60.0) AS total_running_minutes
    FROM daily_running
    GROUP BY warehouse_id, usage_date
  ),
  billing_agg AS (
    SELECT
      u.usage_date,
      u.usage_metadata.warehouse_id AS warehouse_id,
      LEAST(
        SUM((UNIX_TIMESTAMP(LEAST(u.usage_end_time, CAST(u.usage_date AS TIMESTAMP) + INTERVAL 1 DAY))
             - UNIX_TIMESTAMP(GREATEST(u.usage_start_time, CAST(u.usage_date AS TIMESTAMP)))) / 60.0),
        1440.0
      ) AS billing_running_minutes,
      SUM(u.usage_quantity * COALESCE(p.pricing.default, 0)) AS total_spend
    FROM system.billing.usage u
    LEFT JOIN system.billing.list_prices p
      ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND p.price_end_time IS NULL
    WHERE u.usage_date >= {date_start} AND u.usage_date <= CURRENT_DATE()
      AND u.usage_metadata.warehouse_id IS NOT NULL
      AND u.usage_quantity > 0
    GROUP BY u.usage_date, u.usage_metadata.warehouse_id
  ),
  combined_uptime AS (
    SELECT
      COALESCE(e.warehouse_id, b.warehouse_id) AS warehouse_id,
      COALESCE(e.usage_date, b.usage_date) AS usage_date,
      COALESCE(e.total_running_minutes, b.billing_running_minutes) AS total_running_minutes,
      CASE WHEN e.warehouse_id IS NOT NULL THEN 'events' ELSE 'billing' END AS uptime_source
    FROM warehouse_uptime e
    FULL OUTER JOIN billing_agg b
      ON e.warehouse_id = b.warehouse_id AND e.usage_date = b.usage_date
  ),
  wh_info AS (
    SELECT warehouse_id, warehouse_name, warehouse_size, workspace_id, warehouse_type,
      COALESCE(auto_stop_minutes, 10) AS auto_stop_mins,
      COALESCE(max_clusters, 1) AS max_num_clusters
    FROM system.compute.warehouses
    QUALIFY ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC) = 1
  ),
  qw_raw AS (
    SELECT
      qh.compute.warehouse_id AS warehouse_id,
      DATE(GREATEST(qh.start_time, CAST({date_start} AS TIMESTAMP))) AS usage_date,
      GREATEST(qh.start_time, CAST(DATE(qh.start_time) AS TIMESTAMP)) AS w_start,
      LEAST(COALESCE(qh.end_time, CAST(DATE(qh.start_time) AS TIMESTAMP) + INTERVAL 1 DAY),
            CAST(DATE(qh.start_time) AS TIMESTAMP) + INTERVAL 1 DAY) AS w_end
    FROM system.query.history qh
    WHERE qh.start_time >= CAST({date_start} AS TIMESTAMP)
      AND qh.start_time < CURRENT_DATE() + INTERVAL 1 DAY
      AND qh.compute.warehouse_id IS NOT NULL
      AND COALESCE(qh.end_time, CURRENT_TIMESTAMP()) > CAST(DATE(qh.start_time) AS TIMESTAMP)
  ),
  qw_flagged AS (
    SELECT warehouse_id, usage_date, w_start, w_end,
      MAX(w_end) OVER (
        PARTITION BY warehouse_id, usage_date ORDER BY w_start
        ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
      ) AS prev_max_end
    FROM qw_raw
  ),
  qw_grouped AS (
    SELECT warehouse_id, usage_date, w_start, w_end,
      SUM(CASE WHEN prev_max_end IS NULL OR w_start > prev_max_end THEN 1 ELSE 0 END)
        OVER (PARTITION BY warehouse_id, usage_date ORDER BY w_start
              ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS grp
    FROM qw_flagged
  ),
  qw_merged AS (
    SELECT warehouse_id, usage_date, grp, MIN(w_start) AS grp_start, MAX(w_end) AS grp_end
    FROM qw_grouped
    GROUP BY warehouse_id, usage_date, grp
  ),
  busy_union AS (
    SELECT warehouse_id, usage_date,
      SUM(GREATEST((UNIX_TIMESTAMP(grp_end) - UNIX_TIMESTAMP(grp_start)) / 60.0, 0)) AS busy_union_minutes
    FROM qw_merged
    GROUP BY warehouse_id, usage_date
  ),
  qw_with_lag AS (
    SELECT qm.warehouse_id, qm.usage_date, qm.grp_start, qm.grp_end,
      LAG(qm.grp_end) OVER (PARTITION BY qm.warehouse_id, qm.usage_date ORDER BY qm.grp_start) AS prev_end,
      wh.auto_stop_mins
    FROM qw_merged qm
    JOIN wh_info wh ON qm.warehouse_id = wh.warehouse_id
  ),
  warm_hold AS (
    SELECT warehouse_id, usage_date,
      SUM(GREATEST(LEAST((UNIX_TIMESTAMP(grp_start) - UNIX_TIMESTAMP(prev_end)) / 60.0, auto_stop_mins * 1.0), 0)) AS warm_hold_minutes
    FROM qw_with_lag
    WHERE prev_end IS NOT NULL
    GROUP BY warehouse_id, usage_date
  )
  SELECT
    u.usage_date,
    u.warehouse_id,
    COALESCE(i.warehouse_name, u.warehouse_id) AS warehouse_name,
    COALESCE(i.warehouse_size, 'Unknown') AS warehouse_size,
    CASE WHEN i.warehouse_type = 'SERVERLESS' THEN 'SERVERLESS' ELSE 'CLASSIC' END AS warehouse_type,
    i.workspace_id,
    u.uptime_source,
    u.total_running_minutes,
    COALESCE(bu.busy_union_minutes, 0) AS busy_union_minutes,
    GREATEST(u.total_running_minutes - COALESCE(bu.busy_union_minutes, 0), 0) AS idle_minutes,
    COALESCE(wh.warm_hold_minutes, 0) AS warm_hold_minutes,
    COALESCE(i.auto_stop_mins, 10) AS auto_stop_mins,
    COALESCE(i.max_num_clusters, 1) AS max_num_clusters,
    COALESCE(c.total_spend, 0) AS total_spend,
    CASE
      WHEN COALESCE(i.warehouse_type, 'CLASSIC') != 'SERVERLESS'
        AND i.max_num_clusters <= 1
        AND u.uptime_source = 'events'
        AND u.total_running_minutes > 0
      THEN COALESCE(c.total_spend, 0)
           * GREATEST(u.total_running_minutes - COALESCE(bu.busy_union_minutes, 0), 0)
           / u.total_running_minutes
      ELSE CAST(NULL AS DOUBLE)
    END AS estimated_idle_spend
  FROM combined_uptime u
  LEFT JOIN busy_union bu ON u.warehouse_id = bu.warehouse_id AND u.usage_date = bu.usage_date
  LEFT JOIN warm_hold wh ON u.warehouse_id = wh.warehouse_id AND u.usage_date = wh.usage_date
  JOIN wh_info i ON u.warehouse_id = i.warehouse_id
  LEFT JOIN billing_agg c ON u.warehouse_id = c.warehouse_id AND u.usage_date = c.usage_date
  WHERE u.total_running_minutes >= 1
"""


CREATE_DAILY_WAREHOUSE_IDLE = f"""
CREATE OR REPLACE TABLE `{{catalog}}`.`{{schema}}`.`daily_warehouse_idle` CLUSTER BY (usage_date, warehouse_id) AS
{_idle_body("DATE_SUB(CURRENT_DATE(), {billing_lookback_days})")}
"""

MERGE_DAILY_WAREHOUSE_IDLE = _merge(
    "daily_warehouse_idle",
    ["usage_date", "warehouse_id"],
    _idle_body("DATE('{reprocess_start}')"),
)

_KPI_FACTS_BODY = """
  SELECT
    u.usage_date,
    u.workspace_id,
    COUNT(DISTINCT CASE WHEN u.usage_metadata.job_id IS NOT NULL THEN CAST(u.usage_metadata.job_id AS STRING) END) AS distinct_jobs,
    SUM(CASE WHEN u.usage_metadata.job_id IS NOT NULL THEN 1 ELSE 0 END) AS job_run_rows,
    COUNT(DISTINCT CASE WHEN u.usage_metadata.job_id IS NOT NULL THEN u.identity_metadata.run_as END) AS distinct_job_owners,
    COUNT(DISTINCT CAST(u.usage_metadata.cluster_id AS STRING)) AS distinct_clusters,
    COUNT(DISTINCT CASE WHEN u.billing_origin_product = 'SQL' THEN CAST(u.usage_metadata.warehouse_id AS STRING) END) AS distinct_sql_warehouses,
    SUM(CASE WHEN u.billing_origin_product = 'SQL' THEN u.usage_quantity ELSE 0 END) AS sql_dbus,
    COUNT(DISTINCT CASE WHEN u.sku_name LIKE '%INFERENCE%' THEN u.usage_metadata.endpoint_name END) AS distinct_models,
    SUM(CASE WHEN u.sku_name LIKE '%INFERENCE%' THEN u.usage_quantity ELSE 0 END) AS serving_dbus
  FROM system.billing.usage u
  WHERE {date_pred}
    AND u.usage_quantity > 0
  GROUP BY 1, 2
"""

CREATE_DAILY_KPI_FACTS = f"""
CREATE OR REPLACE TABLE `{{catalog}}`.`{{schema}}`.`daily_kpi_facts` CLUSTER BY (usage_date, workspace_id) AS
{_KPI_FACTS_BODY.format(date_pred="u.usage_date >= DATE_SUB(CURRENT_DATE(), {billing_lookback_days})")}
"""

MERGE_DAILY_KPI_FACTS = _merge(
    "daily_kpi_facts",
    ["usage_date", "workspace_id"],
    _KPI_FACTS_BODY.format(date_pred="u.usage_date >= DATE('{reprocess_start}') AND u.usage_date <= CURRENT_DATE()"),
)

CREATE_DAILY_LAKEFLOW_STATS = """
CREATE OR REPLACE TABLE `{catalog}`.`{schema}`.`daily_lakeflow_stats` CLUSTER BY (usage_date, workspace_id) AS
SELECT
  DATE(period_start_time) AS usage_date,
  workspace_id,
  COUNT(*) AS total_runs,
  COUNT(CASE WHEN result_state = 'SUCCEEDED' THEN 1 END) AS successful_runs,
  COALESCE(SUM((unix_timestamp(period_end_time) - unix_timestamp(period_start_time)) / 3600.0), 0) AS total_run_hours
FROM system.lakeflow.job_run_timeline
WHERE DATE(period_start_time) >= DATE_SUB(CURRENT_DATE(), {billing_lookback_days})
  AND period_end_time IS NOT NULL
GROUP BY 1, 2
"""

MERGE_DAILY_LAKEFLOW_STATS = _merge(
    "daily_lakeflow_stats",
    ["usage_date", "workspace_id"],
    """
  SELECT
    DATE(period_start_time) AS usage_date,
    workspace_id,
    COUNT(*) AS total_runs,
    COUNT(CASE WHEN result_state = 'SUCCEEDED' THEN 1 END) AS successful_runs,
    COALESCE(SUM((unix_timestamp(period_end_time) - unix_timestamp(period_start_time)) / 3600.0), 0) AS total_run_hours
  FROM system.lakeflow.job_run_timeline
  WHERE DATE(period_start_time) >= DATE('{reprocess_start}')
    AND DATE(period_start_time) <= CURRENT_DATE()
    AND period_end_time IS NOT NULL
  GROUP BY 1, 2
""",
)

CREATE_DAILY_QUERY_USERS = """
CREATE OR REPLACE TABLE `{catalog}`.`{schema}`.`daily_query_users` CLUSTER BY (usage_date, workspace_id) AS
SELECT
  DATE(start_time) AS usage_date,
  workspace_id,
  COALESCE(executed_by, CAST(executed_as_user_id AS STRING)) AS user_id
FROM system.query.history
WHERE DATE(start_time) >= DATE_SUB(CURRENT_DATE(), {billing_lookback_days})
  AND (executed_by IS NOT NULL OR executed_as_user_id IS NOT NULL)
GROUP BY 1, 2, 3
"""

MERGE_DAILY_QUERY_USERS = _merge(
    "daily_query_users",
    ["usage_date", "workspace_id", "user_id"],
    """
  SELECT
    DATE(start_time) AS usage_date,
    workspace_id,
    COALESCE(executed_by, CAST(executed_as_user_id AS STRING)) AS user_id
  FROM system.query.history
  WHERE DATE(start_time) >= DATE('{reprocess_start}')
    AND DATE(start_time) <= CURRENT_DATE()
    AND (executed_by IS NOT NULL OR executed_as_user_id IS NOT NULL)
  GROUP BY 1, 2, 3
""",
)

CREATE_DAILY_APPS_IDENTITIES = """
CREATE OR REPLACE TABLE `{catalog}`.`{schema}`.`daily_apps_identities` CLUSTER BY (usage_date, workspace_id) AS
SELECT
  u.usage_date,
  u.workspace_id,
  COALESCE(u.usage_metadata.app_id, 'Unknown') AS app_id,
  u.identity_metadata.run_as AS run_as
FROM system.billing.usage u
WHERE u.usage_date >= DATE_SUB(CURRENT_DATE(), {billing_lookback_days})
  AND u.usage_quantity > 0
  AND u.billing_origin_product = 'APPS'
  AND u.identity_metadata.run_as IS NOT NULL
GROUP BY 1, 2, 3, 4
"""

MERGE_DAILY_APPS_IDENTITIES = _merge(
    "daily_apps_identities",
    ["usage_date", "workspace_id", "app_id", "run_as"],
    """
  SELECT
    u.usage_date,
    u.workspace_id,
    COALESCE(u.usage_metadata.app_id, 'Unknown') AS app_id,
    u.identity_metadata.run_as AS run_as
  FROM system.billing.usage u
  WHERE u.usage_date >= DATE('{reprocess_start}')
    AND u.usage_date <= CURRENT_DATE()
    AND u.usage_quantity > 0
    AND u.billing_origin_product = 'APPS'
    AND u.identity_metadata.run_as IS NOT NULL
  GROUP BY 1, 2, 3, 4
""",
)

CREATE_USE_CASE_QUERY_OBJECTS = """
CREATE OR REPLACE TABLE `{catalog}`.`{schema}`.`use_case_query_objects` AS
SELECT CURRENT_DATE() AS snapshot_date, statement_id AS object_id,
  SUBSTRING(COALESCE(statement_text, CONCAT('Query ', statement_id)), 1, 100) AS object_name,
  workspace_id
FROM (
  SELECT statement_id, statement_text, workspace_id, start_time,
    ROW_NUMBER() OVER (ORDER BY start_time DESC) AS rn
  FROM system.query.history
  WHERE statement_id IS NOT NULL
    AND start_time >= DATE_SUB(CURRENT_DATE(), 30)
) t
WHERE rn <= 100
"""

CREATE_USE_CASE_DASHBOARD_OBJECTS = """
CREATE OR REPLACE TABLE `{catalog}`.`{schema}`.`use_case_dashboard_objects` AS
SELECT CURRENT_DATE() AS snapshot_date, dashboard_id AS object_id, name AS object_name, workspace_id
FROM system.dashboards.dashboards
WHERE dashboard_id IS NOT NULL
"""

CREATE_DAILY_AZURE_ACTUALS = """
CREATE OR REPLACE TABLE `{catalog}`.`{schema}`.`daily_azure_actuals` CLUSTER BY (usage_date) AS
SELECT
  DATE(usage_date) AS usage_date,
  usage_metadata.cluster_id AS cluster_id,
  usage_metadata.warehouse_id AS warehouse_id,
  charge_type,
  SUM(cost_in_billing_currency) AS total_cost,
  SUM(cost_in_usd) AS total_cost_usd
FROM `{azure_catalog}`.`{azure_schema}`.`actuals_gold`
WHERE DATE(usage_date) >= DATE_SUB(CURRENT_DATE(), {billing_lookback_days})
GROUP BY 1, 2, 3, 4
"""

MERGE_DAILY_AZURE_ACTUALS = """
MERGE INTO `{catalog}`.`{schema}`.`daily_azure_actuals` AS tgt
USING (
  SELECT
    DATE(usage_date) AS usage_date,
    usage_metadata.cluster_id AS cluster_id,
    usage_metadata.warehouse_id AS warehouse_id,
    charge_type,
    SUM(cost_in_billing_currency) AS total_cost,
    SUM(cost_in_usd) AS total_cost_usd
  FROM `{azure_catalog}`.`{azure_schema}`.`actuals_gold`
  WHERE DATE(usage_date) >= DATE('{reprocess_start}')
    AND DATE(usage_date) <= CURRENT_DATE()
  GROUP BY 1, 2, 3, 4
) AS src
ON tgt.usage_date = src.usage_date
  AND COALESCE(CAST(tgt.cluster_id AS STRING), '') = COALESCE(CAST(src.cluster_id AS STRING), '')
  AND COALESCE(CAST(tgt.warehouse_id AS STRING), '') = COALESCE(CAST(src.warehouse_id AS STRING), '')
  AND COALESCE(tgt.charge_type, '') = COALESCE(src.charge_type, '')
WHEN MATCHED THEN UPDATE SET *
WHEN NOT MATCHED BY TARGET THEN INSERT *
"""

CREATE_DAILY_GCP_ACTUALS = """
CREATE OR REPLACE TABLE `{catalog}`.`{schema}`.`daily_gcp_actuals` CLUSTER BY (usage_date) AS
SELECT
  DATE(usage_start_time) AS usage_date,
  project.id AS project_id,
  project.name AS project_name,
  service.description AS service,
  sku.description AS sku,
  currency,
  SUM(cost) AS total_cost
FROM `{gcp_catalog}`.`{gcp_schema}`.`{gcp_table}`
WHERE DATE(usage_start_time) >= DATE_SUB(CURRENT_DATE(), {billing_lookback_days})
  AND cost > 0
GROUP BY 1, 2, 3, 4, 5, 6
"""

MERGE_DAILY_GCP_ACTUALS = """
MERGE INTO `{catalog}`.`{schema}`.`daily_gcp_actuals` AS tgt
USING (
  SELECT
    DATE(usage_start_time) AS usage_date,
    project.id AS project_id,
    project.name AS project_name,
    service.description AS service,
    sku.description AS sku,
    currency,
    SUM(cost) AS total_cost
  FROM `{gcp_catalog}`.`{gcp_schema}`.`{gcp_table}`
  WHERE DATE(usage_start_time) >= DATE('{reprocess_start}')
    AND DATE(usage_start_time) <= CURRENT_DATE()
    AND cost > 0
  GROUP BY 1, 2, 3, 4, 5, 6
) AS src
ON tgt.usage_date = src.usage_date
  AND COALESCE(tgt.project_id, '') = COALESCE(src.project_id, '')
  AND COALESCE(tgt.service, '') = COALESCE(src.service, '')
  AND COALESCE(tgt.sku, '') = COALESCE(src.sku, '')
  AND COALESCE(tgt.currency, '') = COALESCE(src.currency, '')
WHEN MATCHED THEN UPDATE SET *
WHEN NOT MATCHED BY TARGET THEN INSERT *
"""

VIEWER_CREATE = {
    "daily_warehouse_idle": CREATE_DAILY_WAREHOUSE_IDLE,
    "daily_kpi_facts": CREATE_DAILY_KPI_FACTS,
    "daily_lakeflow_stats": CREATE_DAILY_LAKEFLOW_STATS,
    "daily_query_users": CREATE_DAILY_QUERY_USERS,
    "daily_apps_identities": CREATE_DAILY_APPS_IDENTITIES,
    "use_case_query_objects": CREATE_USE_CASE_QUERY_OBJECTS,
    "use_case_dashboard_objects": CREATE_USE_CASE_DASHBOARD_OBJECTS,
}

VIEWER_MERGE = {
    "daily_warehouse_idle": MERGE_DAILY_WAREHOUSE_IDLE,
    "daily_kpi_facts": MERGE_DAILY_KPI_FACTS,
    "daily_lakeflow_stats": MERGE_DAILY_LAKEFLOW_STATS,
    "daily_query_users": MERGE_DAILY_QUERY_USERS,
    "daily_apps_identities": MERGE_DAILY_APPS_IDENTITIES,
}

VIEWER_REFRESH = {
    "daily_warehouse_idle": {
        "reprocess_days": 14,
        "pk": ["usage_date", "warehouse_id"],
        "source_date_col": "usage_date",
        "overlap_days": 7,
    },
    "daily_kpi_facts": {
        "reprocess_days": 14,
        "pk": ["usage_date", "workspace_id"],
        "source_date_col": "usage_date",
        "overlap_days": 0,
    },
    "daily_lakeflow_stats": {
        "reprocess_days": 14,
        "pk": ["usage_date", "workspace_id"],
        "source_date_col": "usage_date",
        "overlap_days": 0,
    },
    "daily_query_users": {
        "reprocess_days": 14,
        "pk": ["usage_date", "workspace_id", "user_id"],
        "source_date_col": "usage_date",
        "overlap_days": 0,
    },
    "daily_apps_identities": {
        "reprocess_days": 14,
        "pk": ["usage_date", "workspace_id", "app_id", "run_as"],
        "source_date_col": "usage_date",
        "overlap_days": 0,
    },
    "use_case_query_objects": {
        "reprocess_days": 1,
        "pk": ["snapshot_date", "object_id"],
        "source_date_col": "snapshot_date",
        "overlap_days": 0,
    },
    "use_case_dashboard_objects": {
        "reprocess_days": 1,
        "pk": ["snapshot_date", "object_id"],
        "source_date_col": "snapshot_date",
        "overlap_days": 0,
    },
}


def viewer_tables() -> tuple[dict, dict, dict]:
    create = dict(VIEWER_CREATE)
    merge = dict(VIEWER_MERGE)
    refresh = dict(VIEWER_REFRESH)
    az_cat = os.environ.get("AZURE_COST_CATALOG", os.environ.get("COST_OBS_CATALOG", "billing"))
    az_sch = os.environ.get("AZURE_COST_SCHEMA", "azure")
    create["daily_azure_actuals"] = (
        CREATE_DAILY_AZURE_ACTUALS.replace("{azure_catalog}", az_cat).replace("{azure_schema}", az_sch)
    )
    merge["daily_azure_actuals"] = (
        MERGE_DAILY_AZURE_ACTUALS.replace("{azure_catalog}", az_cat).replace("{azure_schema}", az_sch)
    )
    refresh["daily_azure_actuals"] = {
        "reprocess_days": 14,
        "pk": ["usage_date", "cluster_id", "warehouse_id", "charge_type"],
        "source_date_col": "usage_date",
        "overlap_days": 0,
    }
    gcp_cat = os.environ.get("GCP_COST_CATALOG", os.environ.get("COST_OBS_CATALOG", "billing"))
    gcp_sch = os.environ.get("GCP_COST_SCHEMA", "gcp")
    gcp_tbl = os.environ.get("GCP_COST_TABLE", "gcp_billing_export_v1")
    create["daily_gcp_actuals"] = (
        CREATE_DAILY_GCP_ACTUALS.replace("{gcp_catalog}", gcp_cat)
        .replace("{gcp_schema}", gcp_sch)
        .replace("{gcp_table}", gcp_tbl)
    )
    merge["daily_gcp_actuals"] = (
        MERGE_DAILY_GCP_ACTUALS.replace("{gcp_catalog}", gcp_cat)
        .replace("{gcp_schema}", gcp_sch)
        .replace("{gcp_table}", gcp_tbl)
    )
    refresh["daily_gcp_actuals"] = {
        "reprocess_days": 14,
        "pk": ["usage_date", "project_id", "service", "sku", "currency"],
        "source_date_col": "usage_date",
        "overlap_days": 0,
    }
    return create, merge, refresh


VIEWER_GOLD_SQL = {
    "workspace_list": """
SELECT CAST(workspace_id AS STRING) AS workspace_id, MAX(workspace_name) AS workspace_name
FROM `{catalog}`.`{schema}`.`daily_workspace_breakdown`
WHERE usage_date BETWEEN :start_date AND :end_date
  AND workspace_id IS NOT NULL {ws_filter}
GROUP BY workspace_id
ORDER BY COALESCE(MAX(workspace_name), CAST(workspace_id AS STRING))
""",
    "workspace_count_latest": """
SELECT daily_ws AS workspace_count FROM (
  SELECT usage_date, COUNT(DISTINCT workspace_id) AS daily_ws
  FROM `{catalog}`.`{schema}`.`daily_workspace_breakdown`
  WHERE usage_date BETWEEN :start_date AND :end_date {ws_filter}
  GROUP BY usage_date
  ORDER BY usage_date DESC
  LIMIT 1
)
""",
    "warehouse_idle": """
SELECT warehouse_id, MAX(warehouse_name) AS warehouse_name, MAX(warehouse_size) AS warehouse_size,
  MAX(warehouse_type) AS warehouse_type, MAX(workspace_id) AS workspace_id,
  CASE WHEN MAX(CASE WHEN uptime_source = 'events' THEN 1 ELSE 0 END) = 1 THEN 'events' ELSE 'billing' END AS uptime_source,
  SUM(total_running_minutes) AS total_running_minutes,
  SUM(busy_union_minutes) AS busy_union_minutes,
  SUM(idle_minutes) AS idle_minutes,
  SUM(warm_hold_minutes) AS warm_hold_minutes,
  MAX(auto_stop_mins) AS auto_stop_mins,
  MAX(max_num_clusters) AS max_num_clusters,
  SUM(total_spend) AS total_spend,
  SUM(estimated_idle_spend) AS estimated_idle_spend
FROM `{catalog}`.`{schema}`.`daily_warehouse_idle`
WHERE usage_date BETWEEN :start_date AND :end_date {ws_filter}
GROUP BY warehouse_id
HAVING SUM(total_running_minutes) >= 10
ORDER BY COALESCE(SUM(estimated_idle_spend), -1) DESC, SUM(warm_hold_minutes) DESC, SUM(total_spend) DESC
LIMIT 25
""",
    "warehouse_idle_serverless": """
SELECT COUNT(DISTINCT warehouse_id) AS serverless_count
FROM `{catalog}`.`{schema}`.`daily_warehouse_idle`
WHERE warehouse_type = 'SERVERLESS'
""",
    "kpi_billing": """
SELECT
  COUNT(DISTINCT workspace_id) AS active_workspaces,
  SUM(distinct_jobs) AS total_jobs,
  SUM(job_run_rows) AS total_job_runs,
  SUM(distinct_job_owners) AS unique_job_owners,
  SUM(distinct_clusters) AS total_clusters,
  SUM(distinct_sql_warehouses) AS sql_warehouses,
  SUM(sql_dbus) AS sql_dbus,
  SUM(distinct_models) AS models_served,
  SUM(serving_dbus) AS total_serving_dbus
FROM `{catalog}`.`{schema}`.`daily_kpi_facts`
WHERE usage_date BETWEEN :start_date AND :end_date {ws_filter}
""",
    "kpi_lakeflow": """
SELECT SUM(total_runs) AS total_runs, SUM(successful_runs) AS successful_runs,
  SUM(total_run_hours) AS total_run_hours
FROM `{catalog}`.`{schema}`.`daily_lakeflow_stats`
WHERE usage_date BETWEEN :start_date AND :end_date {ws_filter}
""",
    "total_workspaces_alltime": """
SELECT COUNT(DISTINCT workspace_id) AS total_workspaces
FROM `{catalog}`.`{schema}`.`daily_workspace_breakdown`
""",
    "avg_daily_models": """
SELECT COALESCE(ROUND(AVG(daily_models)), 0) AS avg_daily_models
FROM (
  SELECT usage_date, SUM(distinct_models) AS daily_models
  FROM `{catalog}`.`{schema}`.`daily_kpi_facts`
  WHERE usage_date BETWEEN :start_date AND :end_date {ws_filter}
  GROUP BY usage_date
)
""",
    "query_user_count": """
SELECT COUNT(DISTINCT user_id) AS unique_query_users
FROM `{catalog}`.`{schema}`.`daily_query_users`
WHERE usage_date BETWEEN :start_date AND :end_date {ws_filter}
""",
    "stickiness_pct": """
WITH total_users AS (
  SELECT COUNT(DISTINCT user_id) AS total
  FROM `{catalog}`.`{schema}`.`daily_query_users`
  WHERE usage_date BETWEEN :start_date AND :end_date {ws_filter}
),
daily_users AS (
  SELECT usage_date, COUNT(DISTINCT user_id) AS dau
  FROM `{catalog}`.`{schema}`.`daily_query_users`
  WHERE usage_date BETWEEN :start_date AND :end_date {ws_filter}
  GROUP BY usage_date
)
SELECT ROUND(AVG(100.0 * d.dau / NULLIF(t.total, 0)), 1) AS stickiness_pct
FROM daily_users d CROSS JOIN total_users t
""",
    "spend_anomalies": """
WITH daily_stats AS (
  SELECT usage_date, SUM(total_spend) AS daily_spend
  FROM `{catalog}`.`{schema}`.`daily_usage_summary`
  WHERE usage_date BETWEEN :start_date AND :end_date {ws_filter}
  GROUP BY usage_date
),
with_lag AS (
  SELECT usage_date, daily_spend, LAG(daily_spend) OVER (ORDER BY usage_date) AS prev_day_spend
  FROM daily_stats
)
SELECT usage_date, daily_spend, prev_day_spend,
  daily_spend - prev_day_spend AS change_amount,
  ROUND(100.0 * (daily_spend - prev_day_spend) / NULLIF(prev_day_spend, 0), 2) AS change_percent
FROM with_lag
WHERE prev_day_spend IS NOT NULL AND prev_day_spend > 0
ORDER BY ABS(change_percent) DESC
LIMIT 20
""",
    "apps_identities": """
SELECT app_id, run_as
FROM `{catalog}`.`{schema}`.`daily_apps_identities`
WHERE usage_date BETWEEN :start_date AND :end_date {ws_filter}
GROUP BY app_id, run_as
""",
    "available_queries": """
SELECT object_id, object_name, workspace_id
FROM `{catalog}`.`{schema}`.`use_case_query_objects`
WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM `{catalog}`.`{schema}`.`use_case_query_objects`)
LIMIT 100
""",
    "available_dashboards": """
SELECT object_id, object_name, workspace_id
FROM `{catalog}`.`{schema}`.`use_case_dashboard_objects`
WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM `{catalog}`.`{schema}`.`use_case_dashboard_objects`)
LIMIT 100
""",
    "azure_summary": """
SELECT SUM(total_cost) AS total_cost, SUM(total_cost_usd) AS total_cost_usd,
  COUNT(DISTINCT cluster_id) AS cluster_count, COUNT(DISTINCT warehouse_id) AS warehouse_count,
  COUNT(DISTINCT usage_date) AS days_in_range
FROM `{catalog}`.`{schema}`.`daily_azure_actuals`
WHERE usage_date >= :start_date AND usage_date < :end_date
""",
    "azure_by_cluster": """
SELECT cluster_id,
  SUM(CASE WHEN charge_type = 'Compute' THEN total_cost ELSE 0 END) AS compute_cost,
  SUM(CASE WHEN charge_type = 'Storage' THEN total_cost ELSE 0 END) AS storage_cost,
  SUM(CASE WHEN charge_type = 'Networking' THEN total_cost ELSE 0 END) AS network_cost,
  SUM(total_cost) AS total_cost, COUNT(DISTINCT usage_date) AS days_active
FROM `{catalog}`.`{schema}`.`daily_azure_actuals`
WHERE usage_date >= :start_date AND usage_date < :end_date AND cluster_id IS NOT NULL
GROUP BY cluster_id ORDER BY total_cost DESC LIMIT 100
""",
    "azure_by_charge_type": """
SELECT charge_type, SUM(total_cost) AS total_cost, SUM(total_cost_usd) AS total_cost_usd
FROM `{catalog}`.`{schema}`.`daily_azure_actuals`
WHERE usage_date >= :start_date AND usage_date < :end_date
GROUP BY charge_type ORDER BY total_cost DESC
""",
    "azure_timeseries": """
SELECT usage_date AS date, charge_type, SUM(total_cost) AS daily_cost
FROM `{catalog}`.`{schema}`.`daily_azure_actuals`
WHERE usage_date >= :start_date AND usage_date < :end_date
GROUP BY usage_date, charge_type ORDER BY date
""",
    "gcp_summary": """
SELECT SUM(total_cost) AS total_cost, MAX(currency) AS currency,
  COUNT(DISTINCT project_id) AS project_count, COUNT(DISTINCT service) AS service_count,
  COUNT(DISTINCT usage_date) AS days_in_range
FROM `{catalog}`.`{schema}`.`daily_gcp_actuals`
WHERE usage_date >= :start_date AND usage_date < :end_date
""",
    "gcp_by_service": """
SELECT service, SUM(total_cost) AS total_cost, COUNT(DISTINCT usage_date) AS days_active
FROM `{catalog}`.`{schema}`.`daily_gcp_actuals`
WHERE usage_date >= :start_date AND usage_date < :end_date
GROUP BY service ORDER BY total_cost DESC LIMIT 50
""",
    "gcp_by_project": """
SELECT project_id, MAX(project_name) AS project_name, SUM(total_cost) AS total_cost,
  COUNT(DISTINCT service) AS service_count
FROM `{catalog}`.`{schema}`.`daily_gcp_actuals`
WHERE usage_date >= :start_date AND usage_date < :end_date
GROUP BY project_id ORDER BY total_cost DESC LIMIT 50
""",
    "gcp_timeseries": """
SELECT usage_date AS date, service, SUM(total_cost) AS daily_cost
FROM `{catalog}`.`{schema}`.`daily_gcp_actuals`
WHERE usage_date >= :start_date AND usage_date < :end_date
GROUP BY usage_date, service ORDER BY date
""",
    "gcp_by_sku": """
SELECT service, sku, SUM(total_cost) AS total_cost
FROM `{catalog}`.`{schema}`.`daily_gcp_actuals`
WHERE usage_date >= :start_date AND usage_date < :end_date
GROUP BY service, sku ORDER BY total_cost DESC LIMIT 100
""",
    "platform_kpi_trend_total_queries": """
SELECT usage_date AS date, SUM(total_queries) AS value
FROM `{catalog}`.`{schema}`.`daily_query_stats`
WHERE usage_date BETWEEN :start_date AND :end_date {ws_filter}
GROUP BY usage_date ORDER BY usage_date
""",
    "platform_kpi_trend_total_rows_read": """
SELECT usage_date AS date, SUM(total_rows_read) AS value
FROM `{catalog}`.`{schema}`.`daily_query_stats`
WHERE usage_date BETWEEN :start_date AND :end_date {ws_filter}
GROUP BY usage_date ORDER BY usage_date
""",
    "platform_kpi_trend_total_bytes_read": """
SELECT usage_date AS date, SUM(total_bytes_read) AS value
FROM `{catalog}`.`{schema}`.`daily_query_stats`
WHERE usage_date BETWEEN :start_date AND :end_date {ws_filter}
GROUP BY usage_date ORDER BY usage_date
""",
    "platform_kpi_trend_total_compute_seconds": """
SELECT usage_date AS date, SUM(total_compute_seconds) AS value
FROM `{catalog}`.`{schema}`.`daily_query_stats`
WHERE usage_date BETWEEN :start_date AND :end_date {ws_filter}
GROUP BY usage_date ORDER BY usage_date
""",
    "platform_kpi_trend_total_users": """
SELECT usage_date AS date, COUNT(DISTINCT user_id) AS value
FROM `{catalog}`.`{schema}`.`daily_query_users`
WHERE usage_date BETWEEN :start_date AND :end_date {ws_filter}
GROUP BY usage_date ORDER BY usage_date
""",
    "platform_kpi_trend_total_jobs": """
SELECT usage_date AS date, SUM(distinct_jobs) AS value
FROM `{catalog}`.`{schema}`.`daily_kpi_facts`
WHERE usage_date BETWEEN :start_date AND :end_date {ws_filter}
GROUP BY usage_date ORDER BY usage_date
""",
    "platform_kpi_trend_total_job_runs": """
SELECT usage_date AS date, SUM(job_run_rows) AS value
FROM `{catalog}`.`{schema}`.`daily_kpi_facts`
WHERE usage_date BETWEEN :start_date AND :end_date {ws_filter}
GROUP BY usage_date ORDER BY usage_date
""",
    "platform_kpi_trend_active_notebooks": """
SELECT usage_date AS date, SUM(distinct_clusters) AS value
FROM `{catalog}`.`{schema}`.`daily_kpi_facts`
WHERE usage_date BETWEEN :start_date AND :end_date {ws_filter}
GROUP BY usage_date ORDER BY usage_date
""",
    "platform_kpi_trend_active_workspaces": """
SELECT usage_date AS date, COUNT(DISTINCT workspace_id) AS value
FROM `{catalog}`.`{schema}`.`daily_workspace_breakdown`
WHERE usage_date BETWEEN :start_date AND :end_date {ws_filter}
GROUP BY usage_date ORDER BY usage_date
""",
    "platform_kpi_trend_models_served": """
SELECT usage_date AS date, SUM(distinct_models) AS value
FROM `{catalog}`.`{schema}`.`daily_kpi_facts`
WHERE usage_date BETWEEN :start_date AND :end_date {ws_filter}
GROUP BY usage_date ORDER BY usage_date
""",
    "platform_kpi_trend_successful_runs": """
SELECT usage_date AS date, SUM(successful_runs) AS value
FROM `{catalog}`.`{schema}`.`daily_lakeflow_stats`
WHERE usage_date BETWEEN :start_date AND :end_date {ws_filter}
GROUP BY usage_date ORDER BY usage_date
""",
    "platform_kpi_trend_unique_warehouses": """
SELECT usage_date AS date, COUNT(DISTINCT warehouse_id) AS value
FROM `{catalog}`.`{schema}`.`daily_dbsql_warehouse_type`
WHERE usage_date BETWEEN :start_date AND :end_date {ws_filter}
GROUP BY usage_date ORDER BY usage_date
""",
    "platform_kpi_trend_stickiness": """
WITH total_users AS (
  SELECT COUNT(DISTINCT user_id) AS total
  FROM `{catalog}`.`{schema}`.`daily_query_users`
  WHERE usage_date BETWEEN :start_date AND :end_date {ws_filter}
),
daily_users AS (
  SELECT usage_date AS date, COUNT(DISTINCT user_id) AS dau
  FROM `{catalog}`.`{schema}`.`daily_query_users`
  WHERE usage_date BETWEEN :start_date AND :end_date {ws_filter}
  GROUP BY usage_date
)
SELECT d.date, 100.0 * d.dau / NULLIF(t.total, 0) AS value
FROM daily_users d CROSS JOIN total_users t
ORDER BY d.date
""",
}
