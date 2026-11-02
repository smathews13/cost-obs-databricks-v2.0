"""Azure Cost Export Setup - Creates medallion architecture tables for actual Azure costs.

Mirrors aws_cur_setup.py but for Azure Cost Management Export (amortized cost format).

Prerequisites (customer must complete):
1. Enable Cost Management exports in Azure Portal
2. Configure export to Azure Blob Storage (CSV or Parquet)
3. Create Unity Catalog Storage Credential with Azure Blob access
4. Create Unity Catalog External Location pointing to the export container

This module automates:
- Bronze table creation (raw Azure Cost Export CSV/Parquet schema)
- Gold table creation (aggregated, Databricks-linked, UI-ready)
"""

import logging
import os
from typing import Any

from backend.db import execute_query

logger = logging.getLogger(__name__)


def get_catalog_schema() -> tuple[str, str]:
    """Get the catalog and schema for Azure cost tables from environment.

    Defaults match the cloud-infra-costs private preview DAB repo
    (catalog=billing, schema=azure). Override via env vars if needed.
    """
    catalog = os.getenv("AZURE_COST_CATALOG", os.getenv("COST_OBS_CATALOG", "billing"))
    schema = os.getenv("AZURE_COST_SCHEMA", "azure")
    return catalog, schema


# ── Check / setup SQL ────────────────────────────────────────────────────────

CHECK_AZURE_TABLES = """
SELECT table_name
FROM information_schema.tables
WHERE table_catalog = '{catalog}'
  AND table_schema = '{schema}'
  AND table_name IN ('actuals_bronze', 'actuals_gold')
"""

CREATE_AZURE_SCHEMA = """
CREATE SCHEMA IF NOT EXISTS {catalog}.{schema}
COMMENT 'Azure Cost Management Export data processed via medallion architecture'
"""

# ── Bronze table (raw Azure Cost Export schema) ───────────────────────────────
# Matches the standard Azure amortized cost export CSV column set (EA / MCA).
CREATE_BRONZE_TABLE = """
CREATE TABLE IF NOT EXISTS {catalog}.{schema}.actuals_bronze (
  -- Billing identifiers
  BillingAccountId          STRING,
  BillingAccountName        STRING,
  BillingPeriodStartDate    DATE,
  BillingPeriodEndDate      DATE,
  BillingProfileId          STRING,
  BillingProfileName        STRING,
  InvoiceSectionId          STRING,
  InvoiceSectionName        STRING,

  -- Subscription
  SubscriptionId            STRING,
  SubscriptionName          STRING,

  -- Date
  Date                      DATE,

  -- Charge classification
  ChargeType                STRING,
  Frequency                 STRING,

  -- Resource
  ResourceId                STRING,
  ResourceName              STRING,
  ResourceType              STRING,
  ResourceGroup             STRING,
  ResourceLocation          STRING,

  -- Meter / service
  ConsumedService           STRING,
  MeterId                   STRING,
  MeterName                 STRING,
  MeterCategory             STRING,
  MeterSubCategory          STRING,
  MeterRegion               STRING,
  ProductName               STRING,
  ProductId                 STRING,
  PartNumber                STRING,
  OfferId                   STRING,

  -- Pricing
  PricingModel              STRING,
  UnitPrice                 DOUBLE,
  EffectivePrice            DOUBLE,
  paygPrice                 DOUBLE,
  UnitOfMeasure             STRING,
  Quantity                  DOUBLE,
  Term                      STRING,

  -- Cost (EA / PAYG)
  CostInBillingCurrency     DOUBLE,
  BillingCurrency           STRING,
  CostInUsd                 DOUBLE,

  -- Reservation / Savings Plan
  ReservationId             STRING,
  ReservationName           STRING,
  BenefitId                 STRING,
  BenefitName               STRING,

  -- Tags and cost allocation
  CostCenter                STRING,
  Tags                      STRING,
  IsAzureCreditEligible     STRING,

  -- Publisher
  PublisherName             STRING,
  PublisherType             STRING,

  -- Additional metadata
  AdditionalInfo            STRING,
  ServiceInfo1              STRING,
  ServiceInfo2              STRING,

  -- Databricks resource tags (extracted from Tags JSON)
  tag_databricks_cluster_id        STRING,
  tag_databricks_cluster_name      STRING,
  tag_databricks_warehouse_id      STRING,
  tag_databricks_job_id            STRING,
  tag_databricks_instance_pool_id  STRING,
  tag_databricks_creator           STRING,

  _loaded_at                TIMESTAMP DEFAULT CURRENT_TIMESTAMP()
)
USING DELTA
COMMENT 'Raw Azure Cost Management Export data — bronze layer'
"""

