export type ReadinessStatus =
  | "healthy"
  | "not_configured"
  | "timeout_starting"
  | "permission_denied"
  | "internal_error"
  | "unavailable";

export interface ReadinessCheck {
  table?: string;
  name: string;
  description: string;
  required?: boolean;
  granted: boolean;
  category: "core" | "enhanced";
  source?: string;
  fix_sql?: string;
  error?: string;
}

export interface ReadinessWarehouse {
  name: string;
  description: string;
  category: "core";
  source: "app_resource" | "http_path" | "none";
  granted: boolean;
  error?: string;
  fix_sql?: string;
}

export interface ReadinessResult {
  overall: "ready" | "core_ready" | "needs_action" | "not_ready";
  warehouse: ReadinessWarehouse;
  core: ReadinessCheck[];
  enhanced: ReadinessCheck[];
  sp_client_id: string;
}

/** Normalise a raw API response into a well-typed ReadinessResult.
 *  Guards against missing fields so the UI never crashes on a partial payload. */
export function normalizeReadinessResult(raw: unknown): ReadinessResult | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Record<string, unknown>;
  const overall = (r.overall as ReadinessResult["overall"]) ?? "not_ready";
  const warehouse = r.warehouse as ReadinessWarehouse | undefined;
  if (!warehouse) return null;
  return {
    overall,
    warehouse: {
      name: String(warehouse.name ?? "SQL Warehouse"),
      description: String(warehouse.description ?? ""),
      category: "core",
      source: (warehouse.source as ReadinessWarehouse["source"]) ?? "none",
      granted: Boolean(warehouse.granted),
      error: warehouse.error != null ? String(warehouse.error) : undefined,
      fix_sql: warehouse.fix_sql != null ? String(warehouse.fix_sql) : undefined,
    },
    core: Array.isArray(r.core) ? (r.core as ReadinessCheck[]) : [],
    enhanced: Array.isArray(r.enhanced) ? (r.enhanced as ReadinessCheck[]) : [],
    sp_client_id: String(r.sp_client_id ?? ""),
  };
}
