import { useState, useRef, useEffect } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { READINESS_QUERY_KEY } from "@/hooks/useFeatureAvailability";
import { MvSourcesSection } from "./MvSourcesSection";
import { Group, Select, SecondaryButton, DangerOutlineButton, TextInput, Callout, T, MONO } from "./dubois";

// Kept for the Data & tables section's storage-location chip (it imports this type).
export interface AppConfigInfo {
  warehouse: { id: string; name: string | null; size: string | null; state: string; source?: "app_resource" | "http_path" | "none" } | null;
  identity: { display_name: string | null; user_name: string | null } | null;
  storage_location: { catalog: string; schema: string; catalog_source?: "env_var" | "default"; schema_source?: "env_var" | "default" } | null;
  version?: { commit_sha: string; branch?: string; repo?: string; commit_date?: string };
}

// Small red danger indicator with a hover tooltip for per-column errors.
function ColWarn({ error }: { error: string }) {
  return (
    <span className="group relative inline-block ml-1 cursor-help" title={error}>
      <svg className="h-3 w-3" style={{ color: T.dangerFg }} viewBox="0 0 20 20" fill="currentColor">
        <path fillRule="evenodd" d="M8.485 2.495c.673-1.167 2.357-1.167 3.03 0l6.28 10.875c.673 1.167-.17 2.625-1.516 2.625H3.72c-1.347 0-2.189-1.458-1.515-2.625L8.485 2.495zM10 5a.75.75 0 01.75.75v3.5a.75.75 0 01-1.5 0v-3.5A.75.75 0 0110 5zm0 9a1 1 0 100-2 1 1 0 000 2z" clipRule="evenodd" />
      </svg>
    </span>
  );
}

// Module-level — survives tab switches (component unmount/remount)
let _mvRefreshing = false;
const TRIGGER_LABEL: Record<string, string> = { manual: "manual", scheduled: "nightly", startup: "post-deploy" };

function latestManualRun(history?: Array<{ timestamp: string; status: string; trigger: string }>) {
  return [...(history ?? [])].reverse().find((entry) => entry.trigger === "manual") ?? null;
}
let _mvPrevRefreshTime: string | null = null;
let _mvDeadline = 0;
let _mvPollInterval: ReturnType<typeof setInterval> | null = null;
let _mvPollCallback: (() => Promise<void>) | null = null;
let _mvLastResult: string | null = null;

const RETENTION: Record<string, string> = {
  daily_usage_summary: "~3yr (billing.usage)",
  daily_product_breakdown: "~3yr (billing.usage)",
  daily_workspace_breakdown: "~3yr (billing.usage)",
  sql_tool_attribution: "~13mo (query.history)",
  daily_query_stats: "~13mo (query.history)",
  dbsql_cost_per_query: "~13mo (query.history)",
};