# ── Gold table (aggregated, ready for dashboard) ──────────────────────────────
CREATE_GOLD_TABLE = """
CREATE TABLE IF NOT EXISTS {catalog}.{schema}.actuals_gold (
  usage_date              DATE,
  subscription_id         STRING,
  subscription_name       STRING,
  resource_group          STRING,
  location                STRING,

  -- Charge classification (maps to charge_type for parity with AWS gold)
  charge_type             STRING,   -- Compute / Storage / Networking / Other
  pricing_model           STRING,   -- OnDemand / Reservation / Spot / SavingsPlan

  -- Azure-specific service metadata
  meter_category          STRING,
  meter_subcategory       STRING,
  consumed_service        STRING,

  -- VM / instance details
  vm_size                 STRING,   -- e.g. Standard_D8s_v3
  instance_family         STRING,   -- e.g. Standard_D

  -- Databricks resource linkage
  usage_metadata          STRUCT<
                            cluster_id:       STRING,
                            cluster_name:     STRING,
                            warehouse_id:     STRING,
                            job_id:           STRING,
                            instance_pool_id: STRING,
                            cluster_creator:  STRING
                          >,

  -- Cost allocation tags
  cost_center             STRING,
  environment             STRING,
  project                 STRING,
  team                    STRING,

  -- Cost metrics
  cost_in_billing_currency   DOUBLE,   -- primary cost metric
  cost_in_usd                DOUBLE,
  effective_price            DOUBLE,
  unit_price                 DOUBLE,
  total_quantity             DOUBLE,
  line_item_count            BIGINT,
  currency_code              STRING
)
USING DELTA
COMMENT 'Aggregated Azure Cost Export — gold layer, ready for dashboard'
PARTITIONED BY (usage_date)
"""

# ── Silver → Gold transformation ─────────────────────────────────────────────
CREATE_GOLD_FROM_BRONZE = """
CREATE OR REPLACE TABLE {catalog}.{schema}.actuals_gold AS
SELECT
  Date                                    AS usage_date,
  SubscriptionId                          AS subscription_id,
  SubscriptionName                        AS subscription_name,
  ResourceGroup                           AS resource_group,
  ResourceLocation                        AS location,

  -- Charge type classification
  CASE
    WHEN MeterCategory LIKE '%Virtual Machine%' OR MeterCategory LIKE '%Compute%'   THEN 'Compute'
    WHEN MeterCategory LIKE '%Storage%'                                              THEN 'Storage'
    WHEN MeterCategory LIKE '%Bandwidth%' OR MeterCategory LIKE '%Network%'         THEN 'Networking'
    ELSE 'Other'
  END AS charge_type,

  COALESCE(PricingModel, 'OnDemand') AS pricing_model,

  MeterCategory    AS meter_category,
  MeterSubCategory AS meter_subcategory,
  ConsumedService  AS consumed_service,

  -- Extract VM size from ResourceType / AdditionalInfo heuristic
  REGEXP_EXTRACT(COALESCE(AdditionalInfo, ''), '"ServiceType":"([^"]+)"', 1) AS vm_size,

  -- Azure instance family: Standard_D8s_v3 → Standard_D
  REGEXP_EXTRACT(
    REGEXP_EXTRACT(COALESCE(AdditionalInfo, ''), '"ServiceType":"([^"]+)"', 1),
    '^(Standard_[A-Z]+)', 1
  ) AS instance_family,

  STRUCT(
    tag_databricks_cluster_id       AS cluster_id,
    tag_databricks_cluster_name     AS cluster_name,
    tag_databricks_warehouse_id     AS warehouse_id,
    tag_databricks_job_id           AS job_id,
    tag_databricks_instance_pool_id AS instance_pool_id,
    tag_databricks_creator          AS cluster_creator
  ) AS usage_metadata,

  CostCenter   AS cost_center,
  NULL         AS environment,
  NULL         AS project,
  NULL         AS team,

  SUM(CostInBillingCurrency) AS cost_in_billing_currency,
  SUM(CostInUsd)             AS cost_in_usd,
  AVG(EffectivePrice)        AS effective_price,
  AVG(UnitPrice)             AS unit_price,
  SUM(Quantity)              AS total_quantity,
  COUNT(*)                   AS line_item_count,
  MAX(BillingCurrency)       AS currency_code

FROM {catalog}.{schema}.actuals_bronze
WHERE Date IS NOT NULL
GROUP BY
  Date, SubscriptionId, SubscriptionName, ResourceGroup, ResourceLocation,
  MeterCategory, MeterSubCategory, ConsumedService, PricingModel,
  AdditionalInfo, CostCenter,
  tag_databricks_cluster_id, tag_databricks_cluster_name,
  tag_databricks_warehouse_id, tag_databricks_job_id,
  tag_databricks_instance_pool_id, tag_databricks_creator
"""

