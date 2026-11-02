import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { debuggerRunState } from "./debuggerState";

interface InstallReport {
  version?: { commit_sha: string; branch?: string; repo?: string; commit_date?: string };
  warehouse?: {
    id: string | null;
    name?: string | null;
    size?: string | null;
    state?: string;
    source?: string;
  };
  identity?: { display_name: string | null; user_name: string | null } | null;
  storage_location?: {
    catalog: string;
    schema: string;
  };
}

interface AuthStatusSlim {
  auth_mode?: "unknown" | "user" | "sp";
  identity?: "user_oauth" | "service_principal";
  locked_to_sp?: boolean;
  has_sql_scope?: boolean | null;
  sp_display_name?: string;
  sp_user_name?: string;
  sp_client_id?: string;
  user_email?: string | null;
}

interface DiagCheck {
  id: string;
  category: string;
  label: string;
  status: "pass" | "fail" | "warn" | "skip";
  detail: string;
  fix: string;
  root_cause?: string;
  missing_tables?: string[];
  /** Optional backend-supplied failure class. When absent, inferred from category. */
  failure_class?: "warehouse_permission" | "system_table_grant" | "missing_mv" | "schema_mismatch" | "stale_data" | "internal_error" | "not_configured";
}

/** Infer the failure class when the backend doesn't supply one. */
function inferFailureClass(check: DiagCheck): DiagCheck["failure_class"] {
  if (check.failure_class) return check.failure_class;
  if (check.category === "permissions") {
    if (check.label.toLowerCase().includes("warehouse")) return "warehouse_permission";
    return "system_table_grant";
  }
  if (check.category === "materialized_views") return "missing_mv";
  if (check.category === "configuration") return "not_configured";
  if (check.category === "data" && check.label.toLowerCase().includes("stale")) return "stale_data";
  return "internal_error";
}

const FAILURE_CLASS_LABELS: Record<NonNullable<DiagCheck["failure_class"]>, { label: string; color: string }> = {
  warehouse_permission: { label: "Warehouse permission", color: "bg-red-100 text-red-700" },
  system_table_grant:  { label: "System table grant",   color: "bg-red-100 text-red-700" },
  missing_mv:          { label: "Missing MV",            color: "bg-orange-100 text-orange-700" },
  schema_mismatch:     { label: "Schema mismatch",       color: "bg-orange-100 text-orange-700" },
  stale_data:          { label: "Stale data",            color: "bg-amber-100 text-amber-700" },
  internal_error:      { label: "Internal error",        color: "bg-gray-100 text-gray-600" },
  not_configured:      { label: "Not configured",        color: "bg-gray-100 text-gray-600" },
};

/** Returns a more specific action label for the Fix button based on failure class. */
function fixActionLabel(check: DiagCheck): string {
  const fc = inferFailureClass(check);
  if (fc === "warehouse_permission") return "Grant SQL";
  if (fc === "system_table_grant")   return "Grant SQL";
  if (fc === "missing_mv")           return "Rebuild";
  if (fc === "schema_mismatch")      return "Rebuild";
  if (fc === "not_configured")       return "Configure";
  return "Show Fix";
}

/** Returns true when the fix text looks like SQL that should be rendered as a code block.
 * Requires BOTH a leading SQL keyword AND a statement-terminating semicolon — prose
 * remediation that merely starts with a keyword word (e.g. "Drop and rebuild the
 * materialized view…") has no semicolon and must render as prose, not a code block. */
function fixIsSql(fix: string): boolean {
  const t = fix.trim();
  return /^(GRANT|CREATE|DROP|ALTER|INSERT|USE|SHOW)\b/i.test(t) && /;/.test(t);
}

interface DiagResult {
  checks: DiagCheck[];
  summary: { passed: number; failed: number; warned: number; total: number };
}

const CATEGORY_ORDER = ["configuration", "permissions", "materialized_views", "data", "tab_health"];
const CATEGORY_LABELS: Record<string, string> = {
  configuration: "Configuration",
  permissions: "System Table Permissions",
  materialized_views: "Materialized Views",
  data: "Data",
  tab_health: "Tab Visualizations",
};

