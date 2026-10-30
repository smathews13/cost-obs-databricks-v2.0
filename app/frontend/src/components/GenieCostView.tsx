import { useMemo, useState } from "react";
import {
  AreaChart, Area, XAxis, YAxis,
  CartesianGrid, Tooltip, ResponsiveContainer, Legend,
} from "recharts";
import { format, parseISO } from "date-fns";
import type {
  GenieCostDashboardBundle,
  GenieCostSurfaceRow,
  GenieCostTimeseriesRow,
  GenieCostWorkspaceRow,
  GenieSpaceInventory,
} from "@/types/billing";
import { formatCurrency } from "@/utils/formatters";
import { PageHero, InfoPanel } from "@/components/brand";
import { KPICard } from "@/components/ui/KPICard";
import { C } from "@/theme";
import { useGenieCostDiagnostics } from "@/hooks/useBillingData";

// ── Number formatting helpers ─────────────────────────────────────────────────

function fmtPct(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return "—";
  return `${(v * 100).toFixed(1)}%`;
}

function fmtDateShort(iso?: string | null): string {
  if (!iso) return "—";
  try { return format(parseISO(iso), "MMM d"); } catch { return String(iso); }
}

// ── KPI card ──────────────────────────────────────────────────────────────────

function KpiCard({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return <KPICard title={label} value={value} subtitle={sub} />;
}

// ── Empty banner ─────────────────────────────────────────────────────────────

function NoGenieBanner() {
  return (
    <div className="rounded-lg bg-white border p-8 flex flex-col items-center justify-center gap-3 text-center" style={{ borderColor: "#E5E5E5" }}>
      <div className="flex h-12 w-12 items-center justify-center rounded-full" style={{ background: "rgba(255,54,33,0.08)" }}>
        <svg className="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor" style={{ color: "#FF3621" }}>
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
        </svg>
      </div>
      <h3 className="text-base font-semibold text-gray-900">No Genie spend in this window</h3>
      <p className="max-w-md text-sm text-gray-500">
        No <code className="bg-gray-100 px-1 rounded text-xs">system.billing.usage</code> rows matched
        the Genie inclusion filter for the selected date range. If you expected data here, open the
        Diagnostics tab to see what SKUs are present in your billing data.
      </p>
    </div>
  );
}

// ── Surface breakdown card ────────────────────────────────────────────────────

function SurfaceBreakdownCard({ rows }: { rows: GenieCostSurfaceRow[] }) {
  const total = rows.reduce((s, r) => s + r.total_usd, 0);
  return (
    <div className="rounded-lg bg-white border p-5" style={{ borderColor: "#E5E5E5" }}>
      <h3 className="mb-3 text-sm font-semibold text-gray-700">Spend by Genie Surface</h3>
      {rows.length === 0 ? (
        <p className="py-3 text-center text-xs text-gray-500">No spend by surface yet.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="min-w-full">
            <thead className="bg-gray-50">
              <tr className="border-b border-gray-200">
                <th className="px-3 py-2.5 text-left text-xs font-medium uppercase tracking-wider text-gray-500">Surface</th>
                <th className="px-3 py-2.5 text-right text-xs font-medium uppercase tracking-wider text-gray-500">Total</th>
                <th className="px-3 py-2.5 text-right text-xs font-medium uppercase tracking-wider text-gray-500">Charged</th>
                <th className="px-3 py-2.5 text-right text-xs font-medium uppercase tracking-wider text-gray-500">Free-tier</th>
                <th className="px-3 py-2.5 text-right text-xs font-medium uppercase tracking-wider text-gray-500">% of Genie</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.surface} className="border-b border-gray-100 last:border-0">
                  <td className="px-3 py-2.5 text-sm">
                    <span className="font-medium text-gray-900">{r.surface}</span>
                    {r.surface === "Genie Unknown" && (
                      <span
                        title="This spend passes the Genie inclusion filter but has no known surface. Open the Diagnostics tab to see its surface values and SKUs."
                        className="ml-2 inline-flex rounded-full border border-amber-200 bg-amber-50 px-1.5 py-0.5 text-[10px] font-medium text-amber-700"
                      >
                        unmapped
                      </span>
                    )}
                  </td>
                  <td className="px-3 py-2.5 text-right text-sm font-medium text-gray-900">{formatCurrency(r.total_usd)}</td>
                  <td className="px-3 py-2.5 text-right text-sm text-gray-700">{formatCurrency(r.charged_usd)}</td>
                  <td className="px-3 py-2.5 text-right text-sm text-gray-700">{formatCurrency(r.free_tier_usd)}</td>
                  <td className="px-3 py-2.5 text-right text-sm text-gray-500">
                    {total > 0 ? `${((r.total_usd / total) * 100).toFixed(1)}%` : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ── Timeseries chart ─────────────────────────────────────────────────────────

function GenieTimeseriesChart({ rows }: { rows: GenieCostTimeseriesRow[] }) {
  if (rows.length === 0) return null;
  return (
    <div className="rounded-lg bg-white border p-5" style={{ borderColor: "#E5E5E5" }}>
      <h3 className="mb-3 text-sm font-semibold text-gray-700">Genie Spend Over Time</h3>
      <ResponsiveContainer width="100%" height={280}>
        <AreaChart data={rows} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
          <XAxis
            dataKey="usage_date"
            tick={{ fontSize: 11 }}
            tickFormatter={(d: string) => fmtDateShort(d)}
          />
          <YAxis tick={{ fontSize: 11 }} tickFormatter={(v: number) => formatCurrency(v)} />
          <Tooltip
            contentStyle={{ fontSize: 12, borderRadius: 8 }}
            formatter={(v, name) => [formatCurrency(Number(v ?? 0)), String(name ?? "")]}
            labelFormatter={(d) => fmtDateShort(String(d ?? ""))}
          />
          <Legend wrapperStyle={{ fontSize: 11 }} />
          <Area isAnimationActive={false} type="monotone" dataKey="genie_code_usd"   name="Genie Code"   stroke="#FF3621" fill="#FF3621" fillOpacity={0.35} stackId="1" />
          <Area isAnimationActive={false} type="monotone" dataKey="genie_one_usd"    name="Genie One"    stroke="#06B6D4" fill="#06B6D4" fillOpacity={0.35} stackId="1" />
          <Area isAnimationActive={false} type="monotone" dataKey="genie_agents_usd" name="Genie Agents" stroke="#3B82F6" fill="#3B82F6" fillOpacity={0.35} stackId="1" />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

// ── Workspace table ──────────────────────────────────────────────────────────

function GenieSpacesPanel({ inventory }: { inventory?: GenieSpaceInventory }) {
  if (!inventory || inventory.available === false) {
    return (
      <div className="rounded-lg bg-white border p-5 text-sm text-gray-500" style={{ borderColor: "#E5E5E5" }}>
        Genie space list is unavailable{inventory?.error ? `: ${inventory.error}` : "."}
      </div>
    );
  }
  if (inventory.spaces.length === 0) {
    return (
      <div className="rounded-lg bg-white border p-5 text-sm text-gray-500" style={{ borderColor: "#E5E5E5" }}>
        No Genie spaces were visible to this app identity.
      </div>
    );
  }
  return (
    <div className="rounded-lg bg-white border p-5" style={{ borderColor: "#E5E5E5" }}>
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-sm font-semibold text-gray-700">Genie spaces</h3>
        {inventory.configured_conversation_count ? (
          <p className="text-xs text-gray-500">
            {inventory.configured_conversation_count} conversation{inventory.configured_conversation_count === 1 ? "" : "s"} on the app space
          </p>
        ) : null}
      </div>
      <div className="overflow-x-auto">
        <table className="min-w-full">
          <thead className="bg-gray-50">
            <tr className="border-b border-gray-200">
              <th className="px-3 py-2.5 text-left text-xs font-medium uppercase tracking-wider text-gray-500">Title</th>
              <th className="px-3 py-2.5 text-left text-xs font-medium uppercase tracking-wider text-gray-500">Space ID</th>
              <th className="px-3 py-2.5 text-left text-xs font-medium uppercase tracking-wider text-gray-500">Warehouse</th>
            </tr>
          </thead>
          <tbody>
            {inventory.spaces.map((s) => (
              <tr key={s.space_id} className="border-b border-gray-100 last:border-0">
                <td className="px-3 py-2.5 text-sm font-medium text-gray-900">
                  {s.title || "(untitled)"}
                  {s.configured && (
                    <span className="ml-2 inline-flex rounded-full border border-green-200 bg-green-50 px-1.5 py-0.5 text-[10px] font-medium text-green-700">
                      Ask Genie
                    </span>
                  )}
                </td>
                <td className="px-3 py-2.5 font-mono text-xs text-gray-600">{s.space_id}</td>
                <td className="px-3 py-2.5 text-xs text-gray-500">{s.warehouse_id || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function WorkspaceTable({ rows, workspaceNameMap }: { rows: GenieCostWorkspaceRow[]; workspaceNameMap?: Record<string, string> }) {
  const [search, setSearch] = useState("");
  // The server falls back to the bare ID when it can't resolve a name; the app-wide
  // map (the same one the workspace filter uses) usually can.
  const named = useMemo(() => rows.map((r) => {
    const resolved = r.workspace_name && r.workspace_name !== r.workspace_id ? r.workspace_name : null;
    return { ...r, workspace_name: resolved || workspaceNameMap?.[r.workspace_id] || r.workspace_id };
  }), [rows, workspaceNameMap]);
  const filtered = useMemo(() => {
    if (!search.trim()) return named;
    const q = search.toLowerCase();
    return named.filter((r) =>
      r.workspace_name.toLowerCase().includes(q) || r.workspace_id.toLowerCase().includes(q),
    );
  }, [named, search]);

  return (
    <div className="rounded-lg bg-white border p-5" style={{ borderColor: "#E5E5E5" }}>
      <div className="mb-3 flex items-center justify-between gap-3">
        <h3 className="text-sm font-semibold text-gray-700">Genie Spend by Workspace</h3>
        {rows.length > 0 && (
          <div className="relative">
            <svg className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-gray-500" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
            </svg>
            <input
              type="text"
              placeholder="Search workspaces..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="w-52 rounded-full border border-gray-200 bg-white py-1.5 pl-9 pr-4 text-xs placeholder:text-gray-500 focus:border-[#FF3621] focus:outline-none focus:ring-1 focus:ring-[#FF3621]"
            />
          </div>
        )}
      </div>
      {filtered.length === 0 ? (
        <p className="py-6 text-center text-sm text-gray-500">
          {rows.length === 0 ? "No workspace-level Genie spend in the window." : `No workspaces match "${search}".`}
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="min-w-full">
            <thead className="bg-gray-50">
              <tr className="border-b border-gray-200">
                <th className="px-3 py-2.5 text-left text-xs font-medium uppercase tracking-wider text-gray-500">Workspace</th>
                <th className="px-3 py-2.5 text-right text-xs font-medium uppercase tracking-wider text-gray-500">Total</th>
                <th className="px-3 py-2.5 text-right text-xs font-medium uppercase tracking-wider text-gray-500">Charged</th>
                <th className="px-3 py-2.5 text-right text-xs font-medium uppercase tracking-wider text-gray-500">Free-tier</th>
                <th className="px-3 py-2.5 text-right text-xs font-medium uppercase tracking-wider text-gray-500">% Free</th>
                <th className="px-3 py-2.5 text-right text-xs font-medium uppercase tracking-wider text-gray-500">Genie Code</th>
                <th className="px-3 py-2.5 text-right text-xs font-medium uppercase tracking-wider text-gray-500">Genie One</th>
                <th className="px-3 py-2.5 text-right text-xs font-medium uppercase tracking-wider text-gray-500">Genie Agents</th>
                <th className="px-3 py-2.5 text-right text-xs font-medium uppercase tracking-wider text-gray-500">% of DBX</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((r) => (
                <tr key={r.workspace_id} className="border-b border-gray-100 last:border-0 hover:bg-gray-50">
                  <td className="px-3 py-2.5 text-sm" title={r.workspace_id}>
                    <span className="font-medium text-gray-900">{r.workspace_name}</span>
                    {r.total_usd__genie_unknown > 0 && (
                      <span
                        title="This workspace has spend under a Genie SKU that doesn't match the known sub-surface patterns. See Diagnostics."
                        className="ml-2 inline-flex rounded-full border border-amber-200 bg-amber-50 px-1.5 py-0.5 text-[10px] font-medium text-amber-700"
                      >
                        unmapped
                      </span>
                    )}
                  </td>
                  <td className="px-3 py-2.5 text-right text-sm font-medium text-gray-900">{formatCurrency(r.total_usd)}</td>
                  <td className="px-3 py-2.5 text-right text-sm text-gray-700">{formatCurrency(r.charged_usd)}</td>
                  <td className="px-3 py-2.5 text-right text-sm text-gray-700">{formatCurrency(r.free_tier_usd)}</td>
                  <td className="px-3 py-2.5 text-right text-sm text-gray-500">{fmtPct(r.pct_free)}</td>
                  <td className="px-3 py-2.5 text-right text-sm text-gray-700">{formatCurrency(r.total_usd__genie_code)}</td>
                  <td className="px-3 py-2.5 text-right text-sm text-gray-700">{formatCurrency(r.total_usd__genie_one)}</td>
                  <td className="px-3 py-2.5 text-right text-sm text-gray-700">{formatCurrency(r.total_usd__genie_agents)}</td>
                  <td className="px-3 py-2.5 text-right text-sm text-gray-500">{fmtPct(r.genie_pct_of_databricks_usd)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ── Diagnostics panel ────────────────────────────────────────────────────────
// Only fetches when open. Surfaces distinct (sku_name, usage_type,
// billing_origin_product) combinations under the Genie inclusion filter so
// field engineers can validate the sub-surface + free-tier CASE mappings
// against real customer data.

function DiagnosticsPanel({ open }: { open: boolean }) {
  const { data, isLoading } = useGenieCostDiagnostics(open);
  if (!open) return null;
  return (
    <div className="rounded-lg bg-white border p-5" style={{ borderColor: "#E5E5E5" }}>
      <div className="mb-3">
        <h3 className="text-sm font-semibold text-gray-700">Diagnostics — distinct Genie-adjacent billing rows</h3>
        <p className="mt-1 text-xs text-gray-500">
          Distinct <code className="bg-gray-100 px-1 rounded text-[10px]">(sku_name, usage_type, billing_origin_product, usage_metadata.genie.surface)</code> combinations
          from the last 90 days of <code className="bg-gray-100 px-1 rounded text-[10px]">system.billing.usage</code> matching
          <code className="ml-1 bg-gray-100 px-1 rounded text-[10px]">%GENIE%</code> or
          <code className="ml-1 bg-gray-100 px-1 rounded text-[10px]">%AI_BI%</code>. Use this to update the
          sub-surface + free-tier CASE mappings in <code className="bg-gray-100 px-1 rounded text-[10px]">genie_cost.py</code>.
        </p>
      </div>
      {isLoading ? (
        <div className="flex items-center gap-3 py-6">
          <div className="h-6 w-6 animate-spin rounded-full border-4 border-gray-200" style={{ borderTopColor: "#FF3621" }} />
          <p className="text-sm text-gray-500">Loading diagnostics…</p>
        </div>
      ) : !data || !data.available ? (
        <p className="py-4 text-sm text-red-600">
          Diagnostics query failed{data?.error ? `: ${data.error}` : "."}
        </p>
      ) : data.rows.length === 0 ? (
        <p className="py-4 text-sm text-gray-500">No Genie-adjacent billing rows found in the last 90 days.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="min-w-full">
            <thead className="bg-gray-50">
              <tr className="border-b border-gray-200">
                <th className="px-3 py-2.5 text-left text-xs font-medium uppercase tracking-wider text-gray-500">sku_name</th>
                <th className="px-3 py-2.5 text-left text-xs font-medium uppercase tracking-wider text-gray-500">usage_type</th>
                <th className="px-3 py-2.5 text-left text-xs font-medium uppercase tracking-wider text-gray-500">billing_origin_product</th>
                <th className="px-3 py-2.5 text-left text-xs font-medium uppercase tracking-wider text-gray-500">genie.surface</th>
                <th className="px-3 py-2.5 text-right text-xs font-medium uppercase tracking-wider text-gray-500">Rows</th>
                <th className="px-3 py-2.5 text-right text-xs font-medium uppercase tracking-wider text-gray-500">Total USD</th>
                <th className="px-3 py-2.5 text-left text-xs font-medium uppercase tracking-wider text-gray-500">Earliest</th>
                <th className="px-3 py-2.5 text-left text-xs font-medium uppercase tracking-wider text-gray-500">Latest</th>
              </tr>
            </thead>
            <tbody>
              {data.rows.map((r, i) => (
                <tr key={`${r.sku_name}-${r.usage_type}-${r.genie_surface}-${i}`} className="border-b border-gray-100 last:border-0">
                  <td className="px-3 py-2.5 font-mono text-xs text-gray-900">{r.sku_name || <span className="italic text-gray-500">null</span>}</td>
                  <td className="px-3 py-2.5 font-mono text-xs text-gray-700">{r.usage_type || <span className="italic text-gray-500">null</span>}</td>
                  <td className="px-3 py-2.5 font-mono text-xs text-gray-700">{r.billing_origin_product || <span className="italic text-gray-500">null</span>}</td>
                  <td className="px-3 py-2.5 font-mono text-xs text-gray-700">{r.genie_surface || <span className="italic text-gray-500">null</span>}</td>
                  <td className="px-3 py-2.5 text-right text-xs text-gray-500">{r.row_count.toLocaleString()}</td>
                  <td className="px-3 py-2.5 text-right text-xs text-gray-700">{formatCurrency(r.total_usd)}</td>
                  <td className="px-3 py-2.5 text-xs text-gray-500">{fmtDateShort(r.earliest_date)}</td>
                  <td className="px-3 py-2.5 text-xs text-gray-500">{fmtDateShort(r.latest_date)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

type SubTab = "overview" | "workspaces" | "spaces" | "diagnostics";

interface GenieCostViewProps {
  data: GenieCostDashboardBundle | undefined;
  isLoading: boolean;
  startDate?: string;
  endDate?: string;
  workspaceIds?: string[];
  workspaceNameMap?: Record<string, string>;
}

export function GenieCostView({ data, isLoading, startDate, endDate, workspaceIds, workspaceNameMap }: GenieCostViewProps) {
  const [subTab, setSubTab] = useState<SubTab>("overview");
  const [infoMinimized, setInfoMinimized] = useState(false);

  if (isLoading) {
    return (
      <div className="rounded-lg bg-white p-8 border flex items-center justify-center gap-3" style={{ borderColor: "#E5E5E5" }}>
        <div className="h-10 w-10 animate-spin rounded-full border-4 border-gray-200" style={{ borderTopColor: "#FF3621" }} />
        <p className="text-sm text-gray-500">Loading Genie cost data…</p>
      </div>
    );
  }

  const summary = data?.summary;
  const workspaces = data?.workspaces ?? [];
  const timeseries = data?.timeseries ?? [];
  const surfaceRows = data?.surface_breakdown ?? [];
  const inventory = data?.inventory;
  const hasData = summary != null && summary.total_usd > 0;

  const SUB_TABS: { id: SubTab; label: string }[] = [
    { id: "overview",    label: "Overview" },
    { id: "workspaces",  label: `Workspaces (${workspaces.length})` },
    { id: "spaces",      label: `Spaces${inventory?.space_count ? ` (${inventory.space_count})` : ""}` },
    { id: "diagnostics", label: "Diagnostics" },
  ];

  return (
    <div className="space-y-4">
      <InfoPanel
        title="Genie tab methodology"
        minimized={infoMinimized}
        onToggle={setInfoMinimized}
      >
        <ul className="list-inside list-disc space-y-1">
          <li><strong>Genie Spend</strong>: DBU cost for Genie, AI/BI, and related SKUs in the selected range</li>
          <li><strong>By Surface</strong>: Split across Genie Code, Genie One, Agents, and other surfaces</li>
          <li><strong>Workspaces</strong>: Which workspaces drive Genie spend</li>
          <li><strong>Diagnostics</strong>: Raw billing SKUs that mapped to Genie so you can check coverage</li>
          <li><strong>Spaces</strong>: Genie spaces this workspace can see, via the Genie SDK. Ask Genie still uses the space created on deploy.</li>
        </ul>
      </InfoPanel>

      <PageHero
        icon={
          <svg fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z" />
          </svg>
        }
        title="Genie"
        subtitle={summary ? `${summary.start_date} → ${summary.end_date} · ${summary.workspace_count} workspace${summary.workspace_count === 1 ? "" : "s"} with Genie spend` : "Genie spend by surface and workspace"}
        dateRange={{ startDate, endDate }}
        workspaceScope={workspaceIds && workspaceIds.length > 0
          ? workspaceIds.length === 1
            ? (workspaceNameMap?.[workspaceIds[0]] || workspaceIds[0])
            : `${workspaceIds.length} workspaces`
          : undefined}
      />

      {/* Sub-tab strip */}
      <div className="flex">
        <div className="flex rounded-lg border border-gray-200 overflow-hidden text-xs font-medium">
          {SUB_TABS.map((t) => (
            <button
              key={t.id}
              onClick={() => setSubTab(t.id)}
              className={`px-3 py-1.5 transition-colors ${subTab === t.id ? "text-white" : "text-gray-600 hover:bg-gray-50"}`}
              style={subTab === t.id ? { backgroundColor: C.navy } : {}}
            >
              {t.label}
            </button>
          ))}
        </div>
      </div>

      {subTab === "overview" && (
        <div className="space-y-4">
          {!hasData ? (
            <NoGenieBanner />
          ) : (
            <>
              {/* KPI cards */}
              <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
                <KpiCard
                  label="Total Genie Spend"
                  value={formatCurrency(summary.total_usd)}
                  sub={`Charged + free-tier over ${summary.start_date} → ${summary.end_date}`}
                />
                <KpiCard
                  label="Charged USD"
                  value={formatCurrency(summary.charged_usd)}
                  sub={summary.other_usage_type_usd > 0 ? `${formatCurrency(summary.other_usage_type_usd)} in unknown usage_type` : undefined}
                />
                <KpiCard
                  label="Free-Tier USD"
                  value={formatCurrency(summary.free_tier_usd)}
                  sub={fmtPct(summary.pct_free) + " of Genie spend"}
                />
                <KpiCard
                  label="Genie % of Databricks Spend"
                  value={fmtPct(summary.genie_pct_of_databricks_usd)}
                  sub={`${formatCurrency(summary.charged_usd)} / ${formatCurrency(summary.databricks_total_usd)}`}
                />
              </div>

              {/* Sub-surface KPIs */}
              <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
                <KpiCard
                  label="Genie Code"
                  value={formatCurrency(summary.total_usd__genie_code)}
                  sub={`${formatCurrency(summary.charged_usd__genie_code)} charged · ${formatCurrency(summary.free_tier_usd__genie_code)} free`}
                />
                <KpiCard
                  label="Genie One"
                  value={formatCurrency(summary.total_usd__genie_one)}
                  sub={`${formatCurrency(summary.charged_usd__genie_one)} charged · ${formatCurrency(summary.free_tier_usd__genie_one)} free`}
                />
                <KpiCard
                  label="Genie Agents"
                  value={formatCurrency(summary.total_usd__genie_agents)}
                  sub={`${formatCurrency(summary.charged_usd__genie_agents)} charged · ${formatCurrency(summary.free_tier_usd__genie_agents)} free`}
                />
              </div>

              <GenieTimeseriesChart rows={timeseries} />
              <SurfaceBreakdownCard rows={surfaceRows} />

              {summary.total_usd__genie_unknown > 0 && (
                <div className="rounded-lg border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">
                  <strong>Unmapped Genie spend detected.</strong>{" "}
                  {formatCurrency(summary.total_usd__genie_unknown)} of spend matches the Genie inclusion filter
                  but has no known surface (Genie Code, Genie One or Genie Agents).
                  Open the <button className="underline hover:no-underline" onClick={() => setSubTab("diagnostics")}>Diagnostics</button>{" "}
                  tab to see its <code className="bg-white px-1 rounded text-xs">genie.surface</code> values and SKUs.
                </div>
              )}
              {summary.other_usage_type_usd > 0 && (
                <div className="rounded-lg border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">
                  <strong>Unknown <code className="bg-white px-1 rounded text-xs">usage_type</code> detected.</strong>{" "}
                  {formatCurrency(summary.other_usage_type_usd)} of spend uses a{" "}
                  <code className="bg-white px-1 rounded text-xs">usage_type</code> value that isn't
                  <code className="ml-1 bg-white px-1 rounded text-xs">BILLABLE</code>,
                  <code className="ml-1 bg-white px-1 rounded text-xs">NON_BILLABLE</code>, or
                  <code className="ml-1 bg-white px-1 rounded text-xs">RELATED</code>. These are excluded from the charged/free-tier
                  buckets above. Open <button className="underline hover:no-underline" onClick={() => setSubTab("diagnostics")}>Diagnostics</button>{" "}
                  to see the actual values.
                </div>
              )}
            </>
          )}
        </div>
      )}

      {subTab === "workspaces" && <WorkspaceTable rows={workspaces} workspaceNameMap={workspaceNameMap} />}

      {subTab === "spaces" && <GenieSpacesPanel inventory={inventory} />}

      {subTab === "diagnostics" && <DiagnosticsPanel open={subTab === "diagnostics"} />}
    </div>
  );
}