# ── COPY INTO for loading Azure exports from Blob storage ────────────────────
COPY_INTO_BRONZE_CSV = """
COPY INTO {catalog}.{schema}.actuals_bronze
FROM '{blob_path}'
FILEFORMAT = CSV
FORMAT_OPTIONS ('header' = 'true', 'inferSchema' = 'true')
COPY_OPTIONS ('mergeSchema' = 'true')
"""

COPY_INTO_BRONZE_PARQUET = """
COPY INTO {catalog}.{schema}.actuals_bronze
FROM '{blob_path}'
FILEFORMAT = PARQUET
COPY_OPTIONS ('mergeSchema' = 'true')
"""


def create_azure_tables(
    catalog: str | None = None,
    schema: str | None = None,
    blob_path: str | None = None,
    load_data: bool = False,
    file_format: str = "CSV",
) -> dict[str, Any]:
    """Create Azure Cost medallion tables.

    Args:
        catalog: Target catalog (default: from env)
        schema: Target schema (default: from env)
        blob_path: Azure Blob path to cost export files
        load_data: Whether to load data from Blob after creating tables
        file_format: 'CSV' or 'PARQUET'

    Returns:
        Dict with status of each operation
    """
    if catalog is None or schema is None:
        cat, sch = get_catalog_schema()
        catalog = catalog or cat
        schema = schema or sch

    results: dict[str, Any] = {}

    try:
        logger.info(f"Creating schema {catalog}.{schema}...")
        execute_query(CREATE_AZURE_SCHEMA.format(catalog=catalog, schema=schema))
        results["schema"] = "created"
    except Exception as e:
        logger.error(f"Failed to create schema: {e}")
        results["schema"] = f"error: {e}"
        return results

    try:
        logger.info("Creating bronze table...")
        execute_query(CREATE_BRONZE_TABLE.format(catalog=catalog, schema=schema))
        results["actuals_bronze"] = "created"
    except Exception as e:
        logger.error(f"Failed to create bronze table: {e}")
        results["actuals_bronze"] = f"error: {e}"

    try:
        logger.info("Creating gold table...")
        execute_query(CREATE_GOLD_TABLE.format(catalog=catalog, schema=schema))
        results["actuals_gold"] = "created"
    except Exception as e:
        logger.error(f"Failed to create gold table: {e}")
        results["actuals_gold"] = f"error: {e}"

    if load_data and blob_path:
        try:
            copy_sql = (COPY_INTO_BRONZE_CSV if file_format.upper() == "CSV" else COPY_INTO_BRONZE_PARQUET)
            execute_query(copy_sql.format(catalog=catalog, schema=schema, blob_path=blob_path))
            results["data_load"] = "completed"
            execute_query(CREATE_GOLD_FROM_BRONZE.format(catalog=catalog, schema=schema))
            results["gold_refresh"] = "completed"
        except Exception as e:
            logger.error(f"Failed to load data: {e}")
            results["data_load"] = f"error: {e}"

    return results


def check_azure_tables(catalog: str, schema: str) -> dict[str, Any]:
    """Check which Azure cost tables exist."""
    try:
        rows = execute_query(CHECK_AZURE_TABLES.format(catalog=catalog, schema=schema))
        return {"existing_tables": [r.get("table_name") for r in rows]}
    except Exception:
        return {"existing_tables": []}
