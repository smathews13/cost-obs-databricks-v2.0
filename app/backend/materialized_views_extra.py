"""Extra daily gold tables so remaining dashboard tabs can serve from Lakebase.

The warehouse still *builds* these from system.* (Postgres cannot see those).
The app reads the copies, not live system tables.
"""

from backend.materialized_views_remaining import remaining_tables

_REM_CREATE, _REM_MERGE, _REM_REFRESH = remaining_tables()

# genie_surface reads usage_metadata.genie.surface (same mapping as
# routers/genie_cost.genie_surface_case_sql); every Genie surface bills under the
# same SKUs, so the SKU patterns are only a fallback. Bump definition_version in
# EXTRA_TABLE_REFRESH when this changes: the MERGE window can't reclassify history.
CREATE_DAILY_SKU_BREAKDOWN = """
CREATE OR REPLACE TABLE `{catalog}`.`{schema}`.`daily_sku_breakdown` CLUSTER BY (usage_date, workspace_id) AS
SELECT
  u.usage_date,
  u.workspace_id,
  u.sku_name,
  u.billing_origin_product,
  u.usage_type,
  CASE
    WHEN u.sku_name LIKE '%ANTHROPIC%' THEN 'FMAPI - Anthropic'
    WHEN u.sku_name LIKE '%OPENAI%' THEN 'FMAPI - OpenAI'
    WHEN u.sku_name LIKE '%GEMINI%' THEN 'FMAPI - Gemini'
    WHEN u.sku_name LIKE '%SERVERLESS_REAL_TIME_INFERENCE%' THEN 'Serverless Inference'
    WHEN u.product_features.model_serving.offering_type = 'BATCH_INFERENCE' THEN 'Batch Inference'
    WHEN u.billing_origin_product = 'VECTOR_SEARCH' THEN 'AI Search'
    WHEN u.sku_name LIKE '%FINE_TUNING%' THEN 'Fine Tuning'
    WHEN u.billing_origin_product = 'MODEL_SERVING' THEN 'Model Serving'
    WHEN u.sku_name LIKE '%VECTOR_SEARCH%' THEN 'AI Search'
    ELSE 'Other AI/ML'
  END AS aiml_category,
  CASE
    WHEN u.billing_origin_product = 'GENIE' OR u.sku_name ILIKE '%GENIE%' OR u.sku_name ILIKE '%AI_BI_GENIE%' THEN
      CASE
        WHEN get_json_object(to_json(u.usage_metadata), '$.genie.surface') = 'GENIE_CODE' OR u.sku_name ILIKE '%GENIE_CODE%' THEN 'genie_code'
        WHEN get_json_object(to_json(u.usage_metadata), '$.genie.surface') = 'GENIE_ONE' OR u.sku_name ILIKE '%GENIE_ONE%' THEN 'genie_one'
        WHEN get_json_object(to_json(u.usage_metadata), '$.genie.surface') = 'GENIE_AGENTS' OR u.sku_name ILIKE '%GENIE_AGENTS%' THEN 'genie_agents'
        ELSE 'unknown'
      END
    ELSE NULL
  END AS genie_surface,
  CASE
    WHEN u.usage_type IN ('BILLABLE', 'TOKEN') THEN 'charged'
    WHEN u.usage_type IN ('NON_BILLABLE', 'RELATED') THEN 'free_tier'
    ELSE 'other'
  END AS charge_class,
  SUM(u.usage_quantity) AS total_dbus,
  SUM(u.usage_quantity * COALESCE(p.pricing.default, 0)) AS total_spend
FROM system.billing.usage u
LEFT JOIN system.billing.list_prices p
  ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND p.price_end_time IS NULL
WHERE u.usage_date >= DATE_SUB(CURRENT_DATE(), {billing_lookback_days})
  AND u.usage_quantity > 0
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8
"""

