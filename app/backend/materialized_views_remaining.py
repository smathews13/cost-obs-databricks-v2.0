"""Gold tables for remaining live dashboard surfaces (warehouse-built, Lakebase-served)."""

from __future__ import annotations

import os

_PRICE_JOIN = """
LEFT JOIN system.billing.list_prices p
  ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND p.price_end_time IS NULL
"""

_USER_CAT = """
    CASE
      WHEN u.billing_origin_product = 'SQL' THEN 'SQL'
      WHEN u.billing_origin_product = 'DLT' OR u.usage_metadata.dlt_pipeline_id IS NOT NULL THEN 'ETL - Streaming'
      WHEN u.billing_origin_product = 'JOBS' THEN 'ETL - Batch'
      WHEN u.sku_name LIKE '%ALL_PURPOSE%' THEN 'Interactive'
      WHEN u.billing_origin_product IN ('SERVING', 'MODEL_SERVING')
           OR u.sku_name LIKE '%SERVING%' OR u.sku_name LIKE '%INFERENCE%'
           OR u.sku_name LIKE '%PROVISIONED_THROUGHPUT%' THEN 'Model Serving'
      WHEN u.sku_name LIKE '%VECTOR_SEARCH%' THEN 'AI Search'
      WHEN u.sku_name LIKE '%FOUNDATION_MODEL%' OR u.sku_name LIKE '%FINE_TUNING%' THEN 'Fine-Tuning'
      WHEN u.sku_name LIKE '%AI_BI%' OR u.sku_name LIKE '%AI_QUERY%'
           OR u.sku_name LIKE '%AI_FUNCTIONS%' THEN 'AI Functions'
      WHEN u.sku_name LIKE '%SERVERLESS%'
           AND u.billing_origin_product NOT IN ('SQL', 'JOBS', 'DLT') THEN 'Serverless'
      ELSE 'Other'
    END
"""


def _merge(table: str, pk: list[str], src_sql: str) -> str:
    parts = []
    for c in pk:
        if c == "usage_date" or c == "snapshot_date":
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


_USER_BODY = f"""
  SELECT
    u.usage_date,
    u.workspace_id,
    u.identity_metadata.run_as AS user_email,
{_USER_CAT} AS product_category,
    SUM(u.usage_quantity * COALESCE(p.pricing.default, 0)) AS total_spend,
    SUM(u.usage_quantity) AS total_dbus
  FROM system.billing.usage u
  {_PRICE_JOIN}
  WHERE {{date_pred}}
    AND u.usage_quantity > 0
    AND u.identity_metadata.run_as IS NOT NULL
  GROUP BY 1, 2, 3, 4
"""

CREATE_DAILY_USER_SPEND = f"""
CREATE OR REPLACE TABLE `{{catalog}}`.`{{schema}}`.`daily_user_spend` CLUSTER BY (usage_date, workspace_id) AS
{_USER_BODY.format(date_pred="u.usage_date >= DATE_SUB(CURRENT_DATE(), {billing_lookback_days})")}
"""

MERGE_DAILY_USER_SPEND = _merge(
    "daily_user_spend",
    ["usage_date", "workspace_id", "user_email", "product_category"],
    _USER_BODY.format(date_pred="u.usage_date >= DATE('{reprocess_start}') AND u.usage_date <= CURRENT_DATE()"),
)

_UNTAGGED_BODY = """
  SELECT usage_date, workspace_id, object_type, object_id, MAX(object_name) AS object_name,
         MAX(owner) AS owner, MAX(extra) AS extra,
         SUM(total_dbus) AS total_dbus, SUM(total_spend) AS total_spend
  FROM (
    SELECT
      u.usage_date, u.workspace_id, 'cluster' AS object_type,
      CAST(u.usage_metadata.cluster_id AS STRING) AS object_id,
      COALESCE(ci.cluster_name, CAST(u.usage_metadata.cluster_id AS STRING)) AS object_name,
      ci.owned_by AS owner, ci.cluster_source AS extra,
      u.usage_quantity AS total_dbus,
      u.usage_quantity * COALESCE(p.pricing.default, 0) AS total_spend
    FROM system.billing.usage u
    LEFT JOIN system.billing.list_prices p
      ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND p.price_end_time IS NULL
    LEFT JOIN (
      SELECT cluster_id, MAX(cluster_name) AS cluster_name, MAX(cluster_source) AS cluster_source, MAX(owned_by) AS owned_by
      FROM system.compute.clusters WHERE delete_time IS NULL GROUP BY cluster_id
    ) ci ON u.usage_metadata.cluster_id = ci.cluster_id
    WHERE {date_pred} AND u.usage_quantity > 0 AND u.usage_metadata.cluster_id IS NOT NULL
      AND (u.custom_tags IS NULL OR size(u.custom_tags) = 0)
    UNION ALL
    SELECT
      u.usage_date, u.workspace_id, 'job', CAST(u.usage_metadata.job_id AS STRING),
      COALESCE(u.usage_metadata.job_name, CAST(u.usage_metadata.job_id AS STRING)),
      CAST(NULL AS STRING), CAST(NULL AS STRING),
      u.usage_quantity, u.usage_quantity * COALESCE(p.pricing.default, 0)
    FROM system.billing.usage u
    LEFT JOIN system.billing.list_prices p
      ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND p.price_end_time IS NULL
    WHERE {date_pred} AND u.usage_quantity > 0 AND u.usage_metadata.job_id IS NOT NULL
      AND (u.custom_tags IS NULL OR size(u.custom_tags) = 0)
    UNION ALL
    SELECT
      u.usage_date, u.workspace_id, 'pipeline', CAST(u.usage_metadata.dlt_pipeline_id AS STRING),
      CAST(u.usage_metadata.dlt_pipeline_id AS STRING),
      CAST(NULL AS STRING), CAST(NULL AS STRING),
      u.usage_quantity, u.usage_quantity * COALESCE(p.pricing.default, 0)
    FROM system.billing.usage u
    LEFT JOIN system.billing.list_prices p
      ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND p.price_end_time IS NULL
    WHERE {date_pred} AND u.usage_quantity > 0 AND u.usage_metadata.dlt_pipeline_id IS NOT NULL
      AND (u.custom_tags IS NULL OR size(u.custom_tags) = 0)
    UNION ALL
    SELECT
      u.usage_date, u.workspace_id, 'warehouse', CAST(u.usage_metadata.warehouse_id AS STRING),
      COALESCE(wi.warehouse_name, CAST(u.usage_metadata.warehouse_id AS STRING)),
      CAST(NULL AS STRING), CAST(NULL AS STRING),
      u.usage_quantity, u.usage_quantity * COALESCE(p.pricing.default, 0)
    FROM system.billing.usage u
    LEFT JOIN system.billing.list_prices p
      ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND p.price_end_time IS NULL
    LEFT JOIN (
      SELECT warehouse_id, MAX(warehouse_name) AS warehouse_name
      FROM system.compute.warehouses WHERE delete_time IS NULL GROUP BY warehouse_id
    ) wi ON u.usage_metadata.warehouse_id = wi.warehouse_id
    WHERE {date_pred} AND u.usage_quantity > 0 AND u.usage_metadata.warehouse_id IS NOT NULL
      AND (u.custom_tags IS NULL OR size(u.custom_tags) = 0)
    UNION ALL
    SELECT
      u.usage_date, u.workspace_id, 'endpoint', COALESCE(u.usage_metadata.endpoint_name, u.usage_metadata.endpoint_id),
      COALESCE(u.usage_metadata.endpoint_name, u.usage_metadata.endpoint_id),
      CAST(NULL AS STRING), CAST(NULL AS STRING),
      u.usage_quantity, u.usage_quantity * COALESCE(p.pricing.default, 0)
    FROM system.billing.usage u
    LEFT JOIN system.billing.list_prices p
      ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND p.price_end_time IS NULL
    WHERE {date_pred} AND u.usage_quantity > 0
      AND (u.usage_metadata.endpoint_name IS NOT NULL OR u.usage_metadata.endpoint_id IS NOT NULL)
      AND (u.custom_tags IS NULL OR size(u.custom_tags) = 0)
  ) raw
  GROUP BY usage_date, workspace_id, object_type, object_id
"""

