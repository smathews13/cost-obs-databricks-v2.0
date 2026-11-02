import { useEffect, useMemo, useState, useRef } from "react";
import { formatIdentity, useSpNameMap } from "@/utils/identity";
import { createPortal } from "react-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useFeatureAvailability } from "@/hooks/useFeatureAvailability";
import { fetchJson } from "@/hooks/useBillingData";
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  Cell,
  Legend,
  BarChart,
  Bar,
  LabelList,
} from "recharts";
import { format, parseISO } from "date-fns";
import type { GranularBreakdownResponse, DBSQLDashboardBundle, QueryCostByWarehouse } from "@/types/billing";
import { KPITrendModal } from "./KPITrendModal";
import { VirtualizedList } from "./VirtualizedList";
import { C } from "@/theme";
import { PageHero, InfoPanel, SourceCapabilityNotice } from "@/components/brand";

interface SQLWarehousing360Props {
  sqlBreakdownData: GranularBreakdownResponse | undefined;
  queryData: DBSQLDashboardBundle | undefined;
  isLoading: boolean;
  topQueriesData?: import("@/types/billing").TopQueriesResponse;
  topQueriesLoading?: boolean;
  host?: string | null;
  startDate?: string;
  endDate?: string;
  workspaceIds?: string[];
  workspaceNameMap?: Record<string, string>;
}

// Colors for query source types
const SOURCE_TYPE_COLORS: Record<string, string> = {
  "GENIE SPACE": C.s5,
  "AI/BI DASHBOARD": C.s2,
  "LEGACY DASHBOARD": C.s5,
  "SQL QUERY": C.s3,
  "NOTEBOOK": C.s4,
  "JOB": C.lava,
  "ALERT": C.s1,
  Unknown: C.slate,
};

const STALE_MS = 25 * 24 * 60 * 60 * 1000;

type WarehouseWithName = QueryCostByWarehouse & { workspace_name?: string | null };
type DBSQLBundleWithTypeTimeseries = DBSQLDashboardBundle & {
  warehouse_type_timeseries?: {
    timeseries?: Array<Record<string, string | number>>;
    warehouse_types?: string[];
  };
};

const COLORS = [C.s2, C.s5, C.s3, C.s3, C.s4, C.s5, C.s1, C.lava, C.slate];

const COST_TOOLTIP_TEXT = "Costs are estimates: the warehouse's billed DBU-hours are divided across all queries in the period, weighted by task duration. A fast query running during a low-activity window can inherit a large share of the hour's cost.";

// Stable module-level formatters for the Top Users bar chart — passing inline
// arrows to Recharts churns identity every render and re-triggers animations,
// which is what caused the labels to flicker.
const USER_BAR_TICK_FMT = (v: number | string) => formatCurrency(typeof v === "number" ? v : Number(v));
const USER_BAR_TOOLTIP_FMT = (value: unknown) => formatCurrency(value as number);
const USER_BAR_TOOLTIP_LABEL_FMT = (label: unknown) => `User: ${label}`;
const USER_BAR_LABEL_FMT = (v: unknown) => `$${Math.round(v as number).toLocaleString()}`;
const USER_BAR_LABEL_STYLE = { fontSize: 11, fill: C.slate };

function InfoTooltip({ text, stopClick }: { text: string; stopClick?: boolean }) {
  const [pos, setPos] = useState<{ x: number; y: number } | null>(null);
  return (
    <span
      className="ml-1 inline-flex cursor-help"
      onMouseEnter={e => setPos({ x: e.clientX, y: e.clientY })}
      onMouseMove={e => setPos({ x: e.clientX, y: e.clientY })}
      onMouseLeave={() => setPos(null)}
      onClick={stopClick ? e => e.stopPropagation() : undefined}
    >
      <span className="flex h-4 w-4 items-center justify-center rounded-full bg-gray-200 text-[10px] font-semibold normal-case text-gray-500">i</span>
      {pos && createPortal(
        <div
          className="pointer-events-none fixed z-[9999] w-64 rounded-lg bg-gray-900 px-3 py-2 text-xs font-normal normal-case leading-relaxed text-white shadow-lg"
          style={{
            top: pos.y - 12,
            transform: 'translateY(-100%)',
            left: Math.min(pos.x + 14, window.innerWidth - 272),
          }}
        >
          {text}
        </div>,
        document.body
      )}
    </span>
  );
}

const formatCurrency = (value: number) =>
  new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 0,
    maximumFractionDigits: 2,
  }).format(value);

const formatNumber = (value: number) =>
  new Intl.NumberFormat("en-US", {
    minimumFractionDigits: 0,
    maximumFractionDigits: 0,
  }).format(value);