MERGE_DAILY_SKU_BREAKDOWN = """
MERGE INTO `{catalog}`.`{schema}`.`daily_sku_breakdown` AS tgt
USING (
  SELECT
    u.usage_date,
    u.workspace_id,
    u.sku_name,
    u.billing_origin_product,
    u.usage_type,
    CASE
      WHEN u.sku_name LIKE '%ANTHROPIC%' THEN 'FMAPI - Anthropic'
      WHEN u.sku_name LIKE '%OPENAI%' THEN 'FMAPI - OpenAI'
      WHEN u.sku_name LIKE '%GEMINI%' THEN 'FMAPI - Gemini'
      WHEN u.sku_name LIKE '%SERVERLESS_REAL_TIME_INFERENCE%' THEN 'Serverless Inference'
      WHEN u.product_features.model_serving.offering_type = 'BATCH_INFERENCE' THEN 'Batch Inference'
      WHEN u.billing_origin_product = 'VECTOR_SEARCH' THEN 'AI Search'
      WHEN u.sku_name LIKE '%FINE_TUNING%' THEN 'Fine Tuning'
      WHEN u.billing_origin_product = 'MODEL_SERVING' THEN 'Model Serving'
      WHEN u.sku_name LIKE '%VECTOR_SEARCH%' THEN 'AI Search'
      ELSE 'Other AI/ML'
    END AS aiml_category,
    CASE
      WHEN u.billing_origin_product = 'GENIE' OR u.sku_name ILIKE '%GENIE%' OR u.sku_name ILIKE '%AI_BI_GENIE%' THEN
        CASE
          WHEN get_json_object(to_json(u.usage_metadata), '$.genie.surface') = 'GENIE_CODE' OR u.sku_name ILIKE '%GENIE_CODE%' THEN 'genie_code'
          WHEN get_json_object(to_json(u.usage_metadata), '$.genie.surface') = 'GENIE_ONE' OR u.sku_name ILIKE '%GENIE_ONE%' THEN 'genie_one'
          WHEN get_json_object(to_json(u.usage_metadata), '$.genie.surface') = 'GENIE_AGENTS' OR u.sku_name ILIKE '%GENIE_AGENTS%' THEN 'genie_agents'
          ELSE 'unknown'
        END
      ELSE NULL
    END AS genie_surface,
    CASE
      WHEN u.usage_type IN ('BILLABLE', 'TOKEN') THEN 'charged'
      WHEN u.usage_type IN ('NON_BILLABLE', 'RELATED') THEN 'free_tier'
      ELSE 'other'
    END AS charge_class,
    SUM(u.usage_quantity) AS total_dbus,
    SUM(u.usage_quantity * COALESCE(p.pricing.default, 0)) AS total_spend
  FROM system.billing.usage u
  LEFT JOIN system.billing.list_prices p
    ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND p.price_end_time IS NULL
  WHERE u.usage_date >= DATE('{reprocess_start}')
    AND u.usage_date <= CURRENT_DATE()
    AND u.usage_quantity > 0
  GROUP BY 1, 2, 3, 4, 5, 6, 7, 8
) AS src
ON tgt.usage_date = src.usage_date
  AND COALESCE(tgt.workspace_id, '') = COALESCE(src.workspace_id, '')
  AND tgt.sku_name = src.sku_name
  AND COALESCE(tgt.billing_origin_product, '') = COALESCE(src.billing_origin_product, '')
  AND COALESCE(tgt.usage_type, '') = COALESCE(src.usage_type, '')
  AND COALESCE(tgt.aiml_category, '') = COALESCE(src.aiml_category, '')
  AND COALESCE(tgt.genie_surface, '') = COALESCE(src.genie_surface, '')
  AND COALESCE(tgt.charge_class, '') = COALESCE(src.charge_class, '')
WHEN MATCHED THEN UPDATE SET *
WHEN NOT MATCHED BY TARGET THEN INSERT *
"""

CREATE_DAILY_JOB_PIPELINE_SPEND = """
CREATE OR REPLACE TABLE `{catalog}`.`{schema}`.`daily_job_pipeline_spend` CLUSTER BY (usage_date, workspace_id) AS
WITH pipeline_info AS (
  SELECT pipeline_id, MAX(name) AS pipeline_name, MAX(created_by) AS creator_name
  FROM system.lakeflow.pipelines
  GROUP BY pipeline_id
)
SELECT
  u.usage_date,
  u.workspace_id,
  CASE
    WHEN u.usage_metadata.dlt_pipeline_id IS NOT NULL THEN 'SDP Pipeline'
    WHEN u.usage_metadata.job_id IS NOT NULL THEN 'Job'
    ELSE 'Unknown'
  END AS object_type,
  COALESCE(u.usage_metadata.dlt_pipeline_id, u.usage_metadata.job_id) AS object_id,
  COALESCE(pi.pipeline_name, u.usage_metadata.job_name, u.usage_metadata.dlt_pipeline_id, u.usage_metadata.job_id) AS object_name,
  COALESCE(MAX(pi.creator_name), MAX(u.identity_metadata.run_as)) AS owner,
  SUM(u.usage_quantity) AS total_dbus,
  SUM(u.usage_quantity * COALESCE(p.pricing.default, 0)) AS total_spend,
  COUNT(DISTINCT u.usage_metadata.job_run_id) AS total_runs
FROM system.billing.usage u
LEFT JOIN system.billing.list_prices p
  ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND p.price_end_time IS NULL
LEFT JOIN pipeline_info pi ON u.usage_metadata.dlt_pipeline_id = pi.pipeline_id
WHERE u.usage_date >= DATE_SUB(CURRENT_DATE(), {billing_lookback_days})
  AND u.usage_quantity > 0
  AND (u.billing_origin_product IN ('JOBS', 'DLT') OR u.usage_metadata.dlt_pipeline_id IS NOT NULL)
  AND (u.usage_metadata.dlt_pipeline_id IS NOT NULL OR u.usage_metadata.job_id IS NOT NULL)
GROUP BY 1, 2, 3, 4, 5
"""