function StatusIcon({ status }: { status: DiagCheck["status"] }) {
  if (status === "pass") {
    return (
      <svg className="h-4 w-4 shrink-0 text-green-500" viewBox="0 0 20 20" fill="currentColor">
        <path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.857-9.809a.75.75 0 00-1.214-.882l-3.483 4.79-1.88-1.88a.75.75 0 10-1.06 1.061l2.5 2.5a.75.75 0 001.137-.089l4-5.5z" clipRule="evenodd" />
      </svg>
    );
  }
  if (status === "fail") {
    return (
      <svg className="h-4 w-4 shrink-0 text-red-500" viewBox="0 0 20 20" fill="currentColor">
        <path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.28 7.22a.75.75 0 00-1.06 1.06L8.94 10l-1.72 1.72a.75.75 0 101.06 1.06L10 11.06l1.72 1.72a.75.75 0 101.06-1.06L11.06 10l1.72-1.72a.75.75 0 00-1.06-1.06L10 8.94 8.28 7.22z" clipRule="evenodd" />
      </svg>
    );
  }
  if (status === "warn") {
    return (
      <svg className="h-4 w-4 shrink-0 text-amber-500" viewBox="0 0 20 20" fill="currentColor">
        <path fillRule="evenodd" d="M8.485 2.495c.673-1.167 2.357-1.167 3.03 0l6.28 10.875c.673 1.167-.17 2.625-1.516 2.625H3.72c-1.347 0-2.189-1.458-1.515-2.625L8.485 2.495zM10 5a.75.75 0 01.75.75v3.5a.75.75 0 01-1.5 0v-3.5A.75.75 0 0110 5zm0 9a1 1 0 100-2 1 1 0 000 2z" clipRule="evenodd" />
      </svg>
    );
  }
  return (
    <svg className="h-4 w-4 shrink-0 text-gray-300" viewBox="0 0 20 20" fill="currentColor">
      <circle cx="10" cy="10" r="8" />
    </svg>
  );
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      onClick={() => { navigator.clipboard.writeText(text).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1800); }); }}
      className="shrink-0 rounded px-2 py-0.5 text-[10px] font-medium text-gray-500 border border-gray-200 hover:border-gray-400 hover:text-gray-700 transition-colors"
    >
      {copied ? "Copied" : "Copy"}
    </button>
  );
}

function CheckRow({ check }: { check: DiagCheck }) {
  const [expanded, setExpanded] = useState(false);
  const [detailExpanded, setDetailExpanded] = useState(false);
  const hasFix = !!(check.fix && check.status !== "pass");
  const isAlert = check.status === "fail" || check.status === "warn";
  const DETAIL_TRUNCATE = 180;
  const detailTruncated = check.detail && check.detail.length > DETAIL_TRUNCATE;
  const displayDetail = detailTruncated && !detailExpanded
    ? check.detail.slice(0, DETAIL_TRUNCATE) + "…"
    : check.detail;

  const fc = isAlert ? inferFailureClass(check) : undefined;
  const fcMeta = fc ? FAILURE_CLASS_LABELS[fc] : undefined;

  const fixBtnClass = check.status === "fail"
    ? "shrink-0 rounded px-2.5 py-1 text-[11px] font-medium text-white bg-red-500 hover:bg-red-600"
    : "shrink-0 rounded px-2.5 py-1 text-[11px] font-medium text-white bg-amber-500 hover:bg-amber-600";

  return (
    <div className={`rounded border px-3 py-2 ${
      check.status === "fail" ? "border-red-100 bg-red-50" :
      check.status === "warn" ? "border-amber-100 bg-amber-50" :
      "border-gray-100 bg-white"
    }`}>
      <div className="flex items-start gap-2">
        <StatusIcon status={check.status} />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5 flex-wrap">
            <span className="text-xs font-medium text-gray-800">{check.label}</span>
            {fcMeta && (
              <span className={`inline-flex items-center rounded px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide ${fcMeta.color}`}>
                {fcMeta.label}
              </span>
            )}
          </div>
          {check.root_cause && (
            <p className="mt-0.5 text-[11px] font-semibold text-gray-900">{check.root_cause}</p>
          )}
          {isAlert && check.detail ? (
            <div>
              <p className="mt-0.5 text-[11px] text-red-500 break-all">{displayDetail}</p>
              {detailTruncated && (
                <button
                  onClick={() => setDetailExpanded(e => !e)}
                  className="mt-0.5 text-[10px] text-gray-500 underline hover:text-gray-600"
                >
                  {detailExpanded ? "show less" : "show more"}
                </button>
              )}
            </div>
          ) : check.detail ? (
            <p className="mt-0.5 text-[11px] text-gray-600">{displayDetail}</p>
          ) : null}
          {expanded && hasFix && (
            <div className="mt-2 space-y-1">
              {fixIsSql(check.fix) ? (
                <>
                  <div className="flex items-center justify-between">
                    <span className="text-[10px] font-medium uppercase tracking-wide text-gray-500">Run as metastore admin</span>
                    <CopyButton text={check.fix} />
                  </div>
                  <pre className="overflow-x-auto whitespace-pre-wrap rounded bg-gray-900 px-3 py-2 text-[11px] leading-relaxed text-green-400">
                    {check.fix}
                  </pre>
                </>
              ) : (
                <p className="whitespace-pre-wrap rounded border border-gray-200 bg-white px-2.5 py-2 text-[10px] leading-relaxed text-gray-700">
                  {check.fix}
                </p>
              )}
            </div>
          )}
        </div>
        {hasFix && (
          <button onClick={() => setExpanded(e => !e)} className={fixBtnClass}>
            {expanded ? "Hide" : fixActionLabel(check)}
          </button>
        )}
      </div>
    </div>
  );
}