CREATE_DAILY_UNTAGGED_OBJECTS = f"""
CREATE OR REPLACE TABLE `{{catalog}}`.`{{schema}}`.`daily_untagged_objects` CLUSTER BY (usage_date, workspace_id) AS
{_UNTAGGED_BODY.format(date_pred="u.usage_date >= DATE_SUB(CURRENT_DATE(), {billing_lookback_days})")}
"""

MERGE_DAILY_UNTAGGED_OBJECTS = _merge(
    "daily_untagged_objects",
    ["usage_date", "workspace_id", "object_type", "object_id"],
    _UNTAGGED_BODY.format(date_pred="u.usage_date >= DATE('{reprocess_start}') AND u.usage_date <= CURRENT_DATE()"),
)

_MODEL_BODY = """
  SELECT
    u.usage_date,
    u.workspace_id,
    COALESCE(
      u.usage_metadata.endpoint_name,
      cn.cluster_name,
      CASE WHEN u.usage_metadata.job_id IS NOT NULL THEN CONCAT('Job #', CAST(u.usage_metadata.job_id AS STRING)) END,
      CAST(u.usage_metadata.cluster_id AS STRING),
      CASE WHEN u.sku_name LIKE '%FEATURE%' THEN 'Feature Store Run'
           WHEN u.sku_name LIKE '%TRAINING%' THEN 'Serverless Fine-tuning' END,
      'Unknown'
    ) AS model_name,
    CASE
      WHEN u.sku_name LIKE '%FEATURE%' THEN 'Feature Store'
      WHEN u.sku_name LIKE '%TRAINING%' THEN 'Model Training'
      WHEN u.sku_name LIKE '%ANTHROPIC%' OR u.sku_name LIKE '%OPENAI%'
           OR u.sku_name LIKE '%GEMINI%' OR u.sku_name LIKE '%META%'
           OR u.sku_name LIKE '%COHERE%' OR u.sku_name LIKE '%MISTRAL%'
           OR u.sku_name LIKE '%AI21%' OR u.sku_name LIKE '%MOSAIC%'
           OR u.sku_name LIKE '%DBRX%' OR u.sku_name LIKE '%LLAMA%'
           OR u.billing_origin_product = 'FOUNDATION_MODEL_SERVING' THEN 'Foundation Model API'
      ELSE NULL
    END AS model_type,
    SUM(u.usage_quantity) AS total_dbus,
    SUM(u.usage_quantity * COALESCE(p.pricing.default, 0)) AS total_spend
  FROM system.billing.usage u
  LEFT JOIN system.billing.list_prices p
    ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND p.price_end_time IS NULL
  LEFT JOIN (
    SELECT cluster_id, MAX(cluster_name) AS cluster_name FROM system.compute.clusters GROUP BY cluster_id
  ) cn ON u.usage_metadata.cluster_id = cn.cluster_id
  WHERE {date_pred} AND u.usage_quantity > 0
    AND (
      u.sku_name LIKE '%FEATURE%' OR u.sku_name LIKE '%TRAINING%'
      OR u.sku_name LIKE '%ANTHROPIC%' OR u.sku_name LIKE '%OPENAI%'
      OR u.sku_name LIKE '%GEMINI%' OR u.sku_name LIKE '%META%'
      OR u.sku_name LIKE '%COHERE%' OR u.sku_name LIKE '%MISTRAL%'
      OR u.sku_name LIKE '%AI21%' OR u.sku_name LIKE '%MOSAIC%'
      OR u.sku_name LIKE '%DBRX%' OR u.sku_name LIKE '%LLAMA%'
      OR u.billing_origin_product = 'FOUNDATION_MODEL_SERVING'
    )
  GROUP BY 1, 2, 3, 4
"""

CREATE_DAILY_AIML_MODELS = f"""
CREATE OR REPLACE TABLE `{{catalog}}`.`{{schema}}`.`daily_aiml_models` CLUSTER BY (usage_date, workspace_id) AS
SELECT * FROM (
{_MODEL_BODY.format(date_pred="u.usage_date >= DATE_SUB(CURRENT_DATE(), {billing_lookback_days})")}
) t WHERE model_type IS NOT NULL
"""

MERGE_DAILY_AIML_MODELS = _merge(
    "daily_aiml_models",
    ["usage_date", "workspace_id", "model_name", "model_type"],
    "SELECT * FROM (\n"
    + _MODEL_BODY.format(date_pred="u.usage_date >= DATE('{reprocess_start}') AND u.usage_date <= CURRENT_DATE()")
    + "\n) t WHERE model_type IS NOT NULL",
)

_ML_CLUSTER_BODY = """
  SELECT
    u.usage_date,
    u.workspace_id,
    cm.cluster_id,
    MAX(cm.cluster_name) AS cluster_name,
    MAX(cm.spark_version) AS runtime_version,
    MAX(cm.owned_by) AS owner,
    SUM(u.usage_quantity) AS total_dbus,
    SUM(u.usage_quantity * COALESCE(p.pricing.default, 0)) AS total_spend
  FROM (
    SELECT c.cluster_id, MAX(c.cluster_name) AS cluster_name, MAX(c.dbr_version) AS spark_version, MAX(c.owned_by) AS owned_by
    FROM system.compute.clusters c
    WHERE c.dbr_version IS NOT NULL
      AND (LOWER(c.dbr_version) LIKE '%-ml-%' OR LOWER(c.dbr_version) LIKE '%-gpu-%')
    GROUP BY c.cluster_id
  ) cm
  INNER JOIN system.billing.usage u ON u.usage_metadata.cluster_id = cm.cluster_id
  LEFT JOIN system.billing.list_prices p
    ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND p.price_end_time IS NULL
  WHERE {date_pred} AND u.usage_quantity > 0
  GROUP BY u.usage_date, u.workspace_id, cm.cluster_id
"""

CREATE_DAILY_AIML_ML_CLUSTERS = f"""
CREATE OR REPLACE TABLE `{{catalog}}`.`{{schema}}`.`daily_aiml_ml_clusters` CLUSTER BY (usage_date, workspace_id) AS
{_ML_CLUSTER_BODY.format(date_pred="u.usage_date >= DATE_SUB(CURRENT_DATE(), {billing_lookback_days})")}
"""