MERGE_DAILY_JOB_PIPELINE_SPEND = """
MERGE INTO `{catalog}`.`{schema}`.`daily_job_pipeline_spend` AS tgt
USING (
  WITH pipeline_info AS (
    SELECT pipeline_id, MAX(name) AS pipeline_name, MAX(created_by) AS creator_name
    FROM system.lakeflow.pipelines
    GROUP BY pipeline_id
  )
  SELECT
    u.usage_date,
    u.workspace_id,
    CASE
      WHEN u.usage_metadata.dlt_pipeline_id IS NOT NULL THEN 'SDP Pipeline'
      WHEN u.usage_metadata.job_id IS NOT NULL THEN 'Job'
      ELSE 'Unknown'
    END AS object_type,
    COALESCE(u.usage_metadata.dlt_pipeline_id, u.usage_metadata.job_id) AS object_id,
    COALESCE(pi.pipeline_name, u.usage_metadata.job_name, u.usage_metadata.dlt_pipeline_id, u.usage_metadata.job_id) AS object_name,
    COALESCE(MAX(pi.creator_name), MAX(u.identity_metadata.run_as)) AS owner,
    SUM(u.usage_quantity) AS total_dbus,
    SUM(u.usage_quantity * COALESCE(p.pricing.default, 0)) AS total_spend,
    COUNT(DISTINCT u.usage_metadata.job_run_id) AS total_runs
  FROM system.billing.usage u
  LEFT JOIN system.billing.list_prices p
    ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND p.price_end_time IS NULL
  LEFT JOIN pipeline_info pi ON u.usage_metadata.dlt_pipeline_id = pi.pipeline_id
  WHERE u.usage_date >= DATE('{reprocess_start}')
    AND u.usage_date <= CURRENT_DATE()
    AND u.usage_quantity > 0
    AND (u.billing_origin_product IN ('JOBS', 'DLT') OR u.usage_metadata.dlt_pipeline_id IS NOT NULL)
    AND (u.usage_metadata.dlt_pipeline_id IS NOT NULL OR u.usage_metadata.job_id IS NOT NULL)
  GROUP BY 1, 2, 3, 4, 5
) AS src
ON tgt.usage_date = src.usage_date
  AND COALESCE(tgt.workspace_id, '') = COALESCE(src.workspace_id, '')
  AND tgt.object_type = src.object_type
  AND tgt.object_id = src.object_id
WHEN MATCHED THEN UPDATE SET *
WHEN NOT MATCHED BY TARGET THEN INSERT *
"""

CREATE_DAILY_INTERACTIVE_SPEND = """
CREATE OR REPLACE TABLE `{catalog}`.`{schema}`.`daily_interactive_spend` CLUSTER BY (usage_date, workspace_id) AS
WITH cluster_info AS (
  SELECT cluster_id, MAX(cluster_name) AS cluster_name
  FROM system.compute.clusters
  GROUP BY cluster_id
)
SELECT
  u.usage_date,
  u.workspace_id,
  u.usage_metadata.cluster_id AS cluster_id,
  MAX(ci.cluster_name) AS cluster_name,
  u.usage_metadata.notebook_path AS notebook_path,
  u.identity_metadata.run_as AS run_as_user,
  SUM(u.usage_quantity) AS total_dbus,
  SUM(u.usage_quantity * COALESCE(p.pricing.default, 0)) AS total_spend,
  COUNT(DISTINCT u.usage_metadata.notebook_id) AS notebook_count
FROM system.billing.usage u
LEFT JOIN system.billing.list_prices p
  ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND p.price_end_time IS NULL
LEFT JOIN cluster_info ci ON u.usage_metadata.cluster_id = ci.cluster_id
WHERE u.usage_date >= DATE_SUB(CURRENT_DATE(), {billing_lookback_days})
  AND u.usage_quantity > 0
  AND u.sku_name LIKE '%ALL_PURPOSE%'
GROUP BY 1, 2, 3, 5, 6
"""

MERGE_DAILY_INTERACTIVE_SPEND = """
MERGE INTO `{catalog}`.`{schema}`.`daily_interactive_spend` AS tgt
USING (
  WITH cluster_info AS (
    SELECT cluster_id, MAX(cluster_name) AS cluster_name
    FROM system.compute.clusters
    GROUP BY cluster_id
  )
  SELECT
    u.usage_date,
    u.workspace_id,
    u.usage_metadata.cluster_id AS cluster_id,
    MAX(ci.cluster_name) AS cluster_name,
    u.usage_metadata.notebook_path AS notebook_path,
    u.identity_metadata.run_as AS run_as_user,
    SUM(u.usage_quantity) AS total_dbus,
    SUM(u.usage_quantity * COALESCE(p.pricing.default, 0)) AS total_spend,
    COUNT(DISTINCT u.usage_metadata.notebook_id) AS notebook_count
  FROM system.billing.usage u
  LEFT JOIN system.billing.list_prices p
    ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND p.price_end_time IS NULL
  LEFT JOIN cluster_info ci ON u.usage_metadata.cluster_id = ci.cluster_id
  WHERE u.usage_date >= DATE('{reprocess_start}')
    AND u.usage_date <= CURRENT_DATE()
    AND u.usage_quantity > 0
    AND u.sku_name LIKE '%ALL_PURPOSE%'
  GROUP BY 1, 2, 3, 5, 6
) AS src
ON tgt.usage_date = src.usage_date
  AND COALESCE(tgt.workspace_id, '') = COALESCE(src.workspace_id, '')
  AND COALESCE(tgt.cluster_id, '') = COALESCE(src.cluster_id, '')
  AND COALESCE(tgt.notebook_path, '') = COALESCE(src.notebook_path, '')
  AND COALESCE(tgt.run_as_user, '') = COALESCE(src.run_as_user, '')
WHEN MATCHED THEN UPDATE SET *
WHEN NOT MATCHED BY TARGET THEN INSERT *
"""

