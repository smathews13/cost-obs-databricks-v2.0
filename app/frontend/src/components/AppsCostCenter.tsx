import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useQueryClient } from "@tanstack/react-query";
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
} from "recharts";
import type { AppsDashboardBundle, AppsApp, AppsConnectedArtifact, DateRange } from "@/types/billing";
import { useAppsDashboardBundle } from "@/hooks/useBillingData";
import { KPITrendModal } from "./KPITrendModal";
import { KPICard } from "@/components/ui/KPICard";
import { VirtualizedList } from "./VirtualizedList";
import { formatIdentity } from "@/utils/identity";
import { appIconSize } from "@/utils/appTiles";
import { C } from "@/theme";
import { PageHero, Chip, InfoPanel } from "@/components/brand";

interface AppsCostCenterProps {
  data: AppsDashboardBundle | undefined;
  isLoading: boolean;
  host?: string | null;
  startDate?: string;
  endDate?: string;
  dateRange?: DateRange;
  enableHostingComparison?: boolean;
  workspaceIds?: string[];
  workspaceNameMap?: Record<string, string>;
}

const APP_COLORS = [C.s2, C.s5, C.s3, C.s3, C.s4, C.s5, C.s1, C.lava, C.slate, C.s5];

const PIE_COLORS = {
  active: C.s3,   // green
  inactive: C.s4, // amber
  // s5 is a real hex token. C.muted is overwritten to an HSL tuple and
  // Recharts treats that as an invalid fill, so the slice rendered black.
  historical: C.s5,
};

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

// ── App Hosting Cost Comparison (Experimental) ────────────────────────────
// TCO data from Databricks Apps vs DIY Infrastructure battlecard.
// Hidden costs that DIY cloud deployments incur per-app, annualized.

interface InfraCostLine {
  label: string;
  lowPerApp: number;   // low estimate per app per year
  highPerApp: number;  // high estimate per app per year
  description: string;
  oneTime?: boolean;   // true = amortised over Year 1 only
}

const DIY_INFRA_COSTS: InfraCostLine[] = [
  { label: "Compute (EC2 m5.large equivalent)", lowPerApp: 840, highPerApp: 840, description: "On-demand EC2 m5.large, 24×7 ($0.096/hr)" },
  { label: "Load Balancer (ALB)", lowPerApp: 200, highPerApp: 400, description: "Application Load Balancer + data processing fees" },
  { label: "NAT Gateway", lowPerApp: 200, highPerApp: 400, description: "NAT gateway for outbound traffic in private subnets" },
  { label: "DevOps & CI/CD Setup", lowPerApp: 8000, highPerApp: 15000, description: "Container orchestration, pipelines, IaC (Year 1)", oneTime: true },
  { label: "Ongoing Maintenance", lowPerApp: 12000, highPerApp: 25000, description: "Patching, upgrades, on-call, incident response" },
  { label: "Security & Compliance", lowPerApp: 5000, highPerApp: 10000, description: "WAF, secrets management, vulnerability scanning (Year 1)", oneTime: true },
  { label: "Observability Stack", lowPerApp: 2500, highPerApp: 5000, description: "APM, logging, dashboards, alerting" },
  { label: "Databricks Data Access Layer", lowPerApp: 7000, highPerApp: 15000, description: "VPN/peering, auth integration for data access (Year 1)", oneTime: true },
];