const formatDuration = (seconds: number) => {
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  if (seconds < 3600) return `${(seconds / 60).toFixed(1)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
};

const formatDate = (dateStr: string) => {
  try {
    return format(parseISO(dateStr), "MMM d");
  } catch {
    return dateStr;
  }
};

type SortField = "cost" | "dbus" | "duration_seconds" | "executed_by";
type SortDirection = "asc" | "desc";



interface SourceQuery {
  statement_id: string;
  query_source_type: string;
  executed_by: string;
  statement_preview: string;
  duration_seconds: number;
  cost: number;
  dbus: number;
  query_profile_url: string | null;
  source_url: string | null;
}

export function SQLWarehousing360({ queryData, isLoading, topQueriesData, topQueriesLoading, host, startDate, endDate, workspaceIds, workspaceNameMap }: SQLWarehousing360Props) {
  const spNameMap = useSpNameMap();
  const [sortField, setSortField] = useState<SortField>("cost");
  const [sortDirection, setSortDirection] = useState<SortDirection>("desc");
  const [queriesPage, setQueriesPage] = useState(1);
  const [showHistoricalQueries, setShowHistoricalQueries] = useState(false);
  const { tableGranted } = useFeatureAvailability();
  const queryHistoryGranted = tableGranted("system.query.history");
  const [selectedKPI, setSelectedKPI] = useState<{kpi: string; label: string; variant?: "billing" | "platform"} | null>(null);
  const [selectedSource, setSelectedSource] = useState<string | null>(null);
  const [sourceQueriesCache, setSourceQueriesCache] = useState<Record<string, SourceQuery[]>>({});
  const [sourceQueriesLoading, setSourceQueriesLoading] = useState(false);
  const [querySourceFilters, setQuerySourceFilters] = useState<string[]>([]);
  const [querySourceDropdownOpen, setQuerySourceDropdownOpen] = useState(false);
  const [querySearch, setQuerySearch] = useState("");
  const [warehouseSizeWsFilter, setWarehouseSizeWsFilter] = useState<string[]>([]);
  const [warehouseSizeWsInitialized, setWarehouseSizeWsInitialized] = useState(false);
  const [whSizeDropdownOpen, setWhSizeDropdownOpen] = useState(false);
  const [whSizeWsSearch, setWhSizeWsSearch] = useState("");
  const whSizeDropdownRef = useRef<HTMLDivElement>(null);

  // Close warehouse size dropdown on outside click; reset search on close so reopening starts fresh
  useEffect(() => {
    if (!whSizeDropdownOpen) return;
    const handleClick = (e: MouseEvent) => {
      if (whSizeDropdownRef.current && !whSizeDropdownRef.current.contains(e.target as Node)) {
        setWhSizeWsSearch("");
        setWhSizeDropdownOpen(false);
      }
    };
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, [whSizeDropdownOpen]);

  // Derive current source queries from cache
  const sourceQueries = selectedSource ? (sourceQueriesCache[selectedSource] || []) : [];

  // Prefetch top queries for ALL source types when data loads
  const prefetchSourceKey = (queryData?.by_source?.sources?.map((s) => s.query_source_type) || []).join(",");
  useEffect(() => {
    if (!startDate || !endDate || !prefetchSourceKey) return;
    const prefetchSourceTypes = prefetchSourceKey.split(",");
    let cancelled = false;
    const fetchAll = async () => {
      const results: Record<string, SourceQuery[]> = {};
      await Promise.all(
        prefetchSourceTypes.map(async (sourceType) => {
          try {
            const params = new URLSearchParams({ source_type: sourceType, limit: "5" });
            params.set("start_date", startDate);
            params.set("end_date", endDate);
            const res = await fetch(`/api/dbsql/top-queries-by-source?${params}`);
            const result = await res.json();
            results[sourceType] = result.queries || [];
          } catch {
            results[sourceType] = [];
          }
        })
      );
      if (!cancelled) setSourceQueriesCache(results);
    };
    fetchAll();
    return () => { cancelled = true; };
  }, [startDate, endDate, prefetchSourceKey]);

  const handleSourceClick = (sourceType: string) => {
    setSelectedSource(sourceType);
    // If not in cache yet, fetch on demand
    if (!sourceQueriesCache[sourceType]) {
      setSourceQueriesLoading(true);
      const params = new URLSearchParams({ source_type: sourceType, limit: "5" });
      if (startDate) params.set("start_date", startDate);
      if (endDate) params.set("end_date", endDate);
      fetch(`/api/dbsql/top-queries-by-source?${params}`)
        .then((res) => res.json())
        .then((result) => {
          setSourceQueriesCache((prev) => ({ ...prev, [sourceType]: result.queries || [] }));
        })
        .catch(() => {
          setSourceQueriesCache((prev) => ({ ...prev, [sourceType]: [] }));
        })
        .finally(() => setSourceQueriesLoading(false));
    }
  };

  // Pre-warm trend queries so modals open instantly
  const queryClient = useQueryClient();
  useEffect(() => {
    if (!startDate || !endDate) return;
    for (const kpi of ["sql_queries", "sql_users", "avg_query_duration"]) {
      queryClient.prefetchQuery({
        queryKey: ["platform-kpi-trend", kpi, startDate, endDate, "daily"],
        queryFn: async () => {
          const params = new URLSearchParams({ kpi, start_date: startDate, end_date: endDate, granularity: "daily" });
          const res = await fetch(`/api/billing/platform-kpi-trend?${params}`);
          if (!res.ok) throw new Error("prefetch failed");
          return res.json();
        },
        staleTime: 5 * 60 * 1000,
      });
    }
  }, [startDate, endDate, queryClient]);

  // Info box minimize state with localStorage persistence
  const MINIMIZE_KEY = "cost-obs-minimize-sql-info";
  const [infoMinimized, setInfoMinimized] = useState(() => {
    if (typeof window !== "undefined") {
      return localStorage.getItem(MINIMIZE_KEY) === "true";
    }
    return false;
  });

  const handleMinimizeToggle = (checked: boolean) => {
    setInfoMinimized(checked);
    if (checked) {
      localStorage.setItem(MINIMIZE_KEY, "true");
    } else {
      localStorage.removeItem(MINIMIZE_KEY);
    }
  };


  const [selectedUser, setSelectedUser] = useState<{ raw: string; display: string } | null>(null);

  const { data: userQueriesData, isLoading: userQueriesLoading, dataUpdatedAt: userQueriesUpdatedAt } = useQuery<{
    available: boolean;
    queries: Array<{
      statement_id: string | null;
      query_source_type: string;
      executed_by: string;
      warehouse_id: string | null;
      workspace_id: string | null;
      statement_preview: string;
      duration_seconds: number;
      cost: number;
      dbus: number;
      query_profile_url: string | null;
      source_url: string | null;
      start_time: string | null;
    }>;
    total_spend: number;
    query_count: number;
  }>({
    queryKey: ["dbsql", "queries-by-user", selectedUser?.raw, startDate, endDate, workspaceIds],
    queryFn: () => {
      const params = new URLSearchParams({ user: selectedUser!.raw });
      if (startDate) params.set("start_date", startDate);
      if (endDate) params.set("end_date", endDate);
      if (workspaceIds?.length) params.set("workspace_ids", workspaceIds.join(","));
      return fetch(`/api/dbsql/queries-by-user?${params}`).then(r => r.json());
    },
    enabled: !!selectedUser,
    staleTime: 5 * 60 * 1000,
  });

  const freshUserQueries = useMemo(() => {
    if (!userQueriesData?.queries) return [];
    return userQueriesData.queries
      .filter(q => !q.start_time || userQueriesUpdatedAt - new Date(q.start_time).getTime() <= STALE_MS)
      .sort((a, b) => b.cost - a.cost);
  }, [userQueriesData, userQueriesUpdatedAt]);
  const staleUserQueries = useMemo(() => {
    if (!userQueriesData?.queries) return [];
    return userQueriesData.queries
      .filter(q => q.start_time && userQueriesUpdatedAt - new Date(q.start_time).getTime() > STALE_MS)
      .sort((a, b) => b.cost - a.cost);
  }, [userQueriesData, userQueriesUpdatedAt]);

  const byUserRows = queryData?.by_user?.users;
  const userBarData = useMemo(() => {
    if (!byUserRows) return [];
    const byUser: Record<string, { user: string; rawUser: string; total_spend: number; query_count: number }> = {};
    for (const u of byUserRows) {
      if (!byUser[u.executed_by]) {
        byUser[u.executed_by] = { user: u.executed_by, rawUser: u.executed_by, total_spend: 0, query_count: 0 };
      }
      byUser[u.executed_by].total_spend += u.total_spend;
      byUser[u.executed_by].query_count += u.query_count;
    }
    return Object.values(byUser)
      .sort((a, b) => b.total_spend - a.total_spend)
      .slice(0, 10)
      .map(u => ({ ...u, user: formatIdentity(u.user, spNameMap) }));
  }, [byUserRows, spNameMap]);

  const timeseriesPoints = queryData?.timeseries?.timeseries;
  const timeseriesData = useMemo(() => {
    if (!timeseriesPoints) return [];
    return timeseriesPoints.map((point) => ({
      ...point,
      date: formatDate(point.date as string),
    }));
  }, [timeseriesPoints]);

  const querySourceTypes = useMemo(() => {
    if (!topQueriesData?.queries) return [];
    const types = new Set(topQueriesData.queries.map((q) => q.query_source_type));
    return Array.from(types).sort();
  }, [topQueriesData]);

  const [querySourceTypesInitialized, setQuerySourceTypesInitialized] = useState(false);
  if (!querySourceTypesInitialized && querySourceTypes.length > 0) {
    setQuerySourceTypesInitialized(true);
    setQuerySourceFilters([...querySourceTypes]);
  }

  if (!warehouseSizeWsInitialized) {
    const wsIds = Array.from(new Set((queryData?.by_warehouse?.warehouses || []).map((w) => w.workspace_id).filter(Boolean))) as string[];
    if (wsIds.length > 0) {
      setWarehouseSizeWsInitialized(true);
      setWarehouseSizeWsFilter(wsIds);
    }
  }

  const isHistoricalQuery = (q: { executed_by: string; statement_preview: string }) =>
    !q.executed_by || q.executed_by === "Unknown" || q.statement_preview === "N/A";
  const allQueries = topQueriesData?.queries || [];
  const historicalQueryCount = allQueries.filter(isHistoricalQuery).length;

  const filteredQueries = useMemo(() => {
    if (!topQueriesData?.queries) return [];
    let queries = [...topQueriesData.queries];
    if (!showHistoricalQueries) {
      queries = queries.filter((q) => !isHistoricalQuery(q));
    }
    if (querySourceFilters.length > 0) {
      queries = queries.filter((q) => querySourceFilters.includes(q.query_source_type));
    }
    queries.sort((a, b) => {
      let aVal: number | string = 0;
      let bVal: number | string = 0;
      switch (sortField) {
        case "cost":
          aVal = a.cost;
          bVal = b.cost;
          break;
        case "dbus":
          aVal = a.dbus;
          bVal = b.dbus;
          break;
        case "duration_seconds":
          aVal = a.duration_seconds;
          bVal = b.duration_seconds;
          break;
        case "executed_by":
          aVal = a.executed_by.toLowerCase();
          bVal = b.executed_by.toLowerCase();
          break;
      }
      if (typeof aVal === "string" && typeof bVal === "string") {
        return sortDirection === "asc" ? aVal.localeCompare(bVal) : bVal.localeCompare(aVal);
      }
      return sortDirection === "asc" ? (aVal as number) - (bVal as number) : (bVal as number) - (aVal as number);
    });
    return queries;
  }, [topQueriesData, sortField, sortDirection, showHistoricalQueries, querySourceFilters]);

  const searchedQueries = querySearch
    ? filteredQueries.filter(q =>
        (q.executed_by || "").toLowerCase().includes(querySearch.toLowerCase()) ||
        (q.query_source_type || "").toLowerCase().includes(querySearch.toLowerCase()) ||
        (q.statement_preview || "").toLowerCase().includes(querySearch.toLowerCase())
      )
    : filteredQueries;
  const queryTotalPages = Math.ceil(searchedQueries.length / 10);
  const queryStart = (queriesPage - 1) * 10;
  const sortedQueries = searchedQueries.slice(queryStart, queryStart + 10);

  const handleSort = (field: SortField) => {
    if (sortField === field) {
      setSortDirection(sortDirection === "asc" ? "desc" : "asc");
    } else {
      setSortField(field);
      setSortDirection("desc");
    }
    setQueriesPage(1);
  };

  if (isLoading) {
    return (
      <div className="flex h-64 flex-col items-center justify-center gap-3">
        <div className="h-12 w-12 animate-spin rounded-full border-4 border-gray-200" style={{ borderTopColor: C.lava }} />
        <p className="text-sm text-gray-500">Loading query analytics...</p>
      </div>
    );
  }

  const summary = queryData?.summary;
  const sourceTypes = queryData?.timeseries?.source_types || [];
  // Only treat query data as unavailable once a response has actually arrived.
  // While queryData is undefined the tab is still loading — don't flash the
  // "not available" banner prematurely.
  const hasQueryData = queryData != null ? queryData.available : undefined;

  // Detection logic (queryData.region_scope) is retained server-side and above; the
  // banner itself is suppressed for now. Flip to true to re-enable the UI callout.
  const SHOW_REGION_SCOPE_BANNER = false;

  return (
    <div className="space-y-6">
      <InfoPanel
        title="SQL tab methodology"
        minimized={infoMinimized}
        onToggle={handleMinimizeToggle}
      >
        <ul className="grid grid-cols-1 gap-1 sm:grid-cols-2">
          <li><strong style={{ color: C.ink }}>Spend by Source:</strong> Click any source (Genie, AI/BI, SQL Editor, Jobs, Notebooks) to drill into the top queries from that source</li>
          <li><strong style={{ color: C.ink }}>Warehouse Spend:</strong> Breakdown by warehouse type and utilization patterns</li>
          <li><strong style={{ color: C.ink }}>SKU Breakdown:</strong> Spend split across Serverless, Pro, Classic, and other SQL SKUs</li>
          <li><strong style={{ color: C.ink }}>Top Users by Query Spend:</strong> Human users and service principals ranked by SQL query cost</li>
        </ul>
      </InfoPanel>
      {/* Region-scope banner — system.compute / system.query are region-scoped, so
          SQL/warehouse detail only covers workspaces in this metastore's region even
          though account-wide billing (spend totals) spans all regions. */}
      {SHOW_REGION_SCOPE_BANNER && queryData?.region_scope?.limited && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 p-4">
          <div className="flex items-start gap-3">
            <svg className="mt-0.5 h-5 w-5 shrink-0 text-amber-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
            <div>
              <p className="text-sm font-medium text-amber-800">
                SQL &amp; warehouse detail is limited to this region
              </p>
              <p className="mt-1 text-sm text-amber-700">
                {queryData.region_scope.missing_workspace_count} workspace{queryData.region_scope.missing_workspace_count === 1 ? "" : "s"} with SQL spend {queryData.region_scope.missing_workspace_count === 1 ? "is" : "are"} outside this metastore&apos;s cloud region.
                Databricks scopes <code className="rounded bg-amber-100 px-1">system.compute</code> and <code className="rounded bg-amber-100 px-1">system.query</code> per region,
                so per-warehouse and per-query detail below covers only the {queryData.region_scope.in_region_workspace_count} in-region workspace{queryData.region_scope.in_region_workspace_count === 1 ? "" : "s"}.
                Account-wide spend totals are unaffected. To see full detail for the other regions, deploy Cost Observability in a workspace in each region.
              </p>
            </div>
          </div>
        </div>
      )}

      {/* Query-level Cost Attribution */}
      {hasQueryData === false ? (
        <SourceCapabilityNotice
          title={queryData?.reason === "shared_scope_unsupported"
            ? "SQL detail is not included in this source"
            : "Query-level cost attribution is not available"}
          description={queryData?.reason === "shared_scope_unsupported"
            ? (queryData.reason_detail || "The selected source publishes account totals but not SQL query and warehouse attribution.")
            : "This tab requires system.query.history access for the app's service principal. Copy the GRANT SQL from Settings → Permissions and run it as a metastore admin, then rebuild tables from Settings → Config."}
          requiredAggregates={["daily_query_stats", "sql_tool_attribution", "dbsql_cost_per_query"]}
        />
      ) : (
        <>
          {/* Header */}
          <PageHero
            icon={
              <svg fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 3v2m6-2v2M9 19v2m6-2v2M5 9H3m2 6H3m18-6h-2m2 6h-2M7 19h10a2 2 0 002-2V7a2 2 0 00-2-2H7a2 2 0 00-2 2v10a2 2 0 002 2zM9 9h6v6H9V9z" />
              </svg>
            }
            title="SQL"
            subtitle="SQL-level cost attribution and warehouse analytics"
            dateRange={{ startDate, endDate }}
            workspaceScope={workspaceIds && workspaceIds.length > 0
              ? workspaceIds.length === 1
                ? (workspaceNameMap?.[workspaceIds[0]] || workspaceIds[0])
                : `${workspaceIds.length} workspaces`
              : undefined}
          />

          {/* Stale data warning — shown when MV exists but has no data in the selected range */}
          {summary?.total_queries === 0 && (summary?.data_range?.total_rows ?? 0) > 0 && (() => {
            const earliest = summary.data_range?.earliest_date;
            const latest = summary.data_range?.latest_date;
            const rangeOutside = earliest && latest && startDate && endDate
              && (endDate < earliest || startDate > latest);
            return (
            <div className="rounded-lg border border-amber-200 bg-amber-50 p-4">
              <div className="flex items-start gap-3">
                <svg className="mt-0.5 h-5 w-5 shrink-0 text-amber-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" />
                </svg>
                <div>
                  <p className="text-sm font-medium text-amber-800">No data in selected range</p>
                  <p className="mt-1 text-sm text-amber-700">
                    {rangeOutside ? (
                      <>The Query data runs from <strong>{earliest}</strong> to <strong>{latest}</strong> — adjust the date range to see data.</>
                    ) : (
                      <>No SQL warehouse queries found for the selected {workspaceIds?.length ? `${workspaceIds.length} workspace${workspaceIds.length > 1 ? 's' : ''}` : 'filters'} in this date range. Try selecting all workspaces or a different date range.</>
                    )}
                  </p>
                </div>
              </div>
            </div>
            );
          })() }

          {/* Summary Cards */}
          {(() => {
            // Show unavailable state instead of fake 0 when query.history is explicitly denied
            // or when summary data is null (no data returned despite query succeeding).
            const summaryUnavailable = queryHistoryGranted === false
              ? "query.history grant required — run SP grants to fix"
              : (summary == null && hasQueryData ? "No summary data returned" : undefined);

            if (summaryUnavailable) {
              return (
                <div className="rounded-lg border border-gray-200 bg-gray-50 p-4 text-sm text-gray-500">
                  <span className="font-medium text-gray-700">Query summary unavailable</span>
                  <span className="ml-2">— {summaryUnavailable}</span>
                </div>
              );
            }

            return (
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <div className="rounded-lg bg-white p-6 border shadow-sm cursor-pointer hover:shadow-md hover:scale-[1.01] transition-all" onClick={() => startDate && endDate && setSelectedKPI({kpi: "sql_spend", label: "Daily SQL Spend Trend", variant: "billing"})}>
              <div className="flex items-center">
                <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-lg bg-orange-100">
                  <svg className="h-6 w-6 text-lava" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8c-1.657 0-3 .895-3 2s1.343 2 3 2 3 .895 3 2-1.343 2-3 2m0-8c1.11 0 2.08.402 2.599 1M12 8V7m0 1v8m0 0v1m0-1c-1.11 0-2.08-.402-2.599-1M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                  </svg>
                </div>
                <div className="ml-4">
                  <div className="text-sm font-medium text-gray-500">Total Query Spend</div>
                  <div className="text-2xl font-semibold text-gray-900">
                    {summary != null ? formatCurrency(summary.total_spend ?? 0) : "—"}
                  </div>
                  <div className="mt-1 text-xs text-gray-500">
                    {summary != null ? `${formatNumber(summary.total_dbus ?? 0)} DBUs · over ${startDate && endDate ? Math.round((new Date(endDate).getTime() - new Date(startDate).getTime()) / 86400000) + 1 : "?"} days` : "—"}
                  </div>
                  <p className="mt-1 text-xs text-lava">See trend →</p>
                </div>
              </div>
            </div>
            <div className="rounded-lg bg-white p-6 border shadow-sm cursor-pointer hover:shadow-md hover:scale-[1.01] transition-all" onClick={() => startDate && endDate && setSelectedKPI({kpi: "sql_queries", label: "Daily SQL Queries", variant: "platform"})}>
              <div className="flex items-center">
                <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-lg bg-orange-100">
                  <svg className="h-6 w-6 text-lava" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-3 7h3m-3 4h3m-6-4h.01M9 16h.01" />
                  </svg>
                </div>
                <div className="ml-4">
                  <div className="text-sm font-medium text-gray-500">Total Queries</div>
                  <div className="text-2xl font-semibold text-gray-900">
                    {summary != null ? formatNumber(summary.total_queries ?? 0) : "—"}
                  </div>
                  <div className="mt-1 text-xs text-gray-500">
                    {summary != null ? (() => {
                      const days = startDate && endDate ? Math.round((new Date(endDate).getTime() - new Date(startDate).getTime()) / 86400000) + 1 : null;
                      const avgPerDay = days ? Math.round((summary.total_queries ?? 0) / days) : null;
                      return `${avgPerDay != null ? formatNumber(avgPerDay) + " avg/day · " : ""}${formatCurrency(summary.avg_cost_per_query ?? 0)}/query`;
                    })() : "—"}
                  </div>
                  <p className="mt-1 text-xs font-medium" style={{ color: C.lava }}>See trend →</p>
                </div>
              </div>
            </div>
            <div className="rounded-lg bg-white p-6 border shadow-sm cursor-pointer hover:shadow-md hover:scale-[1.01] transition-all" onClick={() => startDate && endDate && setSelectedKPI({kpi: "sql_users", label: "Daily SQL Users", variant: "platform"})}>
              <div className="flex items-center">
                <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-lg bg-orange-100">
                  <svg className="h-6 w-6 text-lava" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z" />
                  </svg>
                </div>
                <div className="ml-4">
                  <div className="text-sm font-medium text-gray-500">Unique SQL Users</div>
                  <div className="text-2xl font-semibold text-gray-900">
                    {summary != null ? formatNumber(summary.unique_users ?? 0) : "—"}
                  </div>
                  <div className="mt-1 text-xs text-gray-500">
                    {summary != null ? `across ${formatNumber(summary.unique_warehouses ?? 0)} SQL warehouses` : "—"}
                  </div>
                  <p className="mt-1 text-xs font-medium" style={{ color: C.lava }}>See trend →</p>
                </div>
              </div>
            </div>
            <div className="rounded-lg bg-white p-6 border shadow-sm cursor-pointer hover:shadow-md hover:scale-[1.01] transition-all" onClick={() => startDate && endDate && setSelectedKPI({kpi: "avg_query_duration", label: "Query Duration"})}>
              <div className="flex items-center">
                <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-lg bg-orange-100">
                  <svg className="h-6 w-6 text-lava" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
                  </svg>
                </div>
                <div className="ml-4">
                  <div className="text-sm font-medium text-gray-500">Query Duration</div>
                  <div className="text-2xl font-semibold text-gray-900">
                    {summary != null ? formatDuration(summary.avg_duration_seconds ?? 0) : "—"}
                  </div>
                  <div className="mt-1 text-xs text-gray-500">
                    average per query
                  </div>
                  <p className="mt-1 text-xs font-medium" style={{ color: C.lava }}>See trend →</p>
                </div>
              </div>
            </div>
          </div>
            );
          })()}

          {selectedKPI && startDate && endDate && (
            <KPITrendModal
              variant={selectedKPI.variant ?? "platform"}
              kpi={selectedKPI.kpi}
              kpiLabel={selectedKPI.label}
              isOpen={!!selectedKPI}
              onClose={() => setSelectedKPI(null)}
              startDate={startDate}
              endDate={endDate}
              workspaceIds={workspaceIds}
            />
          )}

          {/* Daily Query Costs + Top Users — side by side */}
          <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
            {/* Timeseries Chart */}
            <div className="rounded-lg bg-white p-6 border " style={{ borderColor: C.hairline }}>
              <h3 className="mb-4 text-lg font-semibold text-gray-900">Query Spend by Source</h3>
              {timeseriesData.length > 0 ? (
                <ResponsiveContainer width="100%" height={300}>
                  <AreaChart data={timeseriesData}>
                    <XAxis dataKey="date" stroke={C.muted} fontSize={12} tickMargin={8} />
                    <YAxis tickFormatter={(v) => formatCurrency(v)} stroke={C.muted} fontSize={12} tickMargin={8} />
                    <Tooltip
                      formatter={(value) => formatCurrency(value as number)}
                      labelFormatter={(label) => `Date: ${label}`}
                    />
                    <Legend wrapperStyle={{ fontSize: 12 }} />
                    {sourceTypes.map((type, idx) => (
                      <Area
                        key={type}
                        type="monotone"
                        dataKey={type}
                        stackId="1"
                        stroke={SOURCE_TYPE_COLORS[type] || COLORS[idx % COLORS.length]}
                        fill={SOURCE_TYPE_COLORS[type] || COLORS[idx % COLORS.length]}
                        fillOpacity={0.6}
                      />
                    ))}
                  </AreaChart>
                </ResponsiveContainer>
              ) : (
                <div className="flex h-[300px] items-center justify-center text-gray-500">
                  No timeseries data available
                </div>
              )}
            </div>

            {/* Top Users Bar Chart */}
            <div className="rounded-lg bg-white p-6 border " style={{ borderColor: C.hairline }}>
              <div className="mb-4 flex items-center justify-between">
                <h3 className="text-lg font-semibold text-gray-900">Top Users by Query Spend</h3>
                <span className="text-xs font-medium" style={{ color: C.lava }}>Click a bar to drill down ↓</span>
              </div>
              {userBarData.length > 0 ? (
                <ResponsiveContainer width="100%" height={300}>
                  <BarChart
                    data={userBarData}
                    layout="vertical"
                    margin={{ left: 0, right: 70 }}
                    style={{ cursor: "pointer" }}
                  >
                    <XAxis type="number" tickFormatter={USER_BAR_TICK_FMT} stroke={C.muted} fontSize={12} tickMargin={8} />
                    <YAxis
                      type="category"
                      dataKey="user"
                      width={160}
                      stroke={C.muted}
                      fontSize={12}
                      tickMargin={8}
                    />
                    <Tooltip
                      formatter={USER_BAR_TOOLTIP_FMT}
                      labelFormatter={USER_BAR_TOOLTIP_LABEL_FMT}
                    />
                    <Bar
                      dataKey="total_spend"
                      radius={[0, 4, 4, 0]}
                      isAnimationActive={false}
                      onClick={(_entry, idx) => {
                        const row = userBarData[idx];
                        if (row) setSelectedUser({ raw: row.rawUser, display: row.user });
                      }}
                    >
                      {userBarData.map((_entry, idx) => (
                        <Cell key={idx} fill={COLORS[idx % COLORS.length]} />
                      ))}
                      <LabelList dataKey="total_spend" position="right" formatter={USER_BAR_LABEL_FMT} style={USER_BAR_LABEL_STYLE} />
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              ) : (
                <div className="flex h-[300px] items-center justify-center text-gray-500">
                  No user data available
                </div>
              )}
            </div>
          </div>

          {/* Warehouse Spend by Type + Warehouse Count by Size — side by side */}
          <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
          <div className="rounded-lg bg-white p-6 border " style={{ borderColor: C.hairline }}>
              <h3 className="mb-4 text-lg font-semibold text-gray-900">Warehouse Spend by Type</h3>
              {(() => {
                const whTypeTs = (queryData as DBSQLBundleWithTypeTimeseries | undefined)?.warehouse_type_timeseries;
                const tsData = whTypeTs?.timeseries || [];
                const whTypes: string[] = whTypeTs?.warehouse_types || [];
                if (tsData.length === 0) {
                  return (
                    <div className="flex h-[300px] items-center justify-center text-gray-500">
                      No warehouse type timeseries data available
                    </div>
                  );
                }
                const typeColors: Record<string, string> = { SERVERLESS: C.s2, PRO: C.s5, CLASSIC: C.s4, Unknown: C.muted };
                return (
                  <ResponsiveContainer width="100%" height={300}>
                    <AreaChart data={tsData} margin={{ top: 5, right: 10, left: 10, bottom: 5 }}>
                      <XAxis
                        dataKey="date"
                        tickFormatter={(d) => {
                          try { return format(parseISO(d), "MMM d"); } catch { return d; }
                        }}
                        stroke={C.muted} fontSize={11}
                      />
                      <YAxis tickFormatter={(v) => formatCurrency(v)} stroke={C.muted} fontSize={11} width={70} />
                      <Tooltip
                        formatter={(value: number | undefined) => formatCurrency(value ?? 0)}
                        labelFormatter={(label) => {
                          try { return format(parseISO(label as string), "MMM d, yyyy"); } catch { return label as string; }
                        }}
                        contentStyle={{ backgroundColor: C.white, border: `1px solid ${C.hairline}`, borderRadius: "8px" }}
                      />
                      <Legend wrapperStyle={{ fontSize: 12 }} />
                      {whTypes.map((wt) => (
                        <Area
                          key={wt}
                          type="monotone"
                          dataKey={wt}
                          stroke={typeColors[wt] || C.slate}
                          fill={typeColors[wt] || C.slate}
                          fillOpacity={0.15}
                          strokeWidth={2}
                        />
                      ))}
                    </AreaChart>
                  </ResponsiveContainer>
                );
              })()}
          </div>

          {/* Warehouse Count by Size */}
          <div className="rounded-lg bg-white p-6 border " style={{ borderColor: C.hairline, overflow: 'visible' }}>
              {(() => {
                const allWh = queryData?.by_warehouse?.warehouses || [];
                // Build workspace list with names
                const wsMap = new Map<string, string>();
                for (const w of allWh) {
                  const wsId = w.workspace_id;
                  const wsName = (w as WarehouseWithName).workspace_name;
                  if (wsId && !wsMap.has(wsId)) {
                    wsMap.set(wsId, workspaceNameMap?.[wsId] || wsName || `Workspace ${wsId}`);
                  }
                }
                const wsEntries = Array.from(wsMap.entries());
                const isPartialWs = warehouseSizeWsFilter.length > 0 && warehouseSizeWsFilter.length < wsEntries.length;
                const selectedWsName = warehouseSizeWsFilter.length === 1 ? (wsMap.get(warehouseSizeWsFilter[0]) || warehouseSizeWsFilter[0]) : null;

                const warehouses = warehouseSizeWsFilter.length === 0
                  ? allWh
                  : allWh.filter((w: QueryCostByWarehouse) => !!w.workspace_id && warehouseSizeWsFilter.includes(w.workspace_id));

                const bySize: Record<string, number> = {};
                for (const w of warehouses) {
                  const s = w.warehouse_size || "UNKNOWN";
                  if (s === "UNKNOWN") continue;
                  bySize[s] = (bySize[s] || 0) + 1;
                }
                const sizeColors = [C.s2, C.s5, C.s3, C.s4, C.lava, C.s5, C.s1, C.lava];
                const chartData = Object.entries(bySize)
                  .sort((a, b) => b[1] - a[1])
                  .map(([name, count]) => ({ name: name.replace(/_/g, " "), count }));

                return (
                  <>
                    <div className="mb-4 flex items-center justify-between">
                      <div>
                        <h3 className="text-lg font-semibold text-gray-900">Warehouse Count by Size</h3>
                        {selectedWsName && (
                          <p className="text-sm text-orange-600 font-medium mt-0.5">Filtered to: {selectedWsName}</p>
                        )}
                        {isPartialWs && warehouseSizeWsFilter.length > 1 && (
                          <p className="text-sm text-orange-600 font-medium mt-0.5">Filtered to {warehouseSizeWsFilter.length} workspaces</p>
                        )}
                      </div>
                      {wsEntries.length > 1 && (
                        <div className="relative" ref={whSizeDropdownRef}>
                          <button
                            onClick={() => { if (whSizeDropdownOpen) setWhSizeWsSearch(""); setWhSizeDropdownOpen(!whSizeDropdownOpen); }}
                            className={`flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium transition-colors ${isPartialWs ? "border-lava text-lava" : "border-gray-300 text-gray-700 hover:bg-gray-50"}`}
                          >
                            {selectedWsName
                              ? (selectedWsName.length > 20 ? selectedWsName.substring(0, 20) + "…" : selectedWsName)
                              : isPartialWs
                              ? `${warehouseSizeWsFilter.length} workspaces`
                              : "Workspace"}
                            <svg className={`h-3 w-3 transition-transform ${whSizeDropdownOpen ? "rotate-180" : ""}`} fill="none" viewBox="0 0 24 24" stroke="currentColor">
                              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                            </svg>
                          </button>
                          {whSizeDropdownOpen && (() => {
                            const q = whSizeWsSearch.trim().toLowerCase();
                            const filteredWs = q
                              ? wsEntries.filter(([wsId, wsName]) => wsName.toLowerCase().includes(q) || wsId.toLowerCase().includes(q))
                              : wsEntries;
                            return (
                            <div className="absolute right-0 top-full z-[9999] mt-1 w-72 rounded-lg border border-gray-200 bg-white shadow-lg">
                              <div className="sticky top-0 flex items-center justify-between border-b border-gray-100 bg-white px-3 py-2">
                                <span className="text-[10px] font-semibold uppercase tracking-wider text-gray-500">Workspace</span>
                                <div className="flex items-center gap-2 text-xs">
                                  <button onClick={(e) => { e.stopPropagation(); setWarehouseSizeWsFilter(wsEntries.map(([id]) => id)); }} className="text-gray-500 hover:text-gray-800">All</button>
                                  <span className="text-gray-300">·</span>
                                  <button onClick={(e) => { e.stopPropagation(); setWarehouseSizeWsFilter([]); }} className="text-gray-500 hover:text-gray-800">Clear</button>
                                </div>
                              </div>
                              <div className="border-b border-gray-100 p-2">
                                <input
                                  type="text"
                                  value={whSizeWsSearch}
                                  onChange={(e) => setWhSizeWsSearch(e.target.value)}
                                  placeholder="Search workspaces..."
                                  className="w-full rounded border border-gray-200 px-2 py-1 text-xs focus:border-lava focus:outline-none focus:ring-1 focus:ring-lava"
                                  autoFocus
                                />
                              </div>
                              {filteredWs.length === 0 ? (
                                <div className="px-3 py-2 text-xs text-gray-500">No matching workspaces</div>
                              ) : (
                                <VirtualizedList
                                  items={filteredWs}
                                  itemHeight={36}
                                  maxHeight={256}
                                  getKey={([wsId]) => wsId}
                                  renderItem={([wsId, wsName]) => (
                                    <button
                                      onClick={() => setWarehouseSizeWsFilter(prev => prev.includes(wsId) ? prev.filter(x => x !== wsId) : [...prev, wsId])}
                                      className="flex w-full items-center gap-2 px-3 py-2.5 text-left text-xs hover:bg-gray-50"
                                    >
                                      <div className={`flex h-4 w-4 shrink-0 items-center justify-center rounded border ${warehouseSizeWsFilter.includes(wsId) ? "border-orange-500 bg-orange-500" : "border-gray-300"}`}>
                                        {warehouseSizeWsFilter.includes(wsId) && <svg className="h-3 w-3 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" /></svg>}
                                      </div>
                                      <span className="truncate text-gray-700">{wsName}</span>
                                    </button>
                                  )}
                                />
                              )}
                            </div>
                            );
                          })()}
                        </div>
                      )}
                    </div>
                    {chartData.length > 0 ? (
                      <ResponsiveContainer width="100%" height={300}>
                        <BarChart data={chartData} layout="vertical" margin={{ left: 20, right: 40 }}>
                          <XAxis type="number" stroke={C.muted} fontSize={12} tickMargin={8} />
                          <YAxis type="category" dataKey="name" width={80} stroke={C.muted} fontSize={12} tickMargin={8} />
                          <Tooltip contentStyle={{ backgroundColor: C.white, border: `1px solid ${C.hairline}`, borderRadius: "8px" }} />
                          <Bar dataKey="count" name="Warehouses" radius={[0, 4, 4, 0]}>
                            {chartData.map((_, idx) => (
                              <Cell key={idx} fill={sizeColors[idx % sizeColors.length]} />
                            ))}
                            <LabelList dataKey="count" position="right" style={{ fontSize: 11, fill: C.slate }} />
                          </Bar>
                        </BarChart>
                      </ResponsiveContainer>
                    ) : (
                      <div className="flex h-[300px] items-center justify-center text-gray-500">No warehouse data available</div>
                    )}
                  </>
                );
              })()}
          </div>
          </div>

          {/* Query Source Breakdown — full width */}
          <div className="rounded-lg bg-white p-6 border " style={{ borderColor: C.hairline }}>
              <h3 className="mb-4 text-lg font-semibold text-gray-900">Query Source Breakdown</h3>
              {queryData?.by_source?.sources && queryData.by_source.sources.length > 0 ? (
                <div className="overflow-x-auto">
                  <table className="min-w-full divide-y divide-gray-200">
                    <thead className="bg-gray-50">
                      <tr>
                        <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500">
                          Source Type
                        </th>
                        <th className="px-4 py-3 text-right text-xs font-medium uppercase tracking-wider text-gray-500">
                          Query Count
                        </th>
                        <th className="px-4 py-3 text-right text-xs font-medium uppercase tracking-wider text-gray-500">
                          Total Spend
                        </th>
                        <th className="px-4 py-3 text-right text-xs font-medium uppercase tracking-wider text-gray-500">
                          Avg Cost/Query
                        </th>
                        <th className="px-4 py-3 text-right text-xs font-medium uppercase tracking-wider text-gray-500">
                          Share
                        </th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-gray-200 bg-white">
                      {queryData.by_source.sources.map((source) => (
                        <tr
                          key={source.query_source_type}
                          className="cursor-pointer hover:bg-gray-50"
                          onClick={() => handleSourceClick(source.query_source_type)}
                        >
                          <td className="whitespace-nowrap px-4 py-3">
                            <div className="flex items-center gap-2">
                              <div
                                className="h-3 w-3 rounded-full"
                                style={{ backgroundColor: SOURCE_TYPE_COLORS[source.query_source_type] || C.slate }}
                              />
                              <span className="font-medium text-gray-900">{source.query_source_type}</span>
                              <svg className="h-3 w-3 text-gray-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
                              </svg>
                            </div>
                          </td>
                          <td className="whitespace-nowrap px-4 py-3 text-right text-sm text-gray-500">
                            {formatNumber(source.query_count)}
                          </td>
                          <td className="whitespace-nowrap px-4 py-3 text-right text-sm font-medium text-gray-900">
                            {formatCurrency(source.total_spend)}
                          </td>
                          <td className="whitespace-nowrap px-4 py-3 text-right text-sm text-gray-500">
                            {formatCurrency(source.avg_cost_per_query)}
                          </td>
                          <td className="whitespace-nowrap px-4 py-3 text-right">
                            <div className="flex items-center justify-end gap-2">
                              <div className="h-2 w-16 overflow-hidden rounded-full bg-gray-200">
                                <div
                                  className="h-full rounded-full"
                                  style={{
                                    width: `${source.percentage}%`,
                                    backgroundColor: SOURCE_TYPE_COLORS[source.query_source_type] || C.slate,
                                  }}
                                />
                              </div>
                              <span className="text-sm text-gray-500">{(source.percentage ?? 0).toFixed(1)}%</span>
                            </div>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <div className="flex h-40 items-center justify-center text-gray-500">
                  No source breakdown available
                </div>
              )}
          </div>

          {/* Top Expensive Queries Table */}
          <div className="rounded-lg bg-white p-6 border " style={{ borderColor: C.hairline }}>
            {/* Single toolbar row: title · show historical · source pills · search */}
            <div className="mb-4 flex flex-wrap items-center gap-2">
              <h3 className="mr-2 text-lg font-semibold text-gray-900 shrink-0">Most Expensive Queries</h3>
              {historicalQueryCount > 0 && (
                <label className="flex shrink-0 items-center gap-1.5 text-xs text-gray-500 cursor-pointer">
                  <input type="checkbox" checked={showHistoricalQueries}
                    onChange={(e) => { setShowHistoricalQueries(e.target.checked); setQueriesPage(1); }}
                    className="rounded border-gray-300 text-orange-600 focus:ring-orange-500" />
                  Show historical ({historicalQueryCount})
                  <span className="relative group ml-0.5">
                    <svg className="inline h-3 w-3 text-gray-500 cursor-help" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>
                    <span className="absolute bottom-full left-1/2 -translate-x-1/2 mb-1 hidden group-hover:block w-56 rounded-lg bg-gray-900 px-2 py-1.5 text-[10px] text-white shadow-lg z-20">Queries with unknown users or unavailable previews</span>
                  </span>
                </label>
              )}
              <div className="relative ml-auto flex items-center gap-2 shrink-0">
                {querySourceDropdownOpen && (
                  <div className="fixed inset-0 z-10" onClick={() => setQuerySourceDropdownOpen(false)} />
                )}
                <div className="relative">
                  <button
                    onClick={() => setQuerySourceDropdownOpen((o) => !o)}
                    className={`flex items-center gap-2 rounded-full border px-3 py-1 text-xs font-medium transition-colors ${querySourceFilters.length > 0 && querySourceFilters.length < querySourceTypes.length ? "border-lava text-lava" : "border-gray-300 text-gray-700 hover:bg-gray-50"}`}
                  >
                    <span className="max-w-[140px] truncate">
                      {querySourceFilters.length === 0 || querySourceFilters.length === querySourceTypes.length
                        ? "Sources"
                        : querySourceFilters.length === 1
                        ? querySourceFilters[0]
                        : `${querySourceFilters.length} Sources`}
                    </span>
                    <svg
                      className={`ml-0.5 h-4 w-4 shrink-0 text-gray-500 transition-transform ${querySourceDropdownOpen ? "rotate-180" : ""}`}
                      fill="none" viewBox="0 0 24 24" stroke="currentColor"
                    >
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                    </svg>
                  </button>
                  {querySourceDropdownOpen && (
                    <div className="absolute right-0 top-full z-[9999] mt-1 min-w-[200px] max-h-64 overflow-y-auto rounded-lg border border-gray-200 bg-white shadow-lg">
                      <div className="sticky top-0 flex items-center justify-between border-b border-gray-100 bg-white px-3 py-2">
                        <span className="text-[10px] font-semibold uppercase tracking-wider text-gray-500">Sources</span>
                        <div className="flex items-center gap-2 text-xs">
                          <button onClick={(e) => { e.stopPropagation(); setQuerySourceFilters([...querySourceTypes]); setQueriesPage(1); }} className="text-gray-500 hover:text-gray-800">All</button>
                          <span className="text-gray-300">·</span>
                          <button onClick={(e) => { e.stopPropagation(); setQuerySourceFilters([]); setQueriesPage(1); }} className="text-gray-500 hover:text-gray-800">Clear</button>
                        </div>
                      </div>
                      {querySourceTypes.map((type) => {
                        return (
                          <button
                            key={type}
                            onClick={() => { setQuerySourceFilters((prev) => prev.includes(type) ? prev.filter((x) => x !== type) : [...prev, type]); setQueriesPage(1); }}
                            className="flex w-full items-center gap-2 px-3 py-2.5 text-left text-xs hover:bg-gray-50"
                          >
                            <div className={`flex h-4 w-4 shrink-0 items-center justify-center rounded border ${querySourceFilters.includes(type) ? "border-orange-500 bg-orange-500" : "border-gray-300"}`}>
                              {querySourceFilters.includes(type) && <svg className="h-3 w-3 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" /></svg>}
                            </div>
                            <span className="truncate text-gray-700">{type}</span>
                          </button>
                        );
                      })}
                    </div>
                  )}
                </div>
                <div className="relative shrink-0">
                  <svg className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-gray-500" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
                  </svg>
                  <input
                    type="text"
                    placeholder="Search queries..."
                    value={querySearch}
                    onChange={(e) => { setQuerySearch(e.target.value); setQueriesPage(1); }}
                    className="w-44 rounded-full border border-gray-200 bg-white py-1.5 pl-9 pr-4 text-xs placeholder:text-gray-400 focus:border-lava focus:outline-none focus:ring-1 focus:ring-lava"
                  />
                </div>
              </div>
            </div>
            {topQueriesLoading && sortedQueries.length === 0 ? (
              <div className="flex h-32 items-center justify-center gap-3">
                <div className="h-6 w-6 animate-spin rounded-full border-2 border-gray-200" style={{ borderTopColor: C.lava }} />
                <span className="text-sm text-gray-500">Loading top queries...</span>
              </div>
            ) : sortedQueries.length > 0 ? (
              <div className="overflow-x-auto">
                <table className="min-w-full divide-y divide-gray-200">
                  <thead className="bg-gray-50">
                    <tr>
                      <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500">
                        Source
                      </th>
                      <th
                        className="cursor-pointer px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500 hover:text-gray-700"
                        onClick={() => handleSort("executed_by")}
                      >
                        User {sortField === "executed_by" && (sortDirection === "asc" ? "↑" : "↓")}
                      </th>
                      <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500">
                        <span className="flex items-center gap-1">
                          Query Preview
                          <InfoTooltip text="If queries show as <Redacted>, this app does not have access to system.query.history. Grant SELECT on this table to the app's service principal." />
                        </span>
                      </th>
                      <th
                        className="cursor-pointer px-4 py-3 text-right text-xs font-medium uppercase tracking-wider text-gray-500 hover:text-gray-700"
                        onClick={() => handleSort("duration_seconds")}
                      >
                        Duration {sortField === "duration_seconds" && (sortDirection === "asc" ? "↑" : "↓")}
                      </th>
                      <th
                        className="cursor-pointer px-4 py-3 text-right text-xs font-medium uppercase tracking-wider text-gray-500 hover:text-gray-700"
                        onClick={() => handleSort("cost")}
                      >
                        Cost {sortField === "cost" && (sortDirection === "asc" ? "↑" : "↓")}
                        <InfoTooltip text={COST_TOOLTIP_TEXT} stopClick />
                      </th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-200 bg-white">
                    {sortedQueries.map((query, idx) => (
                      <tr key={query.statement_id || idx} className="hover:bg-gray-50">
                        <td className="whitespace-nowrap px-4 py-3">
                          <span
                            className="inline-flex rounded-full px-2 py-1 text-xs font-medium"
                            style={{
                              backgroundColor: `${SOURCE_TYPE_COLORS[query.query_source_type] || C.slate}20`,
                              color: SOURCE_TYPE_COLORS[query.query_source_type] || C.slate,
                            }}
                          >
                            {query.query_source_type}
                          </span>
                        </td>
                        <td className="px-4 py-3">
                          <span className="inline-flex rounded-full bg-gray-100 px-2 py-0.5 text-xs font-medium text-gray-700 max-w-40 truncate" title={query.executed_by}>
                            {formatIdentity(query.executed_by, spNameMap)}
                          </span>
                        </td>
                        <td className="max-w-md px-4 py-3 text-sm text-gray-500">
                          {(() => {
                            let qHistUrl: string | null = host ? `${host}/sql/history` : null;
                            if (!qHistUrl && query.query_profile_url) { try { qHistUrl = new URL(query.query_profile_url).origin + "/sql/history"; } catch { /* ignore */ } }
                            return qHistUrl ? (
                              <a href={qHistUrl} target="_blank" rel="noopener noreferrer" className="block truncate font-mono text-xs text-blue-600 hover:text-blue-800 hover:underline" title="Open Query History">
                                {query.statement_preview}
                              </a>
                            ) : (
                              <div className="truncate font-mono text-xs">{query.statement_preview}</div>
                            );
                          })()}
                        </td>
                        <td className="whitespace-nowrap px-4 py-3 text-right text-sm text-gray-500">
                          {formatDuration(query.duration_seconds)}
                        </td>
                        <td className="whitespace-nowrap px-4 py-3 text-right text-sm font-medium text-gray-900">
                          {formatCurrency(query.cost)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {queryTotalPages > 1 && (
                  <div className="mt-4 flex items-center justify-between border-t border-gray-200 pt-4">
                    <p className="text-sm text-gray-700">
                      Showing <span className="font-medium">{queryStart + 1}</span> to <span className="font-medium">{Math.min(queryStart + 10, searchedQueries.length)}</span> of <span className="font-medium">{searchedQueries.length}</span>
                    </p>
                    <div className="flex gap-2">
                      <button onClick={() => setQueriesPage(p => Math.max(1, p - 1))} disabled={queriesPage === 1}
                        className="rounded border border-gray-300 px-3 py-1 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50">Previous</button>
                      <button onClick={() => setQueriesPage(p => Math.min(queryTotalPages, p + 1))} disabled={queriesPage === queryTotalPages}
                        className="rounded border border-gray-300 px-3 py-1 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50">Next</button>
                    </div>
                  </div>
                )}
              </div>
            ) : (
              <div className="flex h-40 items-center justify-center text-gray-500">
                No query data available
              </div>
            )}
          </div>

        </>
      )}

      {/* Source Drilldown Modal — rendered via portal to avoid stacking context issues */}
      {selectedSource && createPortal(
        <div className="fixed inset-0 z-[9999] flex items-center justify-center bg-black/50" onClick={() => setSelectedSource(null)}>
          <div className="mx-4 w-full max-w-5xl rounded-lg bg-white p-6 shadow-xl" onClick={(e) => e.stopPropagation()}>
            <div className="mb-4 flex items-center justify-between">
              <div className="flex items-center gap-3">
                <div
                  className="h-4 w-4 rounded-full"
                  style={{ backgroundColor: SOURCE_TYPE_COLORS[selectedSource] || C.slate }}
                />
                <h3 className="text-lg font-semibold text-gray-900">
                  Top 5 Queries — {selectedSource}
                </h3>
              </div>
              <button onClick={() => setSelectedSource(null)} className="rounded-full p-1 text-gray-500 hover:bg-gray-100 hover:text-gray-600">
                <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>

            {sourceQueriesLoading ? (
              <div className="flex h-40 items-center justify-center">
                <div className="h-8 w-8 animate-spin rounded-full border-4 border-gray-200" style={{ borderTopColor: C.lava }} />
              </div>
            ) : sourceQueries.length > 0 ? (
              <div className="overflow-x-auto">
                <table className="min-w-full divide-y divide-gray-200">
                  <thead className="bg-gray-50">
                    <tr>
                      <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500">User</th>
                      <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500">
                        <span className="flex items-center gap-1">
                          Query Preview
                          <InfoTooltip text="If queries show as <Redacted>, this app does not have access to system.query.history. Grant SELECT on this table to the app's service principal." />
                        </span>
                      </th>
                      <th className="px-4 py-3 text-right text-xs font-medium uppercase tracking-wider text-gray-500">Duration</th>
                      <th className="px-4 py-3 text-right text-xs font-medium uppercase tracking-wider text-gray-500">
                        Cost <InfoTooltip text={COST_TOOLTIP_TEXT} />
                      </th>
                      <th className="px-4 py-3 text-right text-xs font-medium uppercase tracking-wider text-gray-500">History</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-200">
                    {sourceQueries.map((q, idx) => {
                      const srcHistUrl: string | null = host ? `${host}/sql/history` : (() => { try { return q.query_profile_url ? new URL(q.query_profile_url).origin + "/sql/history" : null; } catch { return null; } })();
                      return (
                      <tr key={q.statement_id || idx} className="hover:bg-gray-50">
                        <td className="px-4 py-3">
                          <span className="inline-flex rounded-full bg-gray-100 px-2 py-0.5 text-xs font-medium text-gray-700 max-w-40 truncate" title={q.executed_by}>
                            {formatIdentity(q.executed_by, spNameMap)}
                          </span>
                        </td>
                        <td className="max-w-sm px-4 py-3 text-sm text-gray-500">
                          <div className="truncate font-mono text-xs">{q.statement_preview}</div>
                        </td>
                        <td className="whitespace-nowrap px-4 py-3 text-right text-sm text-gray-500">
                          {formatDuration(q.duration_seconds)}
                        </td>
                        <td className="whitespace-nowrap px-4 py-3 text-right text-sm font-medium text-gray-900">
                          {formatCurrency(q.cost)}
                        </td>
                        <td className="whitespace-nowrap px-4 py-3 text-right">
                          {srcHistUrl
                            ? <a href={srcHistUrl} target="_blank" rel="noopener noreferrer" className="text-xs text-lava hover:underline">History ↗</a>
                            : <span className="text-xs text-gray-500">—</span>
                          }
                        </td>
                      </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            ) : (
              <div className="flex h-40 items-center justify-center text-gray-500">
                No queries found for this source type
              </div>
            )}
          </div>
        </div>,
        document.body
      )}
      {/* User Query Drilldown Modal */}
      {selectedUser && createPortal(
        <div className="fixed inset-0 z-[9999] flex items-center justify-center bg-black/50" onClick={() => setSelectedUser(null)}>
          <div className="mx-4 w-full max-w-4xl rounded-lg bg-white p-6 shadow-xl" onClick={(e) => e.stopPropagation()}>
            <div className="mb-4 flex items-center justify-between">
              <div>
                <h3 className="text-lg font-semibold text-gray-900">Queries — {selectedUser.display}</h3>
                {userQueriesData?.total_spend != null && (
                  <p className="text-sm text-gray-500 mt-0.5">
                    {userQueriesData.query_count} queries · {formatCurrency(userQueriesData.total_spend)} total
                  </p>
                )}
              </div>
              <button onClick={() => setSelectedUser(null)} className="rounded-full p-1 text-gray-500 hover:bg-gray-100 hover:text-gray-600">
                <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>

            {userQueriesLoading ? (
              <div className="flex h-48 items-center justify-center">
                <div className="h-8 w-8 animate-spin rounded-full border-4 border-gray-200" style={{ borderTopColor: C.lava }} />
              </div>
            ) : (userQueriesData?.queries?.length ?? 0) > 0 ? (
              <div className="overflow-x-auto max-h-[60vh] overflow-y-auto">
                <table className="min-w-full divide-y divide-gray-200">
                  <thead className="bg-gray-50 sticky top-0">
                    <tr>
                      <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500">Time</th>
                      <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500">Source</th>
                      <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500">Query</th>
                      <th className="px-4 py-3 text-right text-xs font-medium uppercase tracking-wider text-gray-500">Duration</th>
                      <th className="px-4 py-3 text-right text-xs font-medium uppercase tracking-wider text-gray-500">
                        Cost <InfoTooltip text={COST_TOOLTIP_TEXT} />
                      </th>
                      <th className="px-4 py-3 text-right text-xs font-medium uppercase tracking-wider text-gray-500">History</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-200">
                    {freshUserQueries.map((q, idx) => {
                      const histUrl: string | null = host ? `${host}/sql/history` : (() => { try { return q.query_profile_url ? new URL(q.query_profile_url).origin + "/sql/history" : null; } catch { return null; } })();
                      return (
                        <tr key={q.statement_id || idx} className="hover:bg-gray-50">
                          <td className="whitespace-nowrap px-4 py-3 text-xs text-gray-500">
                            {q.start_time ? (() => { try { return format(new Date(q.start_time), "MMM d, HH:mm"); } catch { return q.start_time; } })() : "—"}
                          </td>
                          <td className="whitespace-nowrap px-4 py-3">
                            <span className="inline-flex rounded-full bg-gray-100 px-2 py-0.5 text-xs font-medium text-gray-700">{q.query_source_type}</span>
                          </td>
                          <td className="max-w-xs px-4 py-3">
                            <div className="truncate font-mono text-xs text-gray-500" title={q.statement_preview}>{q.statement_preview || "—"}</div>
                          </td>
                          <td className="whitespace-nowrap px-4 py-3 text-right text-sm text-gray-500">{formatDuration(q.duration_seconds)}</td>
                          <td className="whitespace-nowrap px-4 py-3 text-right text-sm font-medium text-gray-900">{formatCurrency(q.cost)}</td>
                          <td className="whitespace-nowrap px-4 py-3 text-right">
                            {histUrl
                              ? <a href={histUrl} target="_blank" rel="noopener noreferrer" className="text-xs text-lava hover:underline">History ↗</a>
                              : <span className="text-xs text-gray-500">—</span>}
                          </td>
                        </tr>
                      );
                    })}
                    {staleUserQueries.length > 0 && (
                      <>
                        <tr>
                          <td colSpan={6} className="bg-gray-50 px-4 py-2 text-xs font-medium uppercase tracking-wider text-gray-500">
                            Historical — profile links may be expired (25+ days ago)
                          </td>
                        </tr>
                        {staleUserQueries.map((q, idx) => {
                          const historyUrl: string | null = host ? `${host}/sql/history` : (() => { try { return q.query_profile_url ? new URL(q.query_profile_url).origin + "/sql/history" : null; } catch { return null; } })();
                          return (
                            <tr key={q.statement_id || idx} className="opacity-60 hover:bg-gray-50">
                              <td className="whitespace-nowrap px-4 py-3 text-xs text-gray-500">
                                {q.start_time ? (() => { try { return format(new Date(q.start_time), "MMM d, HH:mm"); } catch { return q.start_time; } })() : "—"}
                              </td>
                              <td className="whitespace-nowrap px-4 py-3">
                                <span className="inline-flex rounded-full bg-gray-100 px-2 py-0.5 text-xs font-medium text-gray-700">{q.query_source_type}</span>
                              </td>
                              <td className="max-w-xs px-4 py-3">
                                <div className="truncate font-mono text-xs text-gray-500" title={q.statement_preview}>{q.statement_preview || "—"}</div>
                              </td>
                              <td className="whitespace-nowrap px-4 py-3 text-right text-sm text-gray-500">{formatDuration(q.duration_seconds)}</td>
                              <td className="whitespace-nowrap px-4 py-3 text-right text-sm font-medium text-gray-900">{formatCurrency(q.cost)}</td>
                              <td className="whitespace-nowrap px-4 py-3 text-right">
                                <div className="flex flex-col items-end gap-1">
                                  <span className="text-xs text-gray-500" title="Databricks query history retention is ~30 days. Profile links for queries older than ~25 days may no longer be accessible.">Expired</span>
                                  {q.statement_id && (
                                    <button onClick={() => navigator.clipboard.writeText(q.statement_id!)} className="font-mono text-xs text-gray-500 hover:text-gray-700" title={`Copy statement ID: ${q.statement_id}`}>
                                      {q.statement_id.slice(0, 8)}… ⎘
                                    </button>
                                  )}
                                  {historyUrl && (
                                    <a href={historyUrl} target="_blank" rel="noopener noreferrer" className="text-xs text-blue-500 hover:underline">History ↗</a>
                                  )}
                                </div>
                              </td>
                            </tr>
                          );
                        })}
                      </>
                    )}
                  </tbody>
                </table>
              </div>
            ) : (
              <div className="flex h-48 items-center justify-center text-gray-500">
                No queries found for this user in the selected date range
              </div>
            )}
          </div>
        </div>,
        document.body
      )}
    </div>
  );
}

export function OptimizeMethodologyPanel() {
  const MINIMIZE_KEY = "cost-obs-minimize-optimize-info";
  const [minimized, setMinimized] = useState(() =>
    typeof window !== "undefined" ? localStorage.getItem(MINIMIZE_KEY) === "true" : false
  );
  const toggle = (v: boolean) => {
    setMinimized(v);
    if (v) localStorage.setItem(MINIMIZE_KEY, "true");
    else localStorage.removeItem(MINIMIZE_KEY);
  };
  return (
    <InfoPanel title="Optimize tab methodology" minimized={minimized} onToggle={toggle}>
      <p className="mb-2 font-medium">Idle Time</p>
      <ul className="list-inside list-disc space-y-1">
        <li><strong>Uptime</strong>: For classic warehouses, derived from <code className="rounded bg-white/60 px-1 text-xs">system.compute.warehouse_events</code> using the time between START and STOP lifecycle events</li>
        <li><strong>Active query time</strong>: Sum of query durations from <code className="rounded bg-white/60 px-1 text-xs">system.query.history</code> for the same warehouse and window</li>
        <li><strong>Idle time</strong>: Uptime minus active query time, floored at zero</li>
        <li><strong>Estimated idle spend</strong>: Classic single-cluster spend prorated by the idle share of uptime</li>
        <li><strong>Serverless</strong>: Uptime, raw idle time, idle percentage, and idle spend are suppressed because wall-clock lifecycle time is not a valid waste measure. Use Warm Hold and auto-stop instead.</li>
      </ul>
      <p className="mb-2 mt-3 font-medium">Rightsizing</p>
      <ul className="list-inside list-disc space-y-1">
        <li><strong>Over scaled</strong>: Multiple clusters were available, but median concurrency never exceeded one</li>
        <li><strong>Oversized</strong>: Query duration and data scanned suggest that a smaller warehouse may be sufficient</li>
        <li>Recommendations use warehouse events and query history over the selected date range</li>
      </ul>
    </InfoPanel>
  );
}

export function WarehouseRightsizingView({ host }: { host?: string | null }) {
  const { data: warehouseHealth, isLoading: healthLoading, error: healthError, refetch: refetchHealth } = useQuery<{
    available: boolean;
    reason?: string;
    reason_detail?: string;
    recommendations: Array<{
      warehouse_id: string;
      warehouse_name: string | null;
      warehouse_size: string | null;
      workspace_id: string;
      recommendation_type: string;
      recommendation_text: string;
    }>;
    warehouses_analyzed: number;
  }>({
    queryKey: ["warehouse-health"],
    queryFn: () => fetchJson("/api/sql/warehouse-health"),
    staleTime: 30 * 60 * 1000,
    retry: false,
  });
  // IDLE_RUNNING is reported by the idle-time table, so the empty state must ignore it too.
  const healthRecs = (warehouseHealth?.recommendations ?? []).filter((r) => r.recommendation_type !== "IDLE_RUNNING");
  const HEALTH_ISSUE_OPTIONS = [
    { value: "OVER_SCALED", label: "Over-Scaled" },
    { value: "OVERSIZED", label: "Oversized" },
  ];
  const [healthIssueFilter, setHealthIssueFilter] = useState<string[]>(() => HEALTH_ISSUE_OPTIONS.map(o => o.value));
  const [healthSearch, setHealthSearch] = useState("");
  const [healthPage, setHealthPage] = useState(1);
  const HEALTH_PAGE_SIZE = 10;
  const [healthIssueDropdownOpen, setHealthIssueDropdownOpen] = useState(false);
  const healthIssueDropdownRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!healthIssueDropdownOpen) return;
    const handler = (e: MouseEvent) => {
      if (healthIssueDropdownRef.current && !healthIssueDropdownRef.current.contains(e.target as Node)) {
        setHealthIssueDropdownOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [healthIssueDropdownOpen]);

  return (
    <div className="rounded-lg border border-gray-200 bg-white p-6">
      <div className="mb-4 flex items-center justify-between">
        <div>
          <h3 className="text-base font-semibold text-gray-900 flex items-center">
            Warehouse Rightsizing
            <InfoTooltip text="Two categories of rightsizing opportunities. Over-scaled: warehouse scaled to 2+ clusters over the last 30 days but peak query concurrency stayed below capacity — consider reducing max_num_clusters. Oversized: a Large or bigger warehouse with average queue time < 15s and median query duration < 3 minutes — consider downsizing one tier." />
          </h3>
        </div>
        <div className="flex items-center gap-3">
          {typeof warehouseHealth?.warehouses_analyzed === "number" && (
            <span className="text-xs text-gray-500">{warehouseHealth.warehouses_analyzed} warehouse{warehouseHealth.warehouses_analyzed !== 1 ? "s" : ""} analyzed</span>
          )}
          {healthRecs.length ? (
            <>
              <div className="relative" ref={healthIssueDropdownRef}>
                <button
                  onClick={() => { setHealthIssueDropdownOpen((o) => !o); }}
                  className={`flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium transition-colors ${healthIssueFilter.length > 0 && healthIssueFilter.length < HEALTH_ISSUE_OPTIONS.length ? "border-lava text-lava" : "border-gray-300 bg-white text-gray-700 hover:bg-gray-50"}`}
                >
                  {healthIssueFilter.length === 0 || healthIssueFilter.length === HEALTH_ISSUE_OPTIONS.length ? "Issues" : healthIssueFilter.length === 1 ? (HEALTH_ISSUE_OPTIONS.find(o => o.value === healthIssueFilter[0])?.label || healthIssueFilter[0]) : `${healthIssueFilter.length} Issues`}
                  <svg className={`h-3 w-3 transition-transform ${healthIssueDropdownOpen ? "rotate-180" : ""}`} fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" /></svg>
                </button>
                {healthIssueDropdownOpen && (
                  <div className="absolute right-0 top-full z-[9999] mt-1 min-w-[180px] max-h-64 overflow-y-auto rounded-lg border border-gray-200 bg-white shadow-lg">
                    <div className="sticky top-0 flex items-center justify-between border-b border-gray-100 bg-white px-3 py-2">
                      <span className="text-[10px] font-semibold uppercase tracking-wider text-gray-500">Issues</span>
                      <div className="flex items-center gap-2 text-xs">
                        <button onClick={(e) => { e.stopPropagation(); setHealthIssueFilter(HEALTH_ISSUE_OPTIONS.map(o => o.value)); setHealthPage(1); }} className="text-gray-500 hover:text-gray-800">All</button>
                        <span className="text-gray-300">·</span>
                        <button onClick={(e) => { e.stopPropagation(); setHealthIssueFilter([]); setHealthPage(1); }} className="text-gray-500 hover:text-gray-800">Clear</button>
                      </div>
                    </div>
                    {HEALTH_ISSUE_OPTIONS.map((opt) => (
                      <button key={opt.value} onClick={() => { setHealthIssueFilter((prev) => prev.includes(opt.value) ? prev.filter(x => x !== opt.value) : [...prev, opt.value]); setHealthPage(1); }}
                        className="flex w-full items-center gap-2 px-3 py-2.5 text-left text-xs hover:bg-gray-50">
                        <div className={`flex h-4 w-4 shrink-0 items-center justify-center rounded border ${healthIssueFilter.includes(opt.value) ? "border-orange-500 bg-orange-500" : "border-gray-300"}`}>
                          {healthIssueFilter.includes(opt.value) && <svg className="h-3 w-3 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" /></svg>}
                        </div>
                        <span className="truncate text-gray-700">{opt.label}</span>
                      </button>
                    ))}
                  </div>
                )}
              </div>
              <div className="relative">
                <svg className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-gray-500" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
                </svg>
                <input
                  type="text"
                  placeholder="Search warehouses..."
                  value={healthSearch}
                  onChange={(e) => { setHealthSearch(e.target.value); setHealthPage(1); }}
                  className="w-44 rounded-full border border-gray-200 bg-white py-1.5 pl-9 pr-4 text-xs placeholder:text-gray-400 focus:border-lava focus:outline-none focus:ring-1 focus:ring-lava"
                />
              </div>
            </>
          ) : null}
        </div>
      </div>

      {healthLoading ? (
        <div className="flex h-24 items-center justify-center">
          <div className="h-6 w-6 animate-spin rounded-full border-2 border-gray-200" style={{ borderTopColor: C.lava }} />
        </div>
      ) : healthError ? (
        <SourceCapabilityNotice
          title="Rightsizing recommendations failed to load"
          description={healthError.message}
          onRetry={() => { void refetchHealth(); }}
        />
      ) : warehouseHealth?.available === false && warehouseHealth.reason === "shared_scope_unsupported" ? (
        <SourceCapabilityNotice
          title="Rightsizing detail is not included in this source"
          description={warehouseHealth.reason_detail || "No current shared aggregate provides warehouse-event grain. The source must publish the aggregate below."}
          requiredAggregates={["warehouse_health_summary"]}
        />
      ) : warehouseHealth?.available === false ? (
        <SourceCapabilityNotice
          title="Rightsizing recommendations are temporarily unavailable"
          description="Warehouse health data unavailable. Requires system.compute.warehouse_events access."
          requiredAggregates={["warehouse_health_summary"]}
        />
      ) : !healthRecs.length ? (
        <div className="rounded-lg p-4 text-sm" style={{ background: C.oatPage, color: C.greenInk }}>
          <span className="inline-flex items-center gap-2">
            <svg className="h-4 w-4 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" /></svg>
            No rightsizing recommendations — all warehouses appear appropriately sized.
          </span>
        </div>
      ) : (() => {
        const badgeColor: Record<string, string> = {
          IDLE_RUNNING: "bg-red-100 text-red-700",
          OVER_SCALED: "bg-amber-100 text-amber-700",
          OVERSIZED: "bg-orange-100 text-orange-700",
        };
        const badgeLabel: Record<string, string> = {
          IDLE_RUNNING: "Idle Running",
          OVER_SCALED: "Over-Scaled",
          OVERSIZED: "Oversized",
        };
        const filtered = healthRecs
          .filter((r) => healthIssueFilter.length === 0 || healthIssueFilter.includes(r.recommendation_type))
          .filter((r) => !healthSearch || (r.warehouse_name || r.warehouse_id || "").toLowerCase().includes(healthSearch.toLowerCase()));
        const totalPages = Math.max(1, Math.ceil(filtered.length / HEALTH_PAGE_SIZE));
        const safePage = Math.min(healthPage, totalPages);
        const pageRecs = filtered.slice((safePage - 1) * HEALTH_PAGE_SIZE, safePage * HEALTH_PAGE_SIZE);
        return (
          <>
            <div className="overflow-x-auto rounded-lg border border-gray-200">
              <table className="min-w-full divide-y divide-gray-200 text-sm">
                <thead className="bg-gray-50">
                  <tr>
                    <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500">Warehouse</th>
                    <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500">Size</th>
                    <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500">Issue</th>
                    <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500">Recommendation</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-200 bg-white">
                  {pageRecs.length === 0 && healthSearch && (
                    <tr>
                      <td colSpan={4} className="px-4 py-8 text-center text-sm text-gray-500">
                        No warehouses match your search.
                      </td>
                    </tr>
                  )}
                  {pageRecs.map((rec, i) => (
                    <tr key={`${rec.warehouse_id}-${rec.recommendation_type}-${i}`} className="hover:bg-gray-50">
                      <td className="px-4 py-3 font-medium text-gray-900">
                        {host ? (
                          <a
                            href={`${host}/sql/warehouses/${rec.warehouse_id}/edit`}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="text-blue-600 hover:underline"
                          >
                            {rec.warehouse_name || rec.warehouse_id}
                          </a>
                        ) : (
                          rec.warehouse_name || rec.warehouse_id
                        )}
                      </td>
                      <td className="px-4 py-3 text-gray-500">{rec.warehouse_size || "—"}</td>
                      <td className="px-4 py-3">
                        <span className={`inline-flex rounded-full px-2 py-0.5 text-xs font-medium ${badgeColor[rec.recommendation_type] || "bg-gray-100 text-gray-700"}`}>
                          {badgeLabel[rec.recommendation_type] || rec.recommendation_type}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-gray-600 text-xs max-w-sm">{rec.recommendation_text}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {totalPages > 1 && (
              <div className="mt-3 flex items-center justify-between text-xs text-gray-500">
                <span>{filtered.length} recommendation{filtered.length !== 1 ? "s" : ""}{healthIssueFilter.length > 0 && healthIssueFilter.length < HEALTH_ISSUE_OPTIONS.length ? ` (filtered)` : ""}</span>
                <div className="flex items-center gap-1">
                  <button
                    onClick={() => setHealthPage((p) => Math.max(1, p - 1))}
                    disabled={safePage <= 1}
                    className="rounded px-2 py-1 disabled:opacity-40 hover:bg-gray-100"
                  >
                    ‹ Prev
                  </button>
                  <span className="px-2">Page {safePage} of {totalPages}</span>
                  <button
                    onClick={() => setHealthPage((p) => Math.min(totalPages, p + 1))}
                    disabled={safePage >= totalPages}
                    className="rounded px-2 py-1 disabled:opacity-40 hover:bg-gray-100"
                  >
                    Next ›
                  </button>
                </div>
              </div>
            )}
          </>
        );
      })()}
    </div>
  );
}

const fmt$ = (v: number) =>
  v >= 1000 ? `$${(v / 1000).toFixed(1)}K` : `$${v.toFixed(0)}`;

const fmtHours = (minutes: number) => {
  const h = Math.floor(minutes / 60);
  const m = Math.round(minutes % 60);
  if (h === 0) return `${m}m`;
  if (m === 0) return `${h}h`;
  return `${h}h ${m}m`;
};

export function WarehouseIdleTimeView({
  host,
  startDate,
  endDate,
  workspaceIds,
}: {
  host?: string | null;
  startDate?: string;
  endDate?: string;
  workspaceIds?: string[];
}) {
  const params = new URLSearchParams();
  if (startDate) params.set("start_date", startDate);
  if (endDate) params.set("end_date", endDate);
  if (workspaceIds && workspaceIds.length > 0) params.set("workspace_ids", workspaceIds.join(","));

  const { data, isLoading, error: idleError, refetch: refetchIdle } = useQuery<{
    available: boolean;
    reason?: string;
    reason_detail?: string;
    serverless_detected: boolean;
    error?: string;
    warehouses: Array<{
      warehouse_id: string | null;
      warehouse_name: string | null;
      warehouse_size: string | null;
      warehouse_type: string;
      workspace_id: string;
      uptime_source?: string;
      total_running_minutes: number;
      busy_union_minutes: number;
      idle_minutes: number;
      idle_pct: number;
      warm_hold_minutes: number;
      keep_alive_score: number;
      auto_stop_mins: number;
      max_num_clusters: number;
      total_spend: number;
      // null when the backend suppressed attribution — serverless, multi-cluster,
      // or when uptime came from the billing fallback rather than lifecycle events.
      estimated_idle_spend: number | null;
      low_confidence: boolean;
    }>;
  }>({
    queryKey: ["warehouse-idle-time", startDate, endDate, workspaceIds?.join(",")],
    queryFn: () =>
      fetchJson(`/api/sql/warehouse-health/idle-time?${params}`),
    staleTime: 30 * 60 * 1000,
    retry: false,
  });
  const idleWarehouses = useMemo(() => data?.warehouses ?? [], [data?.warehouses]);
  const [idlePage, setIdlePage] = useState(1);
  const [idleSearch, setIdleSearch] = useState("");
  const [idleSizeFilter, setIdleSizeFilter] = useState<string[]>([]);
  const [idleTypeFilter, setIdleTypeFilter] = useState<string[]>([]);
  const idleSizeSeen = useRef<Set<string>>(new Set());
  const idleTypeSeen = useRef<Set<string>>(new Set());
  const [idleSizeDropdownOpen, setIdleSizeDropdownOpen] = useState(false);
  const [idleTypeDropdownOpen, setIdleTypeDropdownOpen] = useState(false);
  const idleSizeDropdownRef = useRef<HTMLDivElement>(null);
  const idleTypeDropdownRef = useRef<HTMLDivElement>(null);
  const IDLE_PAGE_SIZE = 10;

  const availableIdleSizes = useMemo(
    () => Array.from(new Set(idleWarehouses.map(w => w.warehouse_size))).filter(Boolean).sort() as string[],
    [idleWarehouses],
  );
  // Normalize warehouse_type to the two labels the dropdown actually shows
  // (Serverless vs Classic). Raw values include 'CLASSIC', 'PRO', 'SERVERLESS';
  // Pro is a classic-family variant and should not appear as a duplicate row.
  const availableIdleTypes = useMemo(
    () => Array.from(new Set(idleWarehouses.map(w => w.warehouse_type))).filter(Boolean) as string[],
    [idleWarehouses],
  );

  useEffect(() => {
    const seen = idleSizeSeen.current;
    const fresh = availableIdleSizes.filter(x => !seen.has(x));
    if (fresh.length === 0) return;
    setIdleSizeFilter(prev => Array.from(new Set([...prev, ...fresh])));
    fresh.forEach(x => seen.add(x));
  }, [availableIdleSizes]);

  useEffect(() => {
    const seen = idleTypeSeen.current;
    const fresh = availableIdleTypes.filter(x => !seen.has(x));
    if (fresh.length === 0) return;
    setIdleTypeFilter(prev => Array.from(new Set([...prev, ...fresh])));
    fresh.forEach(x => seen.add(x));
  }, [availableIdleTypes]);

  useEffect(() => {
    if (!idleSizeDropdownOpen) return;
    const handleClick = (e: MouseEvent) => {
      if (idleSizeDropdownRef.current && !idleSizeDropdownRef.current.contains(e.target as Node)) {
        setIdleSizeDropdownOpen(false);
      }
    };
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, [idleSizeDropdownOpen]);

  useEffect(() => {
    if (!idleTypeDropdownOpen) return;
    const handleClick = (e: MouseEvent) => {
      if (idleTypeDropdownRef.current && !idleTypeDropdownRef.current.contains(e.target as Node)) {
        setIdleTypeDropdownOpen(false);
      }
    };
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, [idleTypeDropdownOpen]);

  return (
    <div className="rounded-lg border border-gray-200 bg-white p-6">
      <div className="mb-4 flex items-start justify-between gap-4">
        <div>
          <h3 className="text-base font-semibold text-gray-900 flex items-center">
            Top Warehouses by Idle Time
            <InfoTooltip text="Idle time = warehouse uptime minus busy time (union of query intervals — bounded by wall-clock even under concurrency). Est. Idle Spend is only computed for CLASSIC single-cluster warehouses because serverless bills per-query with warm-hold at a reduced rate, and multi-cluster warehouses have concurrent cluster billing that wall-clock uptime can't reconstruct. For serverless rows, look at Warm-Hold (minutes held ready between queries, capped at auto_stop_mins) and the 'low conf.' badge — the actionable knob is auto_stop_mins, not raw idle %." />
          </h3>
        </div>
        {data?.available && idleWarehouses.length > 0 && (() => {
          const distinctSizes = availableIdleSizes;
          const distinctTypes = availableIdleTypes;
          return (
            <div className="flex items-center gap-2 shrink-0">
              {distinctSizes.length > 0 && (
                <div className="relative" ref={idleSizeDropdownRef}>
                  <button
                    onClick={() => { setIdleSizeDropdownOpen(o => !o); setIdleTypeDropdownOpen(false); }}
                    className={`flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium transition-colors ${idleSizeFilter.length > 0 && idleSizeFilter.length < distinctSizes.length ? "border-lava text-lava" : "border-gray-300 bg-white text-gray-700 hover:bg-gray-50"}`}
                  >
                    {idleSizeFilter.length === 0 || idleSizeFilter.length === distinctSizes.length ? "Sizes" : idleSizeFilter.length === 1 ? idleSizeFilter[0] : `${idleSizeFilter.length} Sizes`}
                    <svg className={`h-3 w-3 transition-transform ${idleSizeDropdownOpen ? "rotate-180" : ""}`} fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" /></svg>
                  </button>
                  {idleSizeDropdownOpen && (
                    <div className="absolute right-0 top-full z-[9999] mt-1 min-w-[180px] max-h-64 overflow-y-auto rounded-lg border border-gray-200 bg-white shadow-lg">
                      <div className="sticky top-0 flex items-center justify-between border-b border-gray-100 bg-white px-3 py-2">
                        <span className="text-[10px] font-semibold uppercase tracking-wider text-gray-500">Sizes</span>
                        <div className="flex items-center gap-2 text-xs">
                          <button onClick={(e) => { e.stopPropagation(); setIdleSizeFilter([...distinctSizes]); setIdlePage(1); }} className="text-gray-500 hover:text-gray-800">All</button>
                          <span className="text-gray-300">·</span>
                          <button onClick={(e) => { e.stopPropagation(); setIdleSizeFilter([]); setIdlePage(1); }} className="text-gray-500 hover:text-gray-800">Clear</button>
                        </div>
                      </div>
                      {distinctSizes.map(s => (
                        <button key={s} onClick={() => { setIdleSizeFilter((prev) => prev.includes(s) ? prev.filter(x => x !== s) : [...prev, s]); setIdlePage(1); }}
                          className="flex w-full items-center gap-2 px-3 py-2.5 text-left text-xs hover:bg-gray-50">
                          <div className={`flex h-4 w-4 shrink-0 items-center justify-center rounded border ${idleSizeFilter.includes(s) ? "border-orange-500 bg-orange-500" : "border-gray-300"}`}>
                            {idleSizeFilter.includes(s) && <svg className="h-3 w-3 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" /></svg>}
                          </div>
                          <span className="truncate text-gray-700">{s}</span>
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              )}
              {distinctTypes.length > 0 && (
                <div className="relative" ref={idleTypeDropdownRef}>
                  <button
                    onClick={() => { setIdleTypeDropdownOpen(o => !o); setIdleSizeDropdownOpen(false); }}
                    className={`flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium transition-colors ${idleTypeFilter.length > 0 && idleTypeFilter.length < distinctTypes.length ? "border-lava text-lava" : "border-gray-300 bg-white text-gray-700 hover:bg-gray-50"}`}
                  >
                    {idleTypeFilter.length === 0 || idleTypeFilter.length === distinctTypes.length ? "Types" : idleTypeFilter.length === 1 ? (idleTypeFilter[0] === "SERVERLESS" ? "Serverless" : "Classic") : `${idleTypeFilter.length} Types`}
                    <svg className={`h-3 w-3 transition-transform ${idleTypeDropdownOpen ? "rotate-180" : ""}`} fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" /></svg>
                  </button>
                  {idleTypeDropdownOpen && (
                    <div className="absolute right-0 top-full z-[9999] mt-1 min-w-[180px] max-h-64 overflow-y-auto rounded-lg border border-gray-200 bg-white shadow-lg">
                      <div className="sticky top-0 flex items-center justify-between border-b border-gray-100 bg-white px-3 py-2">
                        <span className="text-[10px] font-semibold uppercase tracking-wider text-gray-500">Types</span>
                        <div className="flex items-center gap-2 text-xs">
                          <button onClick={(e) => { e.stopPropagation(); setIdleTypeFilter([...distinctTypes]); setIdlePage(1); }} className="text-gray-500 hover:text-gray-800">All</button>
                          <span className="text-gray-300">·</span>
                          <button onClick={(e) => { e.stopPropagation(); setIdleTypeFilter([]); setIdlePage(1); }} className="text-gray-500 hover:text-gray-800">Clear</button>
                        </div>
                      </div>
                      {distinctTypes.map(t => (
                        <button key={t} onClick={() => { setIdleTypeFilter((prev) => prev.includes(t) ? prev.filter(x => x !== t) : [...prev, t]); setIdlePage(1); }}
                          className="flex w-full items-center gap-2 px-3 py-2.5 text-left text-xs hover:bg-gray-50">
                          <div className={`flex h-4 w-4 shrink-0 items-center justify-center rounded border ${idleTypeFilter.includes(t) ? "border-orange-500 bg-orange-500" : "border-gray-300"}`}>
                            {idleTypeFilter.includes(t) && <svg className="h-3 w-3 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" /></svg>}
                          </div>
                          <span className="truncate text-gray-700">{t === "SERVERLESS" ? "Serverless" : "Classic"}</span>
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              )}
              <div className="relative">
                <svg className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-gray-500" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
                </svg>
                <input
                  type="text"
                  placeholder="Search warehouses..."
                  value={idleSearch}
                  onChange={(e) => { setIdleSearch(e.target.value); setIdlePage(1); }}
                  className="w-44 rounded-full border border-gray-200 bg-white py-1.5 pl-9 pr-4 text-xs placeholder:text-gray-400 focus:border-lava focus:outline-none focus:ring-1 focus:ring-lava"
                />
              </div>
            </div>
          );
        })()}
      </div>

      {isLoading ? (
        <div className="flex h-24 items-center justify-center">
          <div className="h-6 w-6 animate-spin rounded-full border-2 border-gray-200" style={{ borderTopColor: C.lava }} />
        </div>
      ) : idleError ? (
        <SourceCapabilityNotice
          title="Idle time analysis failed to load"
          description={idleError.message}
          onRetry={() => { void refetchIdle(); }}
        />
      ) : data?.available === false && data.reason === "shared_scope_unsupported" ? (
        <SourceCapabilityNotice
          title="Idle-time detail is not included in this source"
          description={data.reason_detail || "No current shared aggregate provides warehouse lifecycle grain. The source must publish the aggregate below."}
          requiredAggregates={["warehouse_idle_summary"]}
        />
      ) : data?.available === false ? (
        <SourceCapabilityNotice
          title="Idle time analysis is temporarily unavailable"
          description={data.error
            ? `Idle time query failed: ${data.error}`
            : "Idle time data unavailable. Requires access to system.compute.warehouse_events and system.query.history."}
          requiredAggregates={["warehouse_idle_summary"]}
        />
      ) : !idleWarehouses.length ? (
        <div className="rounded-lg border border-gray-100 bg-gray-50 p-4 text-sm text-gray-500">
          {data?.serverless_detected
            ? "Idle time via lifecycle events is not available for Serverless SQL Warehouses. Serverless warehouses scale per-query and do not emit start/stop events."
            : "No warehouse uptime data found for this date range."}
        </div>
      ) : (() => {
        const filteredWarehouses = idleWarehouses
          .filter(w => idleSizeFilter.length === 0 || !w.warehouse_size || idleSizeFilter.includes(w.warehouse_size))
          .filter(w => idleTypeFilter.length === 0 || idleTypeFilter.includes(w.warehouse_type))
          .filter(w => !idleSearch || (w.warehouse_name || w.warehouse_id || "").toLowerCase().includes(idleSearch.toLowerCase()));
        const totalIdlePages = Math.max(1, Math.ceil(filteredWarehouses.length / IDLE_PAGE_SIZE));
        const safeIdlePage = Math.min(idlePage, totalIdlePages);
        const pageWarehouses = filteredWarehouses.slice((safeIdlePage - 1) * IDLE_PAGE_SIZE, safeIdlePage * IDLE_PAGE_SIZE);
        return (
          <>
            <div className="overflow-x-auto rounded-lg border border-gray-200">
              <table className="min-w-full divide-y divide-gray-200 text-sm">
                <thead className="bg-gray-50">
                  <tr>
                    <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500">Warehouse</th>
                    <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500">Size</th>
                    <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500">Type</th>
                    <th className="px-4 py-3 text-right text-xs font-medium uppercase tracking-wider text-gray-500">Uptime</th>
                    <th className="px-4 py-3 text-right text-xs font-medium uppercase tracking-wider text-gray-500">Idle Time</th>
                    <th className="px-4 py-3 text-right text-xs font-medium uppercase tracking-wider text-gray-500">Idle %</th>
                    <th className="px-4 py-3 text-right text-xs font-medium uppercase tracking-wider text-gray-500">Total Spend</th>
                    <th className="px-4 py-3 text-right text-xs font-medium uppercase tracking-wider text-gray-500">Est. Idle Spend</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-200 bg-white">
                  {pageWarehouses.length === 0 && idleSearch && (
                    <tr>
                      <td colSpan={8} className="px-4 py-8 text-center text-sm text-gray-500">
                        No warehouses match your search.
                      </td>
                    </tr>
                  )}
                  {pageWarehouses.map((wh, i) => (
                    <tr key={`${wh.warehouse_id}-${i}`} className="hover:bg-gray-50">
                      <td className="px-4 py-3 font-medium text-gray-900">
                        {host && wh.warehouse_id ? (
                          <a href={`${host}/sql/warehouses/${wh.warehouse_id}/edit`} target="_blank" rel="noopener noreferrer" className="text-blue-600 hover:underline">
                            {wh.warehouse_name || wh.warehouse_id}
                          </a>
                        ) : (wh.warehouse_name || wh.warehouse_id)}
                      </td>
                      <td className="px-4 py-3 text-left text-sm text-gray-500">{wh.warehouse_size || "—"}</td>
                      <td className="px-4 py-3 text-left">
                        <span className={`inline-flex rounded-full px-2 py-0.5 text-xs font-medium ${wh.warehouse_type === 'SERVERLESS' ? 'bg-cyan-100 text-cyan-700' : 'bg-gray-100 text-gray-600'}`}>
                          {wh.warehouse_type === 'SERVERLESS' ? 'Serverless' : 'Classic'}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-right text-gray-700">
                        <div className="flex items-center justify-end gap-1.5">
                          <span>{fmtHours(wh.total_running_minutes)}</span>
                          {wh.uptime_source === "billing" && (
                            <span
                              className="inline-flex rounded-full border border-amber-200 bg-amber-50 px-1.5 py-0.5 text-[10px] font-medium text-amber-700"
                              title="Approximate — derived from hourly billing buckets because no lifecycle events were emitted in the window. Sub-hour bursts inflate uptime, which in turn inflates idle time and idle spend."
                            >
                              est.
                            </span>
                          )}
                        </div>
                      </td>
                      <td className="px-4 py-3 text-right text-gray-700">{fmtHours(wh.idle_minutes)}</td>
                      <td className="px-4 py-3 text-right">
                        <div className="flex items-center justify-end gap-1.5">
                          <span
                            className="inline-flex rounded-full px-2 py-0.5 text-xs font-medium"
                            style={wh.idle_pct >= 80 ? { background: C.maroonTint, color: C.maroon } : wh.idle_pct >= 50 ? { background: C.amberTint, color: C.amberInk } : { background: C.oatMed, color: C.slate }}
                          >
                            {wh.idle_pct.toFixed(1)}%
                          </span>
                          {wh.low_confidence && (
                            <span
                              className="inline-flex rounded-full border border-amber-200 bg-amber-50 px-1.5 py-0.5 text-[10px] font-medium text-amber-700"
                              title="Low confidence — serverless with wall-clock uptime above 95% of the window. Almost certainly a keep-alive probe firing under auto_stop_mins, not literal continuous compute. Look at Warm-Hold instead."
                            >
                              low conf.
                            </span>
                          )}
                        </div>
                      </td>
                      <td className="px-4 py-3 text-right text-gray-700">{fmt$(wh.total_spend)}</td>
                      <td className={`px-4 py-3 text-right ${wh.estimated_idle_spend == null ? "text-gray-400" : wh.uptime_source === "billing" ? "text-red-500" : "font-medium text-red-600"}`}>
                        {wh.estimated_idle_spend != null ? (
                          wh.uptime_source === "billing" ? (
                            <span title="Approximate — uptime for this warehouse comes from billing buckets, so the idle-spend proration is directionally noisy.">
                              ~{fmt$(wh.estimated_idle_spend)}
                            </span>
                          ) : (
                            fmt$(wh.estimated_idle_spend)
                          )
                        ) : (
                          <span
                            title={
                              wh.warehouse_type === "SERVERLESS"
                                ? `Serverless — dollar attribution suppressed. Serverless bills per-query with warm-hold at a reduced rate, not full-rate wall-clock. Warm-hold: ${fmtHours(wh.warm_hold_minutes)} at ${wh.auto_stop_mins}m auto_stop_mins. Keep-alive score: ${wh.keep_alive_score.toFixed(1)}%.`
                                : wh.max_num_clusters > 1
                                ? `Multi-cluster (up to ${wh.max_num_clusters} clusters) — wall-clock cluster_count > 0 can't reconstruct concurrent cluster billing. Warm-hold: ${fmtHours(wh.warm_hold_minutes)}.`
                                : wh.uptime_source === "billing"
                                ? "Lifecycle events missing — uptime denominator uncertain, dollar attribution suppressed."
                                : "Suppressed — see Warm-Hold and auto_stop_mins for the actionable metric."
                            }
                          >
                            —
                          </span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {totalIdlePages > 1 && (
              <div className="mt-3 flex items-center justify-between text-xs text-gray-500">
                <span>{filteredWarehouses.length} warehouse{filteredWarehouses.length !== 1 ? "s" : ""}{filteredWarehouses.length !== idleWarehouses.length ? ` of ${idleWarehouses.length}` : ""}</span>
                <div className="flex items-center gap-1">
                  <button onClick={() => setIdlePage((p) => Math.max(1, p - 1))} disabled={safeIdlePage <= 1} className="rounded px-2 py-1 disabled:opacity-40 hover:bg-gray-100">‹ Prev</button>
                  <span className="px-2">Page {safeIdlePage} of {totalIdlePages}</span>
                  <button onClick={() => setIdlePage((p) => Math.min(totalIdlePages, p + 1))} disabled={safeIdlePage >= totalIdlePages} className="rounded px-2 py-1 disabled:opacity-40 hover:bg-gray-100">Next ›</button>
                </div>
              </div>
            )}
          </>
        );
      })()}
    </div>
  );
}