MERGE_DAILY_AIML_ML_CLUSTERS = _merge(
    "daily_aiml_ml_clusters",
    ["usage_date", "workspace_id", "cluster_id"],
    _ML_CLUSTER_BODY.format(date_pred="u.usage_date >= DATE('{reprocess_start}') AND u.usage_date <= CURRENT_DATE()"),
)

_AGENT_BODY = """
  SELECT
    u.usage_date,
    u.workspace_id,
    COALESCE(se.endpoint_name, u.usage_metadata.endpoint_name, u.usage_metadata.endpoint_id, 'Unknown') AS agent_name,
    MAX(u.usage_metadata.endpoint_id) AS endpoint_id,
    MAX(u.billing_origin_product) AS billing_origin_product,
    MAX(se.served_entity_name) AS served_entity_name,
    MAX(CASE
      WHEN u.billing_origin_product = 'SUPERVISOR_AGENT' THEN 'Supervisor Agent'
      WHEN u.billing_origin_product = 'KNOWLEDGE_AGENT' THEN 'Knowledge Assistant'
      WHEN u.billing_origin_product = 'AGENT_BRICKS'
        AND u.product_features.agent_bricks.problem_type = 'AGENT_BRICKS_AI_CLASSIFY' THEN 'AI Classify'
      WHEN u.billing_origin_product = 'AGENT_BRICKS'
        AND u.product_features.agent_bricks.problem_type = 'AGENT_BRICKS_AI_EXTRACT' THEN 'AI Extract'
      ELSE 'Agent'
    END) AS agent_type,
    SUM(u.usage_quantity) AS total_dbus,
    SUM(u.usage_quantity * COALESCE(p.pricing.default, 0)) AS total_spend
  FROM system.billing.usage u
  LEFT JOIN system.billing.list_prices p
    ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND p.price_end_time IS NULL
  LEFT JOIN (
    SELECT endpoint_id AS serving_endpoint_id, MAX(endpoint_name) AS endpoint_name, MAX(served_entity_name) AS served_entity_name
    FROM system.serving.served_entities WHERE endpoint_id IS NOT NULL GROUP BY endpoint_id
  ) se ON u.usage_metadata.endpoint_id = se.serving_endpoint_id
  WHERE {date_pred} AND u.usage_quantity > 0
    AND u.billing_origin_product IN ('AGENT_BRICKS', 'SUPERVISOR_AGENT', 'KNOWLEDGE_AGENT')
  GROUP BY 1, 2, 3
"""

CREATE_DAILY_AIML_AGENTS = f"""
CREATE OR REPLACE TABLE `{{catalog}}`.`{{schema}}`.`daily_aiml_agents` CLUSTER BY (usage_date, workspace_id) AS
{_AGENT_BODY.format(date_pred="u.usage_date >= DATE_SUB(CURRENT_DATE(), {billing_lookback_days})")}
"""

MERGE_DAILY_AIML_AGENTS = _merge(
    "daily_aiml_agents",
    ["usage_date", "workspace_id", "agent_name"],
    _AGENT_BODY.format(date_pred="u.usage_date >= DATE('{reprocess_start}') AND u.usage_date <= CURRENT_DATE()"),
)

_OBJECT_BODY = """
  SELECT usage_date, workspace_id, object_type, object_id, MAX(object_name) AS object_name,
         SUM(total_dbus) AS total_dbus, SUM(total_spend) AS total_spend
  FROM (
    SELECT u.usage_date, u.workspace_id, 'cluster' AS object_type,
      CAST(u.usage_metadata.cluster_id AS STRING) AS object_id,
      CAST(u.usage_metadata.cluster_id AS STRING) AS object_name,
      u.usage_quantity AS total_dbus, u.usage_quantity * COALESCE(p.pricing.default, 0) AS total_spend
    FROM system.billing.usage u
    LEFT JOIN system.billing.list_prices p ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND p.price_end_time IS NULL
    WHERE {date_pred} AND u.usage_quantity > 0 AND u.usage_metadata.cluster_id IS NOT NULL
    UNION ALL
    SELECT u.usage_date, u.workspace_id, 'warehouse', CAST(u.usage_metadata.warehouse_id AS STRING),
      CAST(u.usage_metadata.warehouse_id AS STRING), u.usage_quantity, u.usage_quantity * COALESCE(p.pricing.default, 0)
    FROM system.billing.usage u
    LEFT JOIN system.billing.list_prices p ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND p.price_end_time IS NULL
    WHERE {date_pred} AND u.usage_quantity > 0 AND u.usage_metadata.warehouse_id IS NOT NULL
    UNION ALL
    SELECT u.usage_date, u.workspace_id, 'job', CAST(u.usage_metadata.job_id AS STRING),
      COALESCE(u.usage_metadata.job_name, CAST(u.usage_metadata.job_id AS STRING)),
      u.usage_quantity, u.usage_quantity * COALESCE(p.pricing.default, 0)
    FROM system.billing.usage u
    LEFT JOIN system.billing.list_prices p ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND p.price_end_time IS NULL
    WHERE {date_pred} AND u.usage_quantity > 0 AND u.usage_metadata.job_id IS NOT NULL
    UNION ALL
    SELECT u.usage_date, u.workspace_id, 'endpoint',
      COALESCE(u.usage_metadata.endpoint_id, u.usage_metadata.endpoint_name),
      COALESCE(u.usage_metadata.endpoint_name, u.usage_metadata.endpoint_id),
      u.usage_quantity, u.usage_quantity * COALESCE(p.pricing.default, 0)
    FROM system.billing.usage u
    LEFT JOIN system.billing.list_prices p ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND p.price_end_time IS NULL
    WHERE {date_pred} AND u.usage_quantity > 0
      AND (u.usage_metadata.endpoint_id IS NOT NULL OR u.usage_metadata.endpoint_name IS NOT NULL)
    UNION ALL
    SELECT u.usage_date, u.workspace_id, 'pipeline', CAST(u.usage_metadata.dlt_pipeline_id AS STRING),
      CAST(u.usage_metadata.dlt_pipeline_id AS STRING),
      u.usage_quantity, u.usage_quantity * COALESCE(p.pricing.default, 0)
    FROM system.billing.usage u
    LEFT JOIN system.billing.list_prices p ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND p.price_end_time IS NULL
    WHERE {date_pred} AND u.usage_quantity > 0 AND u.usage_metadata.dlt_pipeline_id IS NOT NULL
  ) raw
  GROUP BY usage_date, workspace_id, object_type, object_id
"""

CREATE_DAILY_OBJECT_SPEND = f"""
CREATE OR REPLACE TABLE `{{catalog}}`.`{{schema}}`.`daily_object_spend` CLUSTER BY (usage_date, workspace_id) AS
{_OBJECT_BODY.format(date_pred="u.usage_date >= DATE_SUB(CURRENT_DATE(), {billing_lookback_days})")}
"""

MERGE_DAILY_OBJECT_SPEND = _merge(
    "daily_object_spend",
    ["usage_date", "workspace_id", "object_type", "object_id"],
    _OBJECT_BODY.format(date_pred="u.usage_date >= DATE('{reprocess_start}') AND u.usage_date <= CURRENT_DATE()"),
)

