import { memo, useState, useEffect, useMemo, useRef, useCallback } from "react";
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  Cell,
  LabelList,
} from "recharts";
import type { ProductBreakdownResponse, WorkspaceBreakdown } from "@/types/billing";
import { formatCurrencyCompact as formatCurrency } from "@/utils/formatters";
import { VirtualizedList } from "./VirtualizedList";
import { C } from "@/theme";

// Hoisted formatters — see SKUBreakdown for rationale.
const fmtCurrency = (v: unknown) => formatCurrency(v as number);
const fmtTooltip = (value: number | undefined) => formatCurrency(value ?? 0);
const fmtTooltipLabel = (label: unknown) => `Product: ${label}`;
const fmtYTick = (v: string) => (v.length > 18 ? v.substring(0, 18) + "..." : v);
const TOOLTIP_STYLE = { backgroundColor: C.white, border: `1px solid ${C.hairline}`, borderRadius: "8px" } as const;
const LABEL_STYLE = { fontSize: 11, fill: C.slate } as const;

interface WsRowProps {
  wsId: string;
  wsName: string;
  selected: boolean;
  onToggle: (id: string) => void;
  historical?: boolean;
}
const WsRow = memo(function WsRow({ wsId, wsName, selected, onToggle, historical }: WsRowProps) {
  return (
    <button
      onClick={() => onToggle(wsId)}
      className="flex w-full items-center gap-2 px-3 py-2.5 text-left text-xs hover:bg-gray-50"
    >
      <div className={`flex h-4 w-4 shrink-0 items-center justify-center rounded border ${selected ? "border-orange-500 bg-orange-500" : "border-gray-300"}`}>
        {selected && <svg className="h-3 w-3 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" /></svg>}
      </div>
      <span className="truncate text-gray-700">{wsName}</span>
      {historical && (
        <span className="ml-auto shrink-0 rounded-full border border-amber-200 bg-amber-50 px-1.5 py-0.5 text-[9px] font-medium text-amber-700" title="This workspace no longer exists in the account — data is historical.">
          historical
        </span>
      )}
    </button>
  );
});

interface ProductBreakdownProps {
  data: ProductBreakdownResponse | undefined;
  isLoading: boolean;
  workspaces?: WorkspaceBreakdown[];
  dateRange?: { startDate: string; endDate: string };
  workspaceNameMap?: Record<string, string>;
}

const CATEGORY_COLORS: Record<string, string> = {
  "SQL - DBSQL": C.s2,
  "SQL - Genie": C.s5,
  SQL: C.s2,
  "ETL - Batch": C.s3,
  "ETL - Streaming": C.s3,
  Interactive: C.s4,
  Serverless: C.s3,
  "Model Serving": C.s1,
  "AI Search": C.s5,
  "Fine-Tuning": C.s4,
  "AI Functions": C.s5,
  Other: C.slate,
};

const COLOR_ROTATION = [
  C.s2, C.lava, C.s5, C.s3, C.s4,
  C.s5, C.s1, C.lava, C.s3, C.slate,
  C.s5, C.s4,
];

