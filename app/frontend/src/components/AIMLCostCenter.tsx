import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { VirtualizedList } from "./VirtualizedList";
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  PieChart,
  Pie,
  Cell,
  Legend,
  type PieLabelRenderProps,
} from "recharts";
import type { AIMLAgentBrick, AIMLCluster, AIMLDashboardBundle } from "@/types/billing";
import { KPITrendModal } from "./KPITrendModal";
import { KPICard } from "@/components/ui/KPICard";
import { formatIdentity, useSpNameMap } from "@/utils/identity";
import { C } from "@/theme";
import { PageHero, InfoPanel } from "@/components/brand";

interface AIMLCostCenterProps {
  data: AIMLDashboardBundle | undefined;
  isLoading: boolean;
  startDate?: string;
  endDate?: string;
  host?: string | null;
  workspaceIds?: string[];
  workspaceNameMap?: Record<string, string>;
  /** When rendered inside the Unity Gateway "Cost" sub-tab, the parent already shows
   *  the page title — hide this component's own "AI/ML" header to avoid a duplicate. */
  embedded?: boolean;
}

// Stable category-to-color mapping for consistent colors across pie + timeseries
const CATEGORY_COLORS: Record<string, string> = {
  "Serverless Inference": C.s5,
  "Model Training": C.s2,
  "Feature Engineering": C.s3,
  "GPU Clusters": C.lava,
  "Model Serving": C.s1,
  "MLflow": C.s5,
  "OpenAI": C.s3,
  "Anthropic": C.s4,
  "Gemini": C.slate,
  "AI Search": C.s5,
  "Fine Tuning": C.s1,
};
type AgentBrickRow = AIMLAgentBrick & {
  served_entity_name?: string | null;
  billing_origin_product?: string | null;
};
type MLClusterRow = AIMLCluster & { workspace_id?: string | null };

const FALLBACK_COLORS = [C.s2, C.s5, C.s3, C.s3, C.s4, C.s5, C.s1, C.lava, C.slate];
const formatCurrency = (value: number) =>
  new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 0,
    maximumFractionDigits: 0,
  }).format(value);

const formatNumber = (value: number) =>
  new Intl.NumberFormat("en-US", {
    minimumFractionDigits: 0,
    maximumFractionDigits: 0,
  }).format(value);