_INFRA_BODY = """
  SELECT
    uf.usage_date,
    uf.workspace_id,
    uf.cluster_id,
    MAX(ci.cluster_name) AS cluster_name,
    MAX(ci.driver_instance_type) AS driver_instance_type,
    MAX(ci.worker_instance_type) AS worker_instance_type,
    MAX(ci.cluster_source) AS cluster_source,
    MAX(wsl.workspace_name) AS workspace_name,
    MAX(uf.cloud) AS cloud,
    SUM(uf.estimated_dbu_hours) AS total_dbu_hours
  FROM (
    SELECT u.usage_date, u.workspace_id, u.usage_metadata.cluster_id AS cluster_id, u.cloud, u.usage_quantity AS estimated_dbu_hours
    FROM system.billing.usage u
    WHERE {date_pred} AND u.usage_quantity > 0 AND u.usage_metadata.cluster_id IS NOT NULL
      AND u.billing_origin_product NOT IN ('SQL', 'DLT')
  ) uf
  LEFT JOIN (
    SELECT cluster_id, MAX(cluster_name) AS cluster_name, MAX(driver_node_type) AS driver_instance_type,
           MAX(worker_node_type) AS worker_instance_type, MAX(cluster_source) AS cluster_source
    FROM system.compute.clusters GROUP BY cluster_id
  ) ci ON uf.cluster_id = ci.cluster_id
  LEFT JOIN system.access.workspaces_latest wsl
    ON CAST(uf.workspace_id AS BIGINT) = CAST(wsl.workspace_id AS BIGINT)
  GROUP BY uf.usage_date, uf.workspace_id, uf.cluster_id
"""

CREATE_DAILY_INFRA_CLUSTER_SPEND = f"""
CREATE OR REPLACE TABLE `{{catalog}}`.`{{schema}}`.`daily_infra_cluster_spend` CLUSTER BY (usage_date, workspace_id) AS
{_INFRA_BODY.format(date_pred="u.usage_date >= DATE_SUB(CURRENT_DATE(), {billing_lookback_days})")}
"""

MERGE_DAILY_INFRA_CLUSTER_SPEND = _merge(
    "daily_infra_cluster_spend",
    ["usage_date", "workspace_id", "cluster_id"],
    _INFRA_BODY.format(date_pred="u.usage_date >= DATE('{reprocess_start}') AND u.usage_date <= CURRENT_DATE()"),
)

_WH_TYPE_BODY = """
  SELECT
    u.usage_date,
    u.workspace_id,
    CAST(u.usage_metadata.warehouse_id AS STRING) AS warehouse_id,
    MAX(w.warehouse_name) AS warehouse_name,
    MAX(COALESCE(w.warehouse_type, 'CLASSIC')) AS warehouse_type,
    MAX(w.warehouse_size) AS warehouse_size,
    MAX(ws.workspace_name) AS workspace_name,
    SUM(u.usage_quantity * COALESCE(p.pricing.default, 0)) AS daily_spend,
    SUM(u.usage_quantity) AS daily_dbus
  FROM system.billing.usage u
  LEFT JOIN system.billing.list_prices p
    ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND p.price_end_time IS NULL
  LEFT JOIN (
    SELECT warehouse_id, MAX(warehouse_name) AS warehouse_name, MAX(warehouse_type) AS warehouse_type,
           MAX(warehouse_size) AS warehouse_size, MAX(workspace_id) AS workspace_id
    FROM system.compute.warehouses GROUP BY warehouse_id
  ) w ON u.usage_metadata.warehouse_id = w.warehouse_id
  LEFT JOIN system.access.workspaces_latest ws ON u.workspace_id = ws.workspace_id
  WHERE {date_pred} AND u.usage_quantity > 0 AND u.billing_origin_product = 'SQL'
    AND u.usage_metadata.warehouse_id IS NOT NULL
  GROUP BY u.usage_date, u.workspace_id, CAST(u.usage_metadata.warehouse_id AS STRING)
"""

CREATE_DAILY_DBSQL_WAREHOUSE_TYPE = f"""
CREATE OR REPLACE TABLE `{{catalog}}`.`{{schema}}`.`daily_dbsql_warehouse_type` CLUSTER BY (usage_date, workspace_id) AS
{_WH_TYPE_BODY.format(date_pred="u.usage_date >= DATE_SUB(CURRENT_DATE(), {billing_lookback_days})")}
"""

MERGE_DAILY_DBSQL_WAREHOUSE_TYPE = _merge(
    "daily_dbsql_warehouse_type",
    ["usage_date", "workspace_id", "warehouse_id"],
    _WH_TYPE_BODY.format(date_pred="u.usage_date >= DATE('{reprocess_start}') AND u.usage_date <= CURRENT_DATE()"),
)

CREATE_DAILY_DBSQL_REGION_SCOPE = """
CREATE OR REPLACE TABLE `{catalog}`.`{schema}`.`daily_dbsql_region_scope` AS
SELECT
  CURRENT_DATE() AS snapshot_date,
  (SELECT COUNT(DISTINCT workspace_id) FROM system.billing.usage
   WHERE billing_origin_product = 'SQL' AND usage_quantity > 0
     AND usage_date >= DATE_SUB(CURRENT_DATE(), {billing_lookback_days})
     AND workspace_id IS NOT NULL) AS billing_ws_count,
  (SELECT COUNT(DISTINCT workspace_id) FROM system.compute.warehouses
   WHERE workspace_id IS NOT NULL) AS compute_ws_count
"""

