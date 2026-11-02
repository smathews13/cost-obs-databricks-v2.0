import { Fragment, useState, useRef, useEffect, useMemo, memo } from "react";
import { useQuery } from "@tanstack/react-query";
import type { WorkspaceBreakdownResponse } from "@/types/billing";
import { formatCurrency, formatNumber, workspaceUrl } from "@/utils/formatters";
import { formatIdentity, useSpNameMap } from "@/utils/identity";
import { VirtualizedList } from "./VirtualizedList";
import { C } from "@/theme";

interface WorkspaceTableProps {
  data: WorkspaceBreakdownResponse | undefined;
  isLoading: boolean;
  host: string | null | undefined;
  workspaceNameMap?: Record<string, string>;
}

function getWorkspaceUrl(host: string | null | undefined, workspaceId: string): string | null {
  if (!host || !workspaceId) return null;
  return workspaceUrl(host, `/browse/folders/workspace?o=${workspaceId}`);
}

const PRODUCT_LABELS: Record<string, string> = {
  ALL_PURPOSE_COMPUTE: "All-Purpose",
  JOBS_COMPUTE: "Jobs",
  JOBS: "Jobs",
  SQL: "SQL Warehouses",
  DLT: "Spark Declarative Pipelines",
  MODEL_SERVING: "Model Serving",
  INTERACTIVE: "Notebooks",
  VECTOR_SEARCH: "AI Search",
  AI_RUNTIME: "AI Runtime",
  AI_GATEWAY: "Unity Gateway",
  AI_FUNCTIONS: "AI Functions",
  AGENT_EVALUATION: "Agent Eval",
  AGENT_BRICKS: "Agent Bricks",
  FOUNDATION_MODEL_TRAINING: "Fine-Tuning",
  DATABASE: "Catalog/Metastore",
  GENOMICS: "Genomics",
  INFERENCE: "Inference",
};

function formatProductName(raw: string): string {
  return PRODUCT_LABELS[raw] || raw.split("_").map(w => w.charAt(0) + w.slice(1).toLowerCase()).join(" ");
}

