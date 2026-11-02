import { useCallback, useState, useRef, useEffect } from "react";
import { formatIdentity, useSpNameMap } from "@/utils/identity";
import { workspaceUrl } from "@/utils/formatters";
import type { TaggingDashboardBundle } from "@/types/billing";

type UntaggedItem = {
  workspace_id: string;
  total_dbus: number;
  total_spend: number;
  days_active: number;
} & Record<string, unknown>;

export type UntaggedTab = "all" | "clusters" | "jobs" | "pipelines" | "warehouses" | "endpoints";

const TYPE_LABELS: Record<string, string> = {
  clusters: "Cluster", jobs: "Job", pipelines: "Pipeline", warehouses: "Warehouse", endpoints: "Endpoint",
};

function getClusterUrl(host: string | null | undefined, _clusterId: string, workspaceId?: string): string | null {
  if (!host) return null;
  const workspaceParam = workspaceId ? `?o=${workspaceId}` : '';
  return workspaceUrl(host, `/compute/interactive${workspaceParam}`);
}

function getJobUrl(host: string | null | undefined, jobId: string): string | null {
  if (!host || !jobId) return null;
  return workspaceUrl(host, `/jobs/${jobId}`);
}

function getPipelineUrl(host: string | null | undefined, pipelineId: string): string | null {
  if (!host || !pipelineId) return null;
  return workspaceUrl(host, `/pipelines/${pipelineId}`);
}

function getWarehouseUrl(host: string | null | undefined, warehouseId: string, workspaceId?: string): string | null {
  if (!host || !warehouseId) return null;
  const workspaceParam = workspaceId ? `?o=${workspaceId}` : '';
  return workspaceUrl(host, `/sql/warehouses/${warehouseId}${workspaceParam}`);
}

function getEndpointUrl(host: string | null | undefined, endpointName: string, workspaceId?: string): string | null {
  if (!host || !endpointName) return null;
  const workspaceParam = workspaceId ? `?o=${workspaceId}` : '';
  return workspaceUrl(host, `/ml/endpoints/${endpointName}${workspaceParam}`);
}

const formatCurrency = (value: number) =>
  new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 0, maximumFractionDigits: 0 }).format(value);

const formatNumber = (value: number) =>
  new Intl.NumberFormat("en-US", { minimumFractionDigits: 0, maximumFractionDigits: 0 }).format(value);

interface SuggestedTag { key: string; usageCount: number; examples: string[] }
interface UntaggedCounts { clusters: number; jobs: number; pipelines: number; warehouses: number; endpoints: number }

interface UntaggedResourcesTableProps {
  data: TaggingDashboardBundle;
  host?: string | null;
  suggestedTags: SuggestedTag[];
  untaggedCounts: UntaggedCounts;
  activeUntaggedTab: UntaggedTab;
  onTabChange: (tab: UntaggedTab) => void;
  searchQuery: string;
  onSearchChange: (q: string) => void;
  currentPage: number;
  onPageChange: (p: number) => void;
  sortField: string;
  sortDirection: "asc" | "desc";
  onSort: (field: string) => void;
  showHistoricalUntagged: boolean;
  onHistoricalToggle: (show: boolean) => void;
  itemsPerPage: number;
}

const SUGGESTED_TAGS_KEY = "cost-obs-minimize-suggested-tags";

const ALL_RESOURCE_TYPES: Exclude<UntaggedTab, "all">[] = ["clusters", "jobs", "pipelines", "warehouses", "endpoints"];