CREATE_DAILY_TAG_COVERAGE = """
CREATE OR REPLACE TABLE `{catalog}`.`{schema}`.`daily_tag_coverage` CLUSTER BY (usage_date, workspace_id) AS
SELECT
  u.usage_date,
  u.workspace_id,
  SUM(u.usage_quantity * COALESCE(p.pricing.default, 0)) AS total_spend,
  SUM(CASE WHEN u.custom_tags IS NOT NULL THEN u.usage_quantity * COALESCE(p.pricing.default, 0) ELSE 0 END) AS tagged_spend
FROM system.billing.usage u
LEFT JOIN system.billing.list_prices p
  ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND p.price_end_time IS NULL
WHERE u.usage_date >= DATE_SUB(CURRENT_DATE(), {billing_lookback_days})
  AND u.usage_quantity > 0
GROUP BY u.usage_date, u.workspace_id
"""

MERGE_DAILY_TAG_COVERAGE = """
MERGE INTO `{catalog}`.`{schema}`.`daily_tag_coverage` AS tgt
USING (
  SELECT
    u.usage_date,
    u.workspace_id,
    SUM(u.usage_quantity * COALESCE(p.pricing.default, 0)) AS total_spend,
    SUM(CASE WHEN u.custom_tags IS NOT NULL THEN u.usage_quantity * COALESCE(p.pricing.default, 0) ELSE 0 END) AS tagged_spend
  FROM system.billing.usage u
  LEFT JOIN system.billing.list_prices p
    ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND p.price_end_time IS NULL
  WHERE u.usage_date >= DATE('{reprocess_start}')
    AND u.usage_date <= CURRENT_DATE()
    AND u.usage_quantity > 0
  GROUP BY u.usage_date, u.workspace_id
) AS src
ON tgt.usage_date = src.usage_date
  AND COALESCE(tgt.workspace_id, '') = COALESCE(src.workspace_id, '')
WHEN MATCHED THEN UPDATE SET *
WHEN NOT MATCHED BY TARGET THEN INSERT *
"""

CREATE_DAILY_AIML_ENDPOINTS = """
CREATE OR REPLACE TABLE `{catalog}`.`{schema}`.`daily_aiml_endpoints` CLUSTER BY (usage_date, workspace_id) AS
SELECT
  u.usage_date,
  u.workspace_id,
  COALESCE(u.usage_metadata.endpoint_name, 'UNKNOWN') AS endpoint_name,
  u.sku_name,
  CASE WHEN u.sku_name LIKE '%LAUNCH%' THEN 'Launch (Scale-from-Zero)' ELSE 'Steady State' END AS cost_type,
  SUM(u.usage_quantity) AS total_dbus,
  SUM(u.usage_quantity * COALESCE(p.pricing.default, 0)) AS total_spend
FROM system.billing.usage u
LEFT JOIN system.billing.list_prices p
  ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND p.price_end_time IS NULL
WHERE u.usage_date >= DATE_SUB(CURRENT_DATE(), {billing_lookback_days})
  AND u.usage_quantity > 0
  AND u.sku_name LIKE '%SERVERLESS_REAL_TIME_INFERENCE%'
GROUP BY 1, 2, 3, 4, 5
"""

MERGE_DAILY_AIML_ENDPOINTS = """
MERGE INTO `{catalog}`.`{schema}`.`daily_aiml_endpoints` AS tgt
USING (
  SELECT
    u.usage_date,
    u.workspace_id,
    COALESCE(u.usage_metadata.endpoint_name, 'UNKNOWN') AS endpoint_name,
    u.sku_name,
    CASE WHEN u.sku_name LIKE '%LAUNCH%' THEN 'Launch (Scale-from-Zero)' ELSE 'Steady State' END AS cost_type,
    SUM(u.usage_quantity) AS total_dbus,
    SUM(u.usage_quantity * COALESCE(p.pricing.default, 0)) AS total_spend
  FROM system.billing.usage u
  LEFT JOIN system.billing.list_prices p
    ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND p.price_end_time IS NULL
  WHERE u.usage_date >= DATE('{reprocess_start}')
    AND u.usage_date <= CURRENT_DATE()
    AND u.usage_quantity > 0
    AND u.sku_name LIKE '%SERVERLESS_REAL_TIME_INFERENCE%'
  GROUP BY 1, 2, 3, 4, 5
) AS src
ON tgt.usage_date = src.usage_date
  AND COALESCE(tgt.workspace_id, '') = COALESCE(src.workspace_id, '')
  AND COALESCE(tgt.endpoint_name, '') = COALESCE(src.endpoint_name, '')
  AND COALESCE(tgt.sku_name, '') = COALESCE(src.sku_name, '')
  AND COALESCE(tgt.cost_type, '') = COALESCE(src.cost_type, '')
WHEN MATCHED THEN UPDATE SET *
WHEN NOT MATCHED BY TARGET THEN INSERT *
"""

CREATE_DAILY_CLOUD_ACTUALS = """
CREATE OR REPLACE TABLE `{catalog}`.`{schema}`.`daily_cloud_actuals` CLUSTER BY (usage_date) AS
SELECT
  DATE(usage_date) AS usage_date,
  usage_metadata.cluster_id AS cluster_id,
  usage_metadata.warehouse_id AS warehouse_id,
  charge_type,
  SUM(unblended_cost) AS unblended_cost,
  SUM(net_unblended_cost) AS net_unblended_cost,
  SUM(amortized_cost) AS amortized_cost,
  SUM(net_amortized_cost) AS net_amortized_cost
FROM `{catalog}`.`{schema}`.`actuals_gold`
WHERE DATE(usage_date) >= DATE_SUB(CURRENT_DATE(), {billing_lookback_days})
GROUP BY 1, 2, 3, 4
"""