// Managed-tables surface for the "Data & tables" settings section — DuBois styled.
// Catalog/schema location, workspace filter, and refresh schedule are rendered by the
// parent DataTablesSection; this owns table status, rebuild, shared sources, and drop.
export function SettingsConfig() {
  const queryClient = useQueryClient();
  const [mvRefreshing, setMvRefreshing] = useState(_mvRefreshing);
  const [mvLastResult, setMvLastResult] = useState<string | null>(_mvLastResult);
  const [lookbackDays, setLookbackDays] = useState(180);
  const noCacheRef = useRef(false);

  const { data: tablesStatus = null, isLoading: tablesLoading, isFetching: tablesFetching, refetch: refetchTables } = useQuery<{
    catalog: string | null;
    schema: string | null;
    error?: string | null;
    auth_error?: string | null;
    refresh_status?: {
      last_refresh_utc: string;
      duration_seconds: number | null;
      hours_since_refresh: number | null;
      stale: boolean;
      in_progress?: { started_utc: string | null; trigger: string | null } | null;
      status: string;
      lookback_days?: number | null;
      error?: string;
      refresh_history?: Array<{
        timestamp: string;
        status: string;
        duration_seconds: number;
        lookback_days: number | null;
        trigger: "manual" | "scheduled" | "startup" | "config";
        note?: string;
        error?: string;
      }>;
    } | null;
    tables: Array<{
      name: string;
      table_type: string | null;
      exists: boolean | null;
      optional?: boolean;
      row_count: number | null;
      min_date: string | null;
      max_date: string | null;
      days_behind: number | null;
      owner?: string | null;
      error?: string;
    }>;
  } | null>({
    queryKey: ["settings-tables-status"],
    queryFn: () => {
      const url = noCacheRef.current ? "/api/settings/tables?no_cache=1" : "/api/settings/tables";
      noCacheRef.current = false;
      return fetch(url).then(r => r.json()).catch(() => null);
    },
    staleTime: 15 * 60 * 1000,
  });

  // Register the poll callback on every render so it always has current closures.
  _mvPollCallback = async () => {
    noCacheRef.current = true;
    const result = await refetchTables();
    const manualRun = latestManualRun(result.data?.refresh_status?.refresh_history);
    if ((manualRun && manualRun.timestamp !== _mvPrevRefreshTime) || Date.now() > _mvDeadline) {
      if (_mvPollInterval) { clearInterval(_mvPollInterval); _mvPollInterval = null; }
      _mvRefreshing = false;
      setMvRefreshing(false);
      queryClient.setQueryData(["rebuild-in-progress"], false);
      const status = manualRun && manualRun.timestamp !== _mvPrevRefreshTime ? manualRun.status : null;
      _mvLastResult = status;
      setMvLastResult(status);
      queryClient.invalidateQueries({ queryKey: READINESS_QUERY_KEY });
      setTimeout(() => { _mvLastResult = null; setMvLastResult(null); }, 30_000);
    }
  };

  useEffect(() => {
    if (_mvRefreshing && !_mvPollInterval) {
      _mvPollInterval = setInterval(() => _mvPollCallback?.(), 30_000);
    }
    return () => { _mvPollCallback = null; };
  }, []);

  async function handleMvRefresh() {
    if (_mvRefreshing) return;
    _mvRefreshing = true;
    _mvLastResult = null;
    _mvDeadline = Date.now() + 15 * 60 * 1000;
    setMvRefreshing(true);
    setMvLastResult(null);
    queryClient.setQueryData(["rebuild-in-progress"], true);
    // The cached status can be 15 minutes old; the server recomputes refresh_status on every call.
    const fresh = await refetchTables();
    _mvPrevRefreshTime = latestManualRun(
      (fresh.data ?? tablesStatus)?.refresh_status?.refresh_history,
    )?.timestamp ?? null;
    try {
      const resp = await fetch(`/api/settings/refresh-mvs?lookback_days=${lookbackDays}`, { method: "POST" });
      if (!resp.ok) {
        _mvRefreshing = false;
        setMvRefreshing(false);
        queryClient.setQueryData(["rebuild-in-progress"], false);
        _mvLastResult = "error";
        setMvLastResult("error");
        return;
      }
    } catch { /* fire-and-forget — server runs refresh in background */ }
    if (_mvPollInterval) clearInterval(_mvPollInterval);
    setTimeout(() => _mvPollCallback?.(), 3_000);
    _mvPollInterval = setInterval(() => _mvPollCallback?.(), 30_000);
  }

  // Drop-tables (wipe) flow
  const [wipePending, setWipePending] = useState(false);
  const [wiping, setWiping] = useState(false);
  const [wipeResult, setWipeResult] = useState<{ ok: boolean; results?: Record<string, string>; error?: string } | null>(null);
  const [wipeConfirmText, setWipeConfirmText] = useState("");

  const handleWipeMVs = async () => {
    setWiping(true);
    setWipeResult(null);
    try {
      const res = await fetch("/api/setup/drop-materialized-views", { method: "DELETE" });
      const data = await res.json();
      if (!res.ok || !data.results) setWipeResult({ ok: false, error: data.error ?? data.detail ?? `HTTP ${res.status}` });
      else setWipeResult(data);
    } catch (e) {
      setWipeResult({ ok: false, error: String(e) });
    } finally {
      setWiping(false);
      setWipePending(false);
      setWipeConfirmText("");
      refetchTables();
      queryClient.invalidateQueries({ queryKey: READINESS_QUERY_KEY });
    }
  };

  const rs = tablesStatus?.refresh_status;
  const running = rs?.in_progress ?? null;
  const rebuildBusy = mvRefreshing || running != null;
  const fmtStarted = (ts: string) => { try { return new Date(ts).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", timeZoneName: "short" }); } catch { return ts; } };
  const runningSince = running?.started_utc ? ` since ${fmtStarted(running.started_utc)}` : "";
  const runningKind = running?.trigger ? `${TRIGGER_LABEL[running.trigger] ?? running.trigger} ` : "";
  const headerStatus = running
    ? `Rebuild running${runningSince}`
    : rs == null || rs.hours_since_refresh == null ? "Never rebuilt"
    : rs.status === "error" ? "Last rebuild failed"
    : rs.stale ? "Stale (>26h)"
    : rs.hours_since_refresh < 1 ? "Rebuilt <1h ago"
    : `Rebuilt ${rs.hours_since_refresh}h ago`;
  // Safety invariant: when a required (non-optional) managed table is already missing,
  // the system is degraded — the drop action is hard-blocked (no break-glass path) so a
  // broken deploy can't be dropped into a worse state. The CONFIRM gate is the second layer.
  const degradedTables = (tablesStatus?.tables ?? []).some((t) => t.exists === false && !t.optional);
  const th: React.CSSProperties = { padding: "6px 10px", fontSize: 11, fontWeight: 600, color: T.textSecondary, whiteSpace: "nowrap" };
  const td: React.CSSProperties = { padding: "6px 10px", fontSize: 12, color: T.text, borderTop: `1px solid ${T.borderRow}` };

  return (
    <div>
      {/* Shared Delta-Sharing sources */}
      <MvSourcesSection />

      {/* Managed tables */}
      <div style={{ fontSize: 13, fontWeight: 600, color: T.text, margin: "4px 0 8px", display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12, flexWrap: "wrap" }}>
        <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
          Managed tables
          <span style={{ fontSize: 12, fontWeight: 400, color: T.textSecondary }}>
            {headerStatus}
          </span>
        </span>
        <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
          <Select value={lookbackDays} onChange={(v) => setLookbackDays(Number(v))} disabled={mvRefreshing}
            options={[{ value: 180, label: "6 months" }, { value: 365, label: "1 year" }, { value: 730, label: "2 years" }, { value: 1095, label: "3 years" }]} />
          <SecondaryButton onClick={() => { noCacheRef.current = true; refetchTables(); }} disabled={mvRefreshing || tablesFetching}>Check status</SecondaryButton>
          <SecondaryButton onClick={handleMvRefresh} disabled={rebuildBusy}>{rebuildBusy ? "Rebuilding…" : "Rebuild now"}</SecondaryButton>
        </span>
      </div>

      {/* Result / error banners */}
      {!mvRefreshing && mvLastResult === "success" && (
        <div style={{ marginBottom: 12 }}><Callout tone="success">Rebuild complete. All materialized views updated successfully.</Callout></div>
      )}
      {!mvRefreshing && (mvLastResult === "partial_error" || mvLastResult === "error") && (
        <div style={{ marginBottom: 12 }}><Callout tone="danger">Rebuild {mvLastResult === "partial_error" ? "partially failed" : "failed"}. See details below.</Callout></div>
      )}
      {!rebuildBusy && rs?.status && ["error", "partial_error"].includes(rs.status) && rs.error && (
        <div style={{ marginBottom: 12 }}><Callout tone="danger"><strong>Last rebuild failed.</strong> {rs.error}</Callout></div>
      )}
      {tablesStatus?.auth_error && (
        <div style={{ marginBottom: 12 }}><Callout tone="warning">{tablesStatus.auth_error}</Callout></div>
      )}
      {rebuildBusy && (
        <div style={{ marginBottom: 12 }}><Callout tone="warning">
          {running
            ? `A ${runningKind}rebuild${running.started_utc ? ` started ${fmtStarted(running.started_utc)}` : ""} is still running. Use Check status to see when it finishes.`
            : "Rebuilding materialized views in the background — this may take a few minutes. The table below updates automatically when complete."}
        </Callout></div>
      )}

      {/* Table */}
      {tablesLoading ? (
        <div style={{ padding: "12px 0", textAlign: "center", fontSize: 12, color: T.textSecondary }}>Checking tables…</div>
      ) : tablesStatus?.tables?.length ? (
        <div style={{ border: `1px solid ${T.borderGroup}`, borderRadius: 8, overflow: "hidden", opacity: mvRefreshing ? 0.5 : 1, transition: "opacity 300ms" }}>
          <div style={{ overflowX: "auto" }}>
            <table style={{ minWidth: "100%", borderCollapse: "collapse" }}>
              <thead style={{ backgroundColor: T.navBg }}>
                <tr>
                  <th style={{ ...th, textAlign: "left" }}>Table</th>
                  <th style={{ ...th, textAlign: "left" }}>Type</th>
                  <th style={{ ...th, textAlign: "right" }}>Rows</th>
                  <th style={{ ...th, textAlign: "right" }}>Retention limit</th>
                  <th style={{ ...th, textAlign: "right" }}>Latest date</th>
                  <th style={{ ...th, textAlign: "right" }}>Freshness</th>
                </tr>
              </thead>
              <tbody>
                {tablesStatus.tables.map((t) => {
                  const billingSource = RETENTION[t.name]?.includes("billing.usage") ?? false;
                  const queryHistorySource = RETENTION[t.name]?.includes("query.history") ?? false;
                  const missing = t.exists === false && !t.optional;
                  const notConfigured = t.exists === false && t.optional;
                  const unknown = t.exists === null;
                  const mark = missing ? { c: T.dangerFg, s: "✗" } : notConfigured ? { c: T.textFaint, s: "–" } : unknown ? { c: T.textFaint, s: "?" } : { c: T.successFg, s: "✓" };
                  let fresh: React.ReactNode = <span style={{ color: T.textFaint }}>—</span>;
                  if (t.days_behind != null) {
                    if (t.days_behind === 0) fresh = <span style={{ color: T.successFg, fontWeight: 600 }}>Today</span>;
                    else if ((billingSource && t.days_behind <= 4) || (queryHistorySource && t.days_behind <= 2)) fresh = <span style={{ color: T.successFg, fontWeight: 600 }}>Up to date</span>;
                    else if (t.days_behind <= 4) fresh = <span style={{ color: T.textSecondary }}>{t.days_behind}d behind</span>;
                    else if (t.days_behind <= 7) fresh = <span style={{ color: T.warningFg, fontWeight: 600 }}>{t.days_behind}d behind</span>;
                    else fresh = <span style={{ color: T.dangerFg, fontWeight: 600 }}>{t.days_behind}d behind</span>;
                  }
                  return (
                    <tr key={t.name}>
                      <td style={{ ...td, fontFamily: MONO }}>
                        <span style={{ color: mark.c, marginRight: 6 }}>{mark.s}</span>{t.name}{t.error && <ColWarn error={t.error} />}
                      </td>
                      <td style={td}>
                        {t.table_type ? <span style={{ fontSize: 10, fontWeight: 500, color: T.textSecondary, backgroundColor: T.codeBg, borderRadius: 3, padding: "1px 6px" }}>{t.table_type}</span> : "—"}
                      </td>
                      <td style={{ ...td, textAlign: "right", fontVariantNumeric: "tabular-nums", color: T.textSecondary }}>{t.row_count != null ? t.row_count.toLocaleString() : "—"}</td>
                      <td style={{ ...td, textAlign: "right", color: T.textSecondary, fontSize: 11 }}>{RETENTION[t.name] ?? "—"}</td>
                      <td style={{ ...td, textAlign: "right", fontFamily: MONO, color: T.textSecondary }}>{t.max_date ? t.max_date.slice(0, 10) : "—"}</td>
                      <td style={{ ...td, textAlign: "right" }}>{fresh}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      ) : (
        <div style={{ border: `1px solid ${T.borderGroup}`, borderRadius: 8, padding: 12, fontSize: 12, color: T.textSecondary }}>
          {tablesStatus?.error ? `Table status error: ${tablesStatus.error}` : "Could not retrieve table status"}
        </div>
      )}

      {/* Rebuild history */}
      {tablesStatus && (() => {
        const history = rs?.refresh_history ?? [];
        const fmtWindow = (d: number) => (!d ? "—" : d === 180 ? "6 months" : d === 365 ? "1 year" : d === 730 ? "2 years" : d === 1095 ? "3 years" : `${d} days`);
        const fmtDuration = (s: number) => (s < 60 ? `${Math.round(s)}s` : `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`);
        const fmtTs = (ts: string) => { try { return new Date(ts).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", timeZoneName: "short" }); } catch { return ts; } };
        const toneFor = (s: string) => s === "success" ? T.successFg : s === "partial_error" ? T.warningFg : s === "config" ? T.primary : s === "dropped" ? T.textFaint : T.dangerFg;
        const resultLabel = (s: string) => s === "partial_error" ? "Partial" : s === "config" ? "Config" : s;
        return (
          <div style={{ marginTop: 16 }}>
            <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", marginBottom: 6 }}>
              <span style={{ fontSize: 12, fontWeight: 600, color: T.text }}>Rebuild history</span>
              <span style={{ fontSize: 10, color: T.textSecondary }}>Last 10 operations</span>
            </div>
            {history.length === 0 ? (
              <p style={{ fontSize: 12, color: T.textSecondary, fontStyle: "italic", margin: "0 2px" }}>No operations recorded yet. History appears here after the first manual or nightly rebuild.</p>
            ) : (
              <div style={{ border: `1px solid ${T.borderGroup}`, borderRadius: 8, overflow: "hidden" }}>
                <table style={{ minWidth: "100%", borderCollapse: "collapse" }}>
                  <thead style={{ backgroundColor: T.navBg }}>
                    <tr>
                      <th style={{ ...th, textAlign: "left" }}>Date / time</th>
                      <th style={{ ...th, textAlign: "left" }}>Trigger</th>
                      <th style={{ ...th, textAlign: "right" }}>Window</th>
                      <th style={{ ...th, textAlign: "right" }}>Duration</th>
                      <th style={{ ...th, textAlign: "right" }}>Result</th>
                    </tr>
                  </thead>
                  <tbody>
                    {[...history].reverse().slice(0, 10).map((e, i) => (
                      <tr key={i}>
                        <td style={{ ...td, fontFamily: MONO, fontSize: 11, color: T.textSecondary }}>{fmtTs(e.timestamp)}</td>
                        <td style={{ ...td, color: T.textSecondary }} title={e.note || undefined}>
                          {/* Config/lineage events (e.g. adding a shared source) carry a note describing the change. */}
                          {e.note ? e.note : <span style={{ textTransform: "capitalize" }}>{e.trigger}</span>}
                        </td>
                        <td style={{ ...td, textAlign: "right", color: T.textSecondary }}>{e.status === "config" ? "—" : fmtWindow(e.lookback_days ?? 0)}</td>
                        <td style={{ ...td, textAlign: "right", color: T.textSecondary, fontVariantNumeric: "tabular-nums" }}>{e.status === "dropped" || e.status === "config" ? "—" : fmtDuration(e.duration_seconds)}</td>
                        <td style={{ ...td, textAlign: "right", color: toneFor(e.status), fontWeight: 600, textTransform: "capitalize" }} title={e.error}>{resultLabel(e.status)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        );
      })()}

      {/* Danger zone */}
      <div style={{ marginTop: 20 }}>
        <Group label="Danger zone" danger>
          <div style={{ padding: "12px 16px" }}>
            <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 12 }}>
              <div>
                <div style={{ fontSize: 13, fontWeight: 600, color: T.dangerFg }}>Drop all managed tables</div>
                <div style={{ fontSize: 12, color: T.dangerFg, marginTop: 2, opacity: 0.85 }}>
                  Permanently deletes the app-managed tables from your catalog. The dashboard stops loading until you rebuild. No source data is lost — <code style={{ fontFamily: MONO }}>system.*</code> is the source of truth.
                </div>
              </div>
              {!wipePending
                ? <DangerOutlineButton disabled={degradedTables} onClick={() => { if (degradedTables) return; setWipePending(true); setWipeResult(null); setWipeConfirmText(""); }}>Drop tables…</DangerOutlineButton>
                : <SecondaryButton onClick={() => { setWipePending(false); setWipeConfirmText(""); }}>Cancel</SecondaryButton>}
            </div>
            {degradedTables && (
              <div style={{ marginTop: 10, fontSize: 12, color: T.warningFg }}>
                A required table is already missing — drop is disabled until the tables are rebuilt.
              </div>
            )}
            {wipePending && (
              <div style={{ marginTop: 12, display: "flex", flexDirection: "column", gap: 8 }}>
                <div style={{ fontSize: 12, color: T.dangerFg }}>Type <code style={{ fontFamily: MONO }}>CONFIRM</code> to enable the drop:</div>
                <div className="flex items-center gap-2">
                  <TextInput mono value={wipeConfirmText} onChange={setWipeConfirmText} placeholder="Type CONFIRM" width={160} />
                  <button
                    type="button" onClick={handleWipeMVs} disabled={wiping || wipeConfirmText !== "CONFIRM"}
                    style={{ height: 32, borderRadius: 4, padding: "0 14px", fontSize: 13, fontWeight: 500, color: "#FFFFFF", backgroundColor: wiping || wipeConfirmText !== "CONFIRM" ? "#E9A6B4" : T.dangerFg, cursor: wiping || wipeConfirmText !== "CONFIRM" ? "not-allowed" : "pointer", border: "none" }}
                  >{wiping ? "Dropping…" : "Confirm drop"}</button>
                </div>
              </div>
            )}
            {wipeResult && (
              <div style={{ marginTop: 10 }}>
                <Callout tone={wipeResult.ok ? "success" : "danger"}>
                  {wipeResult.ok ? "All tables dropped. Use Rebuild to recreate them."
                    : wipeResult.error ? `Drop failed: ${wipeResult.error}`
                    : `Some tables failed to drop: ${Object.entries(wipeResult.results ?? {}).filter(([, v]) => v !== "dropped").map(([k]) => k).join(", ")}`}
                </Callout>
              </div>
            )}
          </div>
        </Group>
      </div>
    </div>
  );
}