export const ProductBreakdown = memo(function ProductBreakdown({ data, isLoading, workspaces, dateRange, workspaceNameMap }: ProductBreakdownProps) {
  const allWsIds = useMemo(
    () => (workspaces ?? []).map((w) => String(w.workspace_id)),
    [workspaces],
  );
  const [selectedWorkspaces, setSelectedWorkspaces] = useState<string[]>([]);
  const [wsFilterInitialized, setWsFilterInitialized] = useState(false);
  const [filteredData, setFilteredData] = useState<ProductBreakdownResponse | undefined>(undefined);
  const [filterLoading, setFilterLoading] = useState(false);
  const [dropdownOpen, setDropdownOpen] = useState(false);
  const [wsSearch, setWsSearch] = useState("");
  const dropdownRef = useRef<HTMLDivElement>(null);

  // Seed with all workspaces on first data load
  if (!wsFilterInitialized && allWsIds.length > 0) {
    setWsFilterInitialized(true);
    setSelectedWorkspaces(allWsIds);
  }

  const isAll = selectedWorkspaces.length === allWsIds.length && allWsIds.length > 0;
  const isPartial = selectedWorkspaces.length > 0 && selectedWorkspaces.length < allWsIds.length;
  const isEmpty = selectedWorkspaces.length === 0;

  const wsKey = useMemo(() => [...selectedWorkspaces].sort().join(','), [selectedWorkspaces]);

  const filterKey = `${wsKey}|${dateRange?.startDate ?? ""}|${dateRange?.endDate ?? ""}|${isAll}|${isEmpty}`;
  const [prevFilterKey, setPrevFilterKey] = useState(filterKey);
  if (prevFilterKey !== filterKey) {
    setPrevFilterKey(filterKey);
    if (isAll || isEmpty) {
      setFilteredData(undefined);
      setFilterLoading(false);
    } else {
      setFilterLoading(true);
    }
  }

  useEffect(() => {
    if (isAll || isEmpty) return;

    let cancelled = false;
    const selectedIds = wsKey.split(",");

    const fetchOne = (wsId: string) => {
      const params = new URLSearchParams();
      if (dateRange?.startDate) params.set("start_date", dateRange.startDate);
      if (dateRange?.endDate) params.set("end_date", dateRange.endDate);
      params.set("workspace_id", wsId);
      return fetch(`/api/billing/by-product?${params}`).then((r) => r.json());
    };

    if (selectedIds.length === 1) {
      fetchOne(selectedIds[0])
        .then((json) => { if (!cancelled) { setFilteredData(json); setFilterLoading(false); } })
        .catch(() => { if (!cancelled) setFilterLoading(false); });
    } else {
      // Merge per-workspace results by product category
      Promise.all(selectedIds.map(fetchOne))
        .then((results: ProductBreakdownResponse[]) => {
          if (cancelled) return;
          const merged: Record<string, { total_spend: number; total_dbus: number; workspace_count: number }> = {};
          for (const r of results) {
            for (const p of r.products || []) {
              const key = p.category;
              if (!merged[key]) merged[key] = { total_spend: 0, total_dbus: 0, workspace_count: 0 };
              merged[key].total_spend += p.total_spend || 0;
              merged[key].total_dbus += p.total_dbus || 0;
              merged[key].workspace_count += p.workspace_count || 0;
            }
          }
          const total = Object.values(merged).reduce((s, x) => s + x.total_spend, 0);
          setFilteredData({
            products: Object.entries(merged).map(([category, v]) => ({
              category,
              total_spend: v.total_spend,
              total_dbus: v.total_dbus,
              workspace_count: v.workspace_count,
              percentage: total > 0 ? (v.total_spend / total) * 100 : 0,
            })).sort((a, b) => b.total_spend - a.total_spend),
            total_spend: total,
            start_date: "",
            end_date: "",
          } as ProductBreakdownResponse);
          setFilterLoading(false);
        })
        .catch(() => { if (!cancelled) setFilterLoading(false); });
    }

    return () => { cancelled = true; };
  }, [wsKey, dateRange?.startDate, dateRange?.endDate, isAll, isEmpty]);

  // Close dropdown on outside click; reset search on close so reopening starts fresh
  useEffect(() => {
    if (!dropdownOpen) return;
    const handleClick = (e: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setWsSearch("");
        setDropdownOpen(false);
      }
    };
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, [dropdownOpen]);

  const displayData = useMemo(
    () => isEmpty
      ? ({ products: [], total_spend: 0, start_date: "", end_date: "" } as ProductBreakdownResponse)
      : isAll
      ? data
      : filteredData,
    [isEmpty, isAll, data, filteredData],
  );
  const showLoading = isLoading || filterLoading;

  const selectedWorkspaceName = useMemo(() => {
    if (selectedWorkspaces.length !== 1 || !workspaces) return null;
    const wsId = selectedWorkspaces[0];
    const ws = workspaces.find((w) => String(w.workspace_id) === wsId);
    return workspaceNameMap?.[wsId] || (ws ? (ws.workspace_name || String(ws.workspace_id)) : wsId);
  }, [selectedWorkspaces, workspaces, workspaceNameMap]);

  const toggleWs = useCallback((wsId: string) => {
    setSelectedWorkspaces((prev) => (prev.includes(wsId) ? prev.filter((x) => x !== wsId) : [...prev, wsId]));
  }, []);
  const selectedSet = useMemo(() => new Set(selectedWorkspaces), [selectedWorkspaces]);
  const wsItems = useMemo(
    () => (workspaces || []).map((ws) => {
      const wsId = String(ws.workspace_id);
      const resolvedName = workspaceNameMap?.[wsId] || ws.workspace_name || null;
      return {
        wsId,
        wsName: resolvedName || String(ws.workspace_id),
        historical: ws.historical ?? !resolvedName,
      };
    }),
    [workspaces, workspaceNameMap],
  );
  const filteredWsItems = useMemo(() => {
    if (!wsSearch.trim()) return wsItems;
    const q = wsSearch.toLowerCase();
    return wsItems.filter((it) => it.wsName.toLowerCase().includes(q) || it.wsId.toLowerCase().includes(q));
  }, [wsItems, wsSearch]);

  const workspaceSelector = workspaces && workspaces.length > 1 ? (
    <div className="relative" ref={dropdownRef}>
      <button
        onClick={() => { if (dropdownOpen) setWsSearch(""); setDropdownOpen(!dropdownOpen); }}
        className={`flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium transition-colors ${isPartial ? "border-lava text-lava" : "border-gray-300 text-gray-700 hover:bg-gray-50"}`}
      >
        {selectedWorkspaceName
          ? selectedWorkspaceName
          : isPartial
          ? `${selectedWorkspaces.length} workspaces`
          : "Workspace"}
        <svg className={`h-3 w-3 transition-transform ${dropdownOpen ? "rotate-180" : ""}`} fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </button>
      {dropdownOpen && (
        <div className="absolute right-0 top-full z-[9999] mt-1 w-72 rounded-lg border border-gray-200 bg-white shadow-lg">
          <div className="sticky top-0 flex items-center justify-between border-b border-gray-100 bg-white px-3 py-2">
            <span className="text-[10px] font-semibold uppercase tracking-wider text-gray-500">Workspace</span>
            <div className="flex items-center gap-2 text-xs">
              <button onClick={(e) => { e.stopPropagation(); setSelectedWorkspaces([...allWsIds]); }} className="text-gray-500 hover:text-gray-800">All</button>
              <span className="text-gray-300">·</span>
              <button onClick={(e) => { e.stopPropagation(); setSelectedWorkspaces([]); }} className="text-gray-500 hover:text-gray-800">Clear</button>
            </div>
          </div>
          <div className="border-b border-gray-100 p-2">
            <input
              type="text"
              value={wsSearch}
              onChange={(e) => setWsSearch(e.target.value)}
              placeholder="Search workspaces..."
              className="w-full rounded border border-gray-200 px-2 py-1 text-xs focus:border-lava focus:outline-none focus:ring-1 focus:ring-lava"
              autoFocus
            />
          </div>
          {filteredWsItems.length === 0 ? (
            <div className="px-3 py-2 text-xs text-gray-500">No matching workspaces</div>
          ) : (
            <VirtualizedList
              items={filteredWsItems}
              itemHeight={36}
              maxHeight={256}
              getKey={(it) => it.wsId}
              renderItem={(it) => (
                <WsRow wsId={it.wsId} wsName={it.wsName} historical={it.historical} selected={selectedSet.has(it.wsId)} onToggle={toggleWs} />
              )}
            />
          )}
        </div>
      )}
    </div>
  ) : null;

  // Memoize before early returns so hook count is stable across renders.
  const chartData = useMemo(
    () =>
      [...(displayData?.products ?? [])]
        .sort((a, b) => b.total_spend - a.total_spend)
        .map((p) => ({
          name: p.category,
          total_spend: p.total_spend,
          percentage: p.percentage,
        })),
    [displayData],
  );

  if (showLoading) {
    return (
      <div className="rounded-lg bg-white p-6 border " style={{ borderColor: C.hairline }}>
        <div className="mb-4 flex items-center justify-between">
          <h3 className="text-lg font-semibold text-gray-900">Spend by Product</h3>
          {workspaceSelector}
        </div>
        <div className="flex h-48 flex-col items-center justify-center gap-3">
          <div className="h-12 w-12 animate-spin rounded-full border-4 border-gray-200" style={{ borderTopColor: C.lava }} />
          <p className="text-sm text-gray-500">Loading product breakdown...</p>
        </div>
      </div>
    );
  }

  if (!displayData || displayData.products.length === 0) {
    return (
      <div className="rounded-lg bg-white p-6 border " style={{ borderColor: C.hairline }}>
        <div className="mb-4 flex items-center justify-between">
          <h3 className="text-lg font-semibold text-gray-900">Spend by Product</h3>
          {workspaceSelector}
        </div>
        <div className="flex h-80 flex-col items-center justify-center gap-2" style={{ color: C.slate }}>
          <p className="text-base font-medium">No product breakdown available</p>
          <p className="text-sm">Try expanding the date range to capture more billing data</p>
        </div>
      </div>
    );
  }

  return (
    <div className="animate-fade-in rounded-lg bg-white p-6 border " style={{ borderColor: C.hairline, overflow: 'visible' }}>
      <div className="mb-4 flex items-center justify-between">
        <div>
          <h3 className="text-lg font-semibold text-gray-900">Spend by Product</h3>
          {selectedWorkspaceName && (
            <p className="text-sm text-orange-600 font-medium mt-0.5">
              Filtered to: {selectedWorkspaceName}
            </p>
          )}
        </div>
        {workspaceSelector}
      </div>
      <ResponsiveContainer width="100%" height={320}>
        <BarChart data={chartData} layout="vertical" margin={{ left: 0, right: 70 }}>
          <XAxis type="number" tickFormatter={fmtCurrency} stroke={C.muted} fontSize={12} tickMargin={8} />
          <YAxis
            type="category"
            dataKey="name"
            width={100}
            stroke={C.muted}
            fontSize={12}
            tickMargin={8}
            tickFormatter={fmtYTick}
          />
          <Tooltip
            formatter={fmtTooltip}
            labelFormatter={fmtTooltipLabel}
            contentStyle={TOOLTIP_STYLE}
          />
          <Bar dataKey="total_spend" name="Spend" radius={[0, 4, 4, 0]} isAnimationActive={false}>
            {chartData.map((entry, idx) => (
              <Cell key={entry.name} fill={CATEGORY_COLORS[entry.name] || COLOR_ROTATION[idx % COLOR_ROTATION.length]} />
            ))}
            <LabelList dataKey="total_spend" position="right" formatter={fmtCurrency} style={LABEL_STYLE} />
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
});