CREATE_WAREHOUSE_OPTIMIZER_FINDINGS = """
CREATE OR REPLACE TABLE `{catalog}`.`{schema}`.`warehouse_optimizer_findings` AS
WITH current_warehouses AS (
  SELECT warehouse_id, warehouse_name, warehouse_size, workspace_id, warehouse_type
  FROM system.compute.warehouses
  QUALIFY ROW_NUMBER() OVER (PARTITION BY warehouse_id ORDER BY change_time DESC) = 1
),
recent_queries AS (
  SELECT DISTINCT compute.warehouse_id AS warehouse_id
  FROM system.query.history
  WHERE start_time >= NOW() - INTERVAL 24 HOUR AND compute.warehouse_id IS NOT NULL
),
running_warehouses AS (
  SELECT warehouse_id, MAX(event_time) AS last_event_time
  FROM system.compute.warehouse_events
  WHERE event_time >= NOW() - INTERVAL 2 HOUR AND cluster_count > 0
  GROUP BY warehouse_id
),
idle AS (
  SELECT rw.warehouse_id, w.warehouse_name, w.warehouse_size, w.workspace_id, w.warehouse_type,
         'IDLE_RUNNING' AS recommendation_type, CAST(rw.last_event_time AS STRING) AS extra_1,
         CAST(NULL AS DOUBLE) AS extra_n1, CAST(NULL AS DOUBLE) AS extra_n2, CAST(NULL AS BIGINT) AS extra_n3
  FROM running_warehouses rw
  JOIN current_warehouses w USING (warehouse_id)
  WHERE rw.warehouse_id NOT IN (SELECT warehouse_id FROM recent_queries)
    AND COALESCE(w.warehouse_type, 'CLASSIC') != 'SERVERLESS'
),
cluster_scale_events AS (
  SELECT warehouse_id, MAX(cluster_count) AS max_clusters_observed
  FROM system.compute.warehouse_events
  WHERE event_time >= NOW() - INTERVAL 30 DAY
  GROUP BY warehouse_id
  HAVING MAX(cluster_count) >= 2
),
max_concurrency AS (
  SELECT warehouse_id, MAX(concurrent_queries) AS max_concurrent
  FROM (
    SELECT compute.warehouse_id AS warehouse_id, DATE_TRUNC('minute', start_time) AS minute_bucket, COUNT(*) AS concurrent_queries
    FROM system.query.history
    WHERE start_time >= NOW() - INTERVAL 30 DAY
      AND compute.warehouse_id IN (SELECT warehouse_id FROM cluster_scale_events)
    GROUP BY 1, 2
  ) t
  GROUP BY 1
),
over_scaled AS (
  SELECT cse.warehouse_id, w.warehouse_name, w.warehouse_size, w.workspace_id, w.warehouse_type,
         'OVER_SCALED' AS recommendation_type, CAST(NULL AS STRING) AS extra_1,
         CAST(cse.max_clusters_observed AS DOUBLE) AS extra_n1,
         CAST(COALESCE(mc.max_concurrent, 0) AS DOUBLE) AS extra_n2,
         CAST(NULL AS BIGINT) AS extra_n3
  FROM cluster_scale_events cse
  JOIN current_warehouses w USING (warehouse_id)
  LEFT JOIN max_concurrency mc USING (warehouse_id)
  WHERE COALESCE(mc.max_concurrent, 0) < (cse.max_clusters_observed * 10)
),
large_warehouses AS (
  SELECT warehouse_id, warehouse_name, warehouse_size, workspace_id
  FROM current_warehouses
  WHERE warehouse_size IN ('Large', 'X-Large', '2X-Large', '3X-Large', '4X-Large')
    AND COALESCE(warehouse_type, 'CLASSIC') != 'SERVERLESS'
),
qstats AS (
  SELECT compute.warehouse_id AS warehouse_id, COUNT(*) AS query_count,
    AVG(COALESCE(waiting_at_capacity_duration_ms, 0) + COALESCE(waiting_for_compute_duration_ms, 0)) AS avg_queue_ms,
    PERCENTILE_APPROX(
      GREATEST(0.0, (UNIX_TIMESTAMP(end_time) - UNIX_TIMESTAMP(start_time))
        - (COALESCE(waiting_at_capacity_duration_ms, 0) + COALESCE(waiting_for_compute_duration_ms, 0)) / 1000.0),
      0.5) AS median_duration_seconds
  FROM system.query.history
  WHERE start_time >= NOW() - INTERVAL 30 DAY
    AND compute.warehouse_id IN (SELECT warehouse_id FROM large_warehouses)
    AND end_time IS NOT NULL
  GROUP BY compute.warehouse_id
  HAVING COUNT(*) >= 5
),
oversized AS (
  SELECT lw.warehouse_id, lw.warehouse_name, lw.warehouse_size, lw.workspace_id,
         CAST(NULL AS STRING) AS warehouse_type, 'OVERSIZED' AS recommendation_type,
         CAST(NULL AS STRING) AS extra_1, q.avg_queue_ms AS extra_n1,
         q.median_duration_seconds AS extra_n2, q.query_count AS extra_n3
  FROM large_warehouses lw
  JOIN qstats q USING (warehouse_id)
  WHERE q.avg_queue_ms < 15000 AND q.median_duration_seconds < 180
)
SELECT CURRENT_DATE() AS snapshot_date, * FROM idle
UNION ALL SELECT CURRENT_DATE() AS snapshot_date, * FROM over_scaled
UNION ALL SELECT CURRENT_DATE() AS snapshot_date, * FROM oversized
"""

# Gold grain names stay route_name/model_name/client_user so Lakebase copies
# keep working. Source expressions are Unity Gateway columns; remaining_tables()
# rewrites them when the derived table is still on the legacy schema.
_CREATE_DAILY_AI_GATEWAY_TMPL = """
CREATE OR REPLACE TABLE `{catalog}`.`{schema}`.`daily_ai_gateway` CLUSTER BY (usage_date, workspace_id) AS
SELECT
  CAST({event_ts} AS DATE) AS usage_date,
  workspace_id,
  COALESCE(CAST({service} AS STRING), '__na__') AS route_name,
  COALESCE(CAST({model} AS STRING), '__na__') AS model_name,
  COALESCE(CAST({requester} AS STRING), '(unknown)') AS client_user,
  COUNT(*) AS request_count,
  SUM(CAST({input_tokens} AS BIGINT)) AS input_tokens,
  SUM(CAST({output_tokens} AS BIGINT)) AS output_tokens,
  SUM(CAST({total_tokens} AS BIGINT)) AS total_tokens,
  AVG(CAST({latency} AS DOUBLE)) AS avg_latency_ms,
  SUM(CASE WHEN CAST({status_code} AS INT) >= 400 THEN 1 ELSE 0 END) AS error_count,
  SUM(CASE WHEN CAST({status_code} AS INT) < 400 THEN 1 ELSE 0 END) AS success_count
FROM {ai_gateway_table}
WHERE CAST({event_ts} AS DATE) >= DATE_SUB(CURRENT_DATE(), {billing_lookback_days})
GROUP BY 1, 2, 3, 4, 5
"""

_UNITY_GATEWAY_SQL_DEFAULTS = {
    "event_ts": "event_time",
    "service": "COALESCE(service_name, endpoint_name)",
    "model": "destination_model",
    "requester": "requester",
    "input_tokens": "COALESCE(input_tokens, 0)",
    "output_tokens": "COALESCE(output_tokens, 0)",
    "total_tokens": "COALESCE(total_tokens, COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0))",
    "latency": "CAST(NULL AS DOUBLE)",
    "status_code": "COALESCE(status_code, 0)",
}

CREATE_DAILY_AI_GATEWAY = _CREATE_DAILY_AI_GATEWAY_TMPL.format(
    catalog="{catalog}",
    schema="{schema}",
    billing_lookback_days="{billing_lookback_days}",
    ai_gateway_table="{ai_gateway_table}",
    **_UNITY_GATEWAY_SQL_DEFAULTS,
)