function buildUrl(host: string | null | undefined, path: string): string | null {
  if (!host) return null;
  const base = host.replace(/^https?:\/\//, '').replace(/\/$/, '');
  return `https://${base}${path}`;
}

function getEndpointUrl(host: string | null | undefined, workspaceId?: string | null): string | null {
  const wsParam = workspaceId ? `?o=${workspaceId}` : '';
  return buildUrl(host, `/ml/endpoints${wsParam}`);
}

function getModelUrl(host: string | null | undefined, workspaceId?: string | null): string | null {
  const wsParam = workspaceId ? `?o=${workspaceId}` : '';
  return buildUrl(host, `/ml/models${wsParam}`);
}

function getClusterUrl(host: string | null | undefined, clusterId: string, workspaceId: string | null): string | null {
  if (!clusterId) return null;
  const wsParam = workspaceId ? `?o=${workspaceId}` : '';
  return buildUrl(host, `/compute/interactive${wsParam}`);
}

export function AIMLCostCenter({ data, isLoading, startDate, endDate, host, workspaceIds, workspaceNameMap, embedded = false }: AIMLCostCenterProps) {
  const spNameMap = useSpNameMap();
  const [endpointsPage, setEndpointsPage] = useState(1);
  const [modelsPage, setModelsPage] = useState(1);
  const [modelsTypeFilters, setModelsTypeFilters] = useState<string[]>([]);
  const modelsTypeSeen = useRef<Set<string>>(new Set());
  const [modelsTypeDropdownOpen, setModelsTypeDropdownOpen] = useState(false);
  const [selectedAgent, setSelectedAgent] = useState<import("@/types/billing").AIMLAgentBrick | null>(null);
  const [showHistoricalAgents, setShowHistoricalAgents] = useState(false);
  const [agentTypeFilter, setAgentTypeFilter] = useState<string[]>([]);
  const agentTypeSeen = useRef<Set<string>>(new Set());
  const [agentsPage, setAgentsPage] = useState(1);
  const [mlClustersPage, setMlClustersPage] = useState(1);
  const [showHistoricalMlClusters, setShowHistoricalMlClusters] = useState(false);
  const [mlClusterSearch, setMlClusterSearch] = useState("");
  const [mlRuntimeFilter, setMlRuntimeFilter] = useState<string[]>([]);
  const mlRuntimeSeen = useRef<Set<string>>(new Set());
  const [endpointsCostTypeFilter, setEndpointsCostTypeFilter] = useState<string | null>(null);
  const [endpointsWorkspaceFilter, setEndpointsWorkspaceFilter] = useState<string[]>([]);
  const endpointsWorkspaceSeen = useRef<Set<string>>(new Set());
  const [endpointsWorkspaceDropdownOpen, setEndpointsWorkspaceDropdownOpen] = useState(false);
  const [endpointsWorkspaceSearch, setEndpointsWorkspaceSearch] = useState("");
  const [endpointsCostTypeDropdownOpen, setEndpointsCostTypeDropdownOpen] = useState(false);
  const [mlRuntimeFilterOpen, setMlRuntimeFilterOpen] = useState(false);
  const mlRuntimeFilterRef = useRef<HTMLDivElement>(null);
  const endpointsWorkspaceFilterRef = useRef<HTMLDivElement>(null);
  const endpointsCostTypeFilterRef = useRef<HTMLDivElement>(null);
  const modelsTypeDropdownRef = useRef<HTMLDivElement>(null);
  const [agentSearch, setAgentSearch] = useState("");
  const [agentTypeDropdownOpen, setAgentTypeDropdownOpen] = useState(false);
  const agentTypeDropdownRef = useRef<HTMLDivElement>(null);
  const PAGE_SIZE = 10;

  // Info box minimize state with localStorage persistence
  const MINIMIZE_KEY = "cost-obs-minimize-aiml-info";
  const [infoMinimized, setInfoMinimized] = useState(() => {
    if (typeof window !== "undefined") {
      return localStorage.getItem(MINIMIZE_KEY) === "true";
    }
    return false;
  });

  const [selectedKPI, setSelectedKPI] = useState<{kpi: string; label: string} | null>(null);

  // Close runtime filter dropdown on outside click
  useEffect(() => {
    if (!mlRuntimeFilterOpen) return;
    const handleClick = (e: MouseEvent) => {
      if (mlRuntimeFilterRef.current && !mlRuntimeFilterRef.current.contains(e.target as Node)) {
        setMlRuntimeFilterOpen(false);
      }
    };
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, [mlRuntimeFilterOpen]);

  // Available lists, memoized so effects run only when the underlying data changes.
  const availableMlRuntimes = useMemo(
    () => Array.from(new Set((data?.ml_clusters?.clusters || []).map(c => c.runtime_version).filter(Boolean))) as string[],
    [data?.ml_clusters?.clusters],
  );
  const availableAgentTypes = useMemo(
    () => Array.from(new Set((data?.agent_bricks?.agents || []).map(a => a.agent_type || "Agent"))),
    [data?.agent_bricks?.agents],
  );
  const availableModelTypes = useMemo(
    () => Array.from(new Set((data?.models?.models || []).map(m => m.model_type))).filter(Boolean) as string[],
    [data?.models?.models],
  );

  // Sync-add pattern: unseen items get added to the filter, preserving user unselections
  // of previously-seen items. On first data load this behaves the same as default-all init.
  useEffect(() => {
    const seen = mlRuntimeSeen.current;
    const fresh = availableMlRuntimes.filter(x => !seen.has(x));
    if (fresh.length === 0) return;
    setMlRuntimeFilter(prev => Array.from(new Set([...prev, ...fresh])));
    fresh.forEach(x => seen.add(x));
  }, [availableMlRuntimes]);

  useEffect(() => {
    const seen = agentTypeSeen.current;
    const fresh = availableAgentTypes.filter(x => !seen.has(x));
    if (fresh.length === 0) return;
    setAgentTypeFilter(prev => Array.from(new Set([...prev, ...fresh])));
    fresh.forEach(x => seen.add(x));
  }, [availableAgentTypes]);

  useEffect(() => {
    const seen = modelsTypeSeen.current;
    const fresh = availableModelTypes.filter(x => !seen.has(x));
    if (fresh.length === 0) return;
    setModelsTypeFilters(prev => Array.from(new Set([...prev, ...fresh])));
    fresh.forEach(x => seen.add(x));
  }, [availableModelTypes]);

  const availableEndpointWorkspaces = useMemo(() => {
    const ids = new Set<string>();
    for (const e of data?.endpoints?.endpoints || []) {
      if (e.workspace_id && e.endpoint_name && e.endpoint_name !== 'UNKNOWN') ids.add(e.workspace_id);
    }
    return Array.from(ids);
  }, [data?.endpoints?.endpoints]);

  useEffect(() => {
    const seen = endpointsWorkspaceSeen.current;
    const fresh = availableEndpointWorkspaces.filter(x => !seen.has(x));
    if (fresh.length === 0) return;
    setEndpointsWorkspaceFilter(prev => Array.from(new Set([...prev, ...fresh])));
    fresh.forEach(x => seen.add(x));
  }, [availableEndpointWorkspaces]);

  useEffect(() => {
    if (!endpointsWorkspaceDropdownOpen) return;
    const handleClick = (e: MouseEvent) => {
      if (endpointsWorkspaceFilterRef.current && !endpointsWorkspaceFilterRef.current.contains(e.target as Node)) {
        setEndpointsWorkspaceDropdownOpen(false);
        setEndpointsWorkspaceSearch("");
      }
    };
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, [endpointsWorkspaceDropdownOpen]);

  useEffect(() => {
    if (!endpointsCostTypeDropdownOpen) return;
    const handleClick = (e: MouseEvent) => {
      if (endpointsCostTypeFilterRef.current && !endpointsCostTypeFilterRef.current.contains(e.target as Node)) {
        setEndpointsCostTypeDropdownOpen(false);
      }
    };
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, [endpointsCostTypeDropdownOpen]);

  useEffect(() => {
    if (!modelsTypeDropdownOpen) return;
    const handleClick = (e: MouseEvent) => {
      if (modelsTypeDropdownRef.current && !modelsTypeDropdownRef.current.contains(e.target as Node)) {
        setModelsTypeDropdownOpen(false);
      }
    };
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, [modelsTypeDropdownOpen]);

  useEffect(() => {
    if (!agentTypeDropdownOpen) return;
    const handleClick = (e: MouseEvent) => {
      if (agentTypeDropdownRef.current && !agentTypeDropdownRef.current.contains(e.target as Node)) {
        setAgentTypeDropdownOpen(false);
      }
    };
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, [agentTypeDropdownOpen]);

  // Pre-warm trend queries so modals open instantly
  const queryClient = useQueryClient();
  useEffect(() => {
    if (!startDate || !endDate) return;
    for (const kpi of ["aiml_spend", "aiml_dbus", "aiml_endpoints", "aiml_avg_endpoint_cost"]) {
      queryClient.prefetchQuery({
        queryKey: ["kpi-trend", kpi, startDate, endDate, "daily"],
        queryFn: async () => {
          const params = new URLSearchParams({ kpi, start_date: startDate, end_date: endDate, granularity: "daily" });
          const res = await fetch(`/api/billing/kpi-trend?${params}`);
          if (!res.ok) throw new Error("prefetch failed");
          return res.json();
        },
        staleTime: 5 * 60 * 1000,
      });
    }
  }, [startDate, endDate, queryClient]);

  const handleMinimizeToggle = (checked: boolean) => {
    setInfoMinimized(checked);
    if (checked) {
      localStorage.setItem(MINIMIZE_KEY, "true");
    } else {
      localStorage.removeItem(MINIMIZE_KEY);
    }
  };

  // Build a single stable color map from ALL category names so pie + timeseries match
  const categoryColorMap = useMemo(() => {
    const map: Record<string, string> = {};
    let fallbackIdx = 0;
    const allNames = new Set<string>();
    // Gather from pie categories (strip "FMAPI - " prefix)
    for (const cat of data?.categories?.categories || []) allNames.add(cat.category.replace(/^FMAPI\s*-\s*/, ""));
    // Gather from timeseries categories
    for (const cat of data?.timeseries?.categories || []) allNames.add(cat);
    // Assign colors: known names get their fixed color, others get fallback in stable order
    for (const name of allNames) {
      if (CATEGORY_COLORS[name]) {
        map[name] = CATEGORY_COLORS[name];
      } else {
        map[name] = FALLBACK_COLORS[fallbackIdx % FALLBACK_COLORS.length];
        fallbackIdx++;
      }
    }
    return map;
  }, [data?.categories, data?.timeseries]);

  const categoriesData = data?.categories;
  const pieData = useMemo(() => {
    if (!categoriesData?.categories) return [];
    return categoriesData.categories.map((cat) => {
      const name = cat.category.replace(/^FMAPI\s*-\s*/, "");
      return {
        name,
        value: cat.total_spend,
        percentage: cat.percentage ?? 0,
        fill: categoryColorMap[name] || FALLBACK_COLORS[0],
      };
    });
  }, [categoriesData, categoryColorMap]);

  const endpointsBundle = data?.endpoints;
  const endpointWorkspaceNames = useMemo(() => {
    const map: Record<string, string> = {};
    for (const e of endpointsBundle?.endpoints || []) {
      if (e.workspace_id && e.workspace_name) map[e.workspace_id] = e.workspace_name;
    }
    return map;
  }, [endpointsBundle]);

  const resolveWsName = useCallback(
    (wsId: string) => workspaceNameMap?.[wsId] || endpointWorkspaceNames[wsId] || `Workspace ${wsId}`,
    [workspaceNameMap, endpointWorkspaceNames],
  );

  const endpointWorkspaces = useMemo(() => {
    if (!endpointsBundle?.endpoints) return [];
    const ids = new Set<string>();
    for (const e of endpointsBundle.endpoints) {
      if (e.workspace_id && e.endpoint_name && e.endpoint_name !== 'UNKNOWN') ids.add(e.workspace_id);
    }
    return Array.from(ids).sort((a, b) => resolveWsName(a).localeCompare(resolveWsName(b)));
  }, [endpointsBundle, resolveWsName]);

  const endpointsWorkspaceFiltered = useMemo(() => {
    if (!endpointsBundle?.endpoints) return [];
    const rows = endpointsBundle.endpoints.filter(e => e.endpoint_name && e.endpoint_name !== 'UNKNOWN');
    // Empty selection (from Clear) is treated as "show all" so Clear doesn't
    // silently hide every endpoint. Only apply the workspace filter when there
    // is an actual partial selection.
    if (endpointsWorkspaceFilter.length === 0) return rows;
    return rows.filter(e => !!e.workspace_id && endpointsWorkspaceFilter.includes(e.workspace_id));
  }, [endpointsBundle, endpointsWorkspaceFilter]);

  const endpointCostTypes = useMemo(() => {
    return Array.from(new Set(endpointsWorkspaceFiltered.map(e => e.cost_type).filter(Boolean)));
  }, [endpointsWorkspaceFiltered]);

  const endpointsData = useMemo(() => {
    const rows = endpointsCostTypeFilter
      ? endpointsWorkspaceFiltered.filter(e => e.cost_type === endpointsCostTypeFilter)
      : endpointsWorkspaceFiltered;
    const byEndpoint: Record<string, { endpoint_name: string; total_spend: number; total_dbus: number; days_active: number; cost_type: string }> = {};
    for (const e of rows) {
      if (!byEndpoint[e.endpoint_name]) {
        byEndpoint[e.endpoint_name] = { endpoint_name: e.endpoint_name, total_spend: 0, total_dbus: 0, days_active: e.days_active, cost_type: e.cost_type };
      }
      byEndpoint[e.endpoint_name].total_spend += e.total_spend;
      byEndpoint[e.endpoint_name].total_dbus += e.total_dbus;
    }
    return Object.values(byEndpoint).sort((a, b) => b.total_spend - a.total_spend);
  }, [endpointsWorkspaceFiltered, endpointsCostTypeFilter]);


  const [prevEndpointsCount, setPrevEndpointsCount] = useState(endpointsData.length);
  if (prevEndpointsCount !== endpointsData.length) {
    setPrevEndpointsCount(endpointsData.length);
    setEndpointsPage(1);
  }

  const endpointsWorkspaceFilterKey = useMemo(
    () => [...endpointsWorkspaceFilter].sort().join(','),
    [endpointsWorkspaceFilter],
  );
  const [prevEndpointsWorkspaceFilterKey, setPrevEndpointsWorkspaceFilterKey] = useState(endpointsWorkspaceFilterKey);
  if (prevEndpointsWorkspaceFilterKey !== endpointsWorkspaceFilterKey) {
    setPrevEndpointsWorkspaceFilterKey(endpointsWorkspaceFilterKey);
    setEndpointsCostTypeFilter(null);
    setEndpointsPage(1);
  }

  if (isLoading) {
    return (
      <div className="flex h-64 flex-col items-center justify-center gap-3">
        <div className="h-12 w-12 animate-spin rounded-full border-4 border-gray-200" style={{ borderTopColor: '#FF3621' }} />
        <p className="text-sm text-gray-500">Loading AI/ML data...</p>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="rounded-lg border border-yellow-200 bg-yellow-50 p-6">
        <div className="flex flex-col items-center justify-center gap-2 py-4">
          <p className="text-base font-medium text-yellow-800">No AI/ML cost data available</p>
          <p className="text-sm text-yellow-700">Try expanding the date range, or check that model serving and inference endpoints are active</p>
        </div>
      </div>
    );
  }

  const summary = data.summary;

  return (
    <div className="animate-fade-in space-y-6">
      <InfoPanel
        title="AI/ML tab methodology"
        minimized={infoMinimized}
        onToggle={handleMinimizeToggle}
      >
        <ul className="list-inside list-disc space-y-1">
          <li><strong>Foundation Models</strong>: Token based usage via Databricks Model Serving (Anthropic, OpenAI, Gemini, Llama)</li>
          <li><strong>Serverless Inference</strong>: Custom model and agent endpoints on Databricks managed infrastructure</li>
          <li><strong>Batch Inference</strong>: Batch inference jobs for large scale offline predictions</li>
          <li><strong>Fine Tuning</strong>: Model tuning runs using Databricks Mosaic AI</li>
          <li><strong>AI Search</strong>: Databricks AI Search index compute costs</li>
          <li><strong>Model Serving</strong>: Other model serving workloads not captured above</li>
        </ul>
      </InfoPanel>

      {/* Header — hidden when embedded (the Unity Gateway view already shows the title) */}
      {!embedded && (
      <PageHero
        icon={
          <svg fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
          </svg>
        }
        title="AI/ML"
        subtitle="AI and machine learning cost attribution and trends"
        dateRange={{ startDate, endDate }}
        workspaceScope={workspaceIds && workspaceIds.length > 0
          ? workspaceIds.length === 1
            ? (workspaceNameMap?.[workspaceIds[0]] || workspaceIds[0])
            : `${workspaceIds.length} workspaces`
          : undefined}
      />
      )}

      {/* Summary Cards */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <KPICard
          title="Total AI/ML Spend"
          value={formatCurrency(summary.total_spend)}
          subtitle={`over ${summary.days_in_range} days`}
          onActivate={startDate && endDate ? () => setSelectedKPI({kpi: "aiml_spend", label: "Daily AI/ML Spend"}) : undefined}
          icon={
            <svg className="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8c-1.657 0-3 .895-3 2s1.343 2 3 2 3 .895 3 2-1.343 2-3 2m0-8c1.11 0 2.08.402 2.599 1M12 8V7m0 1v8m0 0v1m0-1c-1.11 0-2.08-.402-2.599-1M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
          }
        />
        <KPICard
          title="Total DBUs"
          value={formatNumber(summary.total_dbus)}
          subtitle={`over ${summary.days_in_range} days`}
          onActivate={startDate && endDate ? () => setSelectedKPI({kpi: "aiml_dbus", label: "Daily AI/ML DBUs"}) : undefined}
          icon={
            <svg className="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 7h6m0 10v-3m-3 3h.01M9 17h.01M9 14h.01M12 14h.01M15 11h.01M12 11h.01M9 11h.01M7 21h10a2 2 0 002-2V5a2 2 0 00-2-2H7a2 2 0 00-2 2v14a2 2 0 002 2z" />
            </svg>
          }
        />
        <KPICard
          title="Active Endpoints"
          value={formatNumber(summary.endpoint_count || 0)}
          subtitle={`across ${summary.workspace_count || 0} workspaces`}
          infoText="Average number of distinct model serving endpoints with billable usage per day in the selected period."
          onActivate={startDate && endDate ? () => setSelectedKPI({kpi: "aiml_endpoints", label: "Daily Active Endpoints"}) : undefined}
          icon={
            <svg className="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 12h14M5 12a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v4a2 2 0 01-2 2M5 12a2 2 0 00-2 2v4a2 2 0 002 2h14a2 2 0 002-2v-4a2 2 0 00-2-2m-2-4h.01M17 16h.01" />
            </svg>
          }
        />
        <KPICard
          title="Average Daily Endpoint Cost"
          value={formatCurrency(summary.avg_cost_per_endpoint || 0)}
          subtitle="daily per-endpoint"
          onActivate={startDate && endDate ? () => setSelectedKPI({kpi: "aiml_avg_endpoint_cost", label: "Average Daily Endpoint Cost"}) : undefined}
          icon={
            <svg className="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 7h8m0 0v8m0-8l-8 8-4-4-6 6" />
            </svg>
          }
        />
      </div>

      {selectedKPI && startDate && endDate && (
        <KPITrendModal
          kpi={selectedKPI.kpi}
          kpiLabel={selectedKPI.label}
          isOpen={!!selectedKPI}
          onClose={() => setSelectedKPI(null)}
          startDate={startDate}
          endDate={endDate}
          workspaceIds={workspaceIds}
        />
      )}

      {/* Charts Row */}
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        {/* Spend Over Time */}
        <div className="rounded-lg bg-white p-6 border " style={{ borderColor: '#E5E5E5' }}>
          <h3 className="mb-4 text-lg font-semibold text-gray-900">AI/ML Spend Over Time</h3>
          {data.timeseries?.timeseries?.length > 0 ? (
            <ResponsiveContainer width="100%" height={300}>
              <AreaChart data={data.timeseries.timeseries} margin={{ top: 5, right: 10, left: 10, bottom: 5 }}>
                <XAxis
                  dataKey="date"
                  tickFormatter={(date) => new Date(date).toLocaleDateString("en-US", { month: "short", day: "numeric" })}
                />
                <YAxis tickFormatter={(value) => formatCurrency(value)} width={80} />
                <Tooltip
                  formatter={(value) => formatCurrency(value as number)}
                  labelFormatter={(label) => new Date(label).toLocaleDateString()}
                />
                <Legend wrapperStyle={{ fontSize: 12 }} />
                {data.timeseries.categories.map((category, idx) => (
                  <Area isAnimationActive={false}
                    key={category}
                    type="monotone"
                    dataKey={category}
                    stackId="1"
                    stroke={categoryColorMap[category] || FALLBACK_COLORS[idx % FALLBACK_COLORS.length]}
                    fill={categoryColorMap[category] || FALLBACK_COLORS[idx % FALLBACK_COLORS.length]}
                    fillOpacity={0.6}
                  />
                ))}
              </AreaChart>
            </ResponsiveContainer>
          ) : (
            <div className="flex h-64 items-center justify-center text-gray-500">No timeseries data</div>
          )}
        </div>

        {/* Category Breakdown Pie Chart */}
        <div className="rounded-lg bg-white p-6 border " style={{ borderColor: '#E5E5E5' }}>
          <h3 className="mb-4 text-lg font-semibold text-gray-900">Cost by AI Spend Category</h3>
          {pieData.length > 0 ? (
            <ResponsiveContainer width="100%" height={300}>
              <PieChart>
                <Pie isAnimationActive={false}
                  data={pieData}
                  cx="50%"
                  cy="50%"
                  innerRadius={60}
                  outerRadius={90}
                  paddingAngle={2}
                  dataKey="value"
                  label={({ percentage }: PieLabelRenderProps & { percentage?: number }) => percentage != null && percentage >= 5 ? `${Math.round(percentage)}%` : ''}
                  labelLine={false}
                >
                  {pieData.map((entry, index) => (
                    <Cell key={`cell-${index}`} fill={entry.fill} />
                  ))}
                </Pie>
                <Tooltip formatter={(value) => formatCurrency(value as number)} />
                <Legend wrapperStyle={{ fontSize: 12 }} formatter={(value, entry) => `${value} (${Math.round((entry.payload as { percentage?: number } | undefined)?.percentage ?? 0)}%)`} />
              </PieChart>
            </ResponsiveContainer>
          ) : (
            <div className="flex h-64 items-center justify-center text-gray-500">No category data</div>
          )}
        </div>
      </div>

      {/* Top Endpoints & Top Models Side-by-Side */}
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        {/* Top Endpoints Table */}
        <div className="rounded-lg bg-white p-6 border " style={{ borderColor: '#E5E5E5' }}>
          <div className="mb-4 flex items-center gap-2">
            <h3 className="text-lg font-semibold text-gray-900">Top Model Serving Endpoints</h3>
            <div className="group relative">
              <svg className="h-4 w-4 text-gray-500 cursor-help" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
              <div className="absolute left-0 top-6 z-50 hidden w-72 rounded-lg border border-gray-200 bg-white p-3 text-xs text-gray-600 shadow-lg group-hover:block">
                <p className="font-medium text-gray-900 mb-1">Model Serving Endpoints</p>
                <p>Endpoints deployed via Databricks Model Serving using serverless compute. These are pay-per-request inference endpoints that auto-scale to zero. Costs include both steady-state compute and scale-from-zero launch overhead.</p>
              </div>
            </div>
            <div className="ml-auto flex items-center gap-2">
              {endpointWorkspaces.length > 0 && (() => {
                const isPartialWs = endpointsWorkspaceFilter.length > 0 && endpointsWorkspaceFilter.length < endpointWorkspaces.length;
                const singleName = endpointsWorkspaceFilter.length === 1 ? resolveWsName(endpointsWorkspaceFilter[0]) : null;
                return (
                <div className="relative" ref={endpointsWorkspaceFilterRef}>
                  <button
                    onClick={() => { setEndpointsWorkspaceSearch(""); setEndpointsWorkspaceDropdownOpen(o => !o); }}
                    className={`flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium transition-colors ${isPartialWs ? "border-[#FF3621] text-[#FF3621]" : "border-gray-300 text-gray-700 hover:bg-gray-50"}`}
                  >
                    {singleName
                      ? (singleName.length > 20 ? singleName.slice(0, 20) + "…" : singleName)
                      : isPartialWs
                      ? `${endpointsWorkspaceFilter.length} workspaces`
                      : "Workspace"}
                    <svg className={`h-3 w-3 transition-transform ${endpointsWorkspaceDropdownOpen ? "rotate-180" : ""}`} fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" /></svg>
                  </button>
                  {endpointsWorkspaceDropdownOpen && (() => {
                    const q = endpointsWorkspaceSearch.trim().toLowerCase();
                    const filteredWs = q
                      ? endpointWorkspaces.filter(w => resolveWsName(w).toLowerCase().includes(q) || w.toLowerCase().includes(q))
                      : endpointWorkspaces;
                    return (
                    <div className="absolute right-0 top-full z-[9999] mt-1 w-60 rounded-lg border border-gray-200 bg-white shadow-lg">
                      <div className="sticky top-0 flex items-center justify-between border-b border-gray-100 bg-white px-3 py-2">
                        <span className="text-[10px] font-semibold uppercase tracking-wider text-gray-500">Workspace</span>
                        <div className="flex items-center gap-2 text-xs">
                          <button onClick={(e) => { e.stopPropagation(); setEndpointsWorkspaceFilter([...endpointWorkspaces]); setEndpointsPage(1); }} className="text-gray-500 hover:text-gray-800">All</button>
                          <span className="text-gray-300">·</span>
                          <button onClick={(e) => { e.stopPropagation(); setEndpointsWorkspaceFilter([]); setEndpointsPage(1); }} className="text-gray-500 hover:text-gray-800">Clear</button>
                        </div>
                      </div>
                      <div className="border-b border-gray-100 p-2">
                        <input
                          type="text"
                          value={endpointsWorkspaceSearch}
                          onChange={(e) => setEndpointsWorkspaceSearch(e.target.value)}
                          placeholder="Search workspaces..."
                          className="w-full rounded border border-gray-200 px-2 py-1 text-xs focus:border-[#FF3621] focus:outline-none focus:ring-1 focus:ring-[#FF3621]"
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
                        getKey={(wsId) => wsId}
                        renderItem={(wsId) => (
                          <button
                            onClick={() => { setEndpointsWorkspaceFilter(prev => prev.includes(wsId) ? prev.filter(x => x !== wsId) : [...prev, wsId]); setEndpointsPage(1); }}
                            className="flex w-full items-center gap-2 px-3 py-2.5 text-left text-xs hover:bg-gray-50"
                          >
                            <div className={`flex h-4 w-4 shrink-0 items-center justify-center rounded border ${endpointsWorkspaceFilter.includes(wsId) ? "border-orange-500 bg-orange-500" : "border-gray-300"}`}>
                              {endpointsWorkspaceFilter.includes(wsId) && <svg className="h-3 w-3 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" /></svg>}
                            </div>
                            <span className="truncate text-gray-700">{resolveWsName(wsId)}</span>
                          </button>
                        )}
                      />
                      )}
                    </div>
                    );
                  })()}
                </div>
                );
              })()}
              {endpointCostTypes.length > 1 && (
                <div className="relative" ref={endpointsCostTypeFilterRef}>
                  <button
                    onClick={() => setEndpointsCostTypeDropdownOpen(o => !o)}
                    className={`flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium transition-colors ${endpointsCostTypeFilter ? "border-[#FF3621] text-[#FF3621]" : "border-gray-300 text-gray-700 hover:bg-gray-50"}`}
                  >
                    {endpointsCostTypeFilter || "Cost type"}
                    {endpointsCostTypeFilter && (
                      <button onClick={(e) => { e.stopPropagation(); setEndpointsCostTypeFilter(null); setEndpointsPage(1); }} className="ml-0.5 flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded-full bg-orange-100 text-orange-600 hover:bg-orange-200">
                        <svg className="h-2 w-2" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M6 18L18 6M6 6l12 12" /></svg>
                      </button>
                    )}
                    <svg className={`h-3 w-3 transition-transform ${endpointsCostTypeDropdownOpen ? "rotate-180" : ""}`} fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" /></svg>
                  </button>
                  {endpointsCostTypeDropdownOpen && (
                    <div className="absolute right-0 top-full z-[9999] mt-1 max-h-64 w-48 overflow-y-auto rounded-lg border border-gray-200 bg-white shadow-lg">
                      <div className="sticky top-0 flex items-center justify-between border-b border-gray-100 bg-white px-3 py-2">
                        <span className="text-[10px] font-semibold uppercase tracking-wider text-gray-500">Cost Type</span>
                        {endpointsCostTypeFilter && (
                          <button onClick={(e) => { e.stopPropagation(); setEndpointsCostTypeFilter(null); setEndpointsPage(1); setEndpointsCostTypeDropdownOpen(false); }} className="text-xs text-gray-500 hover:text-gray-800">Clear</button>
                        )}
                      </div>
                      {endpointCostTypes.map(ct => (
                        <button
                          key={ct}
                          onClick={() => { setEndpointsCostTypeFilter(ct); setEndpointsPage(1); setEndpointsCostTypeDropdownOpen(false); }}
                          className="flex w-full items-center gap-2 px-3 py-2.5 text-left text-xs hover:bg-gray-50"
                        >
                          <div className={`flex h-4 w-4 shrink-0 items-center justify-center rounded border ${endpointsCostTypeFilter === ct ? "border-orange-500 bg-orange-500" : "border-gray-300"}`}>
                            {endpointsCostTypeFilter === ct && <svg className="h-3 w-3 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" /></svg>}
                          </div>
                          <span className="truncate text-gray-700">{ct}</span>
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </div>
          </div>
          {endpointsData.length > 0 ? (() => {
            const epUrl = getEndpointUrl(host, endpointsWorkspaceFilter.length === 1 ? endpointsWorkspaceFilter[0] : workspaceIds?.[0]);
            return (
              <>
                <div className="overflow-x-auto">
                  <table className="min-w-full divide-y divide-gray-200">
                    <thead className="bg-gray-50">
                      <tr>
                        <th className="px-3 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500">
                          Endpoint
                        </th>
                        <th className="px-3 py-3 text-right text-xs font-medium uppercase tracking-wider text-gray-500">
                          DBUs
                        </th>
                        <th className="px-3 py-3 text-right text-xs font-medium uppercase tracking-wider text-gray-500">
                          Spend
                        </th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-gray-200 bg-white">
                      {endpointsData.slice((endpointsPage - 1) * PAGE_SIZE, endpointsPage * PAGE_SIZE).map((endpoint, idx) => (
                          <tr key={idx} className={idx % 2 === 0 ? "bg-white" : "bg-gray-50"}>
                            <td className="px-3 py-2 text-sm font-medium truncate max-w-[200px]" title={endpoint.endpoint_name}>
                              {epUrl ? (
                                <a href={epUrl} target="_blank" rel="noopener noreferrer" className="text-[#FF3621] hover:underline">
                                  {endpoint.endpoint_name}
                                </a>
                              ) : endpoint.endpoint_name}
                            </td>
                            <td className="whitespace-nowrap px-3 py-2 text-right text-sm text-gray-500">
                              {formatNumber(endpoint.total_dbus)}
                            </td>
                            <td className="whitespace-nowrap px-3 py-2 text-right text-sm text-gray-900">
                              {formatCurrency(endpoint.total_spend)}
                            </td>
                          </tr>
                        ))
                      }
                    </tbody>
                  </table>
                </div>
                {endpointsData.length > PAGE_SIZE && (
                  <div className="mt-3 flex items-center justify-between border-t border-gray-200 pt-3">
                    <p className="text-xs text-gray-500">
                      {(endpointsPage - 1) * PAGE_SIZE + 1}–{Math.min(endpointsPage * PAGE_SIZE, endpointsData.length)} of {endpointsData.length}
                    </p>
                    <div className="flex gap-1">
                      <button onClick={() => setEndpointsPage(p => Math.max(1, p - 1))} disabled={endpointsPage === 1} className="rounded border border-gray-300 px-2 py-1 text-xs text-gray-600 hover:bg-gray-50 disabled:opacity-40">Prev</button>
                      <button onClick={() => setEndpointsPage(p => Math.min(Math.ceil(endpointsData.length / PAGE_SIZE), p + 1))} disabled={endpointsPage >= Math.ceil(endpointsData.length / PAGE_SIZE)} className="rounded border border-gray-300 px-2 py-1 text-xs text-gray-600 hover:bg-gray-50 disabled:opacity-40">Next</button>
                    </div>
                  </div>
                )}
              </>
            );
          })() : (
            <div className="flex h-32 items-center justify-center text-gray-500">No endpoint data available</div>
          )}
        </div>

        {/* Top Models Table */}
        <div className="rounded-lg bg-white p-6 border " style={{ borderColor: '#E5E5E5' }}>
          {(() => {
            const TYPE_LABELS: Record<string, string> = { "Foundation Model API": "FMAPI Call" };
            const typeColors: Record<string, string> = {
              "Feature Store": "bg-cyan-50 text-cyan-700",
              "Model Training": "bg-blue-50 text-blue-700",
              "Foundation Model API": "bg-amber-50 text-amber-700",
            };
            const allModelRows = data.models?.models || [];
            const distinctTypes = Array.from(new Set(allModelRows.map(m => m.model_type))).filter(Boolean);
            const filteredModels = modelsTypeFilters.length === 0 ? allModelRows : allModelRows.filter(m => modelsTypeFilters.includes(m.model_type));
            const pageModels = filteredModels.slice((modelsPage - 1) * PAGE_SIZE, modelsPage * PAGE_SIZE);
            return (
              <>
          <div className="mb-4 flex items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <h3 className="text-lg font-semibold text-gray-900">Top Models</h3>
              <div className="group relative">
                <svg className="h-4 w-4 text-gray-500 cursor-help" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
                <div className="absolute left-0 top-6 z-50 hidden w-72 rounded-lg border border-gray-200 bg-white p-3 text-xs text-gray-600 shadow-lg group-hover:block">
                  <p className="font-medium text-gray-900 mb-1">Top Models</p>
                  <p>Foundation Model API calls (Anthropic, OpenAI, Gemini, etc.), Feature Store lookups, and model training jobs. Unlike serverless endpoints above, these are billed by token usage or feature access rather than compute time.</p>
                </div>
              </div>
            </div>
            {distinctTypes.length > 1 && (() => {
              const isPartialTypes = modelsTypeFilters.length > 0 && modelsTypeFilters.length < distinctTypes.length;
              return (
              <div className="relative" ref={modelsTypeDropdownRef}>
                <button
                  onClick={() => setModelsTypeDropdownOpen((o) => !o)}
                  className={`flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium transition-colors ${isPartialTypes ? "border-[#FF3621] text-[#FF3621]" : "border-gray-300 text-gray-700 hover:bg-gray-50"}`}
                >
                  <span className="max-w-[140px] truncate">
                    {modelsTypeFilters.length === 1
                      ? (TYPE_LABELS[modelsTypeFilters[0]] || modelsTypeFilters[0])
                      : isPartialTypes
                      ? `${modelsTypeFilters.length} Types`
                      : "Types"}
                  </span>
                  <svg className={`h-4 w-4 transition-transform ${modelsTypeDropdownOpen ? "rotate-180" : ""}`} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
                  </svg>
                </button>
                {modelsTypeDropdownOpen && (
                  <div className="absolute right-0 z-20 mt-1 min-w-[200px] max-h-64 overflow-y-auto rounded-lg border border-gray-200 bg-white shadow-lg">
                    <div className="sticky top-0 flex items-center justify-between border-b border-gray-100 bg-white px-3 py-2">
                      <span className="text-[10px] font-semibold uppercase tracking-wider text-gray-500">Types</span>
                      <div className="flex items-center gap-2 text-xs">
                        <button onClick={(e) => { e.stopPropagation(); setModelsTypeFilters([...distinctTypes]); setModelsPage(1); }} className="text-gray-500 hover:text-gray-800">All</button>
                        <span className="text-gray-300">·</span>
                        <button onClick={(e) => { e.stopPropagation(); setModelsTypeFilters([]); setModelsPage(1); }} className="text-gray-500 hover:text-gray-800">Clear</button>
                      </div>
                    </div>
                    {distinctTypes.map((t) => (
                      <button key={t} onClick={() => { setModelsTypeFilters((prev) => prev.includes(t) ? prev.filter((x) => x !== t) : [...prev, t]); setModelsPage(1); }}
                        className="flex w-full items-center gap-2 px-3 py-2.5 text-left text-xs hover:bg-gray-50">
                        <div className={`flex h-4 w-4 shrink-0 items-center justify-center rounded border ${modelsTypeFilters.includes(t) ? "border-orange-500 bg-orange-500" : "border-gray-300"}`}>
                          {modelsTypeFilters.includes(t) && <svg className="h-3 w-3 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" /></svg>}
                        </div>
                        <span className="truncate text-gray-700">{TYPE_LABELS[t] || t}</span>
                      </button>
                    ))}
                  </div>
                )}
              </div>
              );
            })()}
          </div>
          {allModelRows.length > 0 ? (
            <>
              <div className="overflow-x-auto">
                <table className="min-w-full divide-y divide-gray-200">
                  <thead className="bg-gray-50">
                    <tr>
                      <th className="px-3 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500">Model</th>
                      <th className="px-3 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500">Type</th>
                      <th className="px-3 py-3 text-right text-xs font-medium uppercase tracking-wider text-gray-500">Spend</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-200 bg-white">
                    {pageModels.map((model, idx) => {
                      const colorClass = typeColors[model.model_type] || "bg-gray-50 text-gray-700";
                      const typeLabel = TYPE_LABELS[model.model_type] || model.model_type;
                      return (
                        <tr key={idx} className={idx % 2 === 0 ? "bg-white" : "bg-gray-50"}>
                          <td className="px-3 py-2 text-sm font-medium truncate max-w-45" title={model.model_name}>
                            {(() => { const modelUrl = getModelUrl(host, workspaceIds?.[0]); return modelUrl ? <a href={modelUrl} target="_blank" rel="noopener noreferrer" className="text-[#FF3621] hover:underline">{model.model_name}</a> : <span className="text-gray-900">{model.model_name}</span>; })()}
                          </td>
                          <td className="whitespace-nowrap px-3 py-2 text-sm">
                            <span className={`inline-flex rounded-full px-2 py-0.5 text-xs font-medium ${colorClass}`}>{typeLabel}</span>
                          </td>
                          <td className="whitespace-nowrap px-3 py-2 text-right text-sm text-gray-900">{formatCurrency(model.total_spend)}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              {filteredModels.length > PAGE_SIZE && (
                <div className="mt-3 flex items-center justify-between border-t border-gray-200 pt-3">
                  <p className="text-xs text-gray-500">
                    {(modelsPage - 1) * PAGE_SIZE + 1}–{Math.min(modelsPage * PAGE_SIZE, filteredModels.length)} of {filteredModels.length}
                  </p>
                  <div className="flex gap-1">
                    <button onClick={() => setModelsPage(p => Math.max(1, p - 1))} disabled={modelsPage === 1} className="rounded border border-gray-300 px-2 py-1 text-xs text-gray-600 hover:bg-gray-50 disabled:opacity-40">Prev</button>
                    <button onClick={() => setModelsPage(p => Math.min(Math.ceil(filteredModels.length / PAGE_SIZE), p + 1))} disabled={modelsPage >= Math.ceil(filteredModels.length / PAGE_SIZE)} className="rounded border border-gray-300 px-2 py-1 text-xs text-gray-600 hover:bg-gray-50 disabled:opacity-40">Next</button>
                  </div>
                </div>
              )}
            </>
          ) : (
            <div className="flex h-32 items-center justify-center text-gray-500">No model data available</div>
          )}
        </>
      );
    })()}
        </div>
      </div>

      {/* ML Runtime Clusters Table */}
      <div className="rounded-lg bg-white p-6 border " style={{ borderColor: '#E5E5E5' }}>
        {(() => {
          const allMlClusters = data.ml_clusters?.clusters || [];
          const isHistoricalMlCluster = (c: typeof allMlClusters[0]) => !c.cluster_name || c.cluster_name === c.cluster_id || c.cluster_name === "Unknown";
          const historicalMlCount = allMlClusters.filter(isHistoricalMlCluster).length;
          const availableRuntimes = Array.from(new Set(allMlClusters.map(c => c.runtime_version).filter(Boolean))).sort() as string[];
          const searchedMlClusters = allMlClusters.filter(c => showHistoricalMlClusters || !isHistoricalMlCluster(c));
          const runtimeFilteredClusters = mlRuntimeFilter.length === 0 ? searchedMlClusters : searchedMlClusters.filter(c => mlRuntimeFilter.includes(c.runtime_version));
          const filteredMlClusters = mlClusterSearch
            ? runtimeFilteredClusters.filter(c =>
                (c.cluster_name || "").toLowerCase().includes(mlClusterSearch.toLowerCase()) ||
                (c.cluster_id || "").toLowerCase().includes(mlClusterSearch.toLowerCase()) ||
                (c.owner || "").toLowerCase().includes(mlClusterSearch.toLowerCase())
              )
            : runtimeFilteredClusters;
          const mlTotalPages = Math.ceil(filteredMlClusters.length / PAGE_SIZE);
          const mlStart = (mlClustersPage - 1) * PAGE_SIZE;
          const paginatedMlClusters = filteredMlClusters.slice(mlStart, mlStart + PAGE_SIZE);

          return (
            <>
              <div className="mb-2 flex items-center justify-between gap-3">
                <div>
                  <h3 className="text-lg font-semibold text-gray-900">ML Runtime Clusters</h3>
                  <p className="text-xs text-gray-500">Clusters running the Databricks ML Runtime ({filteredMlClusters.length} clusters)</p>
                </div>
                <div className="flex items-center gap-3">
                  {allMlClusters.length > 0 && (
                    <label className="flex items-center gap-1.5 text-xs text-gray-500 cursor-pointer">
                      <input type="checkbox" checked={showHistoricalMlClusters}
                        onChange={(e) => { setShowHistoricalMlClusters(e.target.checked); setMlClustersPage(1); }}
                        className="rounded border-gray-300 text-orange-600 focus:ring-orange-500" />
                      Show historical ({historicalMlCount})
                      <span className="relative group ml-0.5">
                        <svg className="inline h-3 w-3 text-gray-500 cursor-help" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>
                        <span className="absolute bottom-full left-1/2 -translate-x-1/2 mb-1 hidden group-hover:block w-56 rounded-lg bg-gray-900 px-2 py-1.5 text-[10px] text-white shadow-lg z-20">Clusters whose names could not be resolved — likely terminated or from inaccessible workspaces</span>
                      </span>
                    </label>
                  )}
                  {availableRuntimes.length > 0 && (
                    <div className="relative" ref={mlRuntimeFilterRef}>
                      <button
                        onClick={() => setMlRuntimeFilterOpen(o => !o)}
                        className={`flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium transition-colors ${mlRuntimeFilter.length > 0 && mlRuntimeFilter.length < availableRuntimes.length ? "border-[#FF3621] text-[#FF3621]" : "border-gray-300 text-gray-700 hover:bg-gray-50"}`}
                      >
                        {mlRuntimeFilter.length === 0
                          ? "Runtime"
                          : mlRuntimeFilter.length === 1
                          ? (mlRuntimeFilter[0].length > 18 ? mlRuntimeFilter[0].substring(0, 18) + "…" : mlRuntimeFilter[0])
                          : mlRuntimeFilter.length === availableRuntimes.length
                          ? "Runtime"
                          : `${mlRuntimeFilter.length} runtimes`}
                        <svg className={`h-3 w-3 transition-transform ${mlRuntimeFilterOpen ? "rotate-180" : ""}`} fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" /></svg>
                      </button>
                      {mlRuntimeFilterOpen && (
                        <div className="absolute right-0 top-full z-[9999] mt-1 max-h-64 w-56 overflow-y-auto rounded-lg border border-gray-200 bg-white shadow-lg">
                          <div className="sticky top-0 flex items-center justify-between border-b border-gray-100 bg-white px-3 py-2">
                            <span className="text-[10px] font-semibold uppercase tracking-wider text-gray-500">Runtime</span>
                            <div className="flex items-center gap-2 text-xs">
                              <button onClick={(e) => { e.stopPropagation(); setMlRuntimeFilter([...availableRuntimes]); setMlClustersPage(1); }} className="text-gray-500 hover:text-gray-800">All</button>
                              <span className="text-gray-300">·</span>
                              <button onClick={(e) => { e.stopPropagation(); setMlRuntimeFilter([]); setMlClustersPage(1); }} className="text-gray-500 hover:text-gray-800">Clear</button>
                            </div>
                          </div>
                          {availableRuntimes.map(r => (
                            <button
                              key={r}
                              onClick={() => { setMlRuntimeFilter(prev => prev.includes(r) ? prev.filter(x => x !== r) : [...prev, r]); setMlClustersPage(1); }}
                              className="flex w-full items-center gap-2 px-3 py-2.5 text-left text-xs hover:bg-gray-50"
                            >
                              <div className={`flex h-4 w-4 shrink-0 items-center justify-center rounded border ${mlRuntimeFilter.includes(r) ? "border-orange-500 bg-orange-500" : "border-gray-300"}`}>
                                {mlRuntimeFilter.includes(r) && <svg className="h-3 w-3 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" /></svg>}
                              </div>
                              <span className="truncate text-gray-700">{r}</span>
                            </button>
                          ))}
                        </div>
                      )}
                    </div>
                  )}
                  <div className="relative w-44">
                    <svg className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-gray-500" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
                    </svg>
                    <input
                      type="text"
                      placeholder="Search clusters..."
                      value={mlClusterSearch}
                      onChange={(e) => { setMlClusterSearch(e.target.value); setMlClustersPage(1); }}
                      className="w-44 rounded-full border border-gray-200 bg-white py-1.5 pl-9 pr-4 text-xs placeholder:text-gray-400 focus:border-[#FF3621] focus:outline-none focus:ring-1 focus:ring-[#FF3621]"
                    />
                  </div>
                </div>
              </div>
              {paginatedMlClusters.length > 0 ? (
                <div className="mt-3 overflow-x-auto">
                  <table className="min-w-full divide-y divide-gray-200">
                    <thead className="bg-gray-50">
                      <tr>
                        <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500">Cluster Name</th>
                        <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500">Runtime</th>
                        <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500">Owner</th>
                        <th className="px-4 py-3 text-right text-xs font-medium uppercase tracking-wider text-gray-500">DBUs</th>
                        <th className="px-4 py-3 text-right text-xs font-medium uppercase tracking-wider text-gray-500">Spend</th>
                        <th className="px-4 py-3 text-right text-xs font-medium uppercase tracking-wider text-gray-500">Days Active</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-gray-200 bg-white">
                      {paginatedMlClusters.map((cluster, idx) => {
                        const clusterUrl = getClusterUrl(host, cluster.cluster_id, (cluster as MLClusterRow).workspace_id ?? null);
                        return (
                          <tr key={idx} className={idx % 2 === 0 ? "bg-white hover:bg-gray-50" : "bg-gray-50 hover:bg-gray-100"}>
                            <td className="px-4 py-4 text-sm">
                              <div className="flex flex-col gap-0.5">
                                {clusterUrl ? (
                                  <a href={clusterUrl} target="_blank" rel="noopener noreferrer"
                                    className="group flex items-center gap-1 font-medium text-[#FF3621] hover:text-[#E02F1C]">
                                    <span className="truncate max-w-[250px]">{cluster.cluster_name}</span>
                                    <svg className="h-3 w-3 shrink-0 opacity-0 transition-opacity group-hover:opacity-100" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
                                    </svg>
                                  </a>
                                ) : (
                                  <span className="font-medium text-gray-900 truncate max-w-[250px]">{cluster.cluster_name}</span>
                                )}
                                <div className="flex items-center gap-2">
                                  {isHistoricalMlCluster(cluster) && <span className="inline-flex rounded-full bg-gray-100 px-2 py-0.5 text-[10px] font-medium text-gray-500">Historical</span>}
                                  <span className="text-xs text-gray-500 truncate max-w-[200px]">{cluster.cluster_id}</span>
                                </div>
                              </div>
                            </td>
                            <td className="px-4 py-4 text-sm text-gray-500 max-w-[200px] truncate" title={cluster.runtime_version}>{cluster.runtime_version}</td>
                            <td className="px-4 py-4">
                              {cluster.owner ? (
                                <span className="inline-flex rounded-full bg-gray-100 px-2 py-0.5 text-xs font-medium text-gray-700 max-w-40 truncate" title={cluster.owner}>
                                  {formatIdentity(cluster.owner, spNameMap)}
                                </span>
                              ) : <span className="text-sm text-gray-500">—</span>}
                            </td>
                            <td className="whitespace-nowrap px-4 py-4 text-right text-sm text-gray-500">{formatNumber(cluster.total_dbus)}</td>
                            <td className="whitespace-nowrap px-4 py-4 text-right text-sm text-gray-900">{formatCurrency(cluster.total_spend)}</td>
                            <td className="whitespace-nowrap px-4 py-4 text-right text-sm text-gray-500">{cluster.days_active}</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                  {mlTotalPages > 1 && (
                    <div className="mt-4 flex items-center justify-between border-t border-gray-200 pt-4">
                      <p className="text-sm text-gray-700">
                        Showing <span className="font-medium">{mlStart + 1}</span> to <span className="font-medium">{Math.min(mlStart + PAGE_SIZE, filteredMlClusters.length)}</span> of <span className="font-medium">{filteredMlClusters.length}</span>
                      </p>
                      <div className="flex gap-2">
                        <button onClick={() => setMlClustersPage(p => Math.max(1, p - 1))} disabled={mlClustersPage === 1}
                          className="rounded border border-gray-300 px-3 py-1 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50">Previous</button>
                        <button onClick={() => setMlClustersPage(p => Math.min(mlTotalPages, p + 1))} disabled={mlClustersPage === mlTotalPages}
                          className="rounded border border-gray-300 px-3 py-1 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50">Next</button>
                      </div>
                    </div>
                  )}
                </div>
              ) : (
                <div className="flex h-32 items-center justify-center text-gray-500">No ML runtime cluster data available</div>
              )}
            </>
          );
        })()}
      </div>

      {/* Agent Bricks Table */}
      <div className="rounded-lg bg-white p-6 border " style={{ borderColor: '#E5E5E5' }}>
        {(() => {
          const allAgents: AgentBrickRow[] = data.agent_bricks?.agents || [];
          const isHistoricalAgent = (a: typeof allAgents[0]) => a.agent_name === "Unknown" || a.agent_name === a.endpoint_id;
          const historicalAgentCount = allAgents.filter(isHistoricalAgent).length;
          const agentTypes = Array.from(new Set(allAgents.map(a => a.agent_type || "Agent")));
          const filteredAgents = allAgents
            .filter(a => showHistoricalAgents || !isHistoricalAgent(a))
            .filter(a => agentTypeFilter.length === 0 || agentTypeFilter.includes(a.agent_type || "Agent"))
            .filter(a => !agentSearch || (a.agent_name || "").toLowerCase().includes(agentSearch.toLowerCase()) || (a.endpoint_id || "").toLowerCase().includes(agentSearch.toLowerCase()));
          const agentTotalPages = Math.ceil(filteredAgents.length / PAGE_SIZE);
          const agentStart = (agentsPage - 1) * PAGE_SIZE;
          const paginatedAgents = filteredAgents.slice(agentStart, agentStart + PAGE_SIZE);

          return (
            <>
              <div className="mb-2 flex items-center justify-between gap-3">
                <div>
                  <h3 className="text-lg font-semibold text-gray-900">Agent Bricks</h3>
                  <p className="text-xs text-gray-500">Databricks agents and their cost attribution</p>
                </div>
                <div className="flex items-center gap-2">
                  {allAgents.length > 0 && (
                    <label className="flex items-center gap-1.5 text-xs text-gray-500 cursor-pointer">
                      <input
                        type="checkbox"
                        checked={showHistoricalAgents}
                        onChange={(e) => { setShowHistoricalAgents(e.target.checked); setAgentsPage(1); }}
                        className="rounded border-gray-300 text-orange-600 focus:ring-orange-500"
                      />
                      Show historical ({historicalAgentCount})
                      <span className="relative group ml-0.5">
                        <svg className="inline h-3 w-3 text-gray-500 cursor-help" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>
                        <span className="absolute bottom-full left-1/2 -translate-x-1/2 mb-1 hidden group-hover:block w-56 rounded-lg bg-gray-900 px-2 py-1.5 text-[10px] text-white shadow-lg z-20">Agents whose names could not be resolved — likely deleted or renamed</span>
                      </span>
                    </label>
                  )}
                  {agentTypes.length > 1 && (() => {
                    const isPartialAgentTypes = agentTypeFilter.length > 0 && agentTypeFilter.length < agentTypes.length;
                    return (
                    <div className="relative" ref={agentTypeDropdownRef}>
                      <button
                        type="button"
                        onClick={() => setAgentTypeDropdownOpen((o) => !o)}
                        className={`flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium transition-colors ${isPartialAgentTypes ? "border-[#FF3621] text-[#FF3621]" : "border-gray-200 bg-white text-gray-700 hover:bg-gray-50"}`}
                      >
                        {agentTypeFilter.length === 1
                          ? agentTypeFilter[0]
                          : isPartialAgentTypes
                          ? `${agentTypeFilter.length} Types`
                          : "Types"}
                        <svg className={`h-4 w-4 transition-transform ${agentTypeDropdownOpen ? "rotate-180" : ""}`} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                          <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
                        </svg>
                      </button>
                      {agentTypeDropdownOpen && (
                        <div className="absolute right-0 top-full z-[9999] mt-1 min-w-[180px] max-h-64 overflow-y-auto rounded-lg border border-gray-200 bg-white shadow-lg">
                          <div className="sticky top-0 flex items-center justify-between border-b border-gray-100 bg-white px-3 py-2">
                            <span className="text-[10px] font-semibold uppercase tracking-wider text-gray-500">Types</span>
                            <div className="flex items-center gap-2 text-xs">
                              <button type="button" onClick={(e) => { e.stopPropagation(); setAgentTypeFilter([...agentTypes]); setAgentsPage(1); }} className="text-gray-500 hover:text-gray-800">All</button>
                              <span className="text-gray-300">·</span>
                              <button type="button" onClick={(e) => { e.stopPropagation(); setAgentTypeFilter([]); setAgentsPage(1); }} className="text-gray-500 hover:text-gray-800">Clear</button>
                            </div>
                          </div>
                          {agentTypes.map(t => (
                            <button type="button" key={t} onClick={() => { setAgentTypeFilter((prev) => prev.includes(t) ? prev.filter((x) => x !== t) : [...prev, t]); setAgentsPage(1); }}
                              className="flex w-full items-center gap-2 px-3 py-2.5 text-left text-xs hover:bg-gray-50">
                              <div className={`flex h-4 w-4 shrink-0 items-center justify-center rounded border ${agentTypeFilter.includes(t) ? "border-orange-500 bg-orange-500" : "border-gray-300"}`}>
                                {agentTypeFilter.includes(t) && <svg className="h-3 w-3 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" /></svg>}
                              </div>
                              <span className="truncate text-gray-700">{t}</span>
                            </button>
                          ))}
                        </div>
                      )}
                    </div>
                    );
                  })()}
                  <div className="relative w-44">
                    <svg className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-gray-500" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
                    </svg>
                    <input
                      type="text"
                      placeholder="Search agents..."
                      value={agentSearch}
                      onChange={(e) => { setAgentSearch(e.target.value); setAgentsPage(1); }}
                      className="w-44 rounded-full border border-gray-200 bg-white py-1.5 pl-9 pr-4 text-xs placeholder:text-gray-400 focus:border-[#FF3621] focus:outline-none focus:ring-1 focus:ring-[#FF3621]"
                    />
                  </div>
                </div>
              </div>
              {paginatedAgents.length > 0 ? (
                <div className="mt-3 overflow-x-auto">
                  <table className="min-w-full divide-y divide-gray-200">
                    <thead className="bg-gray-50">
                      <tr>
                        <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500">Agent</th>
                        <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500">Type</th>
                        <th className="px-4 py-3 text-right text-xs font-medium uppercase tracking-wider text-gray-500">DBUs</th>
                        <th className="px-4 py-3 text-right text-xs font-medium uppercase tracking-wider text-gray-500">Spend</th>
                        <th className="px-4 py-3 text-right text-xs font-medium uppercase tracking-wider text-gray-500">Avg Daily</th>
                        <th className="px-4 py-3 text-right text-xs font-medium uppercase tracking-wider text-gray-500">Days Active</th>
                        <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500">Last Active</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-gray-200 bg-white">
                      {paginatedAgents.map((agent, idx) => {
                        const isSelected = selectedAgent?.agent_name === agent.agent_name;
                        const endpointUrl = getEndpointUrl(host, agent.workspace_id);
                        return (
                          <React.Fragment key={idx}>
                            <tr
                              className={`cursor-pointer transition-colors ${isSelected ? "bg-orange-50" : idx % 2 === 0 ? "bg-white hover:bg-gray-50" : "bg-gray-50 hover:bg-gray-100"}`}
                              onClick={() => setSelectedAgent(isSelected ? null : agent)}
                            >
                              <td className="px-4 py-4 text-sm">
                                <div className="flex flex-col gap-0.5">
                                  <span className="font-medium text-[#FF3621] max-w-[250px] truncate" title={agent.agent_name}>
                                    {agent.agent_name}
                                  </span>
                                  <div className="flex items-center gap-2">
                                    {isHistoricalAgent(agent) && (
                                      <span className="inline-flex rounded-full bg-gray-100 px-2 py-0.5 text-[10px] font-medium text-gray-500">Historical</span>
                                    )}
                                    {agent.served_entity_name && (
                                      <span className="max-w-[200px] truncate text-xs text-gray-500">{agent.served_entity_name}</span>
                                    )}
                                  </div>
                                </div>
                              </td>
                              <td className="whitespace-nowrap px-4 py-4 text-sm text-gray-600">
                                {(() => {
                                  const t = agent.agent_type || "Agent";
                                  const cls: Record<string, string> = {
                                    "Knowledge Assistant": "bg-cyan-50 text-cyan-700",
                                    "Supervisor Agent": "bg-amber-50 text-amber-700",
                                    "Genie Space": "bg-blue-50 text-blue-700",
                                    "AI Classify": "bg-green-50 text-green-700",
                                    "AI Extract": "bg-orange-50 text-orange-700",
                                    "Agent": "bg-blue-50 text-blue-700",
                                  };
                                  return (
                                    <span className={`inline-flex rounded-full px-2 py-0.5 text-xs font-medium ${cls[t] || "bg-gray-100 text-gray-600"}`}>
                                      {t}
                                    </span>
                                  );
                                })()}
                              </td>
                              <td className="whitespace-nowrap px-4 py-4 text-right text-sm text-gray-500">
                                {formatNumber(agent.total_dbus)}
                              </td>
                              <td className="whitespace-nowrap px-4 py-4 text-right text-sm text-gray-900">
                                {formatCurrency(agent.total_spend)}
                              </td>
                              <td className="whitespace-nowrap px-4 py-4 text-right text-sm text-gray-500">
                                {formatCurrency(agent.avg_daily_spend)}
                              </td>
                              <td className="whitespace-nowrap px-4 py-4 text-right text-sm text-gray-500">
                                {agent.days_active}
                              </td>
                              <td className="whitespace-nowrap px-4 py-4 text-sm text-gray-500">
                                {agent.last_seen || "—"}
                              </td>
                            </tr>
                            {isSelected && (
                              <tr>
                                <td colSpan={7} className="bg-orange-50 px-4 py-4">
                                  <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
                                    <div className="rounded-lg bg-white p-3 border border-orange-200">
                                      <p className="text-xs text-gray-500">Total Spend</p>
                                      <p className="text-lg font-semibold text-gray-900">{formatCurrency(agent.total_spend)}</p>
                                    </div>
                                    <div className="rounded-lg bg-white p-3 border border-orange-200">
                                      <p className="text-xs text-gray-500">Total DBUs</p>
                                      <p className="text-lg font-semibold text-gray-900">{formatNumber(agent.total_dbus)}</p>
                                    </div>
                                    <div className="rounded-lg bg-white p-3 border border-orange-200">
                                      <p className="text-xs text-gray-500">First Seen</p>
                                      <p className="text-lg font-semibold text-gray-900">{agent.first_seen || "—"}</p>
                                    </div>
                                    <div className="rounded-lg bg-white p-3 border border-orange-200">
                                      <p className="text-xs text-gray-500">Workspaces</p>
                                      <p className="text-lg font-semibold text-gray-900">{agent.workspace_count}</p>
                                    </div>
                                  </div>
                                  {(agent.served_entity_name || agent.billing_origin_product) && (
                                    <div className="mt-3 flex flex-wrap gap-2">
                                      {agent.served_entity_name && (
                                        <span className="inline-flex items-center gap-1 rounded-full bg-gray-100 px-2.5 py-0.5 text-xs text-gray-600">
                                          <span className="font-medium">Model:</span> {agent.served_entity_name}
                                        </span>
                                      )}
                                      {agent.billing_origin_product && (
                                        <span className="inline-flex items-center gap-1 rounded-full bg-gray-100 px-2.5 py-0.5 text-xs text-gray-600">
                                          <span className="font-medium">Product:</span> {agent.billing_origin_product}
                                        </span>
                                      )}
                                    </div>
                                  )}
                                  {endpointUrl && (
                                    <div className="mt-3">
                                      <a href={endpointUrl} target="_blank" rel="noopener noreferrer"
                                        className="btn-brand inline-flex items-center gap-1.5 rounded px-3 py-1.5 text-xs font-medium text-white transition-colors">
                                        <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
                                        </svg>
                                        View in Databricks
                                      </a>
                                    </div>
                                  )}
                                </td>
                              </tr>
                            )}
                          </React.Fragment>
                        );
                      })}
                    </tbody>
                  </table>
                  {agentTotalPages > 1 && (
                    <div className="mt-4 flex items-center justify-between border-t border-gray-200 pt-4">
                      <p className="text-sm text-gray-700">
                        Showing <span className="font-medium">{agentStart + 1}</span> to <span className="font-medium">{Math.min(agentStart + PAGE_SIZE, filteredAgents.length)}</span> of <span className="font-medium">{filteredAgents.length}</span>
                      </p>
                      <div className="flex gap-2">
                        <button onClick={() => setAgentsPage(p => Math.max(1, p - 1))} disabled={agentsPage === 1}
                          className="rounded border border-gray-300 px-3 py-1 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50">Previous</button>
                        <button onClick={() => setAgentsPage(p => Math.min(agentTotalPages, p + 1))} disabled={agentsPage === agentTotalPages}
                          className="rounded border border-gray-300 px-3 py-1 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50">Next</button>
                      </div>
                    </div>
                  )}
                </div>
              ) : (
                <div className="flex h-32 items-center justify-center text-gray-500">No agent bricks data available</div>
              )}
            </>
          );
        })()}
      </div>
    </div>
  );
}