MERGE_DAILY_CLOUD_ACTUALS = """
MERGE INTO `{catalog}`.`{schema}`.`daily_cloud_actuals` AS tgt
USING (
  SELECT
    DATE(usage_date) AS usage_date,
    usage_metadata.cluster_id AS cluster_id,
    usage_metadata.warehouse_id AS warehouse_id,
    charge_type,
    SUM(unblended_cost) AS unblended_cost,
    SUM(net_unblended_cost) AS net_unblended_cost,
    SUM(amortized_cost) AS amortized_cost,
    SUM(net_amortized_cost) AS net_amortized_cost
  FROM `{catalog}`.`{schema}`.`actuals_gold`
  WHERE DATE(usage_date) >= DATE('{reprocess_start}')
    AND DATE(usage_date) <= CURRENT_DATE()
  GROUP BY 1, 2, 3, 4
) AS src
ON tgt.usage_date = src.usage_date
  AND COALESCE(tgt.cluster_id, '') = COALESCE(src.cluster_id, '')
  AND COALESCE(tgt.warehouse_id, '') = COALESCE(src.warehouse_id, '')
  AND COALESCE(tgt.charge_type, '') = COALESCE(src.charge_type, '')
WHEN MATCHED THEN UPDATE SET *
WHEN NOT MATCHED BY TARGET THEN INSERT *
"""

CREATE_DAILY_DBSQL_TOP = """
CREATE OR REPLACE TABLE `{catalog}`.`{schema}`.`daily_dbsql_top` CLUSTER BY (usage_date) AS
SELECT
  CAST(start_time AS DATE) AS usage_date,
  statement_id,
  query_source_type,
  executed_by,
  warehouse_id,
  workspace_id,
  SUBSTRING(statement_text, 1, 200) AS statement_preview,
  duration_seconds,
  query_attributed_dollars_estimation AS cost,
  query_attributed_dbus_estimation AS dbus,
  query_profile_url,
  url_helper AS source_url,
  start_time,
  end_time
FROM (
  SELECT
    *,
    ROW_NUMBER() OVER (
      PARTITION BY CAST(start_time AS DATE)
      ORDER BY query_attributed_dollars_estimation DESC
    ) AS rn
  FROM `{catalog}`.`{schema}`.`dbsql_cost_per_query`
  WHERE CAST(start_time AS DATE) >= DATE_SUB(CURRENT_DATE(), {billing_lookback_days})
) ranked
WHERE rn <= 50
"""

MERGE_DAILY_DBSQL_TOP = """
MERGE INTO `{catalog}`.`{schema}`.`daily_dbsql_top` AS tgt
USING (
  SELECT
    CAST(start_time AS DATE) AS usage_date,
    statement_id,
    query_source_type,
    executed_by,
    warehouse_id,
    workspace_id,
    SUBSTRING(statement_text, 1, 200) AS statement_preview,
    duration_seconds,
    query_attributed_dollars_estimation AS cost,
    query_attributed_dbus_estimation AS dbus,
    query_profile_url,
    url_helper AS source_url,
    start_time,
    end_time
  FROM (
    SELECT
      *,
      ROW_NUMBER() OVER (
        PARTITION BY CAST(start_time AS DATE)
        ORDER BY query_attributed_dollars_estimation DESC
      ) AS rn
    FROM `{catalog}`.`{schema}`.`dbsql_cost_per_query`
    WHERE CAST(start_time AS DATE) >= DATE('{reprocess_start}')
      AND CAST(start_time AS DATE) <= CURRENT_DATE()
  ) ranked
  WHERE rn <= 50
) AS src
ON tgt.usage_date = src.usage_date
  AND COALESCE(tgt.statement_id, '') = COALESCE(src.statement_id, '')
WHEN MATCHED THEN UPDATE SET *
WHEN NOT MATCHED BY TARGET THEN INSERT *
"""

CREATE_DAILY_DBSQL_SOURCE = """
CREATE OR REPLACE TABLE `{catalog}`.`{schema}`.`daily_dbsql_source` CLUSTER BY (usage_date, workspace_id) AS
SELECT
  CAST(start_time AS DATE) AS usage_date,
  workspace_id,
  query_source_type,
  warehouse_id,
  executed_by,
  COUNT(*) AS query_count,
  SUM(query_attributed_dollars_estimation) AS total_spend,
  SUM(query_attributed_dbus_estimation) AS total_dbus,
  AVG(duration_seconds) AS avg_duration_seconds
FROM `{catalog}`.`{schema}`.`dbsql_cost_per_query`
WHERE CAST(start_time AS DATE) >= DATE_SUB(CURRENT_DATE(), {billing_lookback_days})
GROUP BY 1, 2, 3, 4, 5
"""

