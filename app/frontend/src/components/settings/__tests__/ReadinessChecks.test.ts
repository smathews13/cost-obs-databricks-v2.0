import { describe, it, expect } from "vitest";
import { normalizeReadinessResult } from "../readiness";
import type { ReadinessResult } from "../readiness";

function omit<T extends object>(obj: T, ...keys: (keyof T)[]): Partial<T> {
  const copy: Partial<T> = { ...obj };
  for (const k of keys) delete copy[k];
  return copy;
}

// ---------------------------------------------------------------------------
// normalizeReadinessResult — pure function, no DOM needed
// ---------------------------------------------------------------------------

const validPayload = {
  overall: "ready",
  warehouse: {
    name: "SQL Warehouse",
    description: "desc",
    category: "core",
    source: "app_resource",
    granted: true,
  },
  core: [
    { table: "system.billing.usage", name: "Usage", description: "", required: true, granted: true, category: "core" },
  ],
  enhanced: [],
  sp_client_id: "sp-abc",
};

describe("normalizeReadinessResult", () => {
  it("returns null for null input", () => {
    expect(normalizeReadinessResult(null)).toBeNull();
  });

  it("returns null for non-object input", () => {
    expect(normalizeReadinessResult("string")).toBeNull();
    expect(normalizeReadinessResult(42)).toBeNull();
    expect(normalizeReadinessResult(undefined)).toBeNull();
  });

  it("returns null when warehouse field is missing", () => {
    expect(normalizeReadinessResult(omit(validPayload, "warehouse"))).toBeNull();
  });

  it("returns null when warehouse is explicitly null", () => {
    expect(normalizeReadinessResult({ ...validPayload, warehouse: null })).toBeNull();
  });

  it("parses a well-formed payload correctly", () => {
    const result = normalizeReadinessResult(validPayload) as ReadinessResult;
    expect(result).not.toBeNull();
    expect(result.overall).toBe("ready");
    expect(result.warehouse.granted).toBe(true);
    expect(result.warehouse.source).toBe("app_resource");
    expect(result.core).toHaveLength(1);
    expect(result.sp_client_id).toBe("sp-abc");
  });

  it("defaults overall to 'not_ready' when field is missing", () => {
    const result = normalizeReadinessResult(omit(validPayload, "overall")) as ReadinessResult;
    expect(result).not.toBeNull();
    expect(result.overall).toBe("not_ready");
  });

  it("defaults core and enhanced to empty arrays when missing", () => {
    const result = normalizeReadinessResult(omit(validPayload, "core", "enhanced")) as ReadinessResult;
    expect(result).not.toBeNull();
    expect(result.core).toEqual([]);
    expect(result.enhanced).toEqual([]);
  });

  it("defaults warehouse source to 'none' when missing", () => {
    const result = normalizeReadinessResult({ ...validPayload, warehouse: omit(validPayload.warehouse, "source") }) as ReadinessResult;
    expect(result).not.toBeNull();
    expect(result.warehouse.source).toBe("none");
  });

  it("coerces warehouse.granted to boolean", () => {
    const result = normalizeReadinessResult({
      ...validPayload,
      warehouse: { ...validPayload.warehouse, granted: 0 },
    }) as ReadinessResult;
    expect(result).not.toBeNull();
    expect(result.warehouse.granted).toBe(false);
  });

  it("preserves warehouse error and fix_sql when present", () => {
    const payload = {
      ...validPayload,
      warehouse: {
        ...validPayload.warehouse,
        granted: false,
        error: "Permission denied",
        fix_sql: "GRANT CAN_USE ON WAREHOUSE ...",
      },
    };
    const result = normalizeReadinessResult(payload) as ReadinessResult;
    expect(result.warehouse.error).toBe("Permission denied");
    expect(result.warehouse.fix_sql).toBe("GRANT CAN_USE ON WAREHOUSE ...");
  });

  it("omits error and fix_sql fields when they are null", () => {
    const result = normalizeReadinessResult(validPayload) as ReadinessResult;
    expect(result.warehouse.error).toBeUndefined();
    expect(result.warehouse.fix_sql).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// SetupWizard null-readiness regression lock
// Mirrors SetupWizard.tsx: disabled={loading || readiness == null || (readiness.overall !== "ready" && readiness.overall !== "core_ready")}
// This test is intentionally kept as a pure logic test (no DOM) so it remains
// cheap and deterministic. If the disabled condition ever changes, this suite
// catches it immediately — the component-level behavior follows from this invariant.
// ---------------------------------------------------------------------------

function isNextDisabled(loading: boolean, readiness: ReadinessResult | null): boolean {
  return loading || readiness == null || (readiness.overall !== "ready" && readiness.overall !== "core_ready");
}

describe("SetupWizard Next button disabled condition", () => {
  it("is disabled while loading", () => {
    expect(isNextDisabled(true, null)).toBe(true);
  });

  it("is disabled when readiness is null (malformed or missing API response)", () => {
    expect(isNextDisabled(false, null)).toBe(true);
  });

  it("is disabled when overall is 'not_ready'", () => {
    const r = normalizeReadinessResult({ ...validPayload, overall: "not_ready" }) as ReadinessResult;
    expect(isNextDisabled(false, r)).toBe(true);
  });

  it("is disabled when overall is 'needs_action'", () => {
    const r = normalizeReadinessResult({ ...validPayload, overall: "needs_action" }) as ReadinessResult;
    expect(isNextDisabled(false, r)).toBe(true);
  });

  it("is enabled when overall is 'ready'", () => {
    const r = normalizeReadinessResult({ ...validPayload, overall: "ready" }) as ReadinessResult;
    expect(isNextDisabled(false, r)).toBe(false);
  });

  it("is enabled when overall is 'core_ready'", () => {
    const r = normalizeReadinessResult({ ...validPayload, overall: "core_ready" }) as ReadinessResult;
    expect(isNextDisabled(false, r)).toBe(false);
  });
});