_MERGE_DAILY_AI_GATEWAY_TMPL = """
MERGE INTO `{catalog}`.`{schema}`.`daily_ai_gateway` AS tgt
USING (
  SELECT
    CAST({event_ts} AS DATE) AS usage_date,
    workspace_id,
    COALESCE(CAST({service} AS STRING), '__na__') AS route_name,
    COALESCE(CAST({model} AS STRING), '__na__') AS model_name,
    COALESCE(CAST({requester} AS STRING), '(unknown)') AS client_user,
    COUNT(*) AS request_count,
    SUM(CAST({input_tokens} AS BIGINT)) AS input_tokens,
    SUM(CAST({output_tokens} AS BIGINT)) AS output_tokens,
    SUM(CAST({total_tokens} AS BIGINT)) AS total_tokens,
    AVG(CAST({latency} AS DOUBLE)) AS avg_latency_ms,
    SUM(CASE WHEN CAST({status_code} AS INT) >= 400 THEN 1 ELSE 0 END) AS error_count,
    SUM(CASE WHEN CAST({status_code} AS INT) < 400 THEN 1 ELSE 0 END) AS success_count
  FROM {ai_gateway_table}
  WHERE CAST({event_ts} AS DATE) >= DATE('{reprocess_start}')
    AND CAST({event_ts} AS DATE) <= CURRENT_DATE()
  GROUP BY 1, 2, 3, 4, 5
) AS src
ON tgt.usage_date = src.usage_date
  AND COALESCE(CAST(tgt.workspace_id AS STRING), '') = COALESCE(CAST(src.workspace_id AS STRING), '')
  AND COALESCE(tgt.route_name, '') = COALESCE(src.route_name, '')
  AND COALESCE(tgt.model_name, '') = COALESCE(src.model_name, '')
  AND COALESCE(tgt.client_user, '') = COALESCE(src.client_user, '')
WHEN MATCHED THEN UPDATE SET *
WHEN NOT MATCHED BY TARGET THEN INSERT *
"""

MERGE_DAILY_AI_GATEWAY = _MERGE_DAILY_AI_GATEWAY_TMPL.format(
    catalog="{catalog}",
    schema="{schema}",
    reprocess_start="{reprocess_start}",
    ai_gateway_table="{ai_gateway_table}",
    **_UNITY_GATEWAY_SQL_DEFAULTS,
)

REMAINING_TABLE_REFRESH = {
    "daily_user_spend": {
        "reprocess_days": 14,
        "pk": ["usage_date", "workspace_id", "user_email", "product_category"],
        "source_date_col": "usage_date",
        "overlap_days": 0,
    },
    "daily_untagged_objects": {
        "reprocess_days": 14,
        "pk": ["usage_date", "workspace_id", "object_type", "object_id"],
        "source_date_col": "usage_date",
        "overlap_days": 0,
    },
    "daily_aiml_models": {
        "reprocess_days": 14,
        "pk": ["usage_date", "workspace_id", "model_name", "model_type"],
        "source_date_col": "usage_date",
        "overlap_days": 0,
    },
    "daily_aiml_ml_clusters": {
        "reprocess_days": 14,
        "pk": ["usage_date", "workspace_id", "cluster_id"],
        "source_date_col": "usage_date",
        "overlap_days": 0,
    },
    "daily_aiml_agents": {
        "reprocess_days": 14,
        "pk": ["usage_date", "workspace_id", "agent_name"],
        "source_date_col": "usage_date",
        "overlap_days": 0,
    },
    "daily_object_spend": {
        "reprocess_days": 14,
        "pk": ["usage_date", "workspace_id", "object_type", "object_id"],
        "source_date_col": "usage_date",
        "overlap_days": 0,
    },
    "daily_infra_cluster_spend": {
        "reprocess_days": 14,
        "pk": ["usage_date", "workspace_id", "cluster_id"],
        "source_date_col": "usage_date",
        "overlap_days": 0,
    },
    "daily_dbsql_warehouse_type": {
        "reprocess_days": 7,
        "pk": ["usage_date", "workspace_id", "warehouse_id"],
        "source_date_col": "usage_date",
        "overlap_days": 0,
    },
    "daily_dbsql_region_scope": {
        "reprocess_days": 1,
        "pk": ["snapshot_date"],
        "source_date_col": "snapshot_date",
        "overlap_days": 0,
    },
    "warehouse_optimizer_findings": {
        "reprocess_days": 1,
        "pk": ["snapshot_date", "warehouse_id", "recommendation_type"],
        "source_date_col": "snapshot_date",
        "overlap_days": 0,
    },
}

REMAINING_CREATE = {
    "daily_user_spend": CREATE_DAILY_USER_SPEND,
    "daily_untagged_objects": CREATE_DAILY_UNTAGGED_OBJECTS,
    "daily_aiml_models": CREATE_DAILY_AIML_MODELS,
    "daily_aiml_ml_clusters": CREATE_DAILY_AIML_ML_CLUSTERS,
    "daily_aiml_agents": CREATE_DAILY_AIML_AGENTS,
    "daily_object_spend": CREATE_DAILY_OBJECT_SPEND,
    "daily_infra_cluster_spend": CREATE_DAILY_INFRA_CLUSTER_SPEND,
    "daily_dbsql_warehouse_type": CREATE_DAILY_DBSQL_WAREHOUSE_TYPE,
    "daily_dbsql_region_scope": CREATE_DAILY_DBSQL_REGION_SCOPE,
    "warehouse_optimizer_findings": CREATE_WAREHOUSE_OPTIMIZER_FINDINGS,
}

REMAINING_MERGE = {
    "daily_user_spend": MERGE_DAILY_USER_SPEND,
    "daily_untagged_objects": MERGE_DAILY_UNTAGGED_OBJECTS,
    "daily_aiml_models": MERGE_DAILY_AIML_MODELS,
    "daily_aiml_ml_clusters": MERGE_DAILY_AIML_ML_CLUSTERS,
    "daily_aiml_agents": MERGE_DAILY_AIML_AGENTS,
    "daily_object_spend": MERGE_DAILY_OBJECT_SPEND,
    "daily_infra_cluster_spend": MERGE_DAILY_INFRA_CLUSTER_SPEND,
    "daily_dbsql_warehouse_type": MERGE_DAILY_DBSQL_WAREHOUSE_TYPE,
}


def ai_gateway_build_sql() -> tuple[str, str] | None:
    """daily_ai_gateway CREATE/MERGE rendered for the live usage-table columns.

    Called at build time, not import time, so app startup never waits on a
    warehouse SHOW COLUMNS and legacy-schema tables get their real columns.
    """
    tbl = os.environ.get("AI_GATEWAY_USAGE_TABLE", "").strip()
    if not tbl:
        return None
    create, merge, _ = remaining_tables(probe_columns=True)
    return create["daily_ai_gateway"], merge["daily_ai_gateway"]


def remaining_tables(probe_columns: bool = False) -> tuple[dict, dict, dict]:
    """Include Unity Gateway gold only when the shared usage table is configured."""
    from backend.materialized_views_viewer import viewer_tables
    _v_create, _v_merge, _v_refresh = viewer_tables()
    create = dict(REMAINING_CREATE)
    create.update(_v_create)
    merge = dict(REMAINING_MERGE)
    merge.update(_v_merge)
    refresh = dict(REMAINING_TABLE_REFRESH)
    refresh.update(_v_refresh)
    tbl = os.environ.get("AI_GATEWAY_USAGE_TABLE", "").strip()
    if tbl:
        aliases = dict(_UNITY_GATEWAY_SQL_DEFAULTS)
        if probe_columns:
            from backend.unity_gateway import pick_usage_columns
            aliases.update(pick_usage_columns(tbl))
        create["daily_ai_gateway"] = _CREATE_DAILY_AI_GATEWAY_TMPL.format(
            catalog="{catalog}",
            schema="{schema}",
            billing_lookback_days="{billing_lookback_days}",
            ai_gateway_table=tbl,
            **aliases,
        )
        merge["daily_ai_gateway"] = _MERGE_DAILY_AI_GATEWAY_TMPL.format(
            catalog="{catalog}",
            schema="{schema}",
            reprocess_start="{reprocess_start}",
            ai_gateway_table=tbl,
            **aliases,
        )
        refresh["daily_ai_gateway"] = {
            "reprocess_days": 7,
            "pk": ["usage_date", "workspace_id", "route_name", "model_name", "client_user"],
            "source_date_col": "usage_date",
            "overlap_days": 1,
        }
    return create, merge, refresh