MERGE_DAILY_DBSQL_SOURCE = """
MERGE INTO `{catalog}`.`{schema}`.`daily_dbsql_source` AS tgt
USING (
  SELECT
    CAST(start_time AS DATE) AS usage_date,
    workspace_id,
    query_source_type,
    warehouse_id,
    executed_by,
    COUNT(*) AS query_count,
    SUM(query_attributed_dollars_estimation) AS total_spend,
    SUM(query_attributed_dbus_estimation) AS total_dbus,
    AVG(duration_seconds) AS avg_duration_seconds
  FROM `{catalog}`.`{schema}`.`dbsql_cost_per_query`
  WHERE CAST(start_time AS DATE) >= DATE('{reprocess_start}')
    AND CAST(start_time AS DATE) <= CURRENT_DATE()
  GROUP BY 1, 2, 3, 4, 5
) AS src
ON tgt.usage_date = src.usage_date
  AND COALESCE(tgt.workspace_id, '') = COALESCE(src.workspace_id, '')
  AND COALESCE(tgt.query_source_type, '') = COALESCE(src.query_source_type, '')
  AND COALESCE(tgt.warehouse_id, '') = COALESCE(src.warehouse_id, '')
  AND COALESCE(tgt.executed_by, '') = COALESCE(src.executed_by, '')
WHEN MATCHED THEN UPDATE SET *
WHEN NOT MATCHED BY TARGET THEN INSERT *
"""

EXTRA_TABLE_REFRESH = {
    "daily_sku_breakdown": {
        "reprocess_days": 14,
        "pk": ["usage_date", "workspace_id", "sku_name", "billing_origin_product", "usage_type", "aiml_category", "genie_surface", "charge_class"],
        "source_date_col": "usage_date",
        "overlap_days": 0,
        # 2: genie_surface from usage_metadata.genie.surface (was SKU patterns only).
        "definition_version": 2,
    },
    "daily_job_pipeline_spend": {
        "reprocess_days": 14,
        "pk": ["usage_date", "workspace_id", "object_type", "object_id"],
        "source_date_col": "usage_date",
        "overlap_days": 0,
    },
    "daily_interactive_spend": {
        "reprocess_days": 14,
        "pk": ["usage_date", "workspace_id", "cluster_id", "notebook_path", "run_as_user"],
        "source_date_col": "usage_date",
        "overlap_days": 0,
    },
    "daily_tag_coverage": {
        "reprocess_days": 14,
        "pk": ["usage_date", "workspace_id"],
        "source_date_col": "usage_date",
        "overlap_days": 0,
    },
    "daily_dbsql_source": {
        "reprocess_days": 7,
        "pk": ["usage_date", "workspace_id", "query_source_type", "warehouse_id", "executed_by"],
        "source_date_col": "usage_date",
        "overlap_days": 1,
    },
    "daily_aiml_endpoints": {
        "reprocess_days": 14,
        "pk": ["usage_date", "workspace_id", "endpoint_name", "sku_name", "cost_type"],
        "source_date_col": "usage_date",
        "overlap_days": 0,
    },
    "daily_cloud_actuals": {
        "reprocess_days": 14,
        "pk": ["usage_date", "cluster_id", "warehouse_id", "charge_type"],
        "source_date_col": "usage_date",
        "overlap_days": 0,
    },
    "daily_dbsql_top": {
        "reprocess_days": 7,
        "pk": ["usage_date", "statement_id"],
        "source_date_col": "usage_date",
        "overlap_days": 1,
    },
    **_REM_REFRESH,
}

EXTRA_CREATE = {
    "daily_sku_breakdown": CREATE_DAILY_SKU_BREAKDOWN,
    "daily_job_pipeline_spend": CREATE_DAILY_JOB_PIPELINE_SPEND,
    "daily_interactive_spend": CREATE_DAILY_INTERACTIVE_SPEND,
    "daily_tag_coverage": CREATE_DAILY_TAG_COVERAGE,
    "daily_dbsql_source": CREATE_DAILY_DBSQL_SOURCE,
    "daily_aiml_endpoints": CREATE_DAILY_AIML_ENDPOINTS,
    "daily_cloud_actuals": CREATE_DAILY_CLOUD_ACTUALS,
    "daily_dbsql_top": CREATE_DAILY_DBSQL_TOP,
    **_REM_CREATE,
}

EXTRA_MERGE = {
    "daily_sku_breakdown": MERGE_DAILY_SKU_BREAKDOWN,
    "daily_job_pipeline_spend": MERGE_DAILY_JOB_PIPELINE_SPEND,
    "daily_interactive_spend": MERGE_DAILY_INTERACTIVE_SPEND,
    "daily_tag_coverage": MERGE_DAILY_TAG_COVERAGE,
    "daily_dbsql_source": MERGE_DAILY_DBSQL_SOURCE,
    "daily_aiml_endpoints": MERGE_DAILY_AIML_ENDPOINTS,
    "daily_cloud_actuals": MERGE_DAILY_CLOUD_ACTUALS,
    "daily_dbsql_top": MERGE_DAILY_DBSQL_TOP,
    **_REM_MERGE,
}

# MV reader SQL (Delta dialect). Postgres twins live in lakebase_read.py.
MV_SKU_BREAKDOWN = """
SELECT
  sku_name AS product,
  COUNT(DISTINCT workspace_id) AS workspaces_using,
  SUM(total_dbus) AS total_dbus,
  SUM(total_spend) AS total_spend,
  ROUND(100.0 * SUM(total_spend) / NULLIF(SUM(SUM(total_spend)) OVER (), 0), 2) AS percentage
FROM `{catalog}`.`{schema}`.`daily_sku_breakdown`
WHERE usage_date BETWEEN :start_date AND :end_date
  {ws_filter}
GROUP BY sku_name
ORDER BY total_spend DESC
LIMIT 100
"""

