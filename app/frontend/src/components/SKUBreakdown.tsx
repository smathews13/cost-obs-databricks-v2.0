import { useMemo, useState, useEffect, useRef, useCallback, memo } from "react";
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  Cell,
  ResponsiveContainer,
  LabelList,
} from "recharts";
import type { SKUBreakdownResponse, WorkspaceBreakdown } from "@/types/billing";
import { formatCurrency } from "@/utils/formatters";
import { VirtualizedList } from "./VirtualizedList";
import { C } from "@/theme";

interface SKUBreakdownProps {
  data: SKUBreakdownResponse | undefined;
  isLoading: boolean;
  workspaces?: WorkspaceBreakdown[];
  dateRange?: { startDate: string; endDate: string };
  workspaceNameMap?: Record<string, string>;
}

const fmtCurrency = (v: unknown) => formatCurrency(v as number);
const fmtTooltip = (value: number | undefined) => formatCurrency(value ?? 0);
const fmtTooltipLabel = (label: unknown) => `SKU: ${label}`;
const fmtYTick = (v: string) => (v.length > 22 ? v.substring(0, 20) + "…" : v);
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

export function SKUBreakdown({ data, isLoading, workspaces, dateRange, workspaceNameMap }: SKUBreakdownProps) {
  const [workspaceFilters, setWorkspaceFilters] = useState<string[]>([]);
  const workspaceFiltersSeen = useRef<Set<string>>(new Set());
  const [filteredData, setFilteredData] = useState<SKUBreakdownResponse | undefined>(undefined);
  const [filterLoading, setFilterLoading] = useState(false);
  const [dropdownOpen, setDropdownOpen] = useState(false);
  const [wsSearch, setWsSearch] = useState("");
  const dropdownRef = useRef<HTMLDivElement>(null);

  const allWorkspaceIds = useMemo(
    () => (workspaces || []).map((ws) => String(ws.workspace_id)),
    [workspaces],
  );

  // Sync-add: unseen workspaces get added to the filter automatically, preserving user
  // unselections of items already seen. First load acts as default-all init.
  useEffect(() => {
    const seen = workspaceFiltersSeen.current;
    const fresh = allWorkspaceIds.filter((x) => !seen.has(x));
    if (fresh.length === 0) return;
    setWorkspaceFilters((prev) => Array.from(new Set([...prev, ...fresh])));
    fresh.forEach((x) => seen.add(x));
  }, [allWorkspaceIds]);

  // Clear sets the filter to []; length === 1 routes through the scoped endpoint,
  // otherwise (empty or partial multi-select) we show the account-wide view. So
  // an empty selection is functionally "all" — don't flag it as an active filter.
  const isWorkspaceFilterActive = workspaceFilters.length > 0 && workspaceFilters.length < allWorkspaceIds.length;

  // Only route through the workspace-scoped endpoint when the user has selected exactly one;
  // "all selected" (default) and any partial multi-selection use the account-wide payload.
  const selectedWorkspace = workspaceFilters.length === 1 ? workspaceFilters[0] : "all";

  const filterKey = `${selectedWorkspace}|${dateRange?.startDate ?? ""}|${dateRange?.endDate ?? ""}`;
  const [prevFilterKey, setPrevFilterKey] = useState(filterKey);
  if (prevFilterKey !== filterKey) {
    setPrevFilterKey(filterKey);
    if (selectedWorkspace === "all") {
      setFilteredData(undefined);
      setFilterLoading(false);
    } else {
      setFilterLoading(true);
    }
  }

  useEffect(() => {
    if (selectedWorkspace === "all") return;

    const params = new URLSearchParams();
    if (dateRange?.startDate) params.set("start_date", dateRange.startDate);
    if (dateRange?.endDate) params.set("end_date", dateRange.endDate);
    params.set("workspace_id", selectedWorkspace);

    const controller = new AbortController();
    fetch(`/api/billing/sku-breakdown?${params}`, { signal: controller.signal })
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      })
      .then((json) => {
        if (controller.signal.aborted) return;
        setFilteredData(json);
        setFilterLoading(false);
      })
      .catch(() => {
        if (controller.signal.aborted) return;
        setFilteredData(undefined);
        setFilterLoading(false);
      });
    return () => controller.abort();
  }, [selectedWorkspace, dateRange?.startDate, dateRange?.endDate]);

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

  const displayData = selectedWorkspace === "all" ? data : filteredData;
  const showLoading = isLoading || filterLoading;

  const selectedWorkspaceName = useMemo(() => {
    if (workspaceFilters.length !== 1 || !workspaces) return null;
    const wsId = workspaceFilters[0];
    const ws = workspaces.find((w) => String(w.workspace_id) === wsId);
    return workspaceNameMap?.[wsId] || (ws ? (ws.workspace_name || String(ws.workspace_id)) : wsId);
  }, [workspaceFilters, workspaces, workspaceNameMap]);

  // Stable toggle callback so memoized WsRow doesn't re-render every keystroke elsewhere.
  const toggleWs = useCallback((wsId: string) => {
    setWorkspaceFilters((prev) => (prev.includes(wsId) ? prev.filter((x) => x !== wsId) : [...prev, wsId]));
  }, []);
  const selectedSet = useMemo(() => new Set(workspaceFilters), [workspaceFilters]);

  const wsItems = useMemo(
    () => (workspaces || []).map((ws) => {
      const wsId = String(ws.workspace_id);
      const resolvedName = workspaceNameMap?.[wsId] || ws.workspace_name || null;
      return { wsId, wsName: resolvedName || wsId, historical: ws.historical ?? !resolvedName };
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
        className={`flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium transition-colors ${isWorkspaceFilterActive ? "border-lava text-lava" : "border-gray-300 text-gray-700 hover:bg-gray-50"}`}
      >
        {workspaceFilters.length === 1
          ? (selectedWorkspaceName || workspaceFilters[0])
          : isWorkspaceFilterActive
          ? `${workspaceFilters.length} Workspaces`
          : "Workspaces"}
        <svg className={`h-3 w-3 transition-transform ${dropdownOpen ? 'rotate-180' : ''}`} fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </button>
      {dropdownOpen && (
        <div className="absolute right-0 top-full z-[9999] mt-1 min-w-[220px] rounded-lg border border-gray-200 bg-white shadow-lg">
          <div className="sticky top-0 flex items-center justify-between border-b border-gray-100 bg-white px-3 py-2">
            <span className="text-[10px] font-semibold uppercase tracking-wider text-gray-500">Workspaces</span>
            <div className="flex items-center gap-2 text-xs">
              <button onClick={(e) => { e.stopPropagation(); setWorkspaceFilters((workspaces || []).map(ws => String(ws.workspace_id))); }} className="text-gray-500 hover:text-gray-800">All</button>
              <span className="text-gray-300">·</span>
              <button onClick={(e) => { e.stopPropagation(); setWorkspaceFilters([]); }} className="text-gray-500 hover:text-gray-800">Clear</button>
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

  const barData = useMemo(() => {
    if (!displayData?.skus?.length) return [];
    return [...displayData.skus]
      .sort((a, b) => b.total_spend - a.total_spend)
      .slice(0, 10)
      .map((sku) => {
        const stripped = (sku.product ?? "").replace(/^(PREMIUM_|STANDARD_|ENTERPRISE_)/i, "");
        const parts = stripped.split("_");
        const label = parts.length > 4 ? parts.slice(0, 4).join(" ") + "…" : stripped.replace(/_/g, " ");
        return { name: label, total_spend: sku.total_spend };
      });
  }, [displayData]);

  if (showLoading) {
    return (
      <div className="rounded-lg bg-white p-6 border " style={{ borderColor: C.hairline }}>
        <div className="mb-4 flex items-center justify-between">
          <h3 className="text-lg font-semibold text-gray-900">Spend by SKU</h3>
          {workspaceSelector}
        </div>
        <div className="flex h-48 flex-col items-center justify-center gap-3">
          <div className="h-12 w-12 animate-spin rounded-full border-4 border-gray-200" style={{ borderTopColor: C.lava }} />
          <p className="text-sm text-gray-500">Loading SKU breakdown...</p>
        </div>
      </div>
    );
  }

  if (!displayData || !displayData.skus?.length) {
    return (
      <div className="rounded-lg bg-white p-6 border " style={{ borderColor: C.hairline }}>
        <div className="mb-4 flex items-center justify-between">
          <h3 className="text-lg font-semibold text-gray-900">Spend by SKU</h3>
          {workspaceSelector}
        </div>
        <div className="flex h-80 flex-col items-center justify-center gap-2 text-gray-500">
          <p className="text-base font-medium">No SKU data available</p>
          <p className="text-sm">Try expanding the date range</p>
        </div>
      </div>
    );
  }

  return (
    <div className="animate-fade-in rounded-lg bg-white p-6 border " style={{ borderColor: C.hairline }}>
      <div className="mb-4 flex items-center justify-between">
        <div>
          <h3 className="text-lg font-semibold text-gray-900">Spend by SKU</h3>
          {selectedWorkspaceName && (
            <p className="text-sm text-orange-600 font-medium mt-0.5">
              Filtered to: {selectedWorkspaceName}
            </p>
          )}
          {workspaceFilters.length > 1 && isWorkspaceFilterActive && (
            <p className="text-xs text-amber-600 mt-1">Showing aggregate view — select one workspace to filter by workspace</p>
          )}
        </div>
        {workspaceSelector}
      </div>
      <ResponsiveContainer width="100%" height={320}>
        <BarChart data={barData} layout="vertical" margin={{ left: -25, right: 70 }}>
          <XAxis type="number" tickFormatter={fmtCurrency} stroke={C.muted} fontSize={12} tickMargin={8} />
          <YAxis
            type="category"
            dataKey="name"
            width={175}
            stroke={C.muted}
            fontSize={11}
            tickMargin={2}
            tickFormatter={fmtYTick}
          />
          <Tooltip
            formatter={fmtTooltip}
            labelFormatter={fmtTooltipLabel}
            contentStyle={TOOLTIP_STYLE}
          />
          <Bar dataKey="total_spend" name="Spend" radius={[0, 4, 4, 0]} isAnimationActive={false}>
            {barData.map((_entry, idx) => (
              <Cell key={idx} fill={idx === 0 ? C.s1 : C.s2} />
            ))}
            <LabelList dataKey="total_spend" position="right" formatter={fmtCurrency} style={LABEL_STYLE} />
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