REMAINING_GOLD_SQL = {
    "users_summary": """
SELECT COUNT(DISTINCT user_email) AS user_count,
  COUNT(DISTINCT workspace_id) AS workspace_count,
  SUM(total_spend) AS total_spend, SUM(total_dbus) AS total_dbus,
  SUM(total_spend) / NULLIF(COUNT(DISTINCT user_email), 0) AS avg_spend_per_user
FROM `{catalog}`.`{schema}`.`daily_user_spend`
WHERE usage_date BETWEEN :start_date AND :end_date {ws_filter}
""",
    "users_top": """
SELECT user_email, SUM(total_spend) AS total_spend, SUM(total_dbus) AS total_dbus,
  COUNT(DISTINCT usage_date) AS active_days, COUNT(DISTINCT workspace_id) AS workspace_count
FROM `{catalog}`.`{schema}`.`daily_user_spend`
WHERE usage_date BETWEEN :start_date AND :end_date {ws_filter}
GROUP BY user_email ORDER BY total_spend DESC
""",
    "users_product": """
SELECT user_email, product_category, SUM(total_spend) AS spend
FROM `{catalog}`.`{schema}`.`daily_user_spend`
WHERE usage_date BETWEEN :start_date AND :end_date {ws_filter}
GROUP BY user_email, product_category ORDER BY spend DESC LIMIT 5000
""",
    "users_timeseries": """
SELECT usage_date AS date, user_email, SUM(total_spend) AS daily_spend
FROM `{catalog}`.`{schema}`.`daily_user_spend`
WHERE usage_date BETWEEN :start_date AND :end_date {ws_filter}
  AND user_email IN (
    SELECT user_email FROM (
      SELECT user_email, SUM(total_spend) AS s FROM `{catalog}`.`{schema}`.`daily_user_spend`
      WHERE usage_date BETWEEN :start_date AND :end_date {ws_filter}
      GROUP BY user_email ORDER BY s DESC LIMIT 6
    ) t
  )
GROUP BY usage_date, user_email ORDER BY date, daily_spend DESC
""",
    "users_by_workspace": """
SELECT workspace_id, COUNT(DISTINCT user_email) AS user_count, SUM(total_spend) AS total_spend
FROM `{catalog}`.`{schema}`.`daily_user_spend`
WHERE usage_date BETWEEN :start_date AND :end_date {ws_filter}
GROUP BY workspace_id ORDER BY total_spend DESC LIMIT 20
""",
    "users_spend_growth": """
SELECT CASE WHEN usage_date <= :mid_date THEN 'first_half' ELSE 'second_half' END AS period,
  SUM(total_spend) AS total_spend
FROM `{catalog}`.`{schema}`.`daily_user_spend`
WHERE usage_date BETWEEN :start_date AND :end_date {ws_filter}
GROUP BY 1
""",
    "users_workspace_detail": """
SELECT workspace_id, user_email, product_category, SUM(total_spend) AS spend
FROM `{catalog}`.`{schema}`.`daily_user_spend`
WHERE usage_date BETWEEN :start_date AND :end_date {ws_filter}
GROUP BY workspace_id, user_email, product_category
ORDER BY spend DESC LIMIT 2000
""",
    "users_growth": """
WITH base AS (
  SELECT date_trunc('month', usage_date) AS month, user_email AS user_id
  FROM `{catalog}`.`{schema}`.`daily_user_spend`
  WHERE usage_date BETWEEN :start_date AND :end_date AND user_email IS NOT NULL
),
user_first AS (SELECT user_id, MIN(month) AS first_month FROM base GROUP BY user_id)
SELECT date_format(b.month, 'yyyy-MM') AS month,
  COUNT(DISTINCT b.user_id) AS active_users,
  COUNT(DISTINCT CASE WHEN b.month = f.first_month THEN b.user_id END) AS new_users
FROM base b JOIN user_first f ON b.user_id = f.user_id
GROUP BY b.month ORDER BY month
""",
    "untagged_clusters": """
SELECT object_id AS cluster_id, MAX(object_name) AS cluster_name, MAX(extra) AS cluster_source,
  MAX(owner) AS owner, MAX(workspace_id) AS workspace_id,
  SUM(total_dbus) AS total_dbus, SUM(total_spend) AS total_spend, COUNT(DISTINCT usage_date) AS days_active
FROM `{catalog}`.`{schema}`.`daily_untagged_objects`
WHERE usage_date BETWEEN :start_date AND :end_date AND object_type = 'cluster' {ws_filter}
GROUP BY object_id ORDER BY total_spend DESC LIMIT 1000
""",
    "untagged_jobs": """
SELECT object_id AS job_id, MAX(object_name) AS job_name, MAX(workspace_id) AS workspace_id,
  SUM(total_dbus) AS total_dbus, SUM(total_spend) AS total_spend, COUNT(DISTINCT usage_date) AS days_active
FROM `{catalog}`.`{schema}`.`daily_untagged_objects`
WHERE usage_date BETWEEN :start_date AND :end_date AND object_type = 'job' {ws_filter}
GROUP BY object_id ORDER BY total_spend DESC LIMIT 1000
""",
    "untagged_pipelines": """
SELECT object_id AS pipeline_id, MAX(object_name) AS pipeline_name, MAX(workspace_id) AS workspace_id,
  SUM(total_dbus) AS total_dbus, SUM(total_spend) AS total_spend, COUNT(DISTINCT usage_date) AS days_active
FROM `{catalog}`.`{schema}`.`daily_untagged_objects`
WHERE usage_date BETWEEN :start_date AND :end_date AND object_type = 'pipeline' {ws_filter}
GROUP BY object_id ORDER BY total_spend DESC LIMIT 1000
""",
    "untagged_warehouses": """
SELECT object_id AS warehouse_id, MAX(object_name) AS warehouse_name, MAX(workspace_id) AS workspace_id,
  SUM(total_dbus) AS total_dbus, SUM(total_spend) AS total_spend, COUNT(DISTINCT usage_date) AS days_active
FROM `{catalog}`.`{schema}`.`daily_untagged_objects`
WHERE usage_date BETWEEN :start_date AND :end_date AND object_type = 'warehouse' {ws_filter}
GROUP BY object_id ORDER BY total_spend DESC LIMIT 1000
""",
    "untagged_endpoints": """
SELECT object_id AS endpoint_name, MAX(workspace_id) AS workspace_id,
  SUM(total_dbus) AS total_dbus, SUM(total_spend) AS total_spend, COUNT(DISTINCT usage_date) AS days_active
FROM `{catalog}`.`{schema}`.`daily_untagged_objects`
WHERE usage_date BETWEEN :start_date AND :end_date AND object_type = 'endpoint' {ws_filter}
GROUP BY object_id ORDER BY total_spend DESC LIMIT 1000
""",
    "aiml_models": """
SELECT model_name, model_type, SUM(total_dbus) AS total_dbus, SUM(total_spend) AS total_spend,
  COUNT(DISTINCT usage_date) AS days_active, COUNT(DISTINCT workspace_id) AS workspace_count
FROM `{catalog}`.`{schema}`.`daily_aiml_models`
WHERE usage_date BETWEEN :start_date AND :end_date {ws_filter}
GROUP BY model_name, model_type ORDER BY total_spend DESC LIMIT 50
""",
    "aiml_ml_clusters": """
SELECT MAX(cluster_name) AS cluster_name, cluster_id, MAX(runtime_version) AS runtime_version,
  MAX(owner) AS owner, MAX(workspace_id) AS workspace_id,
  SUM(total_dbus) AS total_dbus, SUM(total_spend) AS total_spend, COUNT(DISTINCT usage_date) AS days_active
FROM `{catalog}`.`{schema}`.`daily_aiml_ml_clusters`
WHERE usage_date BETWEEN :start_date AND :end_date {ws_filter}
GROUP BY cluster_id ORDER BY total_spend DESC
""",
    "aiml_agent_bricks": """
SELECT agent_name, MAX(endpoint_id) AS endpoint_id, MAX(workspace_id) AS workspace_id,
  MAX(billing_origin_product) AS billing_origin_product, MAX(served_entity_name) AS served_entity_name,
  MAX(agent_type) AS agent_type, SUM(total_dbus) AS total_dbus, SUM(total_spend) AS total_spend,
  COUNT(DISTINCT usage_date) AS days_active, COUNT(DISTINCT workspace_id) AS workspace_count,
  MIN(usage_date) AS first_seen, MAX(usage_date) AS last_seen,
  SUM(total_spend) / NULLIF(COUNT(DISTINCT usage_date), 0) AS avg_daily_spend
FROM `{catalog}`.`{schema}`.`daily_aiml_agents`
WHERE usage_date BETWEEN :start_date AND :end_date {ws_filter}
GROUP BY agent_name ORDER BY total_spend DESC
""",
    "infra_clusters": """
SELECT cluster_id, MAX(cluster_name) AS cluster_name, MAX(driver_instance_type) AS driver_instance_type,
  MAX(worker_instance_type) AS worker_instance_type, MAX(cluster_source) AS cluster_source,
  MAX(workspace_id) AS workspace_id, MAX(workspace_name) AS workspace_name, MAX(cloud) AS cloud,
  SUM(total_dbu_hours) AS total_dbu_hours, COUNT(DISTINCT usage_date) AS days_active
FROM `{catalog}`.`{schema}`.`daily_infra_cluster_spend`
WHERE usage_date BETWEEN :start_date AND :end_date {ws_filter}
GROUP BY cluster_id ORDER BY total_dbu_hours DESC LIMIT 100
""",
    "infra_timeseries": """
SELECT usage_date, MAX(cloud) AS cloud, SUM(total_dbu_hours) AS total_dbu_hours
FROM `{catalog}`.`{schema}`.`daily_infra_cluster_spend`
WHERE usage_date BETWEEN :start_date AND :end_date {ws_filter}
GROUP BY usage_date ORDER BY usage_date
""",
    "infra_summary": """
SELECT SUM(total_dbu_hours) AS total_cost, AVG(daily_clusters) AS avg_clusters_per_day,
  CASE WHEN AVG(daily_clusters) > 0 THEN AVG(daily_cost / daily_clusters) ELSE 0 END AS avg_cost_per_cluster,
  COUNT(*) AS days_in_range
FROM (
  SELECT usage_date, SUM(total_dbu_hours) AS daily_cost, COUNT(DISTINCT cluster_id) AS daily_clusters
  FROM `{catalog}`.`{schema}`.`daily_infra_cluster_spend`
  WHERE usage_date BETWEEN :start_date AND :end_date {ws_filter}
  GROUP BY usage_date
) d
""",
    "wh_meta": """
SELECT warehouse_id, MAX(warehouse_name) AS warehouse_name, MAX(warehouse_type) AS warehouse_type,
  MAX(warehouse_size) AS warehouse_size, MAX(workspace_id) AS workspace_id, MAX(workspace_name) AS workspace_name
FROM `{catalog}`.`{schema}`.`daily_dbsql_warehouse_type`
WHERE usage_date BETWEEN :start_date AND :end_date {ws_filter}
GROUP BY warehouse_id
""",
    "wh_type_billing": """
SELECT usage_date AS date, warehouse_id, SUM(daily_spend) AS daily_spend
FROM `{catalog}`.`{schema}`.`daily_dbsql_warehouse_type`
WHERE usage_date BETWEEN :start_date AND :end_date {ws_filter}
GROUP BY usage_date, warehouse_id
""",
    "region_scope": """
SELECT billing_ws_count AS billing_ws, compute_ws_count AS compute_ws
FROM `{catalog}`.`{schema}`.`daily_dbsql_region_scope`
ORDER BY snapshot_date DESC LIMIT 1
""",
    "warehouse_optimizer": """
SELECT * FROM `{catalog}`.`{schema}`.`warehouse_optimizer_findings`
WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM `{catalog}`.`{schema}`.`warehouse_optimizer_findings`)
""",
    "object_spend": """
SELECT usage_date, object_type, object_id, SUM(total_dbus) AS total_dbus, SUM(total_spend) AS total_spend
FROM `{catalog}`.`{schema}`.`daily_object_spend`
WHERE usage_date BETWEEN :start_date AND :end_date {ws_filter}
GROUP BY usage_date, object_type, object_id
""",
    "available_objects": """
SELECT object_type, object_id, MAX(object_name) AS object_name, MAX(workspace_id) AS workspace_id
FROM `{catalog}`.`{schema}`.`daily_object_spend`
WHERE usage_date >= DATE_SUB(CURRENT_DATE(), 90) AND object_type = :object_type
GROUP BY object_type, object_id ORDER BY object_id LIMIT 100
""",
    "monthly_spend": """
SELECT date_trunc('month', usage_date) AS month, SUM(total_spend) AS total_spend, SUM(total_dbus) AS total_dbus
FROM `{catalog}`.`{schema}`.`daily_usage_summary`
WHERE usage_date BETWEEN :start_date AND :end_date
GROUP BY 1 ORDER BY 1
""",
    "ai_gateway_rows": """
SELECT usage_date, workspace_id, route_name, model_name, client_user,
  request_count, input_tokens, output_tokens, total_tokens, avg_latency_ms, error_count, success_count
FROM `{catalog}`.`{schema}`.`daily_ai_gateway`
WHERE usage_date BETWEEN :start_date AND :end_date {ws_filter}
""",
}

from backend.materialized_views_viewer import VIEWER_GOLD_SQL  # noqa: E402, I001 — late to avoid a circular import

REMAINING_GOLD_SQL.update(VIEWER_GOLD_SQL)