MV_PIPELINE_OBJECTS = """
SELECT
  object_type,
  object_id,
  MAX(object_name) AS object_name,
  MAX(workspace_id) AS workspace_id,
  CAST(NULL AS STRING) AS object_state,
  MAX(owner) AS owner,
  SUM(total_dbus) AS total_dbus,
  SUM(total_spend) AS total_spend,
  SUM(total_runs) AS total_runs
FROM `{catalog}`.`{schema}`.`daily_job_pipeline_spend`
WHERE usage_date BETWEEN :start_date AND :end_date
  {ws_filter}
GROUP BY object_type, object_id
ORDER BY total_spend DESC
LIMIT 200
"""

MV_INTERACTIVE_BREAKDOWN = """
SELECT
  cluster_id,
  MAX(cluster_name) AS cluster_name,
  notebook_path,
  run_as_user,
  MAX(workspace_id) AS workspace_id,
  CAST(NULL AS STRING) AS cluster_state,
  SUM(total_dbus) AS total_dbus,
  SUM(total_spend) AS total_spend,
  COUNT(DISTINCT usage_date) AS days_active,
  SUM(notebook_count) AS notebook_count
FROM `{catalog}`.`{schema}`.`daily_interactive_spend`
WHERE usage_date BETWEEN :start_date AND :end_date
  {ws_filter}
GROUP BY cluster_id, notebook_path, run_as_user
ORDER BY total_spend DESC
LIMIT 100
"""

MV_TAGGING_COVERAGE = """
SELECT
  SUM(total_spend) AS total_spend,
  SUM(tagged_spend) AS tagged_spend,
  SUM(total_spend) - SUM(tagged_spend) AS untagged_spend,
  COUNT(DISTINCT CASE WHEN tagged_spend > 0 THEN workspace_id END) AS tagged_workspaces,
  COUNT(DISTINCT CASE WHEN total_spend > tagged_spend THEN workspace_id END) AS untagged_workspaces
FROM `{catalog}`.`{schema}`.`daily_tag_coverage`
WHERE usage_date BETWEEN :start_date AND :end_date
  {ws_filter}
"""

DBSQL_GOLD_SQL = {
    "summary": """
SELECT
  SUM(query_count) AS total_queries,
  COUNT(DISTINCT executed_by) AS unique_users,
  COUNT(DISTINCT warehouse_id) AS unique_warehouses,
  SUM(total_spend) AS total_spend,
  SUM(total_dbus) AS total_dbus,
  CASE WHEN SUM(query_count) > 0 THEN SUM(total_spend) / SUM(query_count) ELSE 0 END AS avg_cost_per_query,
  AVG(avg_duration_seconds) AS avg_duration_seconds
FROM `{catalog}`.`{schema}`.`daily_dbsql_source`
WHERE usage_date BETWEEN :start_date AND :end_date
  {ws_filter}
""",
    "by_source": """
SELECT
  query_source_type,
  SUM(query_count) AS query_count,
  SUM(total_spend) AS total_spend,
  SUM(total_dbus) AS total_dbus,
  CASE WHEN SUM(query_count) > 0 THEN SUM(total_spend) / SUM(query_count) ELSE 0 END AS avg_cost_per_query
FROM `{catalog}`.`{schema}`.`daily_dbsql_source`
WHERE usage_date BETWEEN :start_date AND :end_date
  {ws_filter}
GROUP BY query_source_type
ORDER BY total_spend DESC
""",
    "by_user": """
SELECT
  executed_by,
  query_source_type,
  SUM(query_count) AS query_count,
  SUM(total_spend) AS total_spend,
  SUM(total_dbus) AS total_dbus
FROM `{catalog}`.`{schema}`.`daily_dbsql_source`
WHERE usage_date BETWEEN :start_date AND :end_date
  {ws_filter}
GROUP BY executed_by, query_source_type
ORDER BY total_spend DESC
LIMIT 100
""",
    "by_warehouse": """
SELECT
  warehouse_id,
  SUM(query_count) AS query_count,
  COUNT(DISTINCT executed_by) AS unique_users,
  SUM(total_spend) AS total_spend,
  SUM(total_dbus) AS total_dbus
FROM `{catalog}`.`{schema}`.`daily_dbsql_source`
WHERE usage_date BETWEEN :start_date AND :end_date
  {ws_filter}
GROUP BY warehouse_id
ORDER BY total_spend DESC
LIMIT 50
""",
    "timeseries": """
SELECT
  usage_date AS date,
  query_source_type,
  SUM(query_count) AS query_count,
  SUM(total_spend) AS daily_spend,
  SUM(total_dbus) AS daily_dbus
FROM `{catalog}`.`{schema}`.`daily_dbsql_source`
WHERE usage_date BETWEEN :start_date AND :end_date
  {ws_filter}
GROUP BY usage_date, query_source_type
ORDER BY date
""",
    "top_queries": """
SELECT
  statement_id,
  query_source_type,
  executed_by,
  warehouse_id,
  workspace_id,
  statement_preview,
  duration_seconds,
  cost,
  dbus,
  query_profile_url,
  source_url,
  start_time,
  end_time
FROM `{catalog}`.`{schema}`.`daily_dbsql_top`
WHERE usage_date BETWEEN :start_date AND :end_date
  {ws_filter}
ORDER BY cost DESC
LIMIT :limit
""",
}