export const WorkspaceTable = memo(function WorkspaceTable({ data, isLoading, host, workspaceNameMap }: WorkspaceTableProps) {
  const spNameMap = useSpNameMap();
  // Whether any Delta-shared summary sources are active. This live breakdown is a
  // system-table query (it needs per-product/per-user detail the shared summary MVs
  // don't carry), so it reflects only THIS account's workspaces — a note explains
  // why its totals can trail the MV-backed KPIs when shared sources are present.
  const { data: mvSources } = useQuery<{ sources: { label: string }[] }>({
    queryKey: ["mv-sources"],
    queryFn: () => fetch("/api/settings/mv-sources").then((r) => r.json()).catch(() => ({ sources: [] })),
    staleTime: 60 * 1000,
  });
  const hasSharedSources = (mvSources?.sources?.length ?? 0) > 0;
  const [currentPage, setCurrentPage] = useState(1);
  const [search, setSearch] = useState("");
  const [showHistorical, setShowHistorical] = useState(false);
  const [productFilters, setProductFilters] = useState<string[]>([]);
  const productFiltersSeen = useRef<Set<string>>(new Set());
  const [userFilters, setUserFilters] = useState<string[]>([]);
  const userFiltersSeen = useRef<Set<string>>(new Set());
  const [productDropdownOpen, setProductDropdownOpen] = useState(false);
  const [userDropdownOpen, setUserDropdownOpen] = useState(false);
  const [userSearch, setUserSearch] = useState("");
  const productDropdownRef = useRef<HTMLDivElement>(null);
  const userDropdownRef = useRef<HTMLDivElement>(null);
  // Reset user search when its dropdown closes so reopening starts fresh
  const [prevUserDropdownOpen, setPrevUserDropdownOpen] = useState(userDropdownOpen);
  if (prevUserDropdownOpen !== userDropdownOpen) {
    setPrevUserDropdownOpen(userDropdownOpen);
    if (!userDropdownOpen) setUserSearch("");
  }
  useEffect(() => {
    if (!productDropdownOpen && !userDropdownOpen) return;
    const handler = (e: MouseEvent) => {
      if (productDropdownOpen && productDropdownRef.current && !productDropdownRef.current.contains(e.target as Node)) setProductDropdownOpen(false);
      if (userDropdownOpen && userDropdownRef.current && !userDropdownRef.current.contains(e.target as Node)) setUserDropdownOpen(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [productDropdownOpen, userDropdownOpen]);

  const workspaces = data?.workspaces;
  const allProducts = useMemo(
    () => [...new Set((workspaces || []).flatMap((ws) => ws.top_products || []))].sort(),
    [workspaces],
  );
  const allUsers = useMemo(
    () => [...new Set((workspaces || []).flatMap((ws) => ws.top_users || []))].sort(),
    [workspaces],
  );

  useEffect(() => {
    const seen = productFiltersSeen.current;
    const fresh = allProducts.filter((x) => !seen.has(x));
    if (fresh.length === 0) return;
    setProductFilters((prev) => Array.from(new Set([...prev, ...fresh])));
    fresh.forEach((x) => seen.add(x));
  }, [allProducts]);

  useEffect(() => {
    const seen = userFiltersSeen.current;
    const fresh = allUsers.filter((x) => !seen.has(x));
    if (fresh.length === 0) return;
    setUserFilters((prev) => Array.from(new Set([...prev, ...fresh])));
    fresh.forEach((x) => seen.add(x));
  }, [allUsers]);

  const itemsPerPage = 10;
  if (isLoading) {
    return (
      <div className="rounded-lg bg-white p-6 border " style={{ borderColor: C.hairline }}>
        <div className="flex h-48 flex-col items-center justify-center gap-3">
          <div className="h-12 w-12 animate-spin rounded-full border-4 border-gray-200" style={{ borderTopColor: C.lava }} />
          <p className="text-sm text-gray-500">Loading workspaces...</p>
        </div>
      </div>
    );
  }

  if (!data || !data.workspaces?.length) {
    return (
      <div className="rounded-lg bg-white p-6 border " style={{ borderColor: C.hairline }}>
        <h3 className="mb-4 text-lg font-semibold text-gray-900">
          Spend by Workspace
        </h3>
        <div className="flex h-40 flex-col items-center justify-center gap-2 text-gray-500">
          <p className="text-base font-medium">No workspace data available</p>
          <p className="text-sm">Try adjusting the date range or verify workspace billing is enabled</p>
        </div>
      </div>
    );
  }

  // Drop rows with no real workspace id — a null id stringified upstream renders
  // as a bogus "Workspace None" row; it should never appear in this view.
  const validWorkspaces = data.workspaces.filter((ws) => {
    const id = ws.workspace_id;
    return id != null && !["", "none", "null"].includes(String(id).trim().toLowerCase());
  });
  // Historical = no resolvable display name (neither a live name from workspaceNameMap
  // nor a name in the billing history). Such a workspace no longer exists in the
  // account (deleted/orphaned), so it's hidden by default behind "Show historical" and
  // badged when shown — keeping the default row count aligned with the active-workspace
  // KPI. Mirrors the backend `historical` flag (workspace_name is None).
  const isHistoricalWs = (ws: typeof data.workspaces[0]) =>
    !ws.workspace_id ||
    (typeof ws.historical === "boolean"
      ? ws.historical
      : !(workspaceNameMap?.[ws.workspace_id] || ws.workspace_name));
  const historicalCount = validWorkspaces.filter((ws) => isHistoricalWs(ws)).length;
  // When all workspaces lack names (workspace_name is unavailable), show everything rather than a blank table.
  const allHistorical = historicalCount === validWorkspaces.length;
  const activeWorkspaces = (showHistorical || allHistorical) ? validWorkspaces : validWorkspaces.filter((ws) => !isHistoricalWs(ws));

  // Clear sets the filter to []; treat empty as "show all" (not "hide everything")
  // so the Clear button matches the All button's effect on visible rows.
  const isProductFilterActive = productFilters.length > 0 && productFilters.length < allProducts.length;
  const isUserFilterActive = userFilters.length > 0 && userFilters.length < allUsers.length;
  const filterActive = isProductFilterActive || isUserFilterActive;
  const filteredByDropdowns = filterActive
    ? activeWorkspaces.filter((ws) => {
        const products = ws.top_products || [];
        const users = ws.top_users || [];
        const productMatch = !isProductFilterActive || productFilters.some((p) => products.includes(p));
        const userMatch = !isUserFilterActive || userFilters.some((u) => users.includes(u));
        return productMatch && userMatch;
      })
    : activeWorkspaces;

  const searchLower = search.toLowerCase();
  const filteredWorkspaces = search
    ? filteredByDropdowns.filter((ws) =>
        (workspaceNameMap?.[ws.workspace_id] || ws.workspace_name || "").toLowerCase().includes(searchLower) ||
        ws.workspace_id.toLowerCase().includes(searchLower) ||
        (ws.top_products || []).some((p) => p.toLowerCase().includes(searchLower)) ||
        (ws.top_users || []).some((u) => u.toLowerCase().includes(searchLower))
      )
    : filteredByDropdowns;

  const totalPages = Math.ceil(filteredWorkspaces.length / itemsPerPage);
  const startIndex = (currentPage - 1) * itemsPerPage;
  const endIndex = startIndex + itemsPerPage;
  const paginatedData = filteredWorkspaces.slice(startIndex, endIndex);

  return (
    <div className="animate-fade-in rounded-lg bg-white p-6 border " style={{ borderColor: C.hairline }}>
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <div className="shrink-0">
          <h3 className="text-lg font-semibold text-gray-900">
            Spend by Workspace
          </h3>
          {hasSharedSources && (
            <p className="mt-0.5 text-xs text-gray-500">
              This workspace's account only — shared sources are included in the totals above, not this live breakdown.
            </p>
          )}
        </div>
        <div className="ml-auto flex shrink-0 items-center gap-2">
          {historicalCount > 0 && !allHistorical && (
            <label className="flex items-center gap-1.5 text-xs text-gray-500 cursor-pointer">
              <input
                type="checkbox"
                checked={showHistorical}
                onChange={(e) => { setShowHistorical(e.target.checked); setCurrentPage(1); }}
                className="rounded border-gray-300 text-orange-600 focus:ring-orange-500"
              />
              Show historical ({historicalCount})
                <span className="relative group ml-0.5">
                  <svg className="inline h-3 w-3 text-gray-500 cursor-help" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>
                  <span className="absolute bottom-full left-1/2 -translate-x-1/2 mb-1 hidden group-hover:block w-56 rounded-lg bg-gray-900 px-2 py-1.5 text-[10px] text-white shadow-lg z-20">Workspaces whose names could not be resolved — likely decommissioned or inaccessible</span>
                </span>
            </label>
          )}
          {/* Product filter dropdown */}
          {allProducts.length > 0 && (
            <div className="relative" ref={productDropdownRef}>
              <button
                onClick={() => { setProductDropdownOpen((o) => !o); setUserDropdownOpen(false); }}
                className={`flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium transition-colors ${isProductFilterActive ? "border-lava text-lava" : "border-gray-300 text-gray-700 hover:bg-gray-50"}`}
              >
                {productFilters.length === 1 ? formatProductName(productFilters[0]) : isProductFilterActive ? `${productFilters.length} Products` : "Products"}
                <svg className={`h-3 w-3 transition-transform ${productDropdownOpen ? "rotate-180" : ""}`} fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" /></svg>
              </button>
              {productDropdownOpen && (
                <div className="absolute right-0 top-full z-[9999] mt-1 min-w-[180px] rounded-lg border border-gray-200 bg-white shadow-lg">
                  <div className="sticky top-0 flex items-center justify-between border-b border-gray-100 bg-white px-3 py-2">
                    <span className="text-[10px] font-semibold uppercase tracking-wider text-gray-500">Products</span>
                    <div className="flex items-center gap-2 text-xs">
                      <button onClick={(e) => { e.stopPropagation(); setProductFilters([...allProducts]); setCurrentPage(1); }} className="text-gray-500 hover:text-gray-800">All</button>
                      <span className="text-gray-300">·</span>
                      <button onClick={(e) => { e.stopPropagation(); setProductFilters([]); setCurrentPage(1); }} className="text-gray-500 hover:text-gray-800">Clear</button>
                    </div>
                  </div>
                  <VirtualizedList
                    items={allProducts}
                    itemHeight={36}
                    maxHeight={256}
                    getKey={(p) => p}
                    renderItem={(p) => (
                      <button onClick={() => { setProductFilters((prev) => prev.includes(p) ? prev.filter((x) => x !== p) : [...prev, p]); setCurrentPage(1); }}
                        className="flex w-full items-center gap-2 px-3 py-2.5 text-left text-xs hover:bg-gray-50">
                        <div className={`flex h-4 w-4 shrink-0 items-center justify-center rounded border ${productFilters.includes(p) ? "border-orange-500 bg-orange-500" : "border-gray-300"}`}>
                          {productFilters.includes(p) && <svg className="h-3 w-3 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" /></svg>}
                        </div>
                        <span className="truncate text-gray-700">{formatProductName(p)}</span>
                      </button>
                    )}
                  />
                </div>
              )}
            </div>
          )}
          {/* User filter dropdown */}
          {allUsers.length > 0 && (
            <div className="relative" ref={userDropdownRef}>
              <button
                onClick={() => { setUserDropdownOpen((o) => !o); setProductDropdownOpen(false); }}
                className={`flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium transition-colors ${isUserFilterActive ? "border-lava text-lava" : "border-gray-300 text-gray-700 hover:bg-gray-50"}`}
              >
                {userFilters.length === 1 ? formatIdentity(userFilters[0], spNameMap) : isUserFilterActive ? `${userFilters.length} Users` : "Users"}
                <svg className={`h-3 w-3 transition-transform ${userDropdownOpen ? "rotate-180" : ""}`} fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" /></svg>
              </button>
              {userDropdownOpen && (() => {
                const q = userSearch.trim().toLowerCase();
                const filteredUsers = q
                  ? allUsers.filter((u) => u.toLowerCase().includes(q) || formatIdentity(u, spNameMap).toLowerCase().includes(q))
                  : allUsers;
                return (
                <div className="absolute right-0 top-full z-[9999] mt-1 min-w-[220px] rounded-lg border border-gray-200 bg-white shadow-lg">
                  <div className="sticky top-0 flex items-center justify-between border-b border-gray-100 bg-white px-3 py-2">
                    <span className="text-[10px] font-semibold uppercase tracking-wider text-gray-500">Users</span>
                    <div className="flex items-center gap-2 text-xs">
                      <button onClick={(e) => { e.stopPropagation(); setUserFilters([...allUsers]); setCurrentPage(1); }} className="text-gray-500 hover:text-gray-800">All</button>
                      <span className="text-gray-300">·</span>
                      <button onClick={(e) => { e.stopPropagation(); setUserFilters([]); setCurrentPage(1); }} className="text-gray-500 hover:text-gray-800">Clear</button>
                    </div>
                  </div>
                  <div className="border-b border-gray-100 p-2">
                    <input
                      type="text"
                      value={userSearch}
                      onChange={(e) => setUserSearch(e.target.value)}
                      placeholder="Search users..."
                      className="w-full rounded border border-gray-200 px-2 py-1 text-xs focus:border-lava focus:outline-none focus:ring-1 focus:ring-lava"
                      autoFocus
                    />
                  </div>
                  {filteredUsers.length === 0 ? (
                    <div className="px-3 py-2 text-xs text-gray-500">No matching users</div>
                  ) : (
                    <VirtualizedList
                      items={filteredUsers}
                      itemHeight={36}
                      maxHeight={256}
                      getKey={(u) => u}
                      renderItem={(u) => (
                        <button onClick={() => { setUserFilters((prev) => prev.includes(u) ? prev.filter((x) => x !== u) : [...prev, u]); setCurrentPage(1); }}
                          className="flex w-full items-center gap-2 px-3 py-2.5 text-left text-xs hover:bg-gray-50">
                          <div className={`flex h-4 w-4 shrink-0 items-center justify-center rounded border ${userFilters.includes(u) ? "border-orange-500 bg-orange-500" : "border-gray-300"}`}>
                            {userFilters.includes(u) && <svg className="h-3 w-3 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" /></svg>}
                          </div>
                          <span className="truncate text-gray-700">{formatIdentity(u, spNameMap)}</span>
                        </button>
                      )}
                    />
                  )}
                </div>
                );
              })()}
            </div>
          )}
          <div className="relative">
            <svg className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-gray-500" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
            </svg>
            <input
              type="text"
              placeholder="Search workspaces..."
              value={search}
              onChange={(e) => { setSearch(e.target.value); setCurrentPage(1); }}
              className="w-44 rounded-full border border-gray-200 bg-white py-1.5 pl-9 pr-4 text-xs placeholder:text-gray-400 focus:border-lava focus:outline-none focus:ring-1 focus:ring-lava"
            />
          </div>
        </div>
      </div>
      <div className="overflow-x-auto">
        <table className="min-w-full">
          <thead className="bg-gray-50">
            <tr className="border-b border-gray-200">
              <th className="px-3 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500">
                Workspace
              </th>
              <th className="px-3 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500">
                Top Products
              </th>
              <th className="px-3 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500">
                Top Users
              </th>
              <th className="px-3 py-3 text-right text-xs font-medium uppercase tracking-wider text-gray-500">
                DBUs
              </th>
              <th className="px-3 py-3 text-right text-xs font-medium uppercase tracking-wider text-gray-500">
                Spend
              </th>
              <th className="px-3 py-3 text-right text-xs font-medium uppercase tracking-wider text-gray-500">
                %
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-200">
            {paginatedData.map((ws) => {
              const url = getWorkspaceUrl(host, ws.workspace_id);

              return (
                <tr key={ws.workspace_id} className="hover:bg-gray-50">
                  <td className="px-3 py-3">
                    {url ? (
                      <div className="flex flex-col gap-0.5">
                        <a
                          href={url}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="group flex items-center gap-1 text-sm font-medium text-lava hover:text-lava-hover"
                        >
                          <span>{workspaceNameMap?.[ws.workspace_id] || ws.workspace_name || `Workspace ${ws.workspace_id}`}</span>
                          <svg className="h-3 w-3 flex-shrink-0 opacity-0 transition-opacity group-hover:opacity-100" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
                          </svg>
                        </a>
                        <div className="flex items-center gap-2">
                          <span className="text-xs text-gray-500">{ws.workspace_id}</span>
                          {isHistoricalWs(ws) && <span className="inline-flex rounded-full bg-gray-100 px-2 py-0.5 text-[10px] font-medium text-gray-500">Historical</span>}
                        </div>
                      </div>
                    ) : (
                      <div className="flex flex-col gap-0.5">
                        <span className="text-sm font-medium text-gray-900">{workspaceNameMap?.[ws.workspace_id] || ws.workspace_name || `Workspace ${ws.workspace_id}`}</span>
                        <div className="flex items-center gap-2">
                          <span className="text-xs text-gray-500">{ws.workspace_id}</span>
                          {isHistoricalWs(ws) && <span className="inline-flex rounded-full bg-gray-100 px-2 py-0.5 text-[10px] font-medium text-gray-500">Historical</span>}
                        </div>
                      </div>
                    )}
                  </td>
                <td className="px-3 py-3">
                  <div className="flex flex-wrap gap-1">
                    {(ws.top_products || []).map((p) => (
                      <span key={p} className="inline-flex rounded-full bg-blue-50 px-2 py-0.5 text-xs font-medium text-blue-700" title={p}>
                        {formatProductName(p)}
                      </span>
                    ))}
                    {(!ws.top_products || ws.top_products.length === 0) && (
                      <span className="text-xs text-gray-500">-</span>
                    )}
                  </div>
                </td>
                <td className="px-3 py-3">
                  <div className="flex flex-wrap gap-1">
                    {(ws.top_users || []).map((u) => (
                      <div key={u} className="flex flex-col gap-0.5">
                        <span className="inline-flex rounded-full bg-gray-100 px-2 py-0.5 text-xs font-medium text-gray-700 max-w-35 truncate" title={u}>
                          {formatIdentity(u, spNameMap)}
                        </span>
                      </div>
                    ))}
                    {(!ws.top_users || ws.top_users.length === 0) && (
                      <span className="text-xs text-gray-500">-</span>
                    )}
                  </div>
                </td>
                <td className="whitespace-nowrap px-3 py-3 text-right text-sm text-gray-500">
                  {formatNumber(ws.total_dbus)}
                </td>
                <td className="whitespace-nowrap px-3 py-3 text-right text-sm text-gray-900">
                  {formatCurrency(ws.total_spend)}
                </td>
                <td className="whitespace-nowrap px-3 py-3 text-right text-sm text-gray-500">
                  <div className="flex items-center justify-end gap-2">
                    <div className="h-2 w-16 overflow-hidden rounded-full bg-gray-200">
                      <div
                        className="h-full rounded-full"
                        style={{ width: `${Math.min(ws.percentage, 100)}%`, backgroundColor: C.s2 }}
                      />
                    </div>
                    <span className="w-12 text-right">
                      {(ws.percentage ?? 0).toFixed(1)}%
                    </span>
                  </div>
                </td>
              </tr>
              );
            })}
          </tbody>
        </table>
        {totalPages > 1 && (
          <div className="mt-4 flex items-center justify-between border-t border-gray-200 pt-4">
            <p className="text-sm text-gray-700">
              Showing <span className="font-medium">{startIndex + 1}</span> to{" "}
              <span className="font-medium">{Math.min(endIndex, filteredWorkspaces.length)}</span> of{" "}
              <span className="font-medium">{filteredWorkspaces.length}</span> workspaces
            </p>
            <div className="flex gap-2">
              <button
                onClick={() => setCurrentPage(Math.max(1, currentPage - 1))}
                disabled={currentPage === 1}
                className="rounded border border-gray-300 px-3 py-1 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50"
              >
                Previous
              </button>
              {Array.from({ length: totalPages }, (_, i) => i + 1)
                .filter((page) => {
                  // Show first, last, current, and pages around current
                  return (
                    page === 1 ||
                    page === totalPages ||
                    (page >= currentPage - 1 && page <= currentPage + 1)
                  );
                })
                .map((page, idx, arr) => {
                  // Add ellipsis if there's a gap
                  const prevPage = arr[idx - 1];
                  const showEllipsis = prevPage && page - prevPage > 1;
                  return (
                    <Fragment key={page}>
                      {showEllipsis && (
                        <span className="px-2 py-1 text-gray-500">
                          ...
                        </span>
                      )}
                      <button
                        onClick={() => setCurrentPage(page)}
                        className={`rounded px-3 py-1 text-sm font-medium ${
                          currentPage === page
                            ? "text-white"
                            : "border border-gray-300 text-gray-700 hover:bg-gray-50"
                        }`}
                        style={currentPage === page ? { backgroundColor: C.lava } : undefined}
                      >
                        {page}
                      </button>
                    </Fragment>
                  );
                })}
              <button
                onClick={() => setCurrentPage(Math.min(totalPages, currentPage + 1))}
                disabled={currentPage === totalPages}
                className="rounded border border-gray-300 px-3 py-1 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50"
              >
                Next
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
});