export function UntaggedResourcesTable({
  data, host, suggestedTags, untaggedCounts,
  searchQuery, onSearchChange,
  currentPage, onPageChange,
  sortField, sortDirection, onSort,
  showHistoricalUntagged, onHistoricalToggle,
  itemsPerPage,
}: UntaggedResourcesTableProps) {
  const spNameMap = useSpNameMap();

  const [suggestedTagsMinimized, setSuggestedTagsMinimized] = useState(() => {
    if (typeof window !== "undefined") {
      return localStorage.getItem(SUGGESTED_TAGS_KEY) === "true";
    }
    return false;
  });

  // Multi-select resource type filter — all selected by default. Replaces the old single-tab picker.
  const [selectedResourceTypes, setSelectedResourceTypes] = useState<Exclude<UntaggedTab, "all">[]>([...ALL_RESOURCE_TYPES]);
  const [tabDropdownOpen, setTabDropdownOpen] = useState(false);
  const tabDropdownRef = useRef<HTMLDivElement>(null);
  const isAllTypesSelected = selectedResourceTypes.length === ALL_RESOURCE_TYPES.length;
  const singleTypeSelected: Exclude<UntaggedTab, "all"> | null = selectedResourceTypes.length === 1 ? selectedResourceTypes[0] : null;
  const activeUntaggedTab: UntaggedTab = singleTypeSelected ?? "all";
  const toggleResourceType = (t: Exclude<UntaggedTab, "all">) => setSelectedResourceTypes(prev => prev.includes(t) ? prev.filter(x => x !== t) : [...prev, t]);

  useEffect(() => {
    if (!tabDropdownOpen) return;
    const handler = (e: MouseEvent) => {
      if (tabDropdownRef.current && !tabDropdownRef.current.contains(e.target as Node)) {
        setTabDropdownOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [tabDropdownOpen]);

  const handleSuggestedTagsMinimize = useCallback((minimized: boolean) => {
    setSuggestedTagsMinimized(minimized);
    if (minimized) {
      localStorage.setItem(SUGGESTED_TAGS_KEY, "true");
    } else {
      localStorage.removeItem(SUGGESTED_TAGS_KEY);
    }
  }, []);

  const resourceTabs: { key: Exclude<UntaggedTab, "all">; label: string; count: number }[] = [
    { key: "clusters", label: "Clusters", count: untaggedCounts.clusters },
    { key: "jobs", label: "Jobs", count: untaggedCounts.jobs },
    { key: "pipelines", label: "SDP Pipelines", count: untaggedCounts.pipelines },
    { key: "warehouses", label: "SQL Warehouses", count: untaggedCounts.warehouses },
    { key: "endpoints", label: "Endpoints", count: untaggedCounts.endpoints },
  ];

  const getItems = () => {
    // Multiple types selected — return an "all"-style union with a _type discriminator.
    if (!singleTypeSelected) {
      const items: Record<string, unknown>[] = [];
      // Empty selection (from Clear) is treated as "show all" so Clear doesn't hide every row.
      const has = (t: Exclude<UntaggedTab, "all">) => selectedResourceTypes.length === 0 || selectedResourceTypes.includes(t);
      if (has("clusters")) items.push(...(data.untagged.clusters?.items || []).map(i => ({ ...i, _name: i.cluster_name, _id: i.cluster_id, _type: "clusters" })));
      if (has("jobs")) items.push(...(data.untagged.jobs?.items || []).map(i => ({ ...i, _name: i.job_name, _id: i.job_id, _type: "jobs" })));
      if (has("pipelines")) items.push(...(data.untagged.pipelines?.items || []).map(i => ({ ...i, _name: i.pipeline_name, _id: i.pipeline_id, _type: "pipelines" })));
      if (has("warehouses")) items.push(...(data.untagged.warehouses?.items || []).map(i => ({ ...i, _name: i.warehouse_name, _id: i.warehouse_id, _type: "warehouses" })));
      if (has("endpoints")) items.push(...(data.untagged.endpoints?.items || []).map(i => ({ ...i, _name: i.endpoint_name, _id: i.endpoint_name, _type: "endpoints" })));
      return items;
    }
    switch (singleTypeSelected) {
      case "clusters": return data.untagged.clusters?.items || [];
      case "jobs": return data.untagged.jobs?.items || [];
      case "pipelines": return data.untagged.pipelines?.items || [];
      case "warehouses": return data.untagged.warehouses?.items || [];
      case "endpoints": return data.untagged.endpoints?.items || [];
      default: return [];
    }
  };

  const getResourceConfig = () => {
    if (activeUntaggedTab === "all") return { nameKey: "_name", idKey: "_id", label: "Resource" };
    switch (activeUntaggedTab) {
      case "clusters": return { nameKey: "cluster_name", idKey: "cluster_id", label: "Cluster" };
      case "jobs": return { nameKey: "job_name", idKey: "job_id", label: "Job" };
      case "pipelines": return { nameKey: "pipeline_name", idKey: "pipeline_id", label: "Pipeline" };
      case "warehouses": return { nameKey: "warehouse_name", idKey: "warehouse_id", label: "Warehouse" };
      case "endpoints": return { nameKey: "endpoint_name", idKey: "endpoint_name", label: "Endpoint" };
      default: return { nameKey: "", idKey: "", label: "" };
    }
  };

  const getExtraColumns = (): { key: string; label: string }[] => {
    if (activeUntaggedTab === "all") return [{ key: "_type", label: "Type" }];
    if (activeUntaggedTab === "clusters") return [{ key: "owner", label: "Owner" }];
    return [];
  };

  const resourceConfig = getResourceConfig();
  const extraColumns = getExtraColumns();
  const allItems = getItems() as unknown as UntaggedItem[];

  const isHistoricalItem = (item: UntaggedItem) => {
    const effectiveType = activeUntaggedTab === "all" ? (item._type as string) : activeUntaggedTab;
    if (effectiveType === "pipelines" || effectiveType === "endpoints") return false;
    const name = item[resourceConfig.nameKey];
    const id = item[resourceConfig.idKey];
    if (!resourceConfig.idKey || resourceConfig.nameKey === resourceConfig.idKey) return !name;
    return !name || name === id;
  };
  const historicalCount = allItems.filter(isHistoricalItem).length;
  const activeItems = showHistoricalUntagged ? allItems : allItems.filter((item: UntaggedItem) => !isHistoricalItem(item));

  const filteredItems = activeItems.filter((item: UntaggedItem) => {
    if (!searchQuery) return true;
    const query = searchQuery.toLowerCase();
    return Object.values(item).some((val) => typeof val === "string" && val.toLowerCase().includes(query));
  });

  const sortedItems = [...filteredItems].sort((a: UntaggedItem, b: UntaggedItem) => {
    const aVal = a[sortField];
    const bVal = b[sortField];
    const modifier = sortDirection === "asc" ? 1 : -1;
    if (aVal == null && bVal == null) return 0;
    if (aVal == null) return 1;
    if (bVal == null) return -1;
    if (typeof aVal === "string" && typeof bVal === "string") return aVal.localeCompare(bVal) * modifier;
    return ((aVal as number) - (bVal as number)) * modifier;
  });

  const totalPages = Math.ceil(sortedItems.length / itemsPerPage);
  const startIndex = (currentPage - 1) * itemsPerPage;
  const paginatedItems = sortedItems.slice(startIndex, startIndex + itemsPerPage);

  const renderSortIcon = (field: string) => {
    if (sortField !== field) return <span className="ml-1 text-gray-300">↕</span>;
    return <span className="ml-1">{sortDirection === "asc" ? "↑" : "↓"}</span>;
  };

  return (
    <div className="rounded-lg bg-white p-6 border" style={{ borderColor: '#E5E5E5' }}>
      <div className="mb-4 flex items-center justify-between">
        <h3 className="text-lg font-semibold text-gray-900">Untagged Resources</h3>
        <span className="text-sm text-red-600">{formatCurrency(data.summary.untagged_spend)} untagged spend</span>
      </div>

      {suggestedTags.length > 0 && allItems.length > 0 && (
        <div className="mb-3 rounded-lg border border-orange-200 bg-orange-50 px-3 py-2">
          <div className="flex items-start gap-2">
            <svg className="mt-0.5 h-4 w-4 flex-shrink-0 text-orange-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
            </svg>
            <div className="flex-1">
              <button className="flex w-full items-center justify-between" onClick={() => handleSuggestedTagsMinimize(!suggestedTagsMinimized)}>
                <p className="text-xs font-medium text-orange-800">Suggested Tags for Your Environment</p>
                <svg className={`h-3.5 w-3.5 text-orange-500 transition-transform ${suggestedTagsMinimized ? "" : "rotate-180"}`} fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                </svg>
              </button>
              {!suggestedTagsMinimized && (
                <>
                  <p className="mt-0.5 text-[11px] text-orange-700">Based on tags already in use across your resources:</p>
                  <div className="mt-1.5 flex flex-wrap gap-1.5">
                    {suggestedTags.map((tag) => (
                      <div key={tag.key} className="group relative">
                        <span className="inline-flex items-center rounded-full bg-orange-100 px-2 py-0.5 text-xs font-medium text-orange-800 cursor-help">
                          {tag.key}
                        </span>
                        <div className="invisible absolute bottom-full left-0 z-10 mb-2 w-64 rounded-lg bg-gray-900 p-3 text-xs text-white opacity-0 shadow-xl transition-all group-hover:visible group-hover:opacity-100">
                          <p className="font-semibold text-orange-300 mb-1">{tag.key}</p>
                          <p className="text-gray-300 mb-1">Used by {tag.usageCount} resources</p>
                          {tag.examples.length > 0 && (
                            <div>
                              <p className="text-gray-500 text-[10px] uppercase">Example values:</p>
                              <div className="flex flex-wrap gap-1 mt-1">
                                {tag.examples.map((ex, i) => (
                                  <span key={i} className="rounded bg-gray-700 px-1.5 py-0.5">{ex}</span>
                                ))}
                              </div>
                            </div>
                          )}
                        </div>
                      </div>
                    ))}
                  </div>
                </>
              )}
            </div>
          </div>
        </div>
      )}

      <div className="mb-4 flex items-center gap-4">
        <label className="ml-auto flex items-center gap-1.5 text-xs text-gray-500 cursor-pointer whitespace-nowrap">
          <input type="checkbox" checked={showHistoricalUntagged}
            onChange={(e) => { onHistoricalToggle(e.target.checked); onPageChange(1); }}
            className="rounded border-gray-300 text-orange-600 focus:ring-orange-500" />
          Show historical ({historicalCount})
          <span className="relative group ml-0.5">
            <svg className="inline h-3 w-3 text-gray-500 cursor-help" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>
            <span className="absolute bottom-full left-0 mb-1 hidden group-hover:block w-56 rounded-lg bg-gray-900 px-2 py-1.5 text-[10px] text-white shadow-lg z-20">Resources whose names could not be resolved — likely deleted or from inaccessible workspaces</span>
          </span>
        </label>
        <div className="relative" ref={tabDropdownRef}>
          <button
            type="button"
            onClick={() => setTabDropdownOpen((o) => !o)}
            className={`flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium transition-colors ${!isAllTypesSelected ? "border-[#FF3621] text-[#FF3621]" : "border-gray-300 text-gray-700 hover:bg-gray-50"}`}
          >
            {singleTypeSelected
              ? (resourceTabs.find((t) => t.key === singleTypeSelected)?.label ?? "Resources")
              : !isAllTypesSelected
              ? `${selectedResourceTypes.length} Resource Types`
              : "All Resources"}
            <svg className={`h-3 w-3 transition-transform ${tabDropdownOpen ? "rotate-180" : ""}`} fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
            </svg>
          </button>
          {tabDropdownOpen && (
            <div className="absolute left-0 top-full z-[9999] mt-1 min-w-[210px] rounded-lg border border-gray-200 bg-white shadow-lg">
              <div className="sticky top-0 flex items-center justify-between border-b border-gray-100 bg-white px-3 py-2">
                <span className="text-[10px] font-semibold uppercase tracking-wider text-gray-500">Resource Type</span>
                <div className="flex items-center gap-2 text-xs">
                  <button type="button" onClick={(e) => { e.stopPropagation(); setSelectedResourceTypes([...ALL_RESOURCE_TYPES]); }} className="text-gray-500 hover:text-gray-800">All</button>
                  <span className="text-gray-300">·</span>
                  <button type="button" onClick={(e) => { e.stopPropagation(); setSelectedResourceTypes([]); }} className="text-gray-500 hover:text-gray-800">Clear</button>
                </div>
              </div>
              {resourceTabs.map((tab) => (
                <button
                  type="button"
                  key={tab.key}
                  onClick={() => toggleResourceType(tab.key)}
                  className="flex w-full items-center justify-between gap-2 px-3 py-2.5 text-left text-xs hover:bg-gray-50"
                >
                  <div className="flex items-center gap-2">
                    <div className={`flex h-4 w-4 shrink-0 items-center justify-center rounded border ${selectedResourceTypes.includes(tab.key) ? "border-orange-500 bg-orange-500" : "border-gray-300"}`}>
                      {selectedResourceTypes.includes(tab.key) && <svg className="h-3 w-3 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" /></svg>}
                    </div>
                    <span className={selectedResourceTypes.includes(tab.key) ? "font-medium text-gray-900" : "text-gray-700"}>{tab.label}</span>
                  </div>
                  {tab.count > 0 && (
                    <span className="rounded-full bg-gray-100 px-1.5 py-0.5 text-[10px] font-medium text-gray-600">
                      {tab.count >= 1000 ? "1000+" : tab.count}
                    </span>
                  )}
                </button>
              ))}
            </div>
          )}
        </div>
        <div className="relative">
          <svg className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-gray-500" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
          </svg>
          <input
            type="text"
            placeholder="Search resources..."
            value={searchQuery}
            onChange={(e) => { onSearchChange(e.target.value); onPageChange(1); }}
            className="w-44 rounded-full border border-gray-200 bg-white py-1.5 pl-9 pr-4 text-xs placeholder:text-gray-400 focus:border-[#FF3621] focus:outline-none focus:ring-1 focus:ring-[#FF3621]"
          />
          {searchQuery && (
            <button onClick={() => { onSearchChange(""); onPageChange(1); }} className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-500 hover:text-gray-600">
              <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          )}
        </div>
      </div>
      {searchQuery && (
        <p className="mb-2 text-xs text-gray-500">{filteredItems.length} result{filteredItems.length !== 1 ? "s" : ""} found</p>
      )}

      {sortedItems.length > 0 ? (
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-gray-200">
            <thead className="bg-gray-50">
              <tr>
                <th className="cursor-pointer px-6 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500 hover:text-gray-700" onClick={() => onSort(resourceConfig.idKey)}>
                  {resourceConfig.label} {renderSortIcon(resourceConfig.idKey)}
                </th>
                {extraColumns.map((col) => (
                  <th key={col.key} className="cursor-pointer px-6 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500 hover:text-gray-700" onClick={() => onSort(col.key)}>
                    {col.label} {renderSortIcon(col.key)}
                  </th>
                ))}
                <th className="cursor-pointer px-6 py-3 text-right text-xs font-medium uppercase tracking-wider text-gray-500 hover:text-gray-700" onClick={() => onSort("total_dbus")}>
                  DBUs {renderSortIcon("total_dbus")}
                </th>
                <th className="cursor-pointer px-6 py-3 text-right text-xs font-medium uppercase tracking-wider text-gray-500 hover:text-gray-700" onClick={() => onSort("total_spend")}>
                  Spend {renderSortIcon("total_spend")}
                </th>
                <th className="cursor-pointer px-6 py-3 text-right text-xs font-medium uppercase tracking-wider text-gray-500 hover:text-gray-700" onClick={() => onSort("days_active")}>
                  Days Active {renderSortIcon("days_active")}
                </th>
                <th className="px-6 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500">Suggested Tags</th>
                <th className="px-6 py-3 text-center text-xs font-medium uppercase tracking-wider text-gray-500">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-200 bg-white">
              {paginatedItems.map((item: UntaggedItem, idx: number) => {
                let resourceUrl: string | null = null;
                const workspaceId = item.workspace_id;
                const effectiveType = activeUntaggedTab === "all" ? (item._type as string) : activeUntaggedTab;
                switch (effectiveType) {
                  case "clusters": resourceUrl = getClusterUrl(host, item.cluster_id as string, workspaceId); break;
                  case "jobs": resourceUrl = getJobUrl(host, item.job_id as string); break;
                  case "pipelines": resourceUrl = getPipelineUrl(host, item.pipeline_id as string); break;
                  case "warehouses": resourceUrl = getWarehouseUrl(host, item.warehouse_id as string, workspaceId); break;
                  case "endpoints": resourceUrl = getEndpointUrl(host, item.endpoint_name as string, workspaceId); break;
                }

                const rawName = item[resourceConfig.nameKey] as string | null | undefined;
                const displayId = item[resourceConfig.idKey] as string | null | undefined;
                const displayName = rawName || displayId || "-";
                const hasDistinctName = rawName && rawName !== displayId;
                const showId = displayId && (hasDistinctName || effectiveType === "clusters" || effectiveType === "warehouses");

                return (
                  <tr key={idx} className="hover:bg-gray-50">
                    <td className="px-6 py-4 text-sm">
                      {resourceUrl ? (
                        <div className="flex flex-col gap-0.5">
                          <a href={resourceUrl} target="_blank" rel="noopener noreferrer" className="group flex max-w-xs items-center gap-1 truncate font-medium text-[#FF3621] hover:text-[#E02F1C]">
                            <span className="truncate">{displayName}</span>
                            <svg className="h-3 w-3 flex-shrink-0 opacity-0 transition-opacity group-hover:opacity-100" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
                            </svg>
                          </a>
                          {showId && <span className="max-w-xs truncate text-xs text-gray-500">{displayId}</span>}
                        </div>
                      ) : (
                        <div className="flex flex-col gap-0.5">
                          <span className="max-w-xs truncate font-medium text-gray-900">{displayName}</span>
                          {showId && <span className="max-w-xs truncate text-xs text-gray-500">{displayId}</span>}
                        </div>
                      )}
                    </td>
                    {extraColumns.map((col) => {
                      const colVal = item[col.key] as string | null | undefined;
                      return (
                      <td key={col.key} className="px-6 py-4 text-sm text-gray-600">
                        {colVal ? (
                          col.key === "_type" ? (
                            <span className="inline-flex rounded-full bg-gray-100 px-2 py-0.5 text-xs font-medium text-gray-700">
                              {TYPE_LABELS[colVal] || colVal}
                            </span>
                          ) : col.key === "owner" ? (
                            <span className="inline-flex rounded-full bg-gray-100 px-2 py-0.5 text-xs font-medium text-gray-700 max-w-40 truncate" title={colVal}>
                              {formatIdentity(colVal, spNameMap)}
                            </span>
                          ) : (
                            <span className="max-w-40 truncate block" title={colVal}>{colVal}</span>
                          )
                        ) : (
                          <span className="text-xs text-gray-500">-</span>
                        )}
                      </td>
                      );
                    })}
                    <td className="whitespace-nowrap px-6 py-4 text-right text-sm text-gray-500">{formatNumber(item.total_dbus)}</td>
                    <td className="whitespace-nowrap px-6 py-4 text-right text-sm text-gray-900">{formatCurrency(item.total_spend)}</td>
                    <td className="whitespace-nowrap px-6 py-4 text-right text-sm text-gray-500">{item.days_active}</td>
                    <td className="px-6 py-4 text-sm">
                      <div className="flex flex-wrap gap-1">
                        {(() => {
                          const tagMap: Record<string, string[]> = {
                            clusters: ["team", "environment", "project"],
                            jobs: ["pipeline", "owner", "schedule"],
                            pipelines: ["data_domain", "tier", "team"],
                            warehouses: ["department", "cost_center", "environment"],
                            endpoints: ["model", "use_case", "team"],
                          };
                          const key = activeUntaggedTab === "all" ? (item._type as string) : activeUntaggedTab;
                          return (tagMap[key] || ["team", "environment", "project"]).map((tag) => (
                            <span key={tag} className="inline-flex items-center rounded bg-gray-100 px-2 py-0.5 text-xs text-gray-600">{tag}</span>
                          ));
                        })()}
                      </div>
                    </td>
                    <td className="whitespace-nowrap px-6 py-4 text-center">
                      {resourceUrl && (
                        <a href={resourceUrl} target="_blank" rel="noopener noreferrer" className="btn-brand inline-flex items-center gap-1 rounded px-3 py-1.5 text-xs font-medium text-white transition-colors">
                          <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M7 7h.01M7 3h5c.512 0 1.024.195 1.414.586l7 7a2 2 0 010 2.828l-7 7a2 2 0 01-2.828 0l-7-7A1.994 1.994 0 013 12V7a4 4 0 014-4z" />
                          </svg>
                          Add Tag
                        </a>
                      )}
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
                <span className="font-medium">{Math.min(startIndex + itemsPerPage, sortedItems.length)}</span> of{" "}
                <span className="font-medium">{sortedItems.length}</span> resources
              </p>
              <div className="flex gap-2">
                <button onClick={() => onPageChange(Math.max(1, currentPage - 1))} disabled={currentPage === 1} className="rounded border border-gray-300 px-3 py-1 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50">
                  Previous
                </button>
                {Array.from({ length: totalPages }, (_, i) => i + 1)
                  .filter((page) => page === 1 || page === totalPages || (page >= currentPage - 1 && page <= currentPage + 1))
                  .map((page, idx, arr) => {
                    const prevPage = arr[idx - 1];
                    const showEllipsis = prevPage && page - prevPage > 1;
                    return (
                      <span key={page} className="flex items-center">
                        {showEllipsis && <span className="px-2 py-1 text-gray-500">...</span>}
                        <button
                          onClick={() => onPageChange(page)}
                          className={`rounded px-3 py-1 text-sm font-medium ${currentPage === page ? "text-white" : "border border-gray-300 text-gray-700 hover:bg-gray-50"}`}
                          style={currentPage === page ? { backgroundColor: '#FF3621' } : undefined}
                        >
                          {page}
                        </button>
                      </span>
                    );
                  })}
                <button onClick={() => onPageChange(Math.min(totalPages, currentPage + 1))} disabled={currentPage === totalPages} className="rounded border border-gray-300 px-3 py-1 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50">
                  Next
                </button>
              </div>
            </div>
          )}
        </div>
      ) : (
        <div className="flex h-32 items-center justify-center text-gray-500">
          {searchQuery
            ? `No results for "${searchQuery}" in ${activeUntaggedTab}`
            : `No untagged ${activeUntaggedTab} found - great job!`}
        </div>
      )}
    </div>
  );
}