_AIML_SKU_WHERE = """
  AND (
    billing_origin_product IN ('MODEL_SERVING', 'VECTOR_SEARCH', 'FINE_TUNING')
    OR sku_name LIKE '%ANTHROPIC%' OR sku_name LIKE '%OPENAI%' OR sku_name LIKE '%GEMINI%'
    OR sku_name LIKE '%INFERENCE%' OR sku_name LIKE '%FINE_TUNING%' OR sku_name LIKE '%VECTOR_SEARCH%'
    OR aiml_category IN (
      'FMAPI - Anthropic', 'FMAPI - OpenAI', 'FMAPI - Gemini',
      'Serverless Inference', 'Batch Inference', 'AI Search',
      'Fine Tuning', 'Model Serving'
    )
  )
"""

AIML_GOLD_SQL = {
    "aiml_summary": """
SELECT
  SUM(total_dbus) AS total_dbus,
  SUM(total_spend) AS total_spend,
  COUNT(DISTINCT workspace_id) AS workspace_count,
  0 AS endpoint_count,
  COUNT(DISTINCT usage_date) AS days_in_range,
  MIN(usage_date) AS first_date,
  MAX(usage_date) AS last_date,
  0 AS avg_endpoints_per_day,
  0 AS avg_cost_per_endpoint
FROM `{catalog}`.`{schema}`.`daily_sku_breakdown`
WHERE usage_date BETWEEN :start_date AND :end_date
  {ws_filter}
""" + _AIML_SKU_WHERE,
    "aiml_providers": """
SELECT
  CASE
    WHEN sku_name LIKE '%ANTHROPIC%' THEN 'Anthropic'
    WHEN sku_name LIKE '%OPENAI%' THEN 'OpenAI'
    WHEN sku_name LIKE '%GEMINI%' THEN 'Gemini'
    ELSE 'Other'
  END AS provider,
  sku_name,
  SUM(total_dbus) AS total_dbus,
  SUM(total_spend) AS total_spend,
  COUNT(DISTINCT workspace_id) AS workspace_count
FROM `{catalog}`.`{schema}`.`daily_sku_breakdown`
WHERE usage_date BETWEEN :start_date AND :end_date
  {ws_filter}
  AND (sku_name LIKE '%ANTHROPIC%' OR sku_name LIKE '%OPENAI%' OR sku_name LIKE '%GEMINI%')
GROUP BY 1, sku_name
ORDER BY total_spend DESC
""",
    "aiml_endpoints": """
SELECT
  endpoint_name,
  workspace_id,
  sku_name,
  cost_type,
  SUM(total_dbus) AS total_dbus,
  SUM(total_spend) AS total_spend,
  COUNT(DISTINCT usage_date) AS days_active
FROM `{catalog}`.`{schema}`.`daily_aiml_endpoints`
WHERE usage_date BETWEEN :start_date AND :end_date
  {ws_filter}
GROUP BY endpoint_name, workspace_id, sku_name, cost_type
ORDER BY total_spend DESC
LIMIT 500
""",
    "aiml_categories": """
SELECT
  aiml_category AS category,
  SUM(total_dbus) AS total_dbus,
  SUM(total_spend) AS total_spend,
  COUNT(DISTINCT workspace_id) AS workspace_count
FROM `{catalog}`.`{schema}`.`daily_sku_breakdown`
WHERE usage_date BETWEEN :start_date AND :end_date
  {ws_filter}
""" + _AIML_SKU_WHERE + """
GROUP BY aiml_category
ORDER BY total_spend DESC
""",
    "aiml_timeseries": """
SELECT
  usage_date,
  CASE
    WHEN aiml_category = 'FMAPI - Anthropic' THEN 'Anthropic'
    WHEN aiml_category = 'FMAPI - OpenAI' THEN 'OpenAI'
    WHEN aiml_category = 'FMAPI - Gemini' THEN 'Gemini'
    WHEN aiml_category = 'Model Serving' THEN 'Other Model Serving'
    ELSE aiml_category
  END AS category,
  SUM(total_dbus) AS total_dbus,
  SUM(total_spend) AS total_spend
FROM `{catalog}`.`{schema}`.`daily_sku_breakdown`
WHERE usage_date BETWEEN :start_date AND :end_date
  {ws_filter}
""" + _AIML_SKU_WHERE + """
GROUP BY usage_date, 2
ORDER BY usage_date, 2
""",
    "aiml_sku_catalog": """
SELECT
  sku_name,
  billing_origin_product,
  usage_type,
  SUM(1) AS usage_records,
  SUM(total_dbus) AS total_dbus,
  SUM(total_spend) AS total_spend
FROM `{catalog}`.`{schema}`.`daily_sku_breakdown`
WHERE usage_date BETWEEN :start_date AND :end_date
  {ws_filter}
  AND (
    billing_origin_product IN ('MODEL_SERVING', 'VECTOR_SEARCH', 'FINE_TUNING', 'AGENT_BRICKS', 'SUPERVISOR_AGENT', 'KNOWLEDGE_AGENT')
    OR sku_name LIKE '%ANTHROPIC%' OR sku_name LIKE '%OPENAI%' OR sku_name LIKE '%GEMINI%'
    OR sku_name LIKE '%INFERENCE%' OR sku_name LIKE '%FINE_TUNING%' OR sku_name LIKE '%VECTOR_SEARCH%'
  )
GROUP BY sku_name, billing_origin_product, usage_type
ORDER BY total_spend DESC
""",
}