function AppHostingComparison({
  appSpend,
  daysInRange,
  appName,
}: {
  appSpend: number;
  daysInRange: number;
  appName: string;
}) {
  const [expanded, setExpanded] = useState(false);

  // Annualize this single app's spend from the selected range
  const annualDatabricksSpend = daysInRange > 0 ? (appSpend / daysInRange) * 365 : appSpend * 12;

  // DIY cost estimates for 1 app (Year 1 — includes one-time costs)
  const diyLow = DIY_INFRA_COSTS.reduce((sum, c) => sum + c.lowPerApp, 0);
  const diyHigh = DIY_INFRA_COSTS.reduce((sum, c) => sum + c.highPerApp, 0);
  const diyMid = (diyLow + diyHigh) / 2;

  // Savings
  const savingsLow = diyLow - annualDatabricksSpend;
  const savingsHigh = diyHigh - annualDatabricksSpend;
  const savingsPercent = diyMid > 0 ? ((diyMid - annualDatabricksSpend) / diyMid) * 100 : 0;

  // Bar widths relative to diyHigh
  const maxVal = Math.max(diyHigh, annualDatabricksSpend);
  const databricksBarPct = maxVal > 0 ? (annualDatabricksSpend / maxVal) * 100 : 0;
  const diyLowBarPct = maxVal > 0 ? (diyLow / maxVal) * 100 : 0;
  const diyHighBarPct = maxVal > 0 ? (diyHigh / maxVal) * 100 : 0;

  return (
    <div className="rounded-lg border-2 border-dashed border-amber-300 bg-amber-50/50 p-6">
      {/* Header */}
      <div className="flex items-start justify-between">
        <div className="flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-amber-100">
            <svg className="h-5 w-5 text-amber-600" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M3 6l3 1m0 0l-3 9a5.002 5.002 0 006.001 0M6 7l3 9M6 7l6-2m6 2l3-1m-3 1l-3 9a5.002 5.002 0 006.001 0M18 7l3 9m-3-9l-6-2m0-2v2m0 16V5m0 16H9m3 0h3" />
            </svg>
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h3 className="text-lg font-semibold text-gray-900">App Hosting Cost Comparison</h3>
              <span className="inline-flex items-center gap-1 rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-medium text-amber-700 border border-amber-200">
                <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19.428 15.428a2 2 0 00-1.022-.547l-2.387-.477a6 6 0 00-3.86.517l-.318.158a6 6 0 01-3.86.517L6.05 15.21a2 2 0 00-1.806.547M8 4h8l-1 1v5.172a2 2 0 00.586 1.414l5 5c1.26 1.26.367 3.414-1.415 3.414H4.828c-1.782 0-2.674-2.154-1.414-3.414l5-5A2 2 0 009 10.172V5L8 4z" />
                </svg>
                Experimental
              </span>
            </div>
            <p className="text-sm text-gray-500">
              What would it cost to self-host <strong>{appName}</strong>?
            </p>
          </div>
        </div>
      </div>

      {/* Visual comparison bars */}
      <div className="mt-5 space-y-3">
        {/* Databricks Apps bar */}
        <div>
          <div className="mb-1 flex items-center justify-between text-sm">
            <span className="font-medium text-gray-700">Databricks Apps (Actual)</span>
            <span className="font-semibold text-gray-900">{formatCurrency(annualDatabricksSpend)}<span className="text-xs font-normal text-gray-500">/yr</span></span>
          </div>
          <div className="h-8 w-full rounded-md bg-gray-100 overflow-hidden">
            <div
              className="flex h-full items-center rounded-md px-3 transition-all duration-500"
              style={{ width: `${Math.max(databricksBarPct, 5)}%`, backgroundColor: C.lava }}
            >
              <span className="text-xs font-medium text-white whitespace-nowrap">
                All-inclusive
              </span>
            </div>
          </div>
        </div>

        {/* DIY bar (range) */}
        <div>
          <div className="mb-1 flex items-center justify-between text-sm">
            <span className="font-medium text-gray-700">Self-Hosted DIY (Estimated)</span>
            <span className="font-semibold text-gray-900">{formatCurrency(diyLow)} – {formatCurrency(diyHigh)}<span className="text-xs font-normal text-gray-500">/yr</span></span>
          </div>
          <div className="relative h-8 w-full rounded-md bg-gray-100 overflow-hidden">
            {/* Low end */}
            <div
              className="absolute inset-y-0 left-0 rounded-md bg-gray-400 opacity-50"
              style={{ width: `${Math.max(diyHighBarPct, 5)}%` }}
            />
            <div
              className="absolute inset-y-0 left-0 flex items-center rounded-md bg-gray-500 px-3"
              style={{ width: `${Math.max(diyLowBarPct, 5)}%` }}
            >
              <span className="text-xs font-medium text-white whitespace-nowrap">
                Compute + infra + people
              </span>
            </div>
          </div>
        </div>
      </div>

      {/* Savings callout */}
      {savingsLow > 0 && (
        <div className="mt-4 rounded-lg border border-green-200 bg-green-50 px-4 py-3">
          <div className="flex items-center gap-2">
            <svg className="h-5 w-5 text-green-600" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
            <p className="text-sm font-medium text-green-800">
              Estimated savings: {formatCurrency(savingsLow)} – {formatCurrency(savingsHigh)}/yr ({savingsPercent.toFixed(0)}% lower TCO with Databricks Apps)
            </p>
          </div>
        </div>
      )}

      {/* Expand / collapse detail */}
      <button
        onClick={() => setExpanded(!expanded)}
        className="mt-4 flex items-center gap-1 text-sm font-medium text-amber-700 hover:text-amber-900"
      >
        {expanded ? "Hide" : "Show"} cost breakdown
        <svg className={`h-4 w-4 transition-transform ${expanded ? "rotate-180" : ""}`} fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </button>

      {expanded && (
        <div className="mt-3 animate-fade-in">
          {/* Infrastructure cost breakdown table */}
          <div className="overflow-hidden rounded-lg border border-gray-200">
            <table className="min-w-full divide-y divide-gray-200 text-sm">
              <thead className="bg-gray-50">
                <tr>
                  <th className="px-4 py-2 text-left font-medium text-gray-600">Infrastructure Component</th>
                  <th className="px-4 py-2 text-right font-medium text-gray-600">Est. Cost/yr</th>
                  <th className="px-4 py-2 text-left font-medium text-gray-600">Notes</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100 bg-white">
                {DIY_INFRA_COSTS.map((cost) => (
                  <tr key={cost.label} className="hover:bg-gray-50">
                    <td className="px-4 py-2 text-gray-800">
                      {cost.label}
                      {cost.oneTime && (
                        <span className="ml-1.5 inline-flex rounded bg-blue-50 px-1.5 py-0.5 text-[10px] font-medium text-blue-600 border border-blue-200">
                          Year 1
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-2 text-right font-medium text-gray-900">
                      {formatCurrency(cost.lowPerApp)}{cost.lowPerApp !== cost.highPerApp ? ` – ${formatCurrency(cost.highPerApp)}` : ""}
                    </td>
                    <td className="px-4 py-2 text-xs text-gray-500">{cost.description}</td>
                  </tr>
                ))}
                {/* Totals row */}
                <tr className="bg-gray-50 font-semibold">
                  <td className="px-4 py-2 text-gray-900">DIY Total (Year 1)</td>
                  <td className="px-4 py-2 text-right text-gray-900">
                    {formatCurrency(diyLow)} – {formatCurrency(diyHigh)}
                  </td>
                  <td className="px-4 py-2 text-xs text-gray-500">Includes one-time + recurring</td>
                </tr>
                {/* Databricks Apps row */}
                <tr style={{ backgroundColor: C.coralTint }}>
                  <td className="px-4 py-2 font-semibold" style={{ color: C.lava }}>Databricks Apps (Actual)</td>
                  <td className="px-4 py-2 text-right font-semibold" style={{ color: C.lava }}>
                    {formatCurrency(annualDatabricksSpend)}
                  </td>
                  <td className="px-4 py-2 text-xs text-gray-500">Compute, infra, security, data access — all included</td>
                </tr>
              </tbody>
            </table>
          </div>

          {/* Explanatory note */}
          <div className="mt-3 rounded-lg border border-gray-200 bg-white px-4 py-3">
            <h4 className="text-xs font-semibold uppercase tracking-wide text-gray-500 mb-1">What's included in Databricks Apps</h4>
            <div className="grid grid-cols-2 gap-x-6 gap-y-1 text-xs text-gray-600">
              <div className="flex items-center gap-1.5">
                <svg className="h-3.5 w-3.5 text-green-500" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" /></svg>
                Managed compute & auto-scaling
              </div>
              <div className="flex items-center gap-1.5">
                <svg className="h-3.5 w-3.5 text-green-500" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" /></svg>
                Built-in load balancing
              </div>
              <div className="flex items-center gap-1.5">
                <svg className="h-3.5 w-3.5 text-green-500" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" /></svg>
                Zero DevOps overhead
              </div>
              <div className="flex items-center gap-1.5">
                <svg className="h-3.5 w-3.5 text-green-500" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" /></svg>
                Native data access (Unity Catalog)
              </div>
              <div className="flex items-center gap-1.5">
                <svg className="h-3.5 w-3.5 text-green-500" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" /></svg>
                Enterprise security & SSO
              </div>
              <div className="flex items-center gap-1.5">
                <svg className="h-3.5 w-3.5 text-green-500" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" /></svg>
                Observability & audit logging
              </div>
            </div>
          </div>

          <p className="mt-3 text-[11px] text-gray-500 italic">
            Estimates based on AWS EC2 (m5.large) on-demand pricing and typical enterprise infrastructure costs.
            Year 1 includes one-time setup costs for DevOps, security, and data access layers.
            Actual costs vary by organization size, compliance requirements, and engineering team rates.
          </p>
        </div>
      )}
    </div>
  );
}

function InfoTooltip({ text }: { text: string }) {
  const [pos, setPos] = useState<{ x: number; y: number } | null>(null);
  return (
    <span
      className="ml-1.5 inline-flex cursor-help"
      onMouseEnter={e => setPos({ x: e.clientX, y: e.clientY })}
      onMouseMove={e => setPos({ x: e.clientX, y: e.clientY })}
      onMouseLeave={() => setPos(null)}
    >
      <span className="flex h-4 w-4 items-center justify-center rounded-full bg-gray-200 text-[10px] font-semibold text-gray-500">i</span>
      {pos && createPortal(
        <div
          className="pointer-events-none fixed z-[9999] w-72 rounded-lg bg-gray-900 px-3 py-2 text-xs font-normal leading-relaxed text-white shadow-lg"
          style={{ top: pos.y - 12, transform: "translateY(-100%)", left: Math.min(pos.x + 14, window.innerWidth - 296) }}
        >
          {text}
        </div>,
        document.body
      )}
    </span>
  );
}

export function AppsCostCenter({ data: initialData, isLoading: initialLoading, host, startDate, endDate, dateRange, enableHostingComparison, workspaceIds, workspaceNameMap }: AppsCostCenterProps) {
  const MINIMIZE_KEY = "cost-obs-minimize-apps-info";

  const [infoMinimized, setInfoMinimized] = useState(() => {
    if (typeof window !== "undefined") {
      return localStorage.getItem(MINIMIZE_KEY) === "true";
    }
    return false;
  });

  const [selectedKPI, setSelectedKPI] = useState<{kpi: string; label: string} | null>(null);
  const [selectedApp, setSelectedApp] = useState<AppsApp | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [selectedWorkspaces, setSelectedWorkspaces] = useState<string[]>([]);
  const selectedWorkspacesSeen = useRef<Set<string>>(new Set());
  const [wsFilterOpen, setWsFilterOpen] = useState(false);
  const [wsFilterSearch, setWsFilterSearch] = useState("");
  const [appsPage, setAppsPage] = useState(1);
  const APPS_PAGE_SIZE = 40;
  const [artifactTypeFilters, setArtifactTypeFilters] = useState<string[]>([]);
  const artifactTypeSeen = useRef<Set<string>>(new Set());
  const artifactAppSeen = useRef<Set<string>>(new Set());
  const [artifactTypeDropdownOpen, setArtifactTypeDropdownOpen] = useState(false);
  const [artifactAppFilter, setArtifactAppFilter] = useState<string[]>([]);
  const [artifactAppFilterOpen, setArtifactAppFilterOpen] = useState(false);
  const [artifactAppFilterSearch, setArtifactAppFilterSearch] = useState("");
  const [artifactSearch, setArtifactSearch] = useState("");
  const [artifactPage, setArtifactPage] = useState(1);
  const artifactsPerPage = 10;

  const { data: freshData, isLoading: freshLoading, isError: freshError, error: freshErrorObj, refetch } = useAppsDashboardBundle(dateRange, workspaceIds, true);
  const data = freshData ?? initialData;
  const isLoading = freshLoading || initialLoading;
  const isError = freshError && !data;

  // Close workspace filter dropdown on outside click
  useEffect(() => {
    if (!wsFilterOpen) return;
    const handleClick = (e: MouseEvent) => {
      if (!(e.target as HTMLElement).closest("[data-ws-filter-dropdown]")) {
        setWsFilterOpen(false);
      }
    };
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, [wsFilterOpen]);

  // Close artifact app filter dropdown on outside click
  useEffect(() => {
    if (!artifactAppFilterOpen) return;
    const handleClick = (e: MouseEvent) => {
      if (!(e.target as HTMLElement).closest("[data-artifact-app-dropdown]")) {
        setArtifactAppFilterOpen(false);
      }
    };
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, [artifactAppFilterOpen]);

  const availableArtifactTypes = useMemo(() => {
    const arts = data?.connected_artifacts || [];
    return [...new Set(arts.map((a: AppsConnectedArtifact) => a.artifact_type))].filter((t): t is string => !!t && t !== 'UNKNOWN' && t !== 'Unknown').sort();
  }, [data?.connected_artifacts]);
  const availableArtifactApps = useMemo(() => {
    const arts = data?.connected_artifacts || [];
    return [...new Set(arts.map((a: AppsConnectedArtifact) => a.app_name).filter(Boolean))].sort() as string[];
  }, [data?.connected_artifacts]);

  useEffect(() => {
    const seen = artifactTypeSeen.current;
    const fresh = availableArtifactTypes.filter(x => !seen.has(x));
    if (fresh.length === 0) return;
    setArtifactTypeFilters(prev => Array.from(new Set([...prev, ...fresh])));
    fresh.forEach(x => seen.add(x));
  }, [availableArtifactTypes]);

  useEffect(() => {
    const seen = artifactAppSeen.current;
    const fresh = availableArtifactApps.filter(x => !seen.has(x));
    if (fresh.length === 0) return;
    setArtifactAppFilter(prev => Array.from(new Set([...prev, ...fresh])));
    fresh.forEach(x => seen.add(x));
  }, [availableArtifactApps]);

  // Close artifact type filter dropdown on outside click
  useEffect(() => {
    if (!artifactTypeDropdownOpen) return;
    const handleClick = (e: MouseEvent) => {
      if (!(e.target as HTMLElement).closest("[data-artifact-type-dropdown]")) {
        setArtifactTypeDropdownOpen(false);
      }
    };
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, [artifactTypeDropdownOpen]);

  const handleToggleWorkspace = useCallback((ws: string) => {
    setSelectedWorkspaces(prev =>
      prev.includes(ws) ? prev.filter(w => w !== ws) : [...prev, ws]
    );
  }, []);

  // Pre-warm trend queries so modals open instantly (uses apps-specific endpoint)
  const queryClient = useQueryClient();
  useEffect(() => {
    if (!startDate || !endDate) return;
    for (const kpi of ["apps_spend", "apps_dbus", "apps_count", "apps_avg_cost_per_app"]) {
      queryClient.prefetchQuery({
        queryKey: ["apps-kpi-trend", kpi, startDate, endDate, "daily"],
        queryFn: async () => {
          const params = new URLSearchParams({ kpi, start_date: startDate, end_date: endDate, granularity: "daily" });
          const res = await fetch(`/api/apps/kpi-trend?${params}`);
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

  // Build stable color map for app names across charts
  const appColorMap = useMemo(() => {
    const map: Record<string, string> = {};
    let idx = 0;
    const allNames = new Set<string>();
    for (const app of data?.apps?.apps || []) {
      allNames.add(app.app_name);
    }
    for (const cat of data?.timeseries?.categories || []) {
      if (cat !== "Other") allNames.add(cat);
    }
    for (const name of allNames) {
      map[name] = APP_COLORS[idx % APP_COLORS.length];
      idx++;
    }
    map["Other"] = C.muted;
    return map;
  }, [data?.apps, data?.timeseries]);

  // Resolve workspace names: prefer billing-wide name map (most complete), then
  // backend name, then a formatted "Workspace <id>" so filter rows never show
  // a bare workspace ID when the name is missing (matches the top-nav pattern).
  const resolveWsName = useCallback((wsId: string) => {
    if (workspaceNameMap?.[wsId]) return workspaceNameMap[wsId];
    const backendWs = data?.workspaces?.find(w => w.id === wsId);
    if (backendWs?.name && backendWs.name !== wsId) return backendWs.name;
    return `Workspace ${wsId}`;
  }, [data?.workspaces, workspaceNameMap]);

  // Available workspaces for filtering (resolved to names)
  const workspacesData = data?.workspaces;
  const availableWorkspaces = useMemo(() => {
    if (!workspacesData) return [];
    return workspacesData
      // Drop null/"None"/"null" ids so a stringified null never becomes an option.
      .filter(ws => ws.id != null && !["", "none", "null"].includes(String(ws.id).trim().toLowerCase()))
      .map(ws => {
        const name = resolveWsName(ws.id);
        // No real name resolved (fell back to "Workspace <id>") => the workspace
        // no longer exists in the account — mark it historical.
        return { id: ws.id, name, historical: name === `Workspace ${ws.id}` };
      })
      .sort((a, b) => a.name.localeCompare(b.name));
  }, [workspacesData, resolveWsName]);

  // Sync-add: unseen workspace IDs get added to the filter automatically.
  useEffect(() => {
    const seen = selectedWorkspacesSeen.current;
    const fresh = availableWorkspaces.map(ws => ws.id).filter(id => !seen.has(id));
    if (fresh.length === 0) return;
    setSelectedWorkspaces(prev => Array.from(new Set([...prev, ...fresh])));
    fresh.forEach(id => seen.add(id));
  }, [availableWorkspaces]);

  // workspace_names on apps holds workspace ids; older payloads held names.
  const selectedWorkspaceKeys = useMemo(() => {
    const byId = new Map(availableWorkspaces.map(ws => [ws.id, ws.name]));
    return new Set(selectedWorkspaces.flatMap(id => [id, byId.get(id) ?? id]));
  }, [selectedWorkspaces, availableWorkspaces]);

  // Filter apps by search query and workspace
  const appsBundle = data?.apps;
  const filteredApps = useMemo(() => {
    if (!appsBundle?.apps) return [];
    // Empty or all-selected → treat as "show all" so Clear doesn't silently
    // hide every row and the default "all workspaces" state doesn't drop apps
    // that have no workspace_names (registry-only rows with no billing yet).
    // Only apply the workspace filter when there is an actual partial
    // selection, and pass through apps without workspace data (their
    // workspace_names is [] whenever there's no APPS billing to map from).
    const isPartial = selectedWorkspaces.length > 0
      && selectedWorkspaces.length < availableWorkspaces.length;
    const apps = !isPartial
      ? appsBundle.apps
      : appsBundle.apps.filter(a =>
          !a.workspace_names?.length
          || a.workspace_names.some(ws => selectedWorkspaceKeys.has(ws))
        );
    if (!searchQuery.trim()) return apps;
    const q = searchQuery.toLowerCase();
    return apps.filter(
      (a) => a.app_name.toLowerCase().includes(q) || a.app_id.toLowerCase().includes(q)
    );
  }, [appsBundle, searchQuery, selectedWorkspaces.length, selectedWorkspaceKeys, availableWorkspaces.length]);

  // Reset to page 1 whenever filters change
  const [appsPageFilters, setAppsPageFilters] = useState({ searchQuery, selectedWorkspaces });
  if (appsPageFilters.searchQuery !== searchQuery || appsPageFilters.selectedWorkspaces !== selectedWorkspaces) {
    setAppsPageFilters({ searchQuery, selectedWorkspaces });
    setAppsPage(1);
  }

  // Tiles aren't always in spend order (idle registry apps come first), so scale
  // against the largest spend shown rather than the first tile.
  const maxTileSpend = useMemo(
    () => filteredApps.reduce((m, a) => Math.max(m, Number(a.total_spend) || 0), 0),
    [filteredApps],
  );

  const totalAppsPages = Math.ceil(filteredApps.length / APPS_PAGE_SIZE);
  const effectiveAppsPage = Math.min(appsPage, Math.max(1, totalAppsPages));
  const paginatedApps = filteredApps.slice((effectiveAppsPage - 1) * APPS_PAGE_SIZE, effectiveAppsPage * APPS_PAGE_SIZE);

  // Build pie chart data: Active vs Inactive vs Historical (unregistered)
  const pieData = useMemo(() => {
    if (!appsBundle) return [];
    const slices: { name: string; value: number; fill: string; count: number }[] = [];

    if (appsBundle.active_count > 0) {
      slices.push({
        name: "Active",
        value: appsBundle.active_count,
        fill: PIE_COLORS.active,
        count: appsBundle.active_count,
      });
    }
    if (appsBundle.inactive_count > 0) {
      slices.push({
        name: "Inactive",
        value: appsBundle.inactive_count,
        fill: PIE_COLORS.inactive,
        count: appsBundle.inactive_count,
      });
    }
    if (appsBundle.unregistered_summary.count > 0) {
      slices.push({
        name: "Historical",
        value: appsBundle.unregistered_summary.count,
        fill: PIE_COLORS.historical,
        count: appsBundle.unregistered_summary.count,
      });
    }
    return slices;
  }, [appsBundle]);

  // Daily timeseries (raw from API, matches date picker range)
  const timeseriesData = data?.timeseries;
  const dailyTimeseries = useMemo(() => {
    if (!timeseriesData?.timeseries?.length) return [];
    return timeseriesData.timeseries;
  }, [timeseriesData]);

  if (isLoading) {
    return (
      <div className="flex h-64 flex-col items-center justify-center gap-3">
        <div className="h-12 w-12 animate-spin rounded-full border-4 border-gray-200" style={{ borderTopColor: C.lava }} />
        <p className="text-sm text-gray-500">Loading Apps data...</p>
      </div>
    );
  }

  if (isError) {
    const errMsg = freshErrorObj instanceof Error ? freshErrorObj.message : null;
    return (
      <div className="rounded-lg border border-red-200 bg-red-50 p-6">
        <div className="flex flex-col items-center justify-center gap-3 py-4">
          <p className="text-base font-medium text-red-800">Failed to load Apps data</p>
          {errMsg
            ? <p className="text-sm text-red-700 font-mono text-center">{errMsg}</p>
            : <p className="text-sm text-red-700">Check server logs for details.</p>
          }
          <button
            onClick={() => refetch()}
            className="mt-1 rounded-md px-4 py-2 text-sm font-medium text-white"
            style={{ backgroundColor: C.lava }}
          >
            Retry
          </button>
        </div>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="rounded-lg border border-yellow-200 bg-yellow-50 p-6">
        <div className="flex flex-col items-center justify-center gap-2 py-4">
          <p className="text-base font-medium text-yellow-800">No Apps cost data available</p>
          <p className="text-sm text-yellow-700">Try expanding the date range, or check that Databricks Apps are deployed and active</p>
        </div>
      </div>
    );
  }

  const summary = data.summary;
  const appsData = data.apps;
  const unregisteredSummary = appsData.unregistered_summary;

  const hostBase = host ? (host.startsWith("http") ? host.replace(/\/$/, "") : `https://${host.replace(/\/$/, "")}`) : null;

  /** Live app endpoint URL (the running frontend). */
  const liveEndpoint = (app: AppsApp) => app.app_url || null;

  /** Backend deployment page in the Databricks workspace. */
  const deploymentUrl = (app: AppsApp) =>
    hostBase && !app.deployed_elsewhere ? `${hostBase}/apps/${app.app_name}` : null;

  return (
    <div className="animate-fade-in space-y-6">
      <InfoPanel
        title="Apps tab methodology"
        minimized={infoMinimized}
        onToggle={handleMinimizeToggle}
      >
        <ul className="list-inside list-disc space-y-1">
          <li><strong>Databricks Apps</strong>: Custom web applications deployed and hosted on Databricks</li>
          <li><strong>Active apps</strong>: Registered apps currently reported as running by Databricks</li>
          <li><strong>Inactive apps</strong>: Registered apps not currently running; recent compute usage is used only when live status is unavailable</li>
          <li><strong>Historical apps</strong>: Billing entries with no matching deployed app (deleted or from other workspaces)</li>
          <li>Costs tracked per app from <code className="rounded bg-white/60 px-1">system.billing.usage</code></li>
        </ul>
      </InfoPanel>

      {/* Header */}
      <PageHero
        icon={
          <svg fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2V6zM14 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2V6zM4 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2v-2zM14 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2v-2z" />
          </svg>
        }
        title="Apps"
        subtitle="Databricks Apps compute cost attribution and trends"
        dateRange={dateRange ?? { startDate, endDate }}
        workspaceScope={workspaceIds && workspaceIds.length > 0
          ? workspaceIds.length === 1
            ? (workspaceNameMap?.[workspaceIds[0]] || workspaceIds[0])
            : `${workspaceIds.length} workspaces`
          : undefined}
      />

      {/* Summary Cards with click-to-trend */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <KPICard
          title="Total App Spend"
          value={formatCurrency(summary.total_spend)}
          subtitle={`over ${summary.days_in_range} days`}
          onActivate={startDate && endDate ? () => setSelectedKPI({kpi: "apps_spend", label: "Daily App Spend"}) : undefined}
          icon={
            <svg className="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8c-1.657 0-3 .895-3 2s1.343 2 3 2 3 .895 3 2-1.343 2-3 2m0-8c1.11 0 2.08.402 2.599 1M12 8V7m0 1v8m0 0v1m0-1c-1.11 0-2.08-.402-2.599-1M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
          }
        />
        <KPICard
          title="Total App DBUs"
          value={formatNumber(summary.total_dbus)}
          subtitle={`over ${summary.days_in_range} days`}
          onActivate={startDate && endDate ? () => setSelectedKPI({kpi: "apps_dbus", label: "Daily App DBUs"}) : undefined}
          icon={
            <svg className="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 7h6m0 10v-3m-3 3h.01M9 17h.01M9 14h.01M12 14h.01M15 11h.01M12 11h.01M9 11h.01M7 21h10a2 2 0 002-2V5a2 2 0 00-2-2H7a2 2 0 00-2 2v14a2 2 0 002 2z" />
            </svg>
          }
        />
        <KPICard
          title="Active Apps"
          value={formatNumber(summary.avg_daily_apps ?? summary.app_count)}
          subtitle={`avg. over ${summary.workspace_count} workspaces`}
          infoText="An app is counted as active on any day it generates compute usage. This shows the daily average — how many apps run on a typical day in the selected period. Apps that are deployed but idle (no compute) are not counted."
          onActivate={startDate && endDate ? () => setSelectedKPI({kpi: "apps_count", label: "Daily Active Apps"}) : undefined}
          icon={
            <svg className="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2V6zM14 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2V6zM4 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2v-2zM14 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2v-2z" />
            </svg>
          }
        />
        <KPICard
          title="Per-App Spend"
          value={formatCurrency(summary.avg_cost_per_app ?? 0)}
          subtitle="daily average"
          onActivate={startDate && endDate ? () => setSelectedKPI({kpi: "apps_avg_cost_per_app", label: "Daily Per-App Spend"}) : undefined}
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
          variant="apps"
        />
      )}

      {/* App Status Breakdown + Spend Over Time — side by side */}
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        {/* App Status Breakdown — Pie Chart */}
        {pieData.length > 0 && (
          <div className="rounded-lg bg-white p-6 border " style={{ borderColor: C.hairline }}>
            <h3 className="mb-4 flex items-center text-lg font-semibold text-gray-900">
              App Status Breakdown
              <InfoTooltip text="Active = apps with compute usage in the last 7 days of the selected range (cumulative count). The Active Apps KPI card above shows the daily average — fewer apps run every single day than appear active over any 7-day window, so the two numbers will differ." />
            </h3>
            <div className="flex flex-col items-center gap-6 md:flex-row md:items-start">
              <ResponsiveContainer width="100%" height={250} className="max-w-xs">
                <PieChart>
                  <Pie isAnimationActive={false}
                    data={pieData}
                    cx="50%"
                    cy="50%"
                    innerRadius={55}
                    outerRadius={90}
                    paddingAngle={2}
                    dataKey="value"
                    nameKey="name"
                    label={false}
                    labelLine={false}
                  >
                    {pieData.map((entry, index) => (
                      <Cell key={`cell-${index}`} fill={entry.fill} />
                    ))}
                  </Pie>
                  <Tooltip formatter={(value: number | undefined) => formatNumber(value ?? 0)} />
                </PieChart>
              </ResponsiveContainer>
              <div className="flex flex-col gap-3 text-sm">
                <div className="flex items-center gap-3">
                  <div className="h-3 w-3 rounded-full" style={{ backgroundColor: PIE_COLORS.active }} />
                  <div>
                    <Chip kind="serverless">{formatNumber(appsData.active_count)} Active</Chip>
                    <p className="text-xs text-gray-500">Apps with compute usage in the last 7 days</p>
                  </div>
                </div>
                <div className="flex items-center gap-3">
                  <div className="h-3 w-3 rounded-full" style={{ backgroundColor: PIE_COLORS.inactive }} />
                  <div>
                    <Chip kind="historical">{formatNumber(appsData.inactive_count)} Inactive</Chip>
                    <p className="text-xs text-gray-500">Deployed but no recent compute usage (may still be running at idle)</p>
                  </div>
                </div>
                <div className="flex items-center gap-3">
                  <div className="h-3 w-3 rounded-full" style={{ backgroundColor: PIE_COLORS.historical }} />
                  <div>
                    <Chip kind="workspace">{formatNumber(unregisteredSummary.count)} Historical</Chip>
                    <p className="text-xs text-gray-500">Deleted or unregistered — exist in billing system tables only</p>
                  </div>
                </div>
              </div>
            </div>
          </div>
        )}

        {/* Spend Over Time — daily */}
        <div className="rounded-lg bg-white p-6 border " style={{ borderColor: C.hairline }}>
          <h3 className="mb-4 text-lg font-semibold text-gray-900">Apps Spend Over Time</h3>
          {dailyTimeseries.length > 0 ? (
            <ResponsiveContainer width="100%" height={250}>
              <AreaChart data={dailyTimeseries} margin={{ top: 5, right: 10, left: 0, bottom: 5 }}>
                <XAxis
                  dataKey="date"
                  tickFormatter={(date) => {
                    const d = new Date(date);
                    return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
                  }}
                  stroke={C.muted}
                  fontSize={12}
                  tickMargin={8}
                />
                <YAxis
                  tickFormatter={(value) => formatCurrency(value)}
                  width={70}
                  stroke={C.muted}
                  fontSize={12}
                />
                <Tooltip
                  formatter={(value: number | string | undefined) => formatCurrency(Number(value ?? 0))}
                  labelFormatter={(label) => {
                    const d = new Date(label);
                    return d.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });
                  }}
                />
                <Area isAnimationActive={false}
                  type="monotone"
                  dataKey="Total"
                  stroke={C.lava}
                  fill={C.lava}
                  fillOpacity={0.15}
                  strokeWidth={2}
                />
              </AreaChart>
            </ResponsiveContainer>
          ) : (
            <div className="flex h-64 items-center justify-center text-gray-500">No timeseries data</div>
          )}
        </div>
      </div>

      {/* App Grid — each app is a clickable tile */}
      <div className="rounded-lg bg-white p-6 border " style={{ borderColor: C.hairline }}>
        <div className="mb-4 flex items-center justify-between gap-3">
          <h3 className="shrink-0 text-lg font-semibold text-gray-900">
            Apps by Spend
            <span className="ml-2 text-sm font-normal text-gray-500">
              ({appsData.total_app_count} app{appsData.total_app_count !== 1 ? "s" : ""})
            </span>
          </h3>
          <div className="flex items-center gap-2">
            {/* Workspace filter */}
            {availableWorkspaces.length > 1 && (
              <div className="relative" data-ws-filter-dropdown>
                <button
                  onClick={() => { setWsFilterOpen(!wsFilterOpen); setWsFilterSearch(""); }}
                  className={`flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium transition-colors ${selectedWorkspaces.length > 0 && selectedWorkspaces.length < availableWorkspaces.length ? "border-lava text-lava" : "border-gray-300 text-gray-700 hover:bg-gray-50"}`}
                >
                  {selectedWorkspaces.length === 0 || selectedWorkspaces.length === availableWorkspaces.length
                    ? "Workspace"
                    : selectedWorkspaces.length === 1
                    ? resolveWsName(selectedWorkspaces[0])
                    : `${selectedWorkspaces.length} Workspaces`}
                  <svg className={`h-3 w-3 transition-transform ${wsFilterOpen ? "rotate-180" : ""}`} fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" /></svg>
                </button>
                {wsFilterOpen && (
                  <div className="absolute right-0 top-full z-50 mt-1 w-72 rounded-lg border border-gray-200 bg-white shadow-lg">
                    <div className="p-2">
                      <input
                        type="text"
                        value={wsFilterSearch}
                        onChange={(e) => setWsFilterSearch(e.target.value)}
                        placeholder="Search workspaces..."
                        className="w-full rounded-md border border-gray-300 px-3 py-1.5 text-sm focus:border-orange-500 focus:outline-none focus:ring-1 focus:ring-orange-500"
                        autoFocus
                      />
                    </div>
                    <div>
                      <div className="sticky top-0 flex items-center justify-between border-b border-gray-100 bg-white px-3 py-2">
                        <span className="text-[10px] font-semibold uppercase tracking-wider text-gray-500">Workspaces</span>
                        <div className="flex items-center gap-2 text-xs">
                          <button onClick={(e) => { e.stopPropagation(); setSelectedWorkspaces(availableWorkspaces.map(ws => ws.id)); }} className="text-gray-500 hover:text-gray-800">All</button>
                          <span className="text-gray-300">·</span>
                          <button onClick={(e) => { e.stopPropagation(); setSelectedWorkspaces([]); }} className="text-gray-500 hover:text-gray-800">Clear</button>
                        </div>
                      </div>
                      {(() => {
                        const filtered = availableWorkspaces.filter(ws => !wsFilterSearch || ws.name.toLowerCase().includes(wsFilterSearch.toLowerCase()));
                        if (filtered.length === 0) {
                          return <div className="px-3 py-2 text-sm text-gray-500">No matching workspaces</div>;
                        }
                        return (
                          <VirtualizedList
                            items={filtered}
                            itemHeight={40}
                            maxHeight={240}
                            getKey={(ws) => ws.id}
                            renderItem={(ws) => (
                              <button
                                onClick={() => handleToggleWorkspace(ws.id)}
                                className="flex w-full items-center gap-2 px-3 py-2.5 text-left text-sm hover:bg-gray-50"
                              >
                                <div className={`flex h-4 w-4 shrink-0 items-center justify-center rounded border ${selectedWorkspaces.includes(ws.id) ? "border-orange-500 bg-orange-500" : "border-gray-300"}`}>
                                  {selectedWorkspaces.includes(ws.id) && (
                                    <svg className="h-3 w-3 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" />
                                    </svg>
                                  )}
                                </div>
                                <span className="truncate text-xs text-gray-700">{ws.name}</span>
                                {ws.historical && (
                                  <span className="ml-auto shrink-0 rounded-full border border-amber-200 bg-amber-50 px-1.5 py-0.5 text-[9px] font-medium text-amber-700" title="This workspace no longer exists in the account — data is historical.">
                                    historical
                                  </span>
                                )}
                              </button>
                            )}
                          />
                        );
                      })()}
                    </div>
                  </div>
                )}
              </div>
            )}
            {/* Search */}
            <div className="relative">
              <svg className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-gray-500" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
              </svg>
              <input
                type="text"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                placeholder="Search apps..."
                className="w-44 rounded-full border border-gray-200 bg-white py-1.5 pl-9 pr-4 text-xs placeholder:text-gray-400 focus:border-lava focus:outline-none focus:ring-1 focus:ring-lava"
              />
              {searchQuery && (
                <button
                  onClick={() => setSearchQuery("")}
                  className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-500 hover:text-gray-600"
                >
                  <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                  </svg>
                </button>
              )}
            </div>
            {selectedApp && (
              <button
                onClick={() => setSelectedApp(null)}
                className="text-xs text-gray-500 hover:text-gray-700"
              >
                ← Back to grid
              </button>
            )}
          </div>
        </div>
        {/* Workspace filter pills — only shown for partial selections (all-selected is the default).
            Hard cap and scroll to prevent unbounded pill sprawl with 50+ workspaces. */}
        {selectedWorkspaces.length > 0 && selectedWorkspaces.length < availableWorkspaces.length && (
          selectedWorkspaces.length > 12 ? (
            <div className="mb-4 flex items-center gap-2 text-xs text-gray-600">
              <span className="rounded-full bg-orange-50 px-2.5 py-0.5 font-medium text-lava">
                {selectedWorkspaces.length} of {availableWorkspaces.length} workspaces selected
              </span>
              <span className="inline-flex items-center gap-2">
                <button onClick={() => setSelectedWorkspaces([...availableWorkspaces.map(w => w.id)])} className="text-gray-500 hover:text-gray-700 underline">All</button>
                <span className="text-gray-300">·</span>
                <button onClick={() => setSelectedWorkspaces([])} className="text-gray-500 hover:text-gray-700 underline">Clear</button>
              </span>
            </div>
          ) : (
            <div className="mb-4 flex max-h-20 flex-wrap items-center gap-1.5 overflow-y-auto pr-1">
              {selectedWorkspaces.map(wsId => (
                <span key={wsId} className="inline-flex max-w-[220px] items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-medium text-white" style={{ backgroundColor: C.lava }}>
                  <span className="truncate">{resolveWsName(wsId)}</span>
                  <button onClick={() => handleToggleWorkspace(wsId)} className="ml-0.5 inline-flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded-full hover:bg-white/20">
                    <svg className="h-2.5 w-2.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                    </svg>
                  </button>
                </span>
              ))}
              <span className="inline-flex items-center gap-2 text-xs">
                <button onClick={() => setSelectedWorkspaces([...availableWorkspaces.map(w => w.id)])} className="text-gray-500 hover:text-gray-700">All</button>
                <span className="text-gray-300">·</span>
                <button onClick={() => setSelectedWorkspaces([])} className="text-gray-500 hover:text-gray-700">Clear</button>
              </span>
            </div>
          )
        )}

        {/* Detail panel — shown when an app is selected */}
        {selectedApp && (
          <div className="mb-6 animate-fade-in rounded-lg border border-gray-200 bg-gray-50 p-5">
            <div className="flex items-start justify-between">
              <div className="flex items-center gap-3">
                <div
                  className="flex h-12 w-12 items-center justify-center rounded-lg text-white text-sm font-bold"
                  style={{ backgroundColor: appColorMap[selectedApp.app_name] || APP_COLORS[0] }}
                >
                  <svg className="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2V6zM14 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2V6zM4 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2v-2zM14 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2v-2z" />
                  </svg>
                </div>
                <div>
                  <h4 className="text-base font-semibold text-gray-900">{selectedApp.app_name}</h4>
                  {selectedApp.app_name !== selectedApp.app_id && (
                    <p className="text-[10px] text-gray-500 font-mono">{selectedApp.app_id}</p>
                  )}
                  <div className="flex items-center gap-3">
                    {liveEndpoint(selectedApp) && (
                      <a
                        href={liveEndpoint(selectedApp)!}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-xs text-lava hover:underline"
                      >
                        Live App Endpoint →
                      </a>
                    )}
                    {deploymentUrl(selectedApp) && (
                      <a
                        href={deploymentUrl(selectedApp)!}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-xs text-blue-600 hover:underline"
                      >
                        Backend Deployment →
                      </a>
                    )}
                  </div>
                </div>
              </div>
              <button
                onClick={() => setSelectedApp(null)}
                className="rounded p-1 text-gray-500 hover:bg-gray-200 hover:text-gray-600"
              >
                <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>

            <div className="mt-4 grid grid-cols-2 gap-4 sm:grid-cols-4">
              <div className="rounded-md bg-white p-3 border border-gray-200">
                <p className="text-xs text-gray-500">Spend</p>
                <p className="text-lg font-semibold text-gray-900">{formatCurrency(selectedApp.total_spend)}</p>
              </div>
              <div className="rounded-md bg-white p-3 border border-gray-200">
                <p className="text-xs text-gray-500">DBUs</p>
                <p className="text-lg font-semibold text-gray-900">{formatNumber(selectedApp.total_dbus)}</p>
              </div>
              <div className="rounded-md bg-white p-3 border border-gray-200">
                <p className="text-xs text-gray-500">Days Active</p>
                <p className="text-lg font-semibold text-gray-900">{selectedApp.days_active}</p>
              </div>
              <div className="rounded-md bg-white p-3 border border-gray-200">
                <p className="text-xs text-gray-500">Last Usage</p>
                <p className="text-lg font-semibold text-gray-900">
                  {selectedApp.last_usage_date
                    ? new Date(selectedApp.last_usage_date).toLocaleDateString("en-US", { month: "short", day: "numeric" })
                    : "—"}
                </p>
              </div>
            </div>

            {/* SKU Cost Breakdown */}
            {selectedApp.sku_breakdown && selectedApp.sku_breakdown.length > 0 && (
              <div className="mt-4">
                <h5 className="mb-2 text-xs font-semibold uppercase tracking-wide text-gray-500">Cost Breakdown by SKU</h5>
                <div className="space-y-2">
                  {selectedApp.sku_breakdown.map((sku) => (
                    <div key={sku.sku_name} className="rounded-md border border-gray-200 bg-white px-3 py-2">
                      <div className="flex items-center justify-between">
                        <span className="text-xs font-medium text-gray-800">{sku.sku_name}</span>
                        <span className="text-xs font-semibold text-gray-900">{formatCurrency(sku.total_spend)}</span>
                      </div>
                      <div className="mt-1 flex items-center gap-2">
                        <div className="h-1.5 flex-1 rounded-full bg-gray-100">
                          <div
                            className="h-1.5 rounded-full"
                            style={{ width: `${Math.min(sku.percentage, 100)}%`, backgroundColor: C.lava }}
                          />
                        </div>
                        <span className="text-[10px] text-gray-500">{sku.percentage.toFixed(1)}%</span>
                      </div>
                      <p className="mt-0.5 text-[10px] text-gray-500">{formatNumber(sku.total_dbus)} DBUs</p>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Per-app hosting cost comparison — experimental */}
            {enableHostingComparison && (
              <div className="mt-4 space-y-3">
                <div className="flex items-start gap-3 rounded-lg border border-orange-300 bg-orange-50 px-4 py-3">
                  <svg className="mt-0.5 h-4 w-4 shrink-0 text-orange-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19.428 15.428a2 2 0 00-1.022-.547l-2.387-.477a6 6 0 00-3.86.517l-.318.158a6 6 0 01-3.86.517L6.05 15.21a2 2 0 00-1.806.547M8 4h8l-1 1v5.172a2 2 0 00.586 1.414l5 5c1.26 1.26.367 3.414-1.415 3.414H4.828c-1.782 0-2.674-2.154-1.414-3.414l5-5A2 2 0 009 10.172V5L8 4z" />
                  </svg>
                  <div>
                    <p className="text-sm font-semibold text-orange-800">Experimental Feature</p>
                    <p className="mt-0.5 text-xs text-orange-700">
                      Hosting cost comparisons are estimates based on industry benchmarks and may not reflect actual infrastructure costs.
                    </p>
                  </div>
                </div>
                <AppHostingComparison
                  appSpend={selectedApp.total_spend}
                  daysInRange={summary.days_in_range}
                  appName={selectedApp.app_name}
                />
              </div>
            )}

            <div className="mt-3 flex items-center gap-4 text-xs text-gray-500">
              <span>{selectedApp.percentage.toFixed(1)}% of total spend</span>
              <span>Workspace count: {selectedApp.workspace_count}</span>
              {selectedApp.deployed_elsewhere ? (
                <span className="rounded bg-gray-100 px-1.5 py-0.5 text-gray-600">Deployed in another workspace</span>
              ) : !selectedApp.is_registered && (
                <span className="rounded bg-yellow-100 px-1.5 py-0.5 text-yellow-700">Not in Apps registry — may be deleted</span>
              )}
            </div>
          </div>
        )}

        {/* Tile grid */}
        {filteredApps.length > 0 ? (
          <div className="grid grid-cols-3 items-start gap-3 sm:grid-cols-4 md:grid-cols-6 lg:grid-cols-8">
            {paginatedApps.map((app, idx) => {
              const isSelected = selectedApp?.app_id === app.app_id;
              const color = appColorMap[app.app_name] || APP_COLORS[idx % APP_COLORS.length];
              const isResolved = app.app_name !== app.app_id;

              const iconSize = appIconSize(app.total_spend, maxTileSpend);

              return (
                <button
                  key={app.app_id}
                  onClick={() => setSelectedApp(isSelected ? null : app)}
                  className={`group relative flex flex-col items-center justify-center rounded-lg border-2 p-3 transition-all hover:shadow-md ${
                    isSelected
                      ? "border-lava shadow-md scale-105"
                      : "border-gray-200 hover:border-gray-300"
                  }`}
                  title={`${app.app_name}${isResolved ? ` (${app.app_id})` : ""}\n${formatCurrency(app.total_spend)} · ${app.days_active}d active`}
                >
                  {/* App icon — letter avatar */}
                  <div
                    className="flex items-center justify-center rounded-md text-white transition-transform group-hover:scale-110"
                    style={{ backgroundColor: color, width: iconSize, height: iconSize }}
                  >
                    <span className="font-bold select-none" style={{ fontSize: Math.max(14, iconSize * 0.4) }}>
                      {(app.app_name || app.app_id || "?").charAt(0).toUpperCase()}
                    </span>
                  </div>
                  {/* App name */}
                  <span className="mt-1.5 w-full truncate text-center text-[10px] font-medium text-gray-700">
                    {app.app_name}
                  </span>
                  {/* Spend label */}
                  <span className="text-[9px] text-gray-500">
                    {formatCurrency(app.total_spend)}
                  </span>
                </button>
              );
            })}
          </div>
        ) : searchQuery ? (
          <div className="flex h-32 items-center justify-center text-gray-500">
            No apps matching "{searchQuery}"
          </div>
        ) : (
          // Not user-filter driven — likely still-warming warehouse or a
          // response we short-cached at 60s. Show the spinner instead of a
          // definitive "No apps found" so the UI doesn't imply confirmed
          // empty when we haven't finished loading.
          <div className="flex h-32 flex-col items-center justify-center gap-3">
            <div className="h-10 w-10 animate-spin rounded-full border-4 border-gray-200" style={{ borderTopColor: C.lava }} />
            <p className="text-sm text-gray-500">Loading apps…</p>
          </div>
        )}

        {/* Pagination */}
        {filteredApps.length > 0 && (
          <div className="mt-4 flex items-center justify-between">
            <span className="text-xs text-gray-500">
              {filteredApps.length} app{filteredApps.length !== 1 ? "s" : ""}
              {searchQuery ? ` matching "${searchQuery}"` : ""}
            </span>
            {totalAppsPages > 1 && (
              <div className="flex items-center gap-1">
                <button
                  onClick={() => setAppsPage(p => Math.max(1, p - 1))}
                  disabled={effectiveAppsPage === 1}
                  className="rounded border border-gray-200 px-2.5 py-1 text-xs text-gray-600 hover:bg-gray-50 disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  ‹ Prev
                </button>
                <span className="px-2 text-xs text-gray-500">{effectiveAppsPage} / {totalAppsPages}</span>
                <button
                  onClick={() => setAppsPage(p => Math.min(totalAppsPages, p + 1))}
                  disabled={effectiveAppsPage >= totalAppsPages}
                  className="rounded border border-gray-200 px-2.5 py-1 text-xs text-gray-600 hover:bg-gray-50 disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  Next ›
                </button>
              </div>
            )}
          </div>
        )}
      </div>

      {/* Connected Resources & Artifacts */}
      {data?.connected_artifacts && data.connected_artifacts.length > 0 && (() => {
        const artifactTypes = [...new Set(data.connected_artifacts.map((a: AppsConnectedArtifact) => a.artifact_type))].filter((t: string) => t && t !== 'UNKNOWN' && t !== 'Unknown').sort();
        const allAppNames = [...new Set(data.connected_artifacts.map((a: AppsConnectedArtifact) => a.app_name).filter(Boolean))].sort() as string[];
        let filteredArtifacts = artifactTypeFilters.length === 0
          ? data.connected_artifacts
          : data.connected_artifacts.filter((a: AppsConnectedArtifact) => artifactTypeFilters.includes(a.artifact_type));
        if (artifactAppFilter.length > 0) {
          filteredArtifacts = filteredArtifacts.filter((a: AppsConnectedArtifact) => artifactAppFilter.includes(a.app_name || ''));
        }
        if (artifactSearch) {
          const q = artifactSearch.toLowerCase();
          filteredArtifacts = filteredArtifacts.filter((a: AppsConnectedArtifact) =>
            a.app_name?.toLowerCase().includes(q) ||
            a.artifact_name?.toLowerCase().includes(q) ||
            a.artifact_type?.toLowerCase().includes(q) ||
            a.artifact_description?.toLowerCase().includes(q)
          );
        }
        const filteredAppNames = allAppNames.filter(n => !artifactAppFilterSearch || n.toLowerCase().includes(artifactAppFilterSearch.toLowerCase()));
        const totalArtifactPages = Math.ceil(filteredArtifacts.length / artifactsPerPage);
        const safePage = Math.min(artifactPage, totalArtifactPages || 1);
        const paginatedArtifacts = filteredArtifacts.slice((safePage - 1) * artifactsPerPage, safePage * artifactsPerPage);

        return (
          <div className="rounded-lg bg-white p-6 border " style={{ borderColor: C.hairline }}>
            {/* Single toolbar row: title · filters · search */}
            <div className="mb-4 flex flex-wrap items-center gap-2">
              <h3 className="mr-2 text-lg font-semibold text-gray-900 shrink-0">Connected Resources</h3>

              <div className="ml-auto flex shrink-0 items-center gap-2">

              {/* App filter dropdown */}
              <div className="relative" data-artifact-app-dropdown>
                <button
                  onClick={() => { setArtifactAppFilterOpen(v => !v); setArtifactAppFilterSearch(""); }}
                  className={`flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium transition-colors ${artifactAppFilter.length > 0 && artifactAppFilter.length < allAppNames.length ? "border-lava text-lava" : "border-gray-300 bg-white text-gray-700 hover:border-gray-400"}`}
                >
                  {artifactAppFilter.length === 0
                    ? 'Filter by app'
                    : artifactAppFilter.length === 1
                    ? artifactAppFilter[0]
                    : artifactAppFilter.length === allAppNames.length
                    ? 'Filter by app'
                    : `${artifactAppFilter.length} apps`}
                  <svg className={`h-3 w-3 transition-transform ${artifactAppFilterOpen ? 'rotate-180' : ''}`} fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                  </svg>
                </button>
                {artifactAppFilterOpen && (
                  <div className="absolute right-0 top-full z-[9999] mt-1 w-64 max-h-72 overflow-y-auto rounded-lg border border-gray-200 bg-white shadow-lg">
                    <div className="sticky top-0 flex items-center justify-between border-b border-gray-100 bg-white px-3 py-2">
                      <span className="text-[10px] font-semibold uppercase tracking-wider text-gray-500">App</span>
                      <div className="flex items-center gap-2 text-xs">
                        <button onClick={(e) => { e.stopPropagation(); setArtifactAppFilter([...allAppNames]); setArtifactPage(1); }} className="text-gray-500 hover:text-gray-800">All</button>
                        <span className="text-gray-300">·</span>
                        <button onClick={(e) => { e.stopPropagation(); setArtifactAppFilter([]); setArtifactPage(1); }} className="text-gray-500 hover:text-gray-800">Clear</button>
                      </div>
                    </div>
                    <div className="p-2">
                      <input
                        autoFocus
                        type="text"
                        value={artifactAppFilterSearch}
                        onChange={e => setArtifactAppFilterSearch(e.target.value)}
                        placeholder="Search apps..."
                        className="w-full rounded border border-gray-200 px-2 py-1.5 text-xs focus:border-lava focus:outline-none focus:ring-1 focus:ring-lava"
                      />
                    </div>
                    {filteredAppNames.length === 0 ? (
                      <p className="px-3 py-2 text-xs text-gray-500">No apps found</p>
                    ) : (
                      <VirtualizedList
                        items={filteredAppNames}
                        itemHeight={36}
                        maxHeight={240}
                        getKey={(n) => n}
                        renderItem={(name) => (
                          <button
                            onClick={() => {
                              setArtifactAppFilter(prev => prev.includes(name) ? prev.filter(x => x !== name) : [...prev, name]);
                              setArtifactPage(1);
                            }}
                            className="flex w-full items-center gap-2 px-3 py-2.5 text-left text-xs hover:bg-gray-50"
                          >
                            <div className={`flex h-4 w-4 shrink-0 items-center justify-center rounded border ${artifactAppFilter.includes(name) ? "border-orange-500 bg-orange-500" : "border-gray-300"}`}>
                              {artifactAppFilter.includes(name) && <svg className="h-3 w-3 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" /></svg>}
                            </div>
                            <span className="truncate text-gray-700">{name}</span>
                          </button>
                        )}
                      />
                    )}
                  </div>
                )}
              </div>

              {/* Resource type multi-select dropdown */}
              <div className="relative" data-artifact-type-dropdown>
                <button
                  onClick={() => setArtifactTypeDropdownOpen(v => !v)}
                  className={`flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium transition-colors ${artifactTypeFilters.length > 0 && artifactTypeFilters.length < artifactTypes.length ? "border-lava text-lava" : "border-gray-300 bg-white text-gray-700 hover:border-gray-400"}`}
                >
                  {artifactTypeFilters.length === 0
                    ? 'Resource type'
                    : artifactTypeFilters.length === 1
                    ? artifactTypeFilters[0].replace(/_/g, ' ')
                    : artifactTypeFilters.length === artifactTypes.length
                    ? 'Resource type'
                    : `${artifactTypeFilters.length} types`}
                  <svg className={`h-3 w-3 transition-transform ${artifactTypeDropdownOpen ? 'rotate-180' : ''}`} fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                  </svg>
                </button>
                {artifactTypeDropdownOpen && (
                  <div className="absolute right-0 top-full z-[9999] mt-1 w-56 max-h-72 overflow-y-auto rounded-lg border border-gray-200 bg-white shadow-lg">
                    <div className="sticky top-0 flex items-center justify-between border-b border-gray-100 bg-white px-3 py-2">
                      <span className="text-[10px] font-semibold uppercase tracking-wider text-gray-500">Types</span>
                      <div className="flex items-center gap-2 text-xs">
                        <button onClick={(e) => { e.stopPropagation(); setArtifactTypeFilters([...(artifactTypes as string[])]); setArtifactPage(1); }} className="text-gray-500 hover:text-gray-800">All</button>
                        <span className="text-gray-300">·</span>
                        <button onClick={(e) => { e.stopPropagation(); setArtifactTypeFilters([]); setArtifactPage(1); }} className="text-gray-500 hover:text-gray-800">Clear</button>
                      </div>
                    </div>
                    {artifactTypes.map((type: string) => {
                      const count = data.connected_artifacts.filter((a: AppsConnectedArtifact) => a.artifact_type === type).length;
                      const checked = artifactTypeFilters.includes(type);
                      return (
                        <button
                          key={type}
                          onClick={() => {
                            setArtifactTypeFilters(prev => prev.includes(type) ? prev.filter(t => t !== type) : [...prev, type]);
                            setArtifactPage(1);
                          }}
                          className="flex w-full items-center gap-2 px-3 py-2.5 text-left text-xs hover:bg-gray-50"
                        >
                          <div className={`flex h-4 w-4 shrink-0 items-center justify-center rounded border ${checked ? "border-orange-500 bg-orange-500" : "border-gray-300"}`}>
                            {checked && <svg className="h-3 w-3 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" /></svg>}
                          </div>
                          <span className="flex-1 truncate text-gray-700">{type.replace(/_/g, ' ')}</span>
                          <span className="text-gray-500">({count})</span>
                        </button>
                      );
                    })}
                  </div>
                )}
              </div>

              {/* Search */}
              <div className="relative shrink-0">
                <svg className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-gray-500" fill="none" viewBox="0 0 24 24" strokeWidth={2} stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
                </svg>
                <input
                  type="text"
                  value={artifactSearch}
                  onChange={(e) => { setArtifactSearch(e.target.value); setArtifactPage(1); }}
                  placeholder="Search resources..."
                  className="w-44 rounded-full border border-gray-200 bg-white py-1.5 pl-9 pr-4 text-xs placeholder:text-gray-400 focus:border-lava focus:outline-none focus:ring-1 focus:ring-lava"
                />
              </div>

              </div>{/* end ml-auto group */}
            </div>

            <div className="overflow-x-auto">
              <table className="min-w-full">
                <thead className="bg-gray-50">
                  <tr className="border-b border-gray-200">
                    <th className="px-3 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500">Resource</th>
                    <th className="px-3 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500">Resource Name</th>
                    <th className="px-3 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500">Resource Type</th>
                    <th className="px-3 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500">Details</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-200">
                  {paginatedArtifacts.map((artifact: AppsConnectedArtifact, idx: number) => {
                    const artifactUrl = hostBase ? (
                      artifact.artifact_type === 'SERVING_ENDPOINT' ? `${hostBase}/ml/endpoints/${artifact.artifact_name}` :
                      artifact.artifact_type === 'SQL_WAREHOUSE' ? `${hostBase}/sql/warehouses/${artifact.artifact_name}` :
                      artifact.artifact_type === 'JOB' ? `${hostBase}/jobs/${artifact.artifact_name}` :
                      artifact.artifact_type === 'SECRET' ? `${hostBase}/secrets/scopes` :
                      null
                    ) : null;

                    const na = (v: string | null | undefined) => (!v || v === 'Unknown' || v === 'UNKNOWN') ? 'N/A' : v;
                    const displayType = na(artifact.artifact_type);
                    const appBackendUrl = hostBase && artifact.app_name ? `${hostBase}/apps/${artifact.app_name}` : null;

                    const isSP = artifact.artifact_type === 'SERVICE_PRINCIPAL';
                    const displayName = isSP ? formatIdentity(artifact.artifact_name) : na(artifact.artifact_name);

                    return (
                      <tr key={`${artifact.app_id}-${artifact.artifact_name}-${idx}`} className="hover:bg-gray-50">
                        <td className="whitespace-nowrap px-3 py-3 text-sm font-medium">
                          {appBackendUrl ? (
                            <a href={appBackendUrl} target="_blank" rel="noopener noreferrer" className="group flex items-center gap-1 text-lava hover:text-lava-hover">
                              <span>{na(artifact.app_name)}</span>
                              <svg className="h-3 w-3 shrink-0 opacity-0 transition-opacity group-hover:opacity-100" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
                              </svg>
                            </a>
                          ) : (
                            <span className="text-gray-900">{na(artifact.app_name)}</span>
                          )}
                        </td>
                        <td className="px-3 py-3 text-sm">
                          <div className="flex flex-col gap-0.5">
                            {artifactUrl ? (
                              <a href={artifactUrl} target="_blank" rel="noopener noreferrer" className="group flex items-center gap-1 font-medium text-lava hover:text-lava-hover">
                                <span title={artifact.artifact_name}>{displayName}</span>
                                <svg className="h-3 w-3 shrink-0 opacity-0 transition-opacity group-hover:opacity-100" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
                                </svg>
                              </a>
                            ) : (
                              <span className="text-gray-700" title={artifact.artifact_name}>{displayName}</span>
                            )}
                          </div>
                        </td>
                        <td className="px-3 py-3">
                          <span className={`inline-flex items-center whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-medium ${
                            artifact.artifact_type === 'SERVING_ENDPOINT' ? 'bg-blue-50 text-blue-700' :
                            artifact.artifact_type === 'SQL_WAREHOUSE' ? 'bg-blue-100 text-blue-700' :
                            artifact.artifact_type === 'SECRET' ? 'bg-yellow-100 text-yellow-700' :
                            artifact.artifact_type === 'JOB' ? 'bg-green-100 text-green-700' :
                            artifact.artifact_type === 'SERVICE_PRINCIPAL' ? 'bg-orange-100 text-orange-700' :
                            artifact.artifact_type === 'LAKEBASE' ? 'bg-cyan-50 text-cyan-700' :
                            'bg-gray-100 text-gray-700'
                          }`}>
                            {displayType.replace(/_/g, ' ')}
                          </span>
                        </td>
                        <td className="px-3 py-3 text-sm text-gray-500">{na(artifact.artifact_description)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>

            {/* Pagination */}
            {totalArtifactPages > 1 && (
              <div className="mt-4 flex items-center justify-between border-t border-gray-200 pt-4">
                <p className="text-sm text-gray-500">
                  Showing {(safePage - 1) * artifactsPerPage + 1}–{Math.min(safePage * artifactsPerPage, filteredArtifacts.length)} of {filteredArtifacts.length}
                </p>
                <div className="flex gap-2">
                  <button
                    onClick={() => setArtifactPage(Math.max(1, safePage - 1))}
                    disabled={safePage === 1}
                    className="rounded border border-gray-300 px-3 py-1 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    Previous
                  </button>
                  {Array.from({ length: totalArtifactPages }, (_, i) => i + 1)
                    .filter((p) => p === 1 || p === totalArtifactPages || (p >= safePage - 1 && p <= safePage + 1))
                    .map((p, idx, arr) => {
                      const prev = arr[idx - 1];
                      const showEllipsis = prev && p - prev > 1;
                      return (
                        <span key={p} className="flex items-center">
                          {showEllipsis && <span className="px-2 py-1 text-gray-500">...</span>}
                          <button
                            onClick={() => setArtifactPage(p)}
                            className={`rounded px-3 py-1 text-sm font-medium ${
                              safePage === p ? 'text-white' : 'border border-gray-300 text-gray-700 hover:bg-gray-50'
                            }`}
                            style={safePage === p ? { backgroundColor: C.lava } : undefined}
                          >
                            {p}
                          </button>
                        </span>
                      );
                    })}
                  <button
                    onClick={() => setArtifactPage(Math.min(totalArtifactPages, safePage + 1))}
                    disabled={safePage === totalArtifactPages}
                    className="rounded border border-gray-300 px-3 py-1 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    Next
                  </button>
                </div>
              </div>
            )}
          </div>
        );
      })()}

    </div>
  );
}