function CategorySection({ category, checks }: { category: string; checks: DiagCheck[] }) {
  const failed = checks.filter(c => c.status === "fail").length;
  const warned = checks.filter(c => c.status === "warn").length;

  return (
    <div>
      <div className="mb-2 flex items-center gap-2">
        <h4 className="text-xs font-semibold uppercase tracking-wide text-gray-500">
          {CATEGORY_LABELS[category] ?? category}
        </h4>
        {failed > 0 && (
          <span className="rounded-full bg-red-100 px-1.5 py-0.5 text-[10px] font-medium text-red-700">
            {failed} failed
          </span>
        )}
        {warned > 0 && (
          <span className="rounded-full bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-700">
            {warned} warning{warned !== 1 ? "s" : ""}
          </span>
        )}
      </div>
      <div className="space-y-1.5">
        {checks.map(c => <CheckRow key={c.id} check={c} />)}
      </div>
    </div>
  );
}

interface SettingsDebuggerProps {
  onGoToConfig?: () => void;
}

export function SettingsDebugger({ onGoToConfig }: SettingsDebuggerProps) {
  const [runKey, setRunKey] = useState(debuggerRunState.runKey);
  const [hasRun, setHasRun] = useState(debuggerRunState.hasRun);

  const { data: installReport, isLoading: configLoading } = useQuery<InstallReport>({
    queryKey: ["settings-install-report"],
    queryFn: async () => {
      const res = await fetch("/api/settings/config");
      if (!res.ok) throw new Error("config fetch failed");
      return res.json();
    },
    staleTime: 5 * 60 * 1000,
    retry: false,
  });

  const { data: authStatusSlim, isLoading: authLoading } = useQuery<AuthStatusSlim>({
    queryKey: ["settings-auth-status"],
    queryFn: async () => {
      const res = await fetch("/api/settings/auth-status");
      if (!res.ok) throw new Error("auth-status fetch failed");
      return res.json();
    },
    staleTime: 10 * 1000,
    retry: false,
  });

  const { data: result, isFetching, isError } = useQuery<DiagResult>({
    queryKey: ["debug-run", runKey],
    queryFn: async () => {
      const res = await fetch("/api/debug/run?no_cache=true");
      if (!res.ok) throw new Error("Diagnostics request failed");
      return res.json();
    },
    enabled: hasRun,
    staleTime: Infinity,
    retry: false,
  });

  const handleRun = () => {
    const next = debuggerRunState.runKey + 1;
    debuggerRunState.runKey = next;
    debuggerRunState.hasRun = true;
    setRunKey(next);
    setHasRun(true);
  };

  const hasMvFailure = result?.checks.some(c => c.category === "materialized_views" && c.status === "fail");
  const { summary } = result ?? { summary: { passed: 0, failed: 0, warned: 0, total: 0 } };

  const grouped = CATEGORY_ORDER
    .map(cat => ({
      category: cat,
      checks: (result?.checks ?? []).filter(c => c.category === cat),
    }))
    .filter(g => g.checks.length > 0);

  return (
    <div className="space-y-3">
      <p className="text-xs text-gray-500">
        Diagnose permission errors, missing materialized views, and data availability issues. Run a full check to get step-by-step remediation.
      </p>

      {/* Deployment snapshot — two columns to minimise height */}
      <div className="rounded-lg border border-gray-200 bg-gray-50 px-3 py-2">
        <p className="mb-2 text-[10px] font-semibold uppercase tracking-wide text-gray-500">Deployment Info</p>
        {(configLoading || authLoading) ? (
          <div className="grid grid-cols-2 gap-x-4 gap-y-1.5">
            {[...Array(6)].map((_, i) => (
              <div key={i} className="grid grid-cols-2 gap-x-3">
                <div className="h-3 w-16 animate-pulse rounded bg-gray-200" />
                <div className="h-3 w-24 animate-pulse rounded bg-gray-200" />
              </div>
            ))}
          </div>
        ) : (
          <div className="grid grid-cols-2 gap-x-6 text-[11px]">
            {/* Left: auth / identity */}
            <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 content-start">
              {installReport?.version?.repo && (
                <>
                  <dt className="text-gray-500">Repo</dt>
                  <dd className="font-mono text-gray-700 truncate text-[10px]">{installReport.version.repo.replace(/^https?:\/\//, "").replace(/\.git$/, "")}</dd>
                </>
              )}
              {installReport?.version?.branch && (
                <>
                  <dt className="text-gray-500">Branch</dt>
                  <dd className="font-mono text-gray-700">{installReport.version.branch}</dd>
                </>
              )}
              <dt className="text-gray-500">Last deploy</dt>
              <dd className="font-mono text-gray-700">
                {installReport?.version?.commit_date || (installReport?.version?.commit_sha
                  ? <code className="rounded bg-gray-200 px-1 font-mono text-gray-700">{installReport.version.commit_sha}</code>
                  : <span className="text-gray-500">—</span>)}
              </dd>
              <dt className="text-gray-500">Auth mode</dt>
              <dd className="font-medium text-gray-700">{authStatusSlim?.auth_mode ?? "—"}</dd>
              <dt className="text-gray-500">Identity</dt>
              <dd className="font-medium text-gray-700">{authStatusSlim?.identity ?? "—"}</dd>
              {authStatusSlim?.locked_to_sp != null && (
                <>
                  <dt className="text-gray-500">Locked to SP</dt>
                  <dd className="font-medium text-gray-700">{authStatusSlim.locked_to_sp ? "yes" : "no"}</dd>
                </>
              )}
              {authStatusSlim?.has_sql_scope != null && (
                <>
                  <dt className="text-gray-500">SQL scope</dt>
                  <dd className="font-medium text-gray-700">{authStatusSlim.has_sql_scope ? "yes" : "no"}</dd>
                </>
              )}
              {authStatusSlim?.sp_display_name && (
                <>
                  <dt className="text-gray-500">SP name</dt>
                  <dd className="font-mono text-gray-700 truncate">{authStatusSlim.sp_display_name}</dd>
                </>
              )}
              {(authStatusSlim?.sp_user_name || authStatusSlim?.sp_client_id) && (
                <>
                  <dt className="text-gray-500">SP client ID</dt>
                  <dd className="font-mono text-gray-700 truncate">{authStatusSlim.sp_user_name || authStatusSlim.sp_client_id}</dd>
                </>
              )}
            </dl>
            {/* Right: warehouse / storage */}
            <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 content-start">
              <dt className="text-gray-500">Warehouse ID</dt>
              <dd className="font-mono text-gray-700 truncate">{installReport?.warehouse?.id ?? "—"}</dd>
              {installReport?.warehouse?.name && (
                <>
                  <dt className="text-gray-500">Name</dt>
                  <dd className="font-medium text-gray-700 truncate">{installReport.warehouse.name}</dd>
                </>
              )}
              {installReport?.warehouse?.size && (
                <>
                  <dt className="text-gray-500">Size</dt>
                  <dd className="font-medium text-gray-700">{installReport.warehouse.size}</dd>
                </>
              )}
              <dt className="text-gray-500">State</dt>
              <dd className="font-medium text-gray-700">{installReport?.warehouse?.state ?? "—"}</dd>
              <dt className="text-gray-500">Source</dt>
              <dd className="font-medium text-gray-700">{installReport?.warehouse?.source ?? "—"}</dd>
              <dt className="text-gray-500">Storage</dt>
              <dd className="font-mono text-gray-700 truncate">
                {installReport?.storage_location
                  ? `${installReport.storage_location.catalog}.${installReport.storage_location.schema}`
                  : "—"}
              </dd>
            </dl>
          </div>
        )}
      </div>

      {/* Run button + summary */}
      <div className="flex items-center gap-3 flex-wrap">
        <button
          onClick={handleRun}
          disabled={isFetching}
          className="inline-flex items-center gap-2 rounded-lg px-4 py-2 text-sm font-medium text-white transition-colors disabled:opacity-60"
          style={{ backgroundColor: isFetching ? "#FFA390" : "#FF3621" }}
        >
          {isFetching ? (
            <>
              <svg className="h-4 w-4 animate-spin" viewBox="0 0 24 24" fill="none">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4l3-3-3-3v4a8 8 0 00-8 8h4z" />
              </svg>
              Running checks…
            </>
          ) : hasRun ? (
            <>
              <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
              </svg>
              Re-run Diagnostics
            </>
          ) : (
            <>
              <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
              Run Diagnostics
            </>
          )}
        </button>

        {result && !isFetching && (
          <div className="flex items-center gap-3 text-sm">
            {summary.failed > 0 && (
              <span className="font-medium text-red-600">
                {summary.failed} failed
              </span>
            )}
            {summary.warned > 0 && (
              <span className="font-medium text-amber-600">
                {summary.warned} warning{summary.warned !== 1 ? "s" : ""}
              </span>
            )}
            {summary.failed === 0 && summary.warned === 0 && (
              <span className="font-medium text-green-600">All {summary.total} checks passed</span>
            )}
          </div>
        )}

        {isError && (
          <span className="text-sm text-red-600">Diagnostics request failed — check server logs</span>
        )}
      </div>

      {/* MV rebuild action */}
      {hasMvFailure && (
        <div className="rounded-lg border border-red-200 bg-red-50 p-3 flex items-start gap-3">
          <svg className="h-4 w-4 shrink-0 text-red-500 mt-0.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
          </svg>
          <div className="flex-1 min-w-0">
            <p className="text-xs font-medium text-red-800">Materialized views are missing or empty</p>
            <p className="mt-0.5 text-[11px] text-red-700">This is the most common cause of $0 across the entire dashboard. Go to Configuration to rebuild them.</p>
          </div>
          {onGoToConfig && (
            <button
              onClick={onGoToConfig}
              className="shrink-0 inline-flex items-center gap-1.5 rounded bg-red-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-red-700"
            >
              Go to Config
            </button>
          )}
        </div>
      )}

      {/* Check results grouped by category */}
      {grouped.length > 0 && (
        <div className="space-y-4">
          {grouped.map(g => (
            <CategorySection key={g.category} category={g.category} checks={g.checks} />
          ))}
        </div>
      )}

      {!hasRun && (
        <div className="rounded-lg border border-gray-200 bg-gray-50 px-4 py-3 flex items-center gap-3">
          <svg className="h-5 w-5 shrink-0 text-gray-300" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" />
          </svg>
          <p className="text-xs text-gray-500">Click <strong>Run Diagnostics</strong> to check permissions, materialized views, and data availability</p>
        </div>
      )}
    </div>
  );
}
