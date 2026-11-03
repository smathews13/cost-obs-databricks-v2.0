"""API endpoints for use case management and tracking.

Deprecated: Use Cases is retired from the app and this router is not mounted.
The request models, queries and table definitions are kept in case the feature
returns; mounting it again would also need the admin checks other write routes have.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from backend.db import clear_query_cache, execute_queries_parallel, execute_query, execute_write

router = APIRouter()
logger = logging.getLogger(__name__)




class CreateUseCaseRequest(BaseModel):
    """Request model for creating a use case."""
    name: str
    description: str | None = None
    owner: str | None = None
    tags: dict[str, str] | None = None
    stage: str | None = "Development"  # Live, Development, Planned, Inactive
    start_date: str | None = None  # YYYY-MM-DD format
    end_date: str | None = None  # YYYY-MM-DD format (optional, for ongoing use cases)
    live_date: str | None = None  # When the use case went live


class UpdateUseCaseRequest(BaseModel):
    """Request model for updating a use case."""
    name: str | None = None
    description: str | None = None
    owner: str | None = None
    status: str | None = None
    tags: dict[str, str] | None = None
    stage: str | None = None  # Live, Development, Planned, Inactive
    start_date: str | None = None  # YYYY-MM-DD format
    end_date: str | None = None  # YYYY-MM-DD format
    live_date: str | None = None  # When the use case went live


class AssignObjectRequest(BaseModel):
    """Request model for assigning an object to a use case."""
    object_type: str  # dashboard, pipeline, endpoint, cluster, job, warehouse
    object_id: str
    object_name: str | None = None
    workspace_id: str | None = None
    assigned_by: str | None = None
    notes: str | None = None
    custom_start_date: str | None = None  # Override use case start date
    custom_end_date: str | None = None  # Override use case end date


DEFAULT_USE_CASE_NAME = "Example: ML Model Training Pipeline"


def create_default_use_case() -> dict[str, Any]:
    """
    Create a default example use case so new users see what one looks like.

    This function is idempotent — it checks if the default use case already
    exists before creating it. Called during app startup, same pattern as
    create_default_cost_alerts().

    Returns:
        Dictionary with creation results
    """
    results: dict[str, Any] = {"created": False, "skipped": False, "error": None}

    try:
        check_query = """
        SELECT use_case_id FROM cost_observability.use_cases
        WHERE name = :name AND status = 'active'
        LIMIT 1
        """
        existing = execute_query(check_query, {"name": DEFAULT_USE_CASE_NAME})
        if existing:
            results["skipped"] = True
            return results

        use_case_id = str(uuid.uuid4())
        now = datetime.now().isoformat()
        start_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

        uc = {
            "use_case_id": use_case_id,
            "name": DEFAULT_USE_CASE_NAME,
            "description": "This is an example use case showing how to track and allocate costs for a machine learning training pipeline. Assign Databricks objects (jobs, clusters, endpoints, warehouses) to group their spend under this use case.",
            "owner": "Cost Admin",
            "tags": {"team": "data-science", "priority": "high"},
            "created_at": now,
            "updated_at": now,
            "status": "active",
            "stage": "Development",
            "start_date": start_date,
            "end_date": None,
            "live_date": None,
        }

        query = """
        INSERT INTO cost_observability.use_cases
        (use_case_id, name, description, owner, tags, created_at, updated_at, status, stage, start_date, end_date, live_date)
        VALUES (
            :use_case_id, :name, :description, :owner,
            MAP('team', 'data-science', 'priority', 'high'),
            cast(:created_at as timestamp), cast(:updated_at as timestamp),
            'active', :stage, cast(:start_date as date), NULL, NULL
        )
        """
        execute_write(query, {k: v for k, v in uc.items() if k != "tags"})

        logger.info(f"Created default use case: {DEFAULT_USE_CASE_NAME} ({use_case_id})")
        results["created"] = True
        return results

    except Exception as e:
        logger.error(f"Failed to create default use case: {e}")
        results["error"] = str(e)
        return results


@router.post("/setup")
async def setup_use_cases_tables() -> dict[str, Any]:
    """
    Create the use cases and use_case_objects tables if they don't exist.

    Returns:
        Success status and message
    """
    try:
        # Read the setup SQL
        with open("server/queries/use_cases_setup.sql", "r") as f:
            setup_sql = f.read()

        # Execute each statement separately
        statements = [s.strip() for s in setup_sql.split(";") if s.strip()]

        for statement in statements:
            if statement:
                await asyncio.to_thread(execute_query, statement, {})

        logger.info("Use cases tables created successfully")
        return {
            "success": True,
            "message": "Use cases tables created successfully"
        }
    except Exception as e:
        logger.error(f"Error setting up use cases tables: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/use-cases")
async def create_use_case(request: CreateUseCaseRequest) -> dict[str, Any]:
    """
    Create a new use case.

    Returns:
        The created use case with ID
    """
    try:
        use_case_id = str(uuid.uuid4())
        now = datetime.now().isoformat()

        # Build query with all fields including new stage and date fields
        query = """
        INSERT INTO cost_observability.use_cases
        (use_case_id, name, description, owner, tags, created_at, updated_at, status, stage, start_date, end_date, live_date)
        VALUES (
            :use_case_id, :name, :description, :owner, :tags,
            cast(:created_at as timestamp), cast(:updated_at as timestamp),
            'active', :stage,
            CASE WHEN :start_date IS NOT NULL THEN cast(:start_date as date) ELSE NULL END,
            CASE WHEN :end_date IS NOT NULL THEN cast(:end_date as date) ELSE NULL END,
            CASE WHEN :live_date IS NOT NULL THEN cast(:live_date as date) ELSE NULL END
        )
        """

        params = {
            "use_case_id": use_case_id,
            "name": request.name,
            "description": request.description,
            "owner": request.owner,
            "tags": request.tags if request.tags else None,
            "created_at": now,
            "updated_at": now,
            "stage": request.stage or "Development",
            "start_date": request.start_date,
            "end_date": request.end_date,
            "live_date": request.live_date
        }

        execute_write(query, params)

        logger.info(f"Created use case: {use_case_id}")

        # Clear cache so new use case appears in lists
        clear_query_cache("use_case")  # Clear only use-case cache entries

        return {
            "success": True,
            "use_case_id": use_case_id,
            "name": request.name,
            "description": request.description,
            "owner": request.owner,
            "status": "active",
            "tags": request.tags or {},
            "stage": request.stage or "Development",
            "start_date": request.start_date,
            "end_date": request.end_date,
            "live_date": request.live_date
        }
    except Exception as e:
        logger.error(f"Error creating use case: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/tags")
async def get_use_case_tags() -> dict[str, Any]:
    """Get all unique tag keys and values from existing use cases."""
    try:
        results = await asyncio.to_thread(execute_query,
            "SELECT tags FROM cost_observability.use_cases WHERE status = 'active' AND tags IS NOT NULL",
            {},
            cache_tag="use_case"
        )
        tag_map: dict[str, set[str]] = {}
        for row in results:
            tags = row.get("tags")
            if isinstance(tags, dict):
                for k, v in tags.items():
                    if k not in tag_map:
                        tag_map[k] = set()
                    if v:
                        tag_map[k].add(str(v))
            elif isinstance(tags, str):
                import json as _json
                try:
                    parsed = _json.loads(tags)
                    if isinstance(parsed, dict):
                        for k, v in parsed.items():
                            if k not in tag_map:
                                tag_map[k] = set()
                            if v:
                                tag_map[k].add(str(v))
                except (ValueError, TypeError):
                    pass

        return {
            "tags": {k: sorted(v) for k, v in sorted(tag_map.items())},
            "count": len(tag_map),
        }
    except Exception as e:
        logger.error(f"Error fetching use case tags: {e}")
        return {"tags": {}, "count": 0}


async def _list_use_cases_internal(
    status: str | None = None,
    stage: str | None = None
) -> list[dict[str, Any]]:
    """Internal helper to list use cases from Delta."""
    query = """
    SELECT
      use_case_id,
      name,
      description,
      owner,
      created_at,
      updated_at,
      status,
      tags,
      stage,
      start_date,
      end_date,
      live_date
    FROM cost_observability.use_cases
    """

    params = {}
    conditions = []

    if status:
        conditions.append("status = :status")
        params["status"] = status

    if stage:
        conditions.append("stage = :stage")
        params["stage"] = stage

    if conditions:
        query += " WHERE " + " AND ".join(conditions)

    query += " ORDER BY created_at DESC"

    results = await asyncio.to_thread(execute_query, query, params, cache_tag="use_case")

    use_cases = []
    for row in results:
        use_cases.append({
            "use_case_id": row.get("use_case_id"),
            "name": row.get("name"),
            "description": row.get("description"),
            "owner": row.get("owner"),
            "created_at": str(row.get("created_at")) if row.get("created_at") else None,
            "updated_at": str(row.get("updated_at")) if row.get("updated_at") else None,
            "status": row.get("status"),
            "tags": row.get("tags") or {},
            "stage": row.get("stage") or "Development",
            "start_date": str(row.get("start_date")) if row.get("start_date") else None,
            "end_date": str(row.get("end_date")) if row.get("end_date") else None,
            "live_date": str(row.get("live_date")) if row.get("live_date") else None
        })

    return use_cases


@router.get("/use-cases")
async def list_use_cases(
    status: str | None = Query(None, description="Filter by status"),
    stage: str | None = Query(None, description="Filter by stage (Live, Development, Planned, Inactive)")
) -> dict[str, Any]:
    """
    List all use cases.

    Returns:
        List of use cases with their details
    """
    try:
        use_cases = await _list_use_cases_internal(status=status, stage=stage)

        return {
            "use_cases": use_cases,
            "count": len(use_cases)
        }
    except Exception as e:
        logger.error(f"Error listing use cases: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/use-cases/{use_case_id}")
async def get_use_case(use_case_id: str) -> dict[str, Any]:
    """
    Get a specific use case with its assigned objects.

    Returns:
        Use case details and assigned objects
    """
    try:
        # Get use case details
        query = """
        SELECT
          use_case_id,
          name,
          description,
          owner,
          created_at,
          updated_at,
          status,
          tags,
          stage,
          start_date,
          end_date,
          live_date
        FROM cost_observability.use_cases
        WHERE use_case_id = :use_case_id
        """

        results = await asyncio.to_thread(execute_query, query, {"use_case_id": use_case_id}, cache_tag="use_case")

        if not results:
            raise HTTPException(status_code=404, detail="Use case not found")

        use_case = results[0]

        # Get assigned objects with custom date ranges
        objects_query = """
        SELECT
          mapping_id,
          object_type,
          object_id,
          object_name,
          workspace_id,
          assigned_at,
          assigned_by,
          notes,
          custom_start_date,
          custom_end_date
        FROM cost_observability.use_case_objects
        WHERE use_case_id = :use_case_id
        ORDER BY assigned_at DESC
        """

        objects_results = await asyncio.to_thread(execute_query, objects_query, {"use_case_id": use_case_id}, cache_tag="use_case")

        objects = []
        for row in objects_results:
            objects.append({
                "mapping_id": row.get("mapping_id"),
                "object_type": row.get("object_type"),
                "object_id": row.get("object_id"),
                "object_name": row.get("object_name"),
                "workspace_id": row.get("workspace_id"),
                "assigned_at": str(row.get("assigned_at")) if row.get("assigned_at") else None,
                "assigned_by": row.get("assigned_by"),
                "notes": row.get("notes"),
                "custom_start_date": str(row.get("custom_start_date")) if row.get("custom_start_date") else None,
                "custom_end_date": str(row.get("custom_end_date")) if row.get("custom_end_date") else None
            })

        return {
            "use_case_id": use_case.get("use_case_id"),
            "name": use_case.get("name"),
            "description": use_case.get("description"),
            "owner": use_case.get("owner"),
            "created_at": str(use_case.get("created_at")) if use_case.get("created_at") else None,
            "updated_at": str(use_case.get("updated_at")) if use_case.get("updated_at") else None,
            "status": use_case.get("status"),
            "tags": use_case.get("tags") or {},
            "stage": use_case.get("stage") or "Development",
            "start_date": str(use_case.get("start_date")) if use_case.get("start_date") else None,
            "end_date": str(use_case.get("end_date")) if use_case.get("end_date") else None,
            "live_date": str(use_case.get("live_date")) if use_case.get("live_date") else None,
            "objects": objects,
            "object_count": len(objects)
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting use case: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/use-cases/{use_case_id}")
async def update_use_case(
    use_case_id: str,
    request: UpdateUseCaseRequest
) -> dict[str, Any]:
    """
    Update a use case.

    Returns:
        Updated use case details
    """
    try:
        # Build update query dynamically based on provided fields
        update_fields = []
        params = {"use_case_id": use_case_id}

        if request.name is not None:
            update_fields.append("name = :name")
            params["name"] = request.name

        if request.description is not None:
            update_fields.append("description = :description")
            params["description"] = request.description

        if request.owner is not None:
            update_fields.append("owner = :owner")
            params["owner"] = request.owner

        if request.status is not None:
            update_fields.append("status = :status")
            params["status"] = request.status

        if request.tags is not None:
            update_fields.append("tags = :tags")
            params["tags"] = request.tags if request.tags else None

        if request.stage is not None:
            update_fields.append("stage = :stage")
            params["stage"] = request.stage

        if request.start_date is not None:
            update_fields.append("start_date = CASE WHEN :start_date = '' THEN NULL ELSE cast(:start_date as date) END")
            params["start_date"] = request.start_date

        if request.end_date is not None:
            update_fields.append("end_date = CASE WHEN :end_date = '' THEN NULL ELSE cast(:end_date as date) END")
            params["end_date"] = request.end_date

        if request.live_date is not None:
            update_fields.append("live_date = CASE WHEN :live_date = '' THEN NULL ELSE cast(:live_date as date) END")
            params["live_date"] = request.live_date

        if not update_fields:
            raise HTTPException(status_code=400, detail="No fields to update")

        update_fields.append("updated_at = cast(:updated_at as timestamp)")
        params["updated_at"] = datetime.now().isoformat()

        query = f"""
        UPDATE cost_observability.use_cases
        SET {", ".join(update_fields)}
        WHERE use_case_id = :use_case_id
        """

        execute_write(query, params)

        logger.info(f"Updated use case: {use_case_id}")

        # Clear cache to ensure fresh data is returned
        clear_query_cache("use_case")  # Clear only use-case cache entries

        # Return updated use case
        return await get_use_case(use_case_id)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating use case: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/use-cases/{use_case_id}")
async def delete_use_case(use_case_id: str) -> dict[str, Any]:
    """
    Delete a use case and all its object assignments.

    Returns:
        Success status
    """
    try:
        execute_write(
            "DELETE FROM cost_observability.use_case_objects WHERE use_case_id = :use_case_id",
            {"use_case_id": use_case_id},
        )
        execute_write(
            "DELETE FROM cost_observability.use_cases WHERE use_case_id = :use_case_id",
            {"use_case_id": use_case_id},
        )

        logger.info(f"Deleted use case: {use_case_id}")

        # Clear cache so deleted use case no longer appears
        clear_query_cache("use_case")  # Clear only use-case cache entries

        return {
            "success": True,
            "message": f"Use case {use_case_id} deleted successfully"
        }
    except Exception as e:
        logger.error(f"Error deleting use case: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/use-cases/{use_case_id}/objects")
async def assign_object_to_use_case(
    use_case_id: str,
    request: AssignObjectRequest
) -> dict[str, Any]:
    """
    Assign an object (dashboard, pipeline, etc.) to a use case.

    Returns:
        The created mapping
    """
    try:
        mapping_id = str(uuid.uuid4())
        now = datetime.now().isoformat()

        query = """
        INSERT INTO cost_observability.use_case_objects
        (mapping_id, use_case_id, object_type, object_id, object_name, workspace_id, assigned_at, assigned_by, notes, custom_start_date, custom_end_date)
        VALUES (
            :mapping_id, :use_case_id, :object_type, :object_id, :object_name, :workspace_id,
            cast(:assigned_at as timestamp), :assigned_by, :notes,
            CASE WHEN :custom_start_date IS NOT NULL THEN cast(:custom_start_date as date) ELSE NULL END,
            CASE WHEN :custom_end_date IS NOT NULL THEN cast(:custom_end_date as date) ELSE NULL END
        )
        """

        params = {
            "mapping_id": mapping_id,
            "use_case_id": use_case_id,
            "object_type": request.object_type,
            "object_id": request.object_id,
            "object_name": request.object_name,
            "workspace_id": request.workspace_id,
            "assigned_at": now,
            "assigned_by": request.assigned_by,
            "notes": request.notes,
            "custom_start_date": request.custom_start_date,
            "custom_end_date": request.custom_end_date
        }

        execute_write(query, params)

        logger.info(f"Assigned {request.object_type} {request.object_id} to use case {use_case_id}")

        # Clear cache so assigned objects appear
        clear_query_cache("use_case")  # Clear only use-case cache entries

        return {
            "success": True,
            "mapping_id": mapping_id,
            "use_case_id": use_case_id,
            "object_type": request.object_type,
            "object_id": request.object_id,
            "object_name": request.object_name,
            "custom_start_date": request.custom_start_date,
            "custom_end_date": request.custom_end_date
        }
    except Exception as e:
        logger.error(f"Error assigning object to use case: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/use-cases/{use_case_id}/objects/{mapping_id}")
async def remove_object_from_use_case(
    use_case_id: str,
    mapping_id: str
) -> dict[str, Any]:
    """
    Remove an object assignment from a use case.

    Returns:
        Success status
    """
    try:
        execute_write(
            "DELETE FROM cost_observability.use_case_objects WHERE mapping_id = :mapping_id AND use_case_id = :use_case_id",
            {"mapping_id": mapping_id, "use_case_id": use_case_id},
        )

        logger.info(f"Removed mapping {mapping_id} from use case {use_case_id}")

        # Clear cache so removed objects no longer appear
        clear_query_cache("use_case")  # Clear only use-case cache entries

        return {
            "success": True,
            "message": "Object removed from use case"
        }
    except Exception as e:
        logger.error(f"Error removing object from use case: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/use-cases/{use_case_id}/analytics")
async def get_use_case_analytics(
    use_case_id: str,
    start_date: str = Query(default=None, description="Start date (YYYY-MM-DD)"),
    end_date: str = Query(default=None, description="End date (YYYY-MM-DD)")
) -> dict[str, Any]:
    """
    Get spend analytics for a specific use case.

    Returns:
        Spend data, trends, and component breakdown
    """
    try:
        # Import here to avoid circular dependency
        from backend.routers.billing import get_default_end_date, get_default_start_date

        params = {
            "use_case_id": use_case_id,
            "start_date": start_date or get_default_start_date(),
            "end_date": end_date or get_default_end_date()
        }

        # Get objects for this use case
        objects_query = """
        SELECT object_type, object_id, workspace_id
        FROM cost_observability.use_case_objects
        WHERE use_case_id = :use_case_id
        """

        objects = await asyncio.to_thread(execute_query, objects_query, {"use_case_id": use_case_id}, cache_tag="use_case")

        if not objects:
            return {
                "use_case_id": use_case_id,
                "total_spend": 0,
                "total_dbus": 0,
                "timeseries": [],
                "by_component": [],
                "by_object_type": []
            }

        # Build lists of object IDs by type for querying
        cluster_ids = [obj["object_id"] for obj in objects if obj["object_type"] == "cluster"]
        warehouse_ids = [obj["object_id"] for obj in objects if obj["object_type"] == "warehouse"]
        job_ids = [obj["object_id"] for obj in objects if obj["object_type"] == "job"]
        endpoint_ids = [obj["object_id"] for obj in objects if obj["object_type"] == "endpoint"]

        wanted = {
            ("cluster", oid) for oid in cluster_ids
        } | {
            ("warehouse", oid) for oid in warehouse_ids
        } | {
            ("job", oid) for oid in job_ids
        } | {
            ("endpoint", oid) for oid in endpoint_ids
        }

        from backend.lakebase_read import PG_OBJECT_SPEND, gold_read
        gold_rows = await asyncio.to_thread(
            gold_read, "object_spend", PG_OBJECT_SPEND, params, ""
        )
        results = [
            r for r in (gold_rows or [])
            if (r.get("object_type"), str(r.get("object_id") or "")) in {
                (t, str(i)) for t, i in wanted
            }
        ]

        # Process results into analytics
        total_spend = 0
        total_dbus = 0
        timeseries_data = {}
        by_component = {}
        by_object_type = {}

        for row in results:
            date = str(row["usage_date"])
            object_type = row["object_type"]
            object_id = row["object_id"]
            dbus = float(row["total_dbus"])
            spend = float(row["total_spend"])

            total_spend += spend
            total_dbus += dbus

            # Timeseries
            if date not in timeseries_data:
                timeseries_data[date] = {"date": date, "spend": 0, "dbus": 0}
            timeseries_data[date]["spend"] += spend
            timeseries_data[date]["dbus"] += dbus

            # By component (individual object)
            component_key = f"{object_type}:{object_id}"
            if component_key not in by_component:
                by_component[component_key] = {
                    "object_type": object_type,
                    "object_id": object_id,
                    "spend": 0,
                    "dbus": 0
                }
            by_component[component_key]["spend"] += spend
            by_component[component_key]["dbus"] += dbus

            # By object type
            if object_type not in by_object_type:
                by_object_type[object_type] = {"object_type": object_type, "spend": 0, "dbus": 0, "count": 0}
            by_object_type[object_type]["spend"] += spend
            by_object_type[object_type]["dbus"] += dbus

        # Count unique objects per type
        for obj_type in by_object_type:
            by_object_type[obj_type]["count"] = len([c for c in by_component.values() if c["object_type"] == obj_type])

        return {
            "use_case_id": use_case_id,
            "total_spend": round(total_spend, 2),
            "total_dbus": round(total_dbus, 2),
            "timeseries": sorted(timeseries_data.values(), key=lambda x: x["date"]),
            "by_component": sorted(by_component.values(), key=lambda x: x["spend"], reverse=True),
            "by_object_type": sorted(by_object_type.values(), key=lambda x: x["spend"], reverse=True)
        }
    except Exception as e:
        logger.error(f"Error getting use case analytics: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/analytics/summary")
async def get_all_use_cases_summary(
    start_date: str = Query(default=None, description="Start date (YYYY-MM-DD)"),
    end_date: str = Query(default=None, description="End date (YYYY-MM-DD)")
) -> dict[str, Any]:
    """
    Get summary analytics across all use cases.

    Returns:
        Total spend by use case and percentage breakdowns
    """
    import asyncio

    try:
        # Get all use cases using internal helper to avoid Query object issues
        use_cases = await _list_use_cases_internal(status="active")

        if not use_cases:
            return {
                "use_cases": [],
                "total_spend": 0,
                "count": 0
            }

        # Fetch analytics for all use cases in PARALLEL (not sequential)
        analytics_tasks = [
            get_use_case_analytics(uc["use_case_id"], start_date=start_date, end_date=end_date)
            for uc in use_cases
        ]
        analytics_results = await asyncio.gather(*analytics_tasks, return_exceptions=True)

        # Build summaries from parallel results
        summaries = []
        total_all_spend = 0

        for uc, analytics in zip(use_cases, analytics_results):
            # Skip if analytics query failed
            if isinstance(analytics, Exception):
                logger.warning(f"Failed to get analytics for use case {uc['use_case_id']}: {analytics}")
                continue

            summaries.append({
                "use_case_id": uc["use_case_id"],
                "name": uc["name"],
                "description": uc.get("description"),
                "owner": uc["owner"],
                "tags": uc.get("tags") or {},
                "stage": uc.get("stage") or "Development",
                "start_date": uc.get("start_date"),
                "end_date": uc.get("end_date"),
                "live_date": uc.get("live_date"),
                "total_spend": analytics["total_spend"],
                "total_dbus": analytics["total_dbus"],
                "object_count": len(analytics["by_component"])
            })

            total_all_spend += analytics["total_spend"]

        # Calculate percentages
        for summary in summaries:
            summary["percentage"] = (summary["total_spend"] / total_all_spend * 100) if total_all_spend > 0 else 0

        # Sort by spend
        summaries.sort(key=lambda x: x["total_spend"], reverse=True)

        return {
            "use_cases": summaries,
            "total_spend": round(total_all_spend, 2),
            "count": len(summaries)
        }
    except Exception as e:
        logger.error(f"Error getting use cases summary: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/monthly-consumption")
async def get_monthly_consumption(
    start_date: str = Query(default=None, description="Start date (YYYY-MM-DD)"),
    end_date: str = Query(default=None, description="End date (YYYY-MM-DD)")
) -> dict[str, Any]:
    """
    Get monthly spend totals with use case live date markers.

    Returns monthly aggregated spend and a list of use case go-live events
    for chart annotations.
    """
    try:
        from datetime import date, timedelta


        # Default to last 12 months if not specified
        if not start_date:
            start_date = (date.today() - timedelta(days=365)).strftime("%Y-%m-%d")
        if not end_date:
            end_date = date.today().strftime("%Y-%m-%d")

        from backend.lakebase_read import PG_MONTHLY_SPEND, gold_read

        live_query = """
        SELECT
          use_case_id,
          name,
          live_date
        FROM cost_observability.use_cases
        WHERE status = 'active'
          AND live_date IS NOT NULL
          AND live_date BETWEEN :start_date AND :end_date
        ORDER BY live_date
        """

        query_params = {"start_date": start_date, "end_date": end_date}

        # Execute both queries in parallel for better performance
        query_results = await asyncio.to_thread(execute_queries_parallel, [
            ("spend", lambda: gold_read("monthly_spend", PG_MONTHLY_SPEND, query_params)),
            ("live", lambda: execute_query(live_query, query_params)),
        ], timeout=60.0)

        spend_results = query_results.get("spend") or []
        live_results = query_results.get("live") or []

        # Process spend results
        months = []
        for row in spend_results:
            month_str = str(row["month"])[:7]  # YYYY-MM format
            months.append({
                "month": month_str,
                "total_spend": float(row["total_spend"]) if row["total_spend"] else 0,
                "total_dbus": float(row["total_dbus"]) if row["total_dbus"] else 0
            })

        # Process live events
        live_events = []
        for row in live_results:
            live_date = str(row["live_date"])
            month_str = live_date[:7]  # YYYY-MM format
            live_events.append({
                "month": month_str,
                "use_case_id": row["use_case_id"],
                "use_case_name": row["name"],
                "live_date": live_date
            })

        return {
            "months": months,
            "live_events": live_events,
            "date_range": {
                "start": start_date,
                "end": end_date
            }
        }
    except Exception as e:
        logger.error(f"Error getting monthly consumption: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/available-objects")
async def get_available_objects(
    object_type: str = Query(..., description="Object type: cluster, pipeline, warehouse, endpoint, job")
) -> dict[str, Any]:
    """
    Get available objects from system tables that can be assigned to use cases.

    Queries Databricks system tables to find objects that can be associated with use cases.
    """
    try:
        # Use billing.usage table as the primary source for finding objects
        # This is more reliable across different Databricks account configurations
        if object_type == "cluster":
            # Get clusters from billing usage which is more reliable
            query = """
            SELECT DISTINCT
              usage_metadata.cluster_id as object_id,
              COALESCE(usage_metadata.cluster_id, 'Cluster') as object_name,
              workspace_id,
              'cluster' as object_type
            FROM system.billing.usage
            WHERE usage_metadata.cluster_id IS NOT NULL
              AND usage_date >= DATE_SUB(CURRENT_DATE(), 90)
              AND usage_quantity > 0
            ORDER BY object_id
            LIMIT 100
            """
        elif object_type == "warehouse":
            # Get warehouses from billing usage
            query = """
            SELECT DISTINCT
              usage_metadata.warehouse_id as object_id,
              COALESCE(usage_metadata.warehouse_id, 'Warehouse') as object_name,
              workspace_id,
              'warehouse' as object_type
            FROM system.billing.usage
            WHERE usage_metadata.warehouse_id IS NOT NULL
              AND usage_date >= DATE_SUB(CURRENT_DATE(), 90)
              AND usage_quantity > 0
            ORDER BY object_id
            LIMIT 100
            """
        elif object_type == "pipeline":
            # Get pipelines from billing usage since system.lakeflow.pipelines
            # may not have consistent column names across accounts
            query = """
            SELECT DISTINCT
              usage_metadata.dlt_pipeline_id as object_id,
              COALESCE(usage_metadata.dlt_pipeline_id, 'Pipeline') as object_name,
              workspace_id,
              'pipeline' as object_type
            FROM system.billing.usage
            WHERE usage_metadata.dlt_pipeline_id IS NOT NULL
              AND usage_date >= DATE_SUB(CURRENT_DATE(), 90)
              AND usage_quantity > 0
            ORDER BY object_id
            LIMIT 100
            """
        elif object_type == "endpoint":
            # Get model serving endpoints from billing
            query = """
            SELECT DISTINCT
              usage_metadata.endpoint_id as object_id,
              COALESCE(usage_metadata.endpoint_name, usage_metadata.endpoint_id, 'Endpoint') as object_name,
              workspace_id,
              'endpoint' as object_type
            FROM system.billing.usage
            WHERE usage_metadata.endpoint_id IS NOT NULL
              AND usage_date >= DATE_SUB(CURRENT_DATE(), 90)
              AND usage_quantity > 0
            ORDER BY object_id
            LIMIT 100
            """
        elif object_type == "job":
            # Get jobs from billing usage
            query = """
            SELECT DISTINCT
              usage_metadata.job_id as object_id,
              COALESCE(usage_metadata.job_id, 'Job') as object_name,
              workspace_id,
              'job' as object_type
            FROM system.billing.usage
            WHERE usage_metadata.job_id IS NOT NULL
              AND usage_date >= DATE_SUB(CURRENT_DATE(), 90)
              AND usage_quantity > 0
            ORDER BY object_id
            LIMIT 100
            """
        elif object_type == "query":
            query = """
            SELECT DISTINCT
              statement_id as object_id,
              SUBSTRING(COALESCE(statement_text, 'Query ' || statement_id), 1, 100) as object_name,
              workspace_id,
              'query' as object_type
            FROM system.query.history
            WHERE statement_id IS NOT NULL
              AND executed_by_user_id IS NOT NULL
              AND start_time >= DATE_SUB(CURRENT_DATE(), 30)
            ORDER BY start_time DESC
            LIMIT 100
            """
        elif object_type == "dashboard":
            query = """
            SELECT DISTINCT
              dashboard_id as object_id,
              name as object_name,
              workspace_id,
              'dashboard' as object_type
            FROM system.dashboards.dashboards
            WHERE dashboard_id IS NOT NULL
            ORDER BY name
            LIMIT 100
            """
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid object_type: {object_type}. Must be one of: cluster, pipeline, warehouse, endpoint, job, query, dashboard"
            )

        from backend.lakebase_read import (
            PG_AVAILABLE_DASHBOARDS,
            PG_AVAILABLE_OBJECTS,
            PG_AVAILABLE_QUERIES,
            gold_read,
            warehouse_view_fallback,
        )
        if object_type in ("cluster", "warehouse", "pipeline", "endpoint", "job"):
            results = await asyncio.to_thread(
                gold_read, "available_objects", PG_AVAILABLE_OBJECTS, {"object_type": object_type}
            )
        elif object_type == "query":
            results = await asyncio.to_thread(
                gold_read, "available_queries", PG_AVAILABLE_QUERIES, {}, "",
                query if warehouse_view_fallback() else None,
            )
        elif object_type == "dashboard":
            results = await asyncio.to_thread(
                gold_read, "available_dashboards", PG_AVAILABLE_DASHBOARDS, {}, "",
                query if warehouse_view_fallback() else None,
            )
        elif warehouse_view_fallback():
            results = await asyncio.to_thread(execute_query, query, {})
        else:
            results = []

        objects = []
        for row in results:
            objects.append({
                "object_id": str(row.get("object_id")),
                "object_name": row.get("object_name") or str(row.get("object_id")),
                "workspace_id": str(row.get("workspace_id")) if row.get("workspace_id") else None,
                "object_type": object_type
            })

        return {
            "objects": objects,
            "count": len(objects),
            "object_type": object_type
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting available objects: {e}")
        raise HTTPException(status_code=500, detail=str(e))
