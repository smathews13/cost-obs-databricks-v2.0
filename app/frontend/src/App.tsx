import React, { useState, useEffect, useMemo, useRef, useCallback, lazy, Suspense } from "react";
import { QueryClient, QueryClientProvider, useIsFetching, useQuery, useQueryClient } from "@tanstack/react-query";
import { TabRefreshRegion } from "@/components/TabRefreshRegion";
import { SetupWizard } from "@/components/SetupWizard";
import { SummaryCards } from "@/components/SummaryCards";
import { SpendChart } from "@/components/SpendChart";
import { ProductBreakdown } from "@/components/ProductBreakdown";
import { WorkspaceTable } from "@/components/WorkspaceTable";
import { PipelineObjectsTable } from "@/components/PipelineObjectsTable";
import { DateRangePicker } from "@/components/DateRangePicker";
import { WorkspaceFilter } from "@/components/WorkspaceFilter";
import { SourceLabelFilter } from "@/components/SourceLabelFilter";
import { TopProgressBar } from "@/components/TopProgressBar";
import { SKUBreakdown } from "@/components/SKUBreakdown";
import { ExportDialog, getRequiredExportTabs, type ExportSections, type ExportFormat } from "@/components/ExportDialog";
import { SettingsDialog, loadTabVisibility, loadAppSettings, firstVisibleDashboardTab, type TabVisibility, type AppSettings } from "@/components/SettingsDialog";
import { PricingProvider, usePricing } from "@/context/PricingContext";
import { SpNameMapContext } from "@/utils/identity";
import { Footer } from "@/components/Footer";
import { C } from "@/theme";
import { CostObsLockup, VersionPill, PageHero, InfoPanel } from "@/components/brand";
import { Spinner } from "@/components/Spinner";
import { UserMenu } from "@/components/UserMenu";
import { DeploymentBadgeFromApi } from "@/components/DeploymentBadge";
import {
  WarehouseGuidanceBanner,
  WarehouseHealthCheckBanner,
} from "@/components/WarehouseGuidanceBanner";
import { applyInfraPricing } from "@/utils/cloudCosts";
import {
  fetchWarehouseHealth,
  warehouseHealthPollInterval,
  type WarehouseHealth,
} from "@/utils/warehouseGuidance";
import { downloadArchitecturePdf } from "@/utils/architectureDownload";
import { Bot, Check, Copy, Database, Settings } from "lucide-react";

// Retry a dynamic import once on failure (handles cold-start chunk load errors)
function lazyWithRetry<T>(factory: () => Promise<T>): Promise<T> {
  return factory().catch(() => factory());
}

// Lazy-loaded tab views — chunks download on first render
const InteractiveBreakdown = lazy(() => lazyWithRetry(() => import("@/components/InteractiveBreakdown").then(m => ({ default: m.InteractiveBreakdown }))));
const CloudCostsView = lazy(() => lazyWithRetry(() => import("@/components/CloudCostsView").then(m => ({ default: m.CloudCostsView }))));
const PlatformKPIsView = lazy(() => lazyWithRetry(() => import("@/components/PlatformKPIsView").then(m => ({ default: m.PlatformKPIsView }))));
// AIMLCostCenter is no longer a top-level tab component; it is imported and
// rendered as the "Cost" sub-tab inside AIGatewayView (V2.1_SPEC §4.3). Kept
// in the preload list so its chunk is warmed before the user opens the tab.
const AIGatewayView = lazy(() => lazyWithRetry(() => import("@/components/AIGatewayView").then(m => ({ default: m.AIGatewayView }))));
const GenieCostView = lazy(() => lazyWithRetry(() => import("@/components/GenieCostView").then(m => ({ default: m.GenieCostView }))));
const AppsCostCenter = lazy(() => lazyWithRetry(() => import("@/components/AppsCostCenter").then(m => ({ default: m.AppsCostCenter }))));
const TaggingHub = lazy(() => lazyWithRetry(() => import("@/components/TaggingHub").then(m => ({ default: m.TaggingHub }))));
const SQLWarehousing360 = lazy(() => lazyWithRetry(() => import("@/components/SQLWarehousing360").then(m => ({ default: m.SQLWarehousing360 }))));
const WarehouseRightsizingView = lazy(() => lazyWithRetry(() => import("@/components/SQLWarehousing360").then(m => ({ default: m.WarehouseRightsizingView }))));
const WarehouseIdleTimeView = lazy(() => lazyWithRetry(() => import("@/components/SQLWarehousing360").then(m => ({ default: m.WarehouseIdleTimeView }))));
const OptimizeMethodologyPanel = lazy(() => lazyWithRetry(() => import("@/components/SQLWarehousing360").then(m => ({ default: m.OptimizeMethodologyPanel }))));
// ForecastingView removed from active app (internal-only; excluded from external mirror)
const UseCases = lazy(() => lazyWithRetry(() => import("@/pages/UseCases")));
const UsersGroups = lazy(() => lazyWithRetry(() => import("@/pages/UsersGroups")));
// BudgetView removed from active app (internal-only; excluded from external mirror)
const ConversationalDrawer = lazy(() => lazyWithRetry(() => import("@/components/ConversationalDrawer").then(m => ({ default: m.ConversationalDrawer }))));

// Preload all lazy chunks during browser idle time so tab switches are instant.
// React.lazy caches the promise, so these import() calls prime the module cache
// before the component ever renders.
function preloadTabChunks() {
  import("@/components/InteractiveBreakdown");
  import("@/components/CloudCostsView");
  import("@/components/PlatformKPIsView");
  import("@/components/AIMLCostCenter");
  import("@/components/AIGatewayView");
  import("@/components/GenieCostView");
  import("@/components/AppsCostCenter");
  import("@/components/TaggingHub");
  import("@/components/SQLWarehousing360");
  import("@/pages/UseCases");
  import("@/pages/UsersGroups");
}

import {
  useAccountInfo,
  useCloudCostsBundle,
  useDashboardBundleFast,
  useSqlBreakdown,
  usePipelineObjects,
  useInteractiveBreakdown,
  useSKUBreakdown,
  useDefaultDateRange,
  useAIMLDashboardBundle,
  useAIGatewayDashboardBundle,
  useGenieCostDashboardBundle,
  useAppsDashboardBundle,
  useTaggingDashboardBundle,
  useDBSQLQueryCosts,
  useDBSQLTopQueries,
  useKPIsBundle,
  useUsersGroupsBundle,
  fetchJson,
} from "@/hooks/useBillingData";
import type { DateRange, WorkspaceBreakdown } from "@/types/billing";
import { generateCostReport } from "@/utils/pdfExport";
import { generateCostCSV } from "@/utils/csvExport";

// Keep the Databricks Apps pod warm while the tab is open.
// Cold starts take 30s–1min; a lightweight ping every 4 min prevents idle suspension.
function useKeepAlive() {
  useEffect(() => {
    const ping = () => fetch("/api/ping", { method: "GET" }).catch(() => {});
    const interval = setInterval(ping, 4 * 60 * 1000);
    const onVisible = () => { if (document.visibilityState === "visible") ping(); };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(interval);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, []);
}

// AI/ML content is now under the Unity Gateway tab as the default "Cost" sub-tab
// (V2.1_SPEC §4.3). "aiml" is no longer a top-level tab value.
type ViewTab = "dbu" | "sql" | "infra" | "optimizer" | "kpis" | "ai-gateway" | "genie" | "apps" | "tagging" | "use-cases" | "users-groups";

const DASHBOARD_TABS: Array<{ id: ViewTab; label: string; icon: React.ReactNode }> = [
  {
    id: "dbu",
    label: "$DBU Spend",
    icon: <svg className="mr-2 -mt-0.5 inline h-4 w-4" viewBox="0 0 24 24" fill="currentColor"><path d="M20 4H4L7 8L4 12H20L17 8L20 4Z" opacity="0.9"/><path d="M20 8H4L7 12L4 16H20L17 12L20 8Z" opacity="0.7"/><path d="M20 12H4L7 16L4 20H20L17 16L20 12Z" opacity="0.5"/></svg>,
  },
  {
    id: "sql",
    label: "SQL",
    icon: <svg className="mr-2 -mt-0.5 inline h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 3v2m6-2v2M9 19v2m6-2v2M5 9H3m2 6H3m18-6h-2m2 6h-2M7 19h10a2 2 0 002-2V7a2 2 0 00-2-2H7a2 2 0 00-2 2v10a2 2 0 002 2zM9 9h6v6H9V9z" /></svg>,
  },
  {
    id: "ai-gateway",
    label: "Unity Gateway",
    icon: <svg className="mr-2 -mt-0.5 inline h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" /></svg>,
  },
  {
    id: "genie",
    label: "Genie",
    icon: <svg className="mr-2 -mt-0.5 inline h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" /></svg>,
  },
  {
    id: "apps",
    label: "Apps",
    icon: <svg className="mr-2 -mt-0.5 inline h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2V6zM14 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2V6zM4 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2v-2zM14 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2v-2z" /></svg>,
  },
  {
    id: "tagging",
    label: "Tagging",
    icon: <svg className="mr-2 -mt-0.5 inline h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M7 7h.01M7 3h5c.512 0 1.024.195 1.414.586l7 7a2 2 0 010 2.828l-7 7a2 2 0 01-2.828 0l-7-7A1.994 1.994 0 013 12V7a4 4 0 014-4z" /></svg>,
  },
  {
    id: "users-groups",
    label: "Users",
    icon: <svg className="mr-2 -mt-0.5 inline h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z" /></svg>,
  },
  {
    id: "kpis",
    label: "KPIs & Trends",
    icon: <svg className="mr-2 -mt-0.5 inline h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" /></svg>,
  },
  {
    id: "infra",
    label: "Cloud Costs",
    icon: <svg className="mr-2 -mt-0.5 inline h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M3 15a4 4 0 004 4h9a5 5 0 10-.1-9.999 5.002 5.002 0 10-9.78 2.096A4.001 4.001 0 003 15z" /></svg>,
  },
  {
    id: "optimizer",
    label: "Optimize",
    icon: <svg className="mr-2 -mt-0.5 inline h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 6V4m0 2a2 2 0 100 4m0-4a2 2 0 110 4m-6 8a2 2 0 100-4m0 4a2 2 0 110-4m0 4v2m0-6V4m6 6v10m6-2a2 2 0 100-4m0 4a2 2 0 110-4m0 4v2m0-6V4" /></svg>,
  },
];

const TAB_LOADING_SECTIONS: Record<ViewTab, string[]> = {
  dbu: ["Spend Over Time", "Spend by Product", "Spend by SKU", "Workspace Breakdown", "Jobs and Pipelines", "Interactive Compute"],
  sql: ["Query Spend Summary", "Spend by Source", "Warehouse Spend", "SKU Breakdown", "Top Users by Query Spend", "Top Queries"],
  infra: ["Infrastructure Costs", "Cluster Costs", "Usage by Instance Family", "Cluster Cost Breakdown"],
  optimizer: ["Idle Time Analysis", "Rightsizing Recommendations"],
  kpis: ["Platform KPIs", "Data Activity", "Compute Activity", "Spend Changes and Trends"],
  "ai-gateway": ["Unity Gateway Spend", "Services", "Models", "Users", "Catalog"],
  genie: ["Genie Spend", "Spend by Surface", "Workspaces", "Spaces"],
  apps: ["Apps Spend Over Time", "Apps by Spend", "Connected Resources"],
  tagging: ["Total Tag Coverage", "Tag Coverage Over Time", "Top Tags", "Spend by Key", "Untagged Resources"],
  "users-groups": ["Top Users by Spend", "User Spend by Product", "User Activity", "Service Principal Activity"],
  "use-cases": ["Use Cases"],
};

export function DashboardTabNavigation({
  activeTab,
  visibility,
  loading,
  onChange,
}: {
  activeTab: ViewTab;
  visibility: TabVisibility;
  loading?: Partial<Record<ViewTab, boolean>>;
  onChange: (tab: ViewTab) => void;
}) {
  const tabRefs = useRef<Partial<Record<ViewTab, HTMLButtonElement | null>>>({});
  const visibleTabs = DASHBOARD_TABS.filter(({ id }) => visibility[id]);

  const moveFocus = (current: ViewTab, key: string) => {
    const currentIndex = visibleTabs.findIndex(({ id }) => id === current);
    if (currentIndex < 0) return;
    let nextIndex: number;
    if (key === "Home") nextIndex = 0;
    else if (key === "End") nextIndex = visibleTabs.length - 1;
    else if (key === "ArrowRight") nextIndex = (currentIndex + 1) % visibleTabs.length;
    else if (key === "ArrowLeft") nextIndex = (currentIndex - 1 + visibleTabs.length) % visibleTabs.length;
    else return;

    const nextTab = visibleTabs[nextIndex].id;
    onChange(nextTab);
    tabRefs.current[nextTab]?.focus();
  };

  return (
    <div className="mt-4 overflow-x-auto overflow-y-hidden" style={{ borderBottom: "1px solid var(--hairline)" }}>
      <nav
        role="tablist"
        aria-label="Dashboard views"
        className="-mb-px flex min-w-max justify-start space-x-7 px-1 sm:justify-center [&_svg]:h-3.75 [&_svg]:w-3.75"
      >
        {visibleTabs.map(({ id, label, icon }) => (
          <button
            key={id}
            ref={(element) => { tabRefs.current[id] = element; }}
            id={`dashboard-tab-${id}`}
            type="button"
            role="tab"
            aria-label={label}
            aria-selected={activeTab === id}
            aria-busy={loading?.[id] || undefined}
            aria-controls={`dashboard-panel-${id}`}
            tabIndex={activeTab === id ? 0 : -1}
            onClick={() => onChange(id)}
            onKeyDown={(event) => {
              if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
                event.preventDefault();
                moveFocus(id, event.key);
              }
            }}
            className={`whitespace-nowrap border-b-2 px-1 py-3 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:ring-gray-400 ${
              activeTab === id
                ? "border-lava text-lava"
                : "border-transparent text-slate hover:bg-oat-page hover:text-ink"
            }`}
          >
            {loading?.[id] ? (
              <span className="mr-2 -mt-0.5 inline-flex h-4 w-4 items-center justify-center" aria-hidden="true">
                <Spinner size="xs" />
              </span>
            ) : icon}
            {label}
          </button>
        ))}
      </nav>
    </div>
  );
}

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30 * 60 * 1000, // 30 minutes - data doesn't change often
      gcTime: 60 * 60 * 1000, // 1 hour cache
      refetchOnWindowFocus: false,
      refetchOnReconnect: false,
      retry: 1,
    },
  },
});

interface User {
  email: string;
  name: string;
  role?: "admin" | "consumer";
}

const RAIL_STATUS_BADGE_CLASS = "rail-status-badge relative inline-flex h-[22px] w-[104px] min-w-0 shrink-0 items-center justify-center gap-[3px] overflow-hidden rounded-[4px] px-[6px] text-[10px] font-bold leading-[10px]";
const RAIL_STATUS_TONES: Record<string, string> = {
  warm: "bg-green-500/20 text-green-200 hover:bg-green-400/25 focus-visible:ring-green-300/40",
  warming_up: "bg-amber-500/20 text-amber-100 hover:bg-amber-400/25 focus-visible:ring-amber-300/40",
  unavailable: "bg-red-500/20 text-red-100 hover:bg-red-400/25 focus-visible:ring-red-300/40",
};

function railStatusClass(status?: string): string {
  return `${RAIL_STATUS_BADGE_CLASS} ${RAIL_STATUS_TONES[status ?? "unavailable"] ?? RAIL_STATUS_TONES.unavailable}`;
}

function DuBoisAccountIcon() {
  return (
    <svg className="h-2.5 w-2.5 opacity-70" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path fill="currentColor" d="M4 8.75h8v-1.5H4zM7 5.75H4v-1.5h3zM4 11.75h8v-1.5H4z" />
      <path fill="currentColor" fillRule="evenodd" d="M1.75 1a.75.75 0 0 0-.75.75v12.5c0 .414.336.75.75.75h12.5a.75.75 0 0 0 .75-.75V5a.75.75 0 0 0-.75-.75H10v-2.5A.75.75 0 0 0 9.25 1zm.75 1.5h6V5c0 .414.336.75.75.75h4.25v7.75h-11z" clipRule="evenodd" />
    </svg>
  );
}

function DatabricksSqlProductIcon() {
  return (
    <svg className="h-3 w-3 opacity-70" viewBox="0 0 512 512" fill="none" aria-hidden="true">
      <path fill="currentColor" fillRule="evenodd" d="M152 448h296V272H152c-48.601 0-88 39.399-88 88s39.399 88 88 88m0-48c22.091 0 40-17.909 40-40s-17.909-40-40-40-40 17.909-40 40 17.909 40 40 40" clipRule="evenodd" />
      <path fill="currentColor" fillRule="evenodd" d="M360 240c48.601 0 88-39.399 88-88s-39.399-88-88-88H64v176zm0-48c22.091 0 40-17.909 40-40s-17.909-40-40-40-40 17.909-40 40 17.909 40 40 40" clipRule="evenodd" />
    </svg>
  );
}

function compactRailBadgeText(value: string): string {
  const text = value.trim();
  const maxLength = /^\d/.test(text) ? 8 : 11;
  return text.length > maxLength ? text.slice(0, maxLength) : text;
}

function RailBadgeTooltip({
  text,
  children,
  align = "center",
}: {
  text: React.ReactNode;
  children: React.ReactNode;
  align?: "start" | "center" | "end";
}) {
  const tooltipId = React.useId();
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    const handleOpen = (event: Event) => {
      setVisible((event as CustomEvent<string>).detail === tooltipId);
    };
    window.addEventListener("cost-obs:rail-tooltip-open", handleOpen);
    return () => window.removeEventListener("cost-obs:rail-tooltip-open", handleOpen);
  }, [tooltipId]);
  const open = () => {
    window.dispatchEvent(new CustomEvent("cost-obs:rail-tooltip-open", {
      detail: tooltipId,
    }));
  };
  return (
    <span
      className="relative inline-flex shrink-0"
      onMouseEnter={open}
      onMouseLeave={() => setVisible(false)}
      onFocusCapture={open}
      onBlurCapture={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) {
          setVisible(false);
        }
      }}
    >
      {children}
      <span
        role="tooltip"
        aria-hidden={!visible}
        className={`pointer-events-none absolute top-[calc(100%+8px)] z-50 w-max max-w-[min(360px,calc(100vw-24px))] rounded-[5px] bg-[#263238] px-2.5 py-1.5 text-left text-[11px] font-medium leading-4 text-white shadow-lg transition-opacity ${align === "start" ? "left-0" : align === "end" ? "right-0" : "left-1/2 -translate-x-1/2"} ${visible ? "opacity-100" : "opacity-0"}`}
      >
        {text}
      </span>
    </span>
  );
}

function CopyableRailBadge({
  value,
  text,
  label,
  entity,
  icon,
  tooltipAlign = "center",
  status = "warm",
}: {
  value: string;
  text: string;
  label: string;
  entity: string;
  icon: React.ReactNode;
  tooltipAlign?: "start" | "center" | "end";
  status?: string;
}) {
  const [copied, setCopied] = useState(false);
  const copyValue = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      // Clipboard permission can be denied; keep the badge usable.
    }
  };
  return (
    <RailBadgeTooltip align={tooltipAlign} text={
      <span className="block space-y-0.5">
        <span className="block">
          <strong>{entity} display name:</strong>{" "}
          <code className="font-mono text-[10.5px]">{text}</code>
        </span>
        <span className="block">
          <strong>{entity} ID:</strong>{" "}
          <code className="font-mono text-[10.5px]">{value}</code>
          {copied ? " (copied)" : ""}
        </span>
      </span>
    }>
      <button
        type="button"
        aria-label={copied ? `${label} copied` : `Copy ${label}`}
        onClick={() => { void copyValue(); }}
        className={`group appearance-none transition-colors focus-visible:outline-none focus-visible:ring-2 ${railStatusClass(status)}`}
      >
        <span className="rail-status-dot healthy-status-dot h-[5px] w-[5px] rounded-full bg-green-400" />
        <span className="min-w-0 overflow-hidden whitespace-nowrap">{compactRailBadgeText(text)}</span>
        {copied ? (
          <Check className="h-2.5 w-2.5" aria-hidden="true" />
        ) : (
          <span className="relative inline-flex h-2.5 w-2.5 shrink-0 items-center justify-center">
            <span className="inline-flex group-hover:hidden group-focus:hidden" aria-hidden="true">
              {icon}
            </span>
            <Copy className="hidden h-2.5 w-2.5 opacity-80 group-hover:block group-focus:block" aria-hidden="true" />
          </span>
        )}
      </button>
    </RailBadgeTooltip>
  );
}

function DBUMethodologyPanel() {
  const minimizeKey = "cost-obs-minimize-dbu-info";
  const [minimized, setMinimized] = useState(() => (
    typeof window !== "undefined" ? localStorage.getItem(minimizeKey) === "true" : false
  ));
  const handleToggle = (value: boolean) => {
    setMinimized(value);
    if (value) localStorage.setItem(minimizeKey, "true");
    else localStorage.removeItem(minimizeKey);
  };

  return (
    <InfoPanel title="$DBU Spend tab methodology" minimized={minimized} onToggle={handleToggle}>
      <ul className="list-inside list-disc space-y-1">
        <li><strong>DBUs</strong> are Databricks Units reported as usage quantity in <code className="rounded bg-white/60 px-1">system.billing.usage</code>.</li>
        <li><strong>Spend</strong> multiplies DBUs by the effective SKU price: account pricing when enabled and available, otherwise current list pricing.</li>
        <li>Date and workspace filters apply throughout this tab; cloud infrastructure charges are reported separately under Cloud Costs.</li>
      </ul>
    </InfoPanel>
  );
}

function AccountPricingBanner() {
  const { useAccountPrices, discountPercent, skuCount, available } = usePricing();
  if (!useAccountPrices) return null;
  return (
    <div className="flex items-center justify-center gap-2 px-4 py-1.5 text-xs font-medium text-white" style={{ backgroundColor: '#10B981' }}>
      <svg className="h-3.5 w-3.5 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
      </svg>
      {available
        ? `Account prices active — ${discountPercent.toFixed(1)}% blended discount (${skuCount} SKUs with negotiated rates, from system.billing.account_prices) applied to Overview and infrastructure spend. Other tabs show list prices.`
        : "Account prices mode active — no negotiated rates found in system.billing.account_prices, showing list prices"}
    </div>
  );
}

function SpGrantsBanner({ onOpenSettings }: { onOpenSettings: () => void }) {
  const [dismissed, setDismissed] = useState(() => sessionStorage.getItem("coc-sp-grants-dismissed") === "1");

  const { data: authStatus } = useQuery<{ user_token_active: boolean; identity: string } | null>({
    queryKey: ["settings-auth-status"],
    queryFn: () => fetch("/api/settings/auth-status").then(r => r.json()).catch(() => null),
    staleTime: 60_000,
  });

  const spMode = authStatus && !authStatus.user_token_active && authStatus.identity === "service_principal";

  const { data: billingAccess } = useQuery<{ ok: boolean; reason?: string; warehouse_id?: string; sp_client_id?: string } | null>({
    queryKey: ["settings-billing-access"],
    queryFn: () => fetch("/api/settings/billing-access").then(r => r.json()).catch(() => null),
    staleTime: 5 * 60_000,
    enabled: !!spMode,
  });

  const isWarehouseIssue = billingAccess?.reason === "warehouse_access";
  const isGrantsIssue = billingAccess?.reason === "grants_missing";

  if (dismissed || !spMode || !billingAccess || billingAccess.ok !== false || (!isWarehouseIssue && !isGrantsIssue)) return null;

  return (
    <div className="flex items-center justify-between gap-4 px-4 py-2.5 bg-amber-50 border-b border-amber-200">
      <div className="flex items-center gap-2 min-w-0">
        <svg className="h-4 w-4 shrink-0 text-amber-600" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" />
        </svg>
        {isWarehouseIssue ? (
          <span className="text-xs text-amber-800">
            <strong>SP missing warehouse access</strong> — the service principal{billingAccess.sp_client_id ? ` (${billingAccess.sp_client_id})` : ""} cannot use the SQL warehouse.
            A workspace admin must run:{" "}
            <code className="rounded bg-amber-100 px-1 font-mono">
              GRANT CAN_USE ON WAREHOUSE {billingAccess.warehouse_id || "<warehouse_id>"} TO `{billingAccess.sp_client_id || "<sp_client_id>"}`
            </code>
          </span>
        ) : (
          <span className="text-xs text-amber-800">
            <strong>SP grants missing</strong> — the service principal lacks system table access after the last git deploy.
            Re-run the Permissions setup to restore access.
          </span>
        )}
      </div>
      <div className="flex items-center gap-2 shrink-0">
        <button
          onClick={() => { onOpenSettings(); }}
          className="text-xs font-medium px-3 py-1.5 rounded"
          style={{ background: "#FF3621", color: "#fff" }}
        >
          {isWarehouseIssue ? "Open Settings → Permissions" : "Re-run Permissions"}
        </button>
        <button
          onClick={() => { sessionStorage.setItem("coc-sp-grants-dismissed", "1"); setDismissed(true); }}
          className="text-xs text-amber-600 hover:text-amber-800 px-1"
          aria-label="Dismiss"
        >
          ✕
        </button>
      </div>
    </div>
  );
}

function Dashboard() {
  useKeepAlive();
  const [appSettings, setAppSettings] = useState<AppSettings>(loadAppSettings);
  const defaultRange = useDefaultDateRange(appSettings.defaultDateRangeDays);
  const [dateRange, setDateRange] = useState<DateRange>(defaultRange);
  const [activeTab, setActiveTab] = useState<ViewTab>(() => {
    // Start on the configured default landing tab, falling back to the first visible
    // tab when that tab is hidden or unknown.
    const v = loadTabVisibility();
    const want = appSettings.defaultLandingTab as ViewTab;
    if (want && v[want as keyof TabVisibility]) return want;
    return firstVisibleDashboardTab(v) as ViewTab;
  });
  const [showExportDialog, setShowExportDialog] = useState(false);
  const [requiredExportTabs, setRequiredExportTabs] = useState<ViewTab[]>([]);
  const [exportPrepared, setExportPrepared] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [showConversational, setShowConversational] = useState(false);
  const [selectedWorkspaceIds, setSelectedWorkspaceIds] = useState<string[]>([]);
  const [tabVisibility, setTabVisibility] = useState<TabVisibility>(loadTabVisibility);
  const [setupCheckPending, setSetupCheckPending] = useState<boolean>(
    () => localStorage.getItem("coc-setup-complete") !== "true" &&
          sessionStorage.getItem("coc-setup-complete") !== "true"
  );
  // true = show wizard, false = show dashboard.
  // Default to false — the status check flips it to true only if setup is genuinely
  // needed. Starting false prevents the wizard from flashing on container restart
  // (where .settings/ is wiped but the DBFS flag returns "ready" within ~2 s).
  const [showSetupWizard, setShowSetupWizard] = useState<boolean>(false);
  // Set when user closes wizard without completing — shows the incomplete banner on dashboard.
  const [setupIncomplete, setSetupIncomplete] = useState(false);
  // Stored so onLaunchWizard can abort the in-flight status check and prevent it from
  // overriding the manually-triggered wizard with a stale "ready" response.
  const setupStatusAbortRef = useRef<AbortController | null>(null);
  const rqClient = useQueryClient();

  const [tabRefreshing, setTabRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const handleTabRefresh = async () => {
    if (tabRefreshing) return;
    setTabRefreshing(true);
    setRefreshError(null);
    try {
      await rqClient.cancelQueries();
      // Await cache clear so server-side cache is empty before any query fires
      await fetch("/api/cache/clear", { method: "POST" }).catch(() => {});
      // refetchQueries bypasses enabled:false — forces all tab-gated queries to fetch too
      await rqClient.refetchQueries({ type: "all" });
    } catch (error) {
      console.error("Failed to refresh tab", error);
      setRefreshError("This tab could not be refreshed. Retry shortly.");
    } finally {
      setTabRefreshing(false);
    }
  };

  // On every load, verify setup status with the server.
  // 60s timeout — allows for cold App pod start.
  useEffect(() => {
    const controller = new AbortController();
    setupStatusAbortRef.current = controller;
    const timeout = setTimeout(() => controller.abort(), 60_000);
    const prevCompleted = () =>
      localStorage.getItem("coc-setup-complete") === "true" ||
      sessionStorage.getItem("coc-setup-complete") === "true";

    fetch("/api/setup/status", { signal: controller.signal })
      .then((r) => r.json())
      .then((status) => {
        clearTimeout(timeout);
        setSetupCheckPending(false);
        if (status?.status === "ready") {
          localStorage.setItem("coc-setup-complete", "true");
          sessionStorage.setItem("coc-setup-complete", "true");
          setShowSetupWizard(false);
        } else if (status?.status === "setup_required") {
          if (!prevCompleted()) {
            localStorage.removeItem("coc-setup-complete");
            sessionStorage.removeItem("coc-setup-complete");
            setShowSetupWizard(true);
          }
        }
      })
      .catch(() => {
        clearTimeout(timeout);
        setSetupCheckPending(false);
        if (!prevCompleted()) {
          setShowSetupWizard(true);
        }
      });

    return () => { clearTimeout(timeout); controller.abort(); };
  }, []);

  const handleSetupComplete = () => {
    localStorage.setItem("coc-setup-complete", "true");
    sessionStorage.setItem("coc-setup-complete", "true");
    // Mark setup complete server-side (survives page refresh, cleared on redeploy)
    fetch("/api/setup/complete", { method: "POST" }).catch(() => {});
    // Save the deploying user as admin (fire-and-forget)
    fetch("/api/setup/bootstrap-admin", { method: "POST" }).catch(() => {});
    setShowSetupWizard(false);
    rqClient.invalidateQueries();
  };

  // On first load after each new deploy, reset all info banner minimize flags so users
  // see best-practice guidance at least once. After that, their collapse preference persists.
  useEffect(() => {
    const BANNER_RESET_VERSION = "2026-03-12";
    const BANNER_RESET_KEY = "coc-banner-reset-v";
    if (localStorage.getItem(BANNER_RESET_KEY) !== BANNER_RESET_VERSION) {
      [
        "cost-obs-minimize-tagging-info",
        "cost-obs-minimize-sql-info",
        "cost-obs-minimize-infra-info",
        "cost-obs-minimize-aiml-info",
        "cost-obs-minimize-kpis-info",
      ].forEach((k) => localStorage.removeItem(k));
      localStorage.setItem(BANNER_RESET_KEY, BANNER_RESET_VERSION);
    }
  }, []);

  // Trigger cache prewarm immediately on mount (runs while permissions dialog is shown)
  useEffect(() => {
    fetch("/api/prewarm", { method: "POST" }).catch(() => {
      // Ignore errors - prewarm is best-effort
    });
  }, []);

  // Auto-refresh interval based on settings
  useEffect(() => {
    if (appSettings.refreshIntervalMinutes <= 0) return;
    const interval = setInterval(() => {
      rqClient.invalidateQueries();
    }, appSettings.refreshIntervalMinutes * 60 * 1000);
    return () => clearInterval(interval);
  }, [appSettings.refreshIntervalMinutes, rqClient]);

  // Density → compact-mode CSS class on root
  useEffect(() => {
    document.documentElement.classList.toggle("compact-mode", appSettings.density === "compact");
  }, [appSettings.density]);

  // Theme → dark-mode CSS class on root ("system" follows prefers-color-scheme).
  useEffect(() => {
    const apply = () => {
      const dark = appSettings.theme === "dark" ||
        (appSettings.theme === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);
      document.documentElement.classList.toggle("dark-mode", dark);
    };
    apply();
    if (appSettings.theme === "system") {
      const mq = window.matchMedia("(prefers-color-scheme: dark)");
      mq.addEventListener("change", apply);
      return () => mq.removeEventListener("change", apply);
    }
  }, [appSettings.theme]);

  const { data: user } = useQuery<User>({
    queryKey: ["user"],
    queryFn: async () => {
      const response = await fetch("/api/user/me");
      if (!response.ok) throw new Error("Failed to fetch user");
      return response.json();
    },
  });

  const { data: accountInfo } = useAccountInfo();

  const { data: authStatus } = useQuery<{
    user_token_active: boolean;
    identity: "user_oauth" | "service_principal";
    locked_to_sp: boolean;
    has_sql_scope: boolean | null;
    sp_client_id?: string;
    sp_display_name?: string;
    sp_user_name?: string;
  } | null>({
    queryKey: ["settings-auth-status"],
    queryFn: () => fetch("/api/settings/auth-status").then(r => r.json()).catch(() => null),
    staleTime: 60 * 1000,
  });

  const {
    applyPricing,
    multiplier: pricingMultiplier,
    useAccountPrices,
    available: accountPricesAvailable,
    setPricingScope,
  } = usePricing();

  // Central warehouse warming poller — single source of truth for warehouse state.
  // Polls every 5s while warming_up, 15s once warm (catches post-idle auto-stop within ~15s).
  const {
    data: warehouseStatus,
    isError: warehouseHealthCheckFailed,
    isFetching: warehouseHealthChecking,
    refetch: retryWarehouseHealthCheck,
  } = useQuery<WarehouseHealth>({
    queryKey: ["health", "sql-warehouse"],
    queryFn: fetchWarehouseHealth,
    retry: 2,
    retryDelay: (attempt) => Math.min(1000 * 2 ** attempt, 4000),
    refetchInterval: (query) => warehouseHealthPollInterval(
      query.state.data?.status,
      query.state.status === "error",
    ),
    staleTime: 0,
  });
  const warehouseReady = warehouseStatus?.status === "warm";

  const { data: lakebaseStatus } = useQuery<{
    status: "warm" | "warming_up" | "unavailable";
    instance_name?: string | null;
    page_url?: string | null;
  }>({
    queryKey: ["health", "lakebase"],
    queryFn: async () => {
      const response = await fetch("/api/health/lakebase");
      if (!response.ok) throw new Error("Lakebase health check failed");
      return response.json();
    },
    retry: 1,
    staleTime: 15_000,
    refetchInterval: 30_000,
  });

  // Tab data is served from Lakebase; do not wait on the warehouse to preload chunks.
  useEffect(() => {
    if ("requestIdleCallback" in window) {
      requestIdleCallback(preloadTabChunks, { timeout: 5000 });
    } else {
      setTimeout(preloadTabChunks, 2000);
    }
  }, []);

  // Fast bundle for quick initial load (uses materialized views)
  const { data: bundle, isLoading: bundleLoading, isError: bundleError } = useDashboardBundleFast(dateRange, selectedWorkspaceIds.length ? selectedWorkspaceIds : undefined);

  // Extract data from fast bundle — apply pricing multiplier when account prices are active
  const summary = useMemo(() => {
    const s = bundle?.summary;
    if (!s || pricingMultiplier === 1.0) return s;
    return {
      ...s,
      total_spend: applyPricing(s.total_spend ?? 0),
      total_dbus: s.total_dbus, // DBUs don't scale with price
    };
  }, [bundle?.summary, pricingMultiplier, applyPricing]);

  const products = useMemo(() => {
    const p = bundle?.products;
    if (!p || pricingMultiplier === 1.0) return p;
    return {
      ...p,
      products: p.products?.map((prod) => ({
        ...prod,
        total_spend: applyPricing(prod.total_spend ?? 0),
      })),
    };
  }, [bundle?.products, pricingMultiplier, applyPricing]);

  const workspaces = useMemo(() => {
    const w = bundle?.workspaces;
    if (!w || pricingMultiplier === 1.0) return w;
    return {
      ...w,
      workspaces: w.workspaces?.map((ws) => ({
        ...ws,
        total_spend: applyPricing(ws.total_spend ?? 0),
      })),
    };
  }, [bundle?.workspaces, pricingMultiplier, applyPricing]);


  const timeseries = useMemo(() => {
    const t = bundle?.timeseries;
    if (!t || pricingMultiplier === 1.0) return t;
    return {
      ...t,
      timeseries: t.timeseries?.map((row) => {
        const scaled: typeof row = { ...row };
        for (const key of Object.keys(row)) {
          if (key !== "date" && typeof row[key] === "number") {
            scaled[key] = applyPricing(row[key] as number);
          }
        }
        return scaled;
      }),
    };
  }, [bundle?.timeseries, pricingMultiplier, applyPricing]);

  const _wsIds = selectedWorkspaceIds.length ? selectedWorkspaceIds : undefined;
  // Weight the blended account-price discount by the same range/workspaces it scales.
  useEffect(() => {
    setPricingScope(dateRange.startDate, dateRange.endDate, _wsIds);
  }, [setPricingScope, dateRange.startDate, dateRange.endDate, _wsIds]);

  // Viewer tabs read Lakebase. Connected invoices and SQL 360 per-query drill still wait on the warehouse.
  const { data: sqlBreakdown, isLoading: sqlLoading } = useSqlBreakdown(dateRange, _wsIds, true);
  const { data: pipelineObjects, isLoading: pipelineLoading } = usePipelineObjects(dateRange, _wsIds, true);
  const { data: interactiveBreakdown, isLoading: interactiveLoading } = useInteractiveBreakdown(dateRange, _wsIds, true);
  const { data: skuBreakdown, isLoading: skuLoading } = useSKUBreakdown(dateRange, _wsIds, true);

  const {
    data: cloudCostsBundle,
    isLoading: infraBundleLoading,
    isError: infraBundleError,
    error: infraBundleErrorObject,
  } = useCloudCostsBundle(dateRange, _wsIds, true);
  const infraBundle = cloudCostsBundle?.infra_bundle;
  const infraCosts = infraBundle?.infra_costs;
  const infraCostsTimeseries = infraBundle?.infra_timeseries;
  const infraPricingMultiplier =
    useAccountPrices && accountPricesAvailable ? pricingMultiplier : 1;
  const pricedInfraCosts = useMemo(
    () => applyInfraPricing(infraCosts, infraPricingMultiplier),
    [infraCosts, infraPricingMultiplier],
  );

  const { data: kpisBundle, isLoading: kpisBundleLoading, isFetching: kpisBundleFetching, isError: kpisBundleError } = useKPIsBundle(dateRange, _wsIds, true);
  const spendAnomalies = kpisBundle?.anomalies;
  const platformKPIs = kpisBundle?.kpis;
  const anomaliesLoading = kpisBundleLoading;
  const kpisLoading = kpisBundleLoading;

  const { data: aimlData, isLoading: aimlLoading } = useAIMLDashboardBundle(dateRange, _wsIds, true);
  const { data: aiGatewayData, isLoading: aiGatewayLoading, isError: aiGatewayError } = useAIGatewayDashboardBundle(dateRange, _wsIds, activeTab === "ai-gateway" || requiredExportTabs.includes("ai-gateway"));
  const { data: genieCostData, isLoading: genieCostLoading } = useGenieCostDashboardBundle(dateRange, _wsIds, activeTab === "genie" || requiredExportTabs.includes("genie"));
  const { data: appsData, isLoading: appsLoading, isError: appsError } = useAppsDashboardBundle(dateRange, _wsIds, true);
  const { data: taggingData, isLoading: taggingLoading, isError: taggingError } = useTaggingDashboardBundle(dateRange, _wsIds, true);

  const awsActualData = cloudCostsBundle?.aws_actual;
  const azureActualData = cloudCostsBundle?.azure_actual;
  const gcpActualData = cloudCostsBundle?.gcp_actual;
  const awsActualLoading = infraBundleLoading;
  const azureActualLoading = infraBundleLoading;
  const gcpActualLoading = infraBundleLoading;

  const { data: dbsqlData, isLoading: dbsqlLoading, isFetching: dbsqlFetching, isError: dbsqlError } = useDBSQLQueryCosts(dateRange, _wsIds, true);
  const { data: dbsqlTopQueriesData, isLoading: dbsqlTopQueriesLoading } = useDBSQLTopQueries(dateRange, _wsIds, warehouseReady);
  const { data: usersGroupsData, isLoading: usersGroupsLoading, isError: usersGroupsError } = useUsersGroupsBundle(dateRange, _wsIds, true);

  // Optimizer tab — prefetch rightsizing and idle-time in background so the tab loads instantly,
  // and capture the data so the PDF export can include it under the Optimize section.
  const { data: optimizeRightsizingData, isLoading: optimizeRightsizingLoading, isError: optimizeRightsizingError } = useQuery<{
    available: boolean;
    warehouses_analyzed: number;
    recommendations: Array<{
      warehouse_id: string;
      warehouse_name: string | null;
      warehouse_size: string | null;
      workspace_id: string;
      recommendation_type: string;
      recommendation_text: string;
    }>;
  }>({
    queryKey: ["warehouse-health"],
    queryFn: () => fetchJson("/api/sql/warehouse-health"),
    staleTime: 30 * 60 * 1000,
    retry: false,
    // Deferred: only fire when the Optimize tab is actually opened. Previously
    // this prefetched at Dashboard mount and could starve concurrent bundles.
    enabled: activeTab === "optimizer" || requiredExportTabs.includes("optimizer"),
  });
  const { data: optimizeIdleData, isLoading: optimizeIdleLoading, isError: optimizeIdleError } = useQuery<{
    available: boolean;
    serverless_detected: boolean;
    warehouses: Array<{
      warehouse_id: string | null;
      warehouse_name: string | null;
      warehouse_size: string | null;
      warehouse_type: string;
      workspace_id: string;
      total_running_minutes: number;
      busy_union_minutes: number;
      idle_minutes: number;
      idle_pct: number;
      total_spend: number;
      estimated_idle_spend: number | null;
    }>;
  }>({
    queryKey: ["warehouse-idle-time", dateRange.startDate, dateRange.endDate, _wsIds?.join(",")],
    queryFn: () => {
      const params = new URLSearchParams();
      if (dateRange.startDate) params.set("start_date", dateRange.startDate);
      if (dateRange.endDate) params.set("end_date", dateRange.endDate);
      if (_wsIds?.length) params.set("workspace_ids", _wsIds.join(","));
      return fetchJson(`/api/sql/warehouse-health/idle-time?${params}`);
    },
    staleTime: 30 * 60 * 1000,
    retry: false,
    enabled: activeTab === "optimizer" || requiredExportTabs.includes("optimizer"),
  });

  // Use Cases tab data - only fetch when feature is enabled
  const useCasesEnabled = appSettings.enableUseCaseTracking;
  useQuery({ queryKey: ["use-cases"], queryFn: async () => { const r = await fetch("/api/use-cases/use-cases?status=active"); if (!r.ok) throw new Error("Failed"); return r.json(); }, enabled: useCasesEnabled });
  useQuery({ queryKey: ["use-cases-summary"], queryFn: async () => { const r = await fetch("/api/use-cases/analytics/summary"); if (!r.ok) throw new Error("Failed"); return r.json(); }, enabled: useCasesEnabled });
  useQuery({ queryKey: ["monthly-consumption"], queryFn: async () => { const r = await fetch("/api/use-cases/monthly-consumption"); if (!r.ok) throw new Error("Failed"); return r.json(); }, enabled: useCasesEnabled });
  useQuery({ queryKey: ["available-tags"], queryFn: async () => { const r = await fetch("/api/tagging/available-tags"); if (!r.ok) return { tags: {}, count: 0 }; return r.json(); } });

  // Workspace list for the filter dropdown — pool-scoped, independent of the bundle.
  const { data: wsListData, isLoading: wsListLoading } = useQuery<{ workspaces: { id: string; name: string; historical?: boolean }[] }>({
    queryKey: ["billing", "workspaces"],
    queryFn: () => fetch("/api/billing/workspaces").then(r => r.json()),
    staleTime: Infinity,
  });
  const workspaceNameMap = useMemo(() => {
    const map: Record<string, string> = {};
    // wsListData has real account-level names; only store entries where a real name exists
    wsListData?.workspaces?.forEach((w) => { if (w.name) map[w.id] = w.name; });
    // Fill any gaps from billing data (workspace_name sometimes populated here too)
    workspaces?.workspaces?.forEach((w: WorkspaceBreakdown) => {
      if (!map[w.workspace_id] && w.workspace_name) map[w.workspace_id] = w.workspace_name;
    });
    return map;
  }, [wsListData?.workspaces, workspaces?.workspaces]);

  // Overlay the merged name map so the top-nav filter picks up billing-derived
  // names when /api/billing/workspaces returns an id-only record.
  const wsFilterList = useMemo(
    () => (wsListData?.workspaces ?? []).map(w => {
      const name = workspaceNameMap[w.id] || w.name || null;
      // Trust the server's `historical` (computed from the true resolved name), so the
      // "workspace display names" toggle nulling names never mislabels a live workspace.
      return { workspace_id: w.id, workspace_name: name, historical: w.historical ?? !name };
    }),
    [wsListData?.workspaces, workspaceNameMap],
  );
  const workspaceScopeCount = wsListData
    ? wsFilterList.filter((workspace) => !workspace.historical).length
    : undefined;

  // An admin can rescope the pool from the setup wizard; drop selections that are
  // no longer in it so the tabs don't keep asking for workspaces out of scope.
  useEffect(() => {
    if (!wsListData?.workspaces?.length) return;
    const inPool = new Set(wsListData.workspaces.map((w) => w.id));
    setSelectedWorkspaceIds((ids) => {
      const kept = ids.filter((id) => inPool.has(id));
      return kept.length === ids.length ? ids : kept;
    });
  }, [wsListData?.workspaces]);

  // Service-principal display-name map — refetches after 10 min so if the first
  // SCIM call missed (e.g. permission granted later, backend was in cold-start),
  // consumers recover without a hard refresh.
  const { data: spListData } = useQuery<{ map: Record<string, string> }>({
    queryKey: ["user", "service-principals"],
    queryFn: () => fetch("/api/user/service-principals").then(r => r.json()),
    staleTime: 10 * 60 * 1000,
  });
  // Memoize so consumers of SpNameMapContext get a stable reference while
  // spListData is undefined (otherwise every parent render churns the context).
  const spNameMap = useMemo(() => spListData?.map ?? {}, [spListData?.map]);

  // Settings data — all prefetched in the background after the main bundle loads.
  // `enabled` gates each query on `!!bundle` so settings requests don't race the
  // critical-path billing queries on cold start.
  const _settingsReady = !!bundle;
  useQuery({ queryKey: ["user-permissions"],      enabled: _settingsReady, queryFn: async () => { const r = await fetch("/api/settings/user-permissions"); if (!r.ok) throw new Error("Failed"); return r.json(); }, staleTime: 5 * 60 * 1000 });
  useQuery({ queryKey: ["app-config"],             enabled: _settingsReady, queryFn: async () => { const r = await fetch("/api/settings/config"); if (!r.ok) throw new Error("Failed"); return r.json(); }, staleTime: 5 * 60 * 1000 });
  // settings-install-report: same endpoint as app-config but separate key used by SettingsDebugger
  useQuery({ queryKey: ["settings-install-report"], enabled: _settingsReady, queryFn: async () => { const r = await fetch("/api/settings/config"); return r.ok ? r.json() : null; }, staleTime: 5 * 60 * 1000 });
  useQuery({ queryKey: ["warehouses"],             enabled: _settingsReady, queryFn: async () => { const r = await fetch("/api/settings/warehouses"); if (!r.ok) throw new Error("Failed"); return r.json(); }, staleTime: 5 * 60 * 1000 });
  useQuery({ queryKey: ["cloud-provider"],         enabled: _settingsReady, queryFn: async () => { const r = await fetch("/api/settings/cloud-provider"); if (!r.ok) throw new Error("Failed"); return r.json(); }, staleTime: 30 * 60 * 1000 });
  useQuery({ queryKey: ["cloud-connections"],      enabled: _settingsReady, queryFn: async () => { const r = await fetch("/api/settings/cloud-connections"); if (!r.ok) throw new Error("Failed"); return r.json(); }, staleTime: 5 * 60 * 1000 });
  useQuery({ queryKey: ["settings-account-prices"], enabled: _settingsReady, queryFn: async () => { const r = await fetch("/api/settings/account-prices"); return r.ok ? r.json() : { available: false, prices: [], source: null, count: 0 }; }, staleTime: 5 * 60 * 1000 });
  useQuery({ queryKey: ["settings-catalog"],       enabled: _settingsReady, queryFn: async () => { const r = await fetch("/api/settings/catalog"); return r.ok ? r.json() : null; }, staleTime: 5 * 60 * 1000 });
  useQuery({ queryKey: ["settings-auth-status"],   enabled: _settingsReady, queryFn: async () => { const r = await fetch("/api/settings/auth-status"); return r.ok ? r.json() : null; }, staleTime: 5 * 60 * 1000 });
  useQuery({ queryKey: ["settings-schedule"],      enabled: _settingsReady, queryFn: async () => { const r = await fetch("/api/settings/schedule"); return r.ok ? r.json() : null; }, staleTime: 5 * 60 * 1000 });
  useQuery({ queryKey: ["setup-workspace-filter"], enabled: _settingsReady, queryFn: async () => { const r = await fetch("/api/setup/workspace-filter"); return r.ok ? r.json() : null; }, staleTime: 5 * 60 * 1000 });
  useQuery({ queryKey: ["billing", "account"],     enabled: _settingsReady, queryFn: async () => { const r = await fetch("/api/billing/account"); return r.ok ? r.json() : null; }, staleTime: Infinity });
  // settings-tables-status is NOT pre-fetched — it runs SQL against every app table
  // and returns stale false-negatives if it fires before the background MV build completes.

  // Memoize infra data transformations to avoid re-creating arrays on every render
  const infraViewData = useMemo(() => pricedInfraCosts ? {
    clusters: (pricedInfraCosts.clusters || []).map(c => ({
      cluster_id: c.cluster_id,
      cluster_name: c.cluster_name,
      driver_instance_type: c.driver_instance_type,
      worker_instance_type: c.worker_instance_type,
      cluster_source: c.cluster_source,
      total_dbu_hours: c.total_dbu_hours,
      days_active: c.days_active,
      percentage: c.percentage,
      workspace_id: (c as any).workspace_id || "",
      workspace_name: (c as any).workspace_name ?? null,
      state: null,
      estimated_aws_cost: c.estimated_cost,
    })),
    instance_families: pricedInfraCosts.instance_families,
    total_estimated_cost: pricedInfraCosts.total_estimated_cost,
    total_dbu_hours: pricedInfraCosts.total_dbu_hours,
    billing_summary: pricedInfraCosts.billing_summary,
    start_date: pricedInfraCosts.start_date,
    end_date: pricedInfraCosts.end_date,
    disclaimer: pricedInfraCosts.disclaimer,
    error: pricedInfraCosts.error,
  } : undefined, [pricedInfraCosts]);

  const infraViewTimeseries = useMemo(() => infraCostsTimeseries ? {
    timeseries: (infraCostsTimeseries.timeseries || []).map(t => ({
      date: t.date,
      "AWS Cost": t["Infrastructure Cost"],
    })),
    categories: ["AWS Cost"],
    start_date: infraCostsTimeseries.start_date,
    end_date: infraCostsTimeseries.end_date,
  } : undefined, [infraCostsTimeseries]);

  const fetchingCount = useIsFetching();

  const prepareExportData = useCallback((sections: ExportSections) => {
    const tabs = getRequiredExportTabs(sections, tabVisibility) as ViewTab[];
    setExportPrepared(false);
    setRequiredExportTabs(tabs);
    void rqClient.invalidateQueries({ refetchType: "active" });
  }, [rqClient, tabVisibility]);

  const retryFailedExportData = useCallback(async () => {
    setExportPrepared(false);
    await rqClient.refetchQueries({
      type: "active",
      predicate: (query) => query.state.status === "error",
    });
  }, [rqClient]);

  const closeExportDialog = useCallback(() => {
    setShowExportDialog(false);
    setRequiredExportTabs([]);
    setExportPrepared(false);
  }, []);

  const handleExport = (sections: ExportSections, format: ExportFormat) => {
    const workspaceFilter = _wsIds?.length
      ? { ids: _wsIds, names: _wsIds.map((id: string) => workspaceNameMap[id] || id) }
      : { ids: [] };

    if (format === "csv") {
      generateCostCSV(
        {
          summary,
          products,
          workspaces,
          skus: skuBreakdown,
          pipelineObjects,
          interactiveBreakdown,
          aiml: aimlData,
          apps: appsData,
          tagging: taggingData,
          users: usersGroupsData,
          query360: dbsqlData ?? undefined,
        },
        sections,
        { start: dateRange.startDate, end: dateRange.endDate },
        workspaceFilter
      );
      return;
    }
    handleExportPDF(sections, workspaceFilter);
  };

  const handleExportPDF = (sections: ExportSections, workspaceFilter?: { ids: string[]; names?: string[] }) => {
    generateCostReport(
      {
        summary,
        products,
        workspaces,
        skus: skuBreakdown,
        anomalies: spendAnomalies,
        pipelineObjects,
        interactiveBreakdown,
        awsCosts: infraCosts ? {
          clusters: (infraCosts.clusters ?? []).map(c => ({
            cluster_id: c.cluster_id,
            cluster_name: c.cluster_name,
            driver_instance_type: c.driver_instance_type,
            worker_instance_type: c.worker_instance_type,
            cluster_source: c.cluster_source,
            total_dbu_hours: c.total_dbu_hours,
            days_active: c.days_active,
            percentage: c.percentage,
            workspace_id: (c as any).workspace_id || "",
            state: null,
            estimated_aws_cost: c.estimated_cost,
          })),
          instance_families: infraCosts.instance_families,
          total_estimated_cost: infraCosts.total_estimated_cost,
          total_dbu_hours: infraCosts.total_dbu_hours,
          start_date: infraCosts.start_date,
          end_date: infraCosts.end_date,
          disclaimer: infraCosts.disclaimer,
          error: infraCosts.error,
        } : undefined,
        aiml: aimlData,
        apps: appsData,
        tagging: taggingData,
        platformKPIs,
        query360: dbsqlData ?? undefined,
        users: usersGroupsData,
        optimize: (optimizeRightsizingData || optimizeIdleData)
          ? { rightsizing: optimizeRightsizingData, idle: optimizeIdleData }
          : undefined,
        dateRange: {
          start: dateRange.startDate,
          end: dateRange.endDate,
        },
        workspaceFilter,
      },
      sections
    );
  };

  const tabLoadingFlags: Partial<Record<ViewTab, boolean>> = {
    dbu: bundleLoading,
    sql: sqlLoading || dbsqlLoading || (requiredExportTabs.includes("sql") && dbsqlData == null && !dbsqlError),
    infra: infraBundleLoading,
    optimizer: optimizeRightsizingLoading || optimizeIdleLoading,
    kpis: kpisLoading,
    "ai-gateway": aiGatewayLoading,
    genie: genieCostLoading,
    apps: appsLoading,
    tagging: taggingLoading,
    "users-groups": usersGroupsLoading,
  };

  const tabErrorFlags: Partial<Record<ViewTab, string>> = {
    dbu: bundleError ? "DBU spend failed to load" : undefined,
    sql: dbsqlError ? "SQL data failed to load" : undefined,
    infra: infraBundleError ? "Cloud costs failed to load" : undefined,
    optimizer: (optimizeRightsizingError || optimizeIdleError) ? "Optimize data failed to load" : undefined,
    kpis: kpisBundleError ? "KPI data failed to load" : undefined,
    "ai-gateway": aiGatewayError ? "Unity Gateway data failed to load" : undefined,
    apps: appsError ? "Apps data failed to load" : undefined,
    tagging: taggingError ? "Tagging data failed to load" : undefined,
    "users-groups": usersGroupsError ? "User data failed to load" : undefined,
  };

  const exportDataLoading = showExportDialog
    && requiredExportTabs.length > 0
    && !exportPrepared
    && (fetchingCount > 0 || requiredExportTabs.some((tab) => Boolean(tabLoadingFlags[tab])));

  useEffect(() => {
    if (!showExportDialog || requiredExportTabs.length === 0) return;
    if (exportDataLoading) return;
    setExportPrepared(true);
  }, [exportDataLoading, requiredExportTabs.length, showExportDialog]);

  if (setupCheckPending) {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center gap-4" style={{ backgroundColor: '#F9F7F4' }}>
        <div className="h-10 w-10 animate-spin rounded-full border-4 border-gray-200" style={{ borderTopColor: '#FF3621' }} />
        <p className="text-sm font-medium" style={{ color: '#1B3139' }}>Checking setup status…</p>
      </div>
    );
  }

  if (showSetupWizard) {
    return (
      <div className="flex min-h-screen items-center justify-center" style={{ backgroundColor: '#F9F7F4' }}>
        <SetupWizard
          onComplete={handleSetupComplete}
          onClose={() => { setShowSetupWizard(false); setSetupIncomplete(true); }}
        />
      </div>
    );
  }

  return (
    <SpNameMapContext.Provider value={spNameMap}>
    <div className="min-h-screen" style={{ backgroundColor: (appSettings.theme === "dark" || (appSettings.theme === "system" && typeof window !== "undefined" && window.matchMedia("(prefers-color-scheme: dark)").matches)) ? '#1B1F23' : C.oatPage }}>
      <TopProgressBar />
      {/* Setup incomplete banner — non-dismissable, shown when wizard was closed without finishing */}
      {setupIncomplete && (
        <div className="flex items-center justify-between gap-4 px-4 py-2.5 border-b" style={{ backgroundColor: '#FFF7ED', borderColor: '#FED7AA' }}>
          <div className="flex items-center gap-2 min-w-0">
            <svg className="h-4 w-4 shrink-0 text-orange-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" />
            </svg>
            <span className="text-xs font-medium text-orange-800">
              Setup incomplete — materialized views have not been created. Data may be slower or incomplete.
            </span>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <button
              onClick={() => {
                setSetupIncomplete(false);
                fetch("/api/setup/mark-complete", { method: "POST" }).catch(() => {});
              }}
              className="rounded-md border border-orange-300 px-3 py-1.5 text-xs font-medium text-orange-800 hover:bg-orange-100 transition-colors"
              title="Mark setup complete without rebuilding — use when the tables already exist"
            >
              Mark complete
            </button>
            <button
              onClick={() => {
                setSetupIncomplete(false);
                fetch("/api/setup/rerun", { method: "POST" }).catch(() => {});
                setShowSetupWizard(true);
              }}
              className="rounded-md px-3 py-1.5 text-xs font-semibold text-white transition-colors"
              style={{ backgroundColor: '#FF3621' }}
            >
              Resume Setup
            </button>
          </div>
        </div>
      )}

      {/* Sticky top chrome: navy account bar + white title/tabs */}
      <div className="sticky top-0 z-30 shadow">
      <div data-testid="account-rail" className="min-h-[52px] overflow-visible bg-[#1B3139] text-white">
        <div className="flex min-h-[52px] w-full min-w-0 flex-wrap items-center gap-[8px] px-[12px] py-[8px] min-[1280px]:gap-[14px] min-[1280px]:px-[20px]">
          <a
            href="https://www.databricks.com"
            target="_blank"
            rel="noreferrer"
            className="flex shrink-0 items-center gap-[10px] rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#FF3621]/35"
          >
            <img src="/brand/databricks-symbol-white.svg" alt="" className="h-[19px] w-[17px]" />
            <span className="hidden flex-col gap-[1px] min-[1180px]:flex">
              <span className="text-[8.5px] font-semibold tracking-[.14em] text-[#E9EFED]/60">BUILT ON</span>
              <span className="text-[14px] font-semibold leading-none text-white">Databricks</span>
            </span>
          </a>

          <DeploymentBadgeFromApi variant="control" />

          <span className="hidden h-[22px] w-px shrink-0 bg-white/[.16] min-[1180px]:block" aria-hidden="true" />

          <div className="order-last flex w-full min-w-0 items-center gap-[8px] sm:order-none sm:w-auto sm:flex-1">
            <WorkspaceFilter
              workspaces={wsFilterList}
              selectedIds={selectedWorkspaceIds}
              onChange={setSelectedWorkspaceIds}
              isLoading={wsListLoading}
              variant="rail"
              showClearButton={false}
            />
            <SourceLabelFilter
              variant="rail"
              localWorkspaces={wsFilterList.map((workspace) => ({
                id: workspace.workspace_id ?? "",
                name: workspace.workspace_name,
              }))}
              workspaceSelection={selectedWorkspaceIds}
              onApplied={async (workspaceIds, origin) => {
                if (origin === "source" && workspaceIds) {
                  setSelectedWorkspaceIds(workspaceIds);
                }
                await rqClient.invalidateQueries();
              }}
            />
            {selectedWorkspaceIds.length > 0 && (
              <button
                type="button"
                onClick={() => setSelectedWorkspaceIds([])}
                aria-label="Clear workspace filter"
                title="Show all workspaces"
                className="flex h-[28px] w-[28px] shrink-0 items-center justify-center rounded-full bg-white/[.14] text-[#E9EFED] opacity-80 transition-colors hover:bg-white/[.22] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#FF3621]/35"
              >
                <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18 18 6M6 6l12 12" />
                </svg>
              </button>
            )}
          </div>

          <div className="min-w-[2px] flex-1 sm:hidden" />

          {user && (
            <>
              <CopyableRailBadge
                value={accountInfo?.account_id || accountInfo?.account_name || "Databricks account"}
                text={accountInfo?.account_name || "Databricks account"}
                label="account ID"
                entity="Account"
                icon={<DuBoisAccountIcon />}
                tooltipAlign="start"
              />
              {authStatus && authStatus.identity !== "user_oauth" && (
                (authStatus.sp_user_name || authStatus.sp_client_id) && (
                  <CopyableRailBadge
                    value={authStatus.sp_client_id || authStatus.sp_user_name || ""}
                    text={authStatus.sp_display_name || authStatus.sp_user_name || authStatus.sp_client_id || ""}
                    label="service principal ID"
                    entity="Service principal"
                    icon={<Bot className="h-2.5 w-2.5 opacity-70" aria-label="Service principal" />}
                  />
                )
              )}
              {warehouseStatus && (
                warehouseStatus.warehouse_id ? (
                  <CopyableRailBadge
                    value={warehouseStatus.warehouse_id}
                    text={warehouseStatus.warehouse_name?.trim()
                      || (warehouseStatus.status === "warm" ? "Active" : warehouseStatus.status === "warming_up" ? "Starting" : "Offline")}
                    label="SQL warehouse ID"
                    entity="SQL warehouse"
                    icon={<DatabricksSqlProductIcon />}
                    status={warehouseStatus.status}
                  />
                ) : (
                  <RailBadgeTooltip text={`SQL warehouse status: ${warehouseStatus.state ?? warehouseStatus.status}.`}>
                    <span className={railStatusClass(warehouseStatus.status)}>
                      {warehouseStatus.status === "warm" ? "Active" : warehouseStatus.status === "warming_up" ? "Starting" : "Offline"}
                    </span>
                  </RailBadgeTooltip>
                )
              )}
              {lakebaseStatus?.instance_name && (
                <CopyableRailBadge
                  value={lakebaseStatus.instance_name}
                  text={lakebaseStatus.instance_name}
                  label="Lakebase instance"
                  entity="Lakebase"
                  icon={<Database className="h-2.5 w-2.5 opacity-70" aria-label="Lakebase" />}
                  status={lakebaseStatus.status === "warm" ? "warm" : lakebaseStatus.status === "warming_up" ? "warming_up" : "unavailable"}
                />
              )}
              <UserMenu
                name={user.name}
                email={user.email}
                isAdmin={user.role === "admin"}
                workspaceHost={accountInfo?.host}
              />
              <div className="flex shrink-0 items-center gap-[4px]">
                {tabVisibility.conversational && (
                  <button
                    type="button"
                    onClick={() => setShowConversational(true)}
                    aria-label="Ask Genie"
                    className="rail-control-border inline-flex h-[28px] w-[28px] shrink-0 items-center justify-center rounded-[7px] border bg-white/[.07] px-0 text-[11.5px] font-semibold text-[#E9EFED] transition-colors hover:bg-[#243F49] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#FF3621]/35 focus-visible:ring-offset-1 focus-visible:ring-offset-[#1B3139] min-[1100px]:w-auto min-[1100px]:gap-1.5 min-[1100px]:px-[9px]"
                    title="Ask Genie"
                  >
                    <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
                    </svg>
                    <span className="hidden min-[1100px]:inline">Ask Genie</span>
                  </button>
                )}
                <button
                  type="button"
                  onClick={() => setShowExportDialog(true)}
                  aria-label="Export"
                  className="rail-control-border inline-flex h-[28px] w-[28px] shrink-0 items-center justify-center rounded-[7px] border bg-[#1B5F96] px-0 text-[11.5px] font-semibold text-white transition-colors hover:bg-[#2272B4] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#4D98D0]/50 focus-visible:ring-offset-1 focus-visible:ring-offset-[#1B3139] min-[1100px]:w-auto min-[1100px]:gap-1.5 min-[1100px]:px-[9px]"
                  title="Export"
                >
                  <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                  </svg>
                  <span className="hidden min-[1100px]:inline">Export</span>
                </button>
                <button
                  type="button"
                  onClick={() => setShowSettings(true)}
                  className="flex h-[32px] w-[32px] shrink-0 items-center justify-center rounded-[6px] text-white opacity-80 transition-colors hover:bg-white/[.10] hover:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#FF3621]/35 focus-visible:ring-offset-1 focus-visible:ring-offset-[#1B3139]"
                  title="App Settings"
                  aria-label="App Settings"
                >
                  <Settings size={17} strokeWidth={1.8} aria-hidden="true" />
                </button>
              </div>
            </>
          )}
        </div>
      </div>

      {warehouseHealthCheckFailed && (
        <WarehouseHealthCheckBanner
          isRetrying={warehouseHealthChecking}
          onRetry={() => { void retryWarehouseHealthCheck(); }}
        />
      )}
      {warehouseStatus && (
        <WarehouseGuidanceBanner
          key={warehouseStatus.warehouse_id ?? "unbound"}
          warehouse={warehouseStatus}
          workspaceHost={accountInfo?.host}
        />
      )}
      <AccountPricingBanner />
      <SpGrantsBanner onOpenSettings={() => setShowSettings(true)} />

      <header className="bg-white">
        <div className="mx-auto max-w-7xl px-4 pt-8 pb-4 sm:px-6 lg:px-8">
          <div className="flex flex-wrap items-center gap-4">
            <div className="flex min-w-0 items-center gap-2">
              <button type="button" onClick={() => setActiveTab("dbu")} title="Back to $DBU Spend" className="cursor-pointer transition-opacity hover:opacity-80">
                <CostObsLockup />
              </button>
              <VersionPill />
            </div>
            <div className="w-full sm:ml-auto sm:w-auto">
              <DateRangePicker value={dateRange} onChange={setDateRange} />
            </div>
          </div>
          <DashboardTabNavigation
            activeTab={activeTab}
            visibility={tabVisibility}
            loading={{
              ...tabLoadingFlags,
              ...(tabRefreshing ? { [activeTab]: true } : {}),
            }}
            onChange={setActiveTab}
          />
        </div>
      </header>
      </div>{/* end sticky top chrome */}

      <main className="mx-auto max-w-7xl px-4 py-6 sm:px-6 lg:px-8">
        <div
          key={activeTab}
          id={`dashboard-panel-${activeTab}`}
          role="tabpanel"
          aria-labelledby={`dashboard-tab-${activeTab}`}
          tabIndex={0}
          className="animate-fade-in relative focus-visible:outline-none focus-visible:shadow-(--focus)"
        >
        <TabRefreshRegion
          isLoading={Boolean(tabLoadingFlags[activeTab]) && !tabRefreshing}
          isRefreshing={tabRefreshing}
          loadingSections={TAB_LOADING_SECTIONS[activeTab]}
          onRefresh={handleTabRefresh}
          onDismissRefreshError={() => setRefreshError(null)}
          refreshError={refreshError}
        >
        <Suspense fallback={
          <div className="flex h-64 flex-col items-center justify-center gap-3">
            <Spinner size="md" />
            <p className="text-sm text-gray-500">Loading...</p>
          </div>
        }>
        {activeTab === "dbu" ? (
          bundleLoading ? (
            <div className="flex h-64 flex-col items-center justify-center gap-3">
              <Spinner size="lg" />
              <p className="text-sm text-gray-500">Loading DBU spend data...</p>
            </div>
          ) : (
          <TabErrorBoundary tabName="$DBU Spend">
          <div className="space-y-6">
            <DBUMethodologyPanel />
            {/* Header */}
            <PageHero
              icon={
                <svg viewBox="0 0 24 24" fill="currentColor">
                  <path d="M20 4H4L7 8L4 12H20L17 8L20 4Z" opacity="0.9"/>
                  <path d="M20 8H4L7 12L4 16H20L17 12L20 8Z" opacity="0.7"/>
                  <path d="M20 12H4L7 16L4 20H20L17 16L20 12Z" opacity="0.5"/>
                </svg>
              }
              title="$DBU Spend"
              subtitle="Databricks Unit consumption and cost breakdown"
              dateRange={dateRange}
              workspaceScope={_wsIds && _wsIds.length > 0
                ? _wsIds.length === 1
                  ? workspaceNameMap[_wsIds[0]] || _wsIds[0]
                  : `${_wsIds.length} workspaces`
                : undefined}
            />


            <SummaryCards
              data={summary}
              isLoading={bundleLoading}
              startDate={dateRange.startDate}
              endDate={dateRange.endDate}
              workspaceIds={_wsIds}
              workspaceScopeCount={workspaceScopeCount}
            />

            <SpendChart data={timeseries} isLoading={bundleLoading} />

            <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
              <ProductBreakdown data={products} isLoading={bundleLoading} workspaces={workspaces?.workspaces} dateRange={dateRange} workspaceNameMap={workspaceNameMap} />
              <SKUBreakdown data={skuBreakdown} isLoading={skuLoading} workspaces={workspaces?.workspaces} dateRange={dateRange} workspaceNameMap={workspaceNameMap} />
            </div>

            <WorkspaceTable data={workspaces} isLoading={bundleLoading} host={accountInfo?.host} workspaceNameMap={workspaceNameMap} />

            <InteractiveBreakdown data={interactiveBreakdown} isLoading={interactiveLoading} host={accountInfo?.host} />

            <PipelineObjectsTable data={pipelineObjects} isLoading={pipelineLoading} host={accountInfo?.host} />
          </div>
          </TabErrorBoundary>
          )
        ) : activeTab === "infra" ? (
          <TabErrorBoundary tabName="Cloud Costs">
          <CloudCostsView
            data={infraViewData}
            isLoading={infraBundleLoading}
            timeseriesData={infraViewTimeseries}
            timeseriesLoading={infraBundleLoading}
            host={accountInfo?.host}
            actualData={awsActualData}
            actualLoading={awsActualLoading}
            azureActualData={azureActualData}
            azureActualLoading={azureActualLoading}
            gcpActualData={gcpActualData}
            gcpActualLoading={gcpActualLoading}
            infraData={pricedInfraCosts}
            infraLoading={infraBundleLoading}
            infraTimeseriesData={infraCostsTimeseries}
            infraTimeseriesLoading={infraBundleLoading}
            startDate={dateRange.startDate}
            endDate={dateRange.endDate}
            detectedCloud={accountInfo?.cloud || undefined}
            workspaceNameMap={workspaceNameMap}
            workspaceIds={_wsIds}
            accountPricingApplied={useAccountPrices && accountPricesAvailable}
            loadError={infraBundleErrorObject?.message}
            partialReasons={cloudCostsBundle?.partial_reasons}
          />
          </TabErrorBoundary>
        ) : activeTab === "optimizer" ? (
          <TabErrorBoundary tabName="Optimize">
          <div className="space-y-6">
            <PageHero
              icon={
                <svg fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 6V4m0 2a2 2 0 100 4m0-4a2 2 0 110 4m-6 8a2 2 0 100-4m0 4a2 2 0 110-4m0 4v2m0-6V4m6 6v10m6-2a2 2 0 100-4m0 4a2 2 0 110-4m0 4v2m0-6V4" />
                </svg>
              }
              title="Optimize"
              subtitle="Rightsizing recommendations and cost optimization insights"
            />
            <OptimizeMethodologyPanel />
            <WarehouseIdleTimeView host={accountInfo?.host} startDate={dateRange.startDate} endDate={dateRange.endDate} workspaceIds={_wsIds} />
            <WarehouseRightsizingView host={accountInfo?.host} />
          </div>
          </TabErrorBoundary>
        ) : activeTab === "kpis" ? (
          <TabErrorBoundary tabName="KPIs & Trends">
          <PlatformKPIsView
            data={platformKPIs}
            isLoading={kpisLoading}
            isFetching={kpisBundleFetching}
            spendAnomalies={spendAnomalies}
            anomaliesLoading={anomaliesLoading}
            startDate={dateRange.startDate}
            endDate={dateRange.endDate}
            workspaceIds={_wsIds}
            workspaceNameMap={workspaceNameMap}
          />
          </TabErrorBoundary>
        ) : activeTab === "ai-gateway" ? (
          <TabErrorBoundary tabName="Unity Gateway">
          <AIGatewayView
            data={aiGatewayData}
            isLoading={aiGatewayLoading}
            aimlData={aimlData}
            aimlLoading={aimlLoading}
            startDate={dateRange.startDate}
            endDate={dateRange.endDate}
            host={accountInfo?.host}
            workspaceIds={_wsIds}
            workspaceNameMap={workspaceNameMap}
          />
          </TabErrorBoundary>
        ) : activeTab === "genie" ? (
          <TabErrorBoundary tabName="Genie">
          <GenieCostView
            data={genieCostData}
            isLoading={genieCostLoading}
            startDate={dateRange.startDate}
            endDate={dateRange.endDate}
            workspaceIds={_wsIds}
            workspaceNameMap={workspaceNameMap}
          />
          </TabErrorBoundary>
        ) : activeTab === "apps" ? (
          <TabErrorBoundary tabName="Apps">
          <AppsCostCenter
            data={appsData}
            isLoading={appsLoading}
            host={accountInfo?.host}
            startDate={dateRange.startDate}
            endDate={dateRange.endDate}
            dateRange={dateRange}
            enableHostingComparison={appSettings.enableAppHostingComparison}
            workspaceIds={_wsIds}
            workspaceNameMap={workspaceNameMap}
          />
          </TabErrorBoundary>
        ) : activeTab === "tagging" ? (
          <TabErrorBoundary tabName="Tagging">
          <TaggingHub
            data={taggingData}
            isLoading={taggingLoading}
            host={accountInfo?.host}
            startDate={dateRange.startDate}
            endDate={dateRange.endDate}
            workspaceIds={_wsIds}
            workspaceNameMap={workspaceNameMap}
          />
          </TabErrorBoundary>
        ) : activeTab === "sql" ? (
          <TabErrorBoundary tabName="SQL">
          <SQLWarehousing360
            sqlBreakdownData={sqlBreakdown}
            queryData={dbsqlData ?? undefined}
            isLoading={sqlLoading || dbsqlLoading || dbsqlFetching || dbsqlData === null}
            topQueriesData={dbsqlTopQueriesData}
            topQueriesLoading={dbsqlTopQueriesLoading}
            host={accountInfo?.host}
            startDate={dateRange.startDate}
            endDate={dateRange.endDate}
            workspaceIds={_wsIds}
            workspaceNameMap={workspaceNameMap}
          />
          </TabErrorBoundary>
        ) : activeTab === "use-cases" ? (
          <TabErrorBoundary tabName="Use Cases"><UseCases /></TabErrorBoundary>
        ) : activeTab === "users-groups" ? (
          <TabErrorBoundary tabName="Users">
          <UsersGroups
            startDate={dateRange.startDate}
            endDate={dateRange.endDate}
            dateRange={dateRange}
            anonymizeUsers={appSettings.anonymizeUsers}
            workspaceIds={_wsIds}
            workspaceNameMap={workspaceNameMap}
          />
          </TabErrorBoundary>
        ) : null}
        </Suspense>
        </TabRefreshRegion>
        </div>
      </main>

      <Footer />

      <ExportDialog
        isOpen={showExportDialog}
        onClose={closeExportDialog}
        onExport={handleExport}
        tabVisibility={{
          ...tabVisibility,
          "use-cases": tabVisibility["use-cases"] && appSettings.enableUseCaseTracking,
        }}
        dataLoading={exportDataLoading}
        dataPrepared={exportPrepared}
        requiredTabs={requiredExportTabs}
        tabLoading={tabLoadingFlags}
        dataErrors={tabErrorFlags}
        onPrepare={prepareExportData}
        onSelectionChange={() => {
          setExportPrepared(false);
          setRequiredExportTabs([]);
        }}
        onRetryFailed={retryFailedExportData}
        enableArchitectureView={appSettings.enableArchitectureView}
        enableMvShareRunbook={appSettings.enableMvShareRunbook}
        isAdmin={user?.role === "admin"}
        onExportArchitecture={downloadArchitecturePdf}
      />

      <SettingsDialog
        isOpen={showSettings}
        onClose={() => setShowSettings(false)}
        tabVisibility={tabVisibility}
        appSettings={appSettings}
        onTabVisibilityChange={(v) => {
          setTabVisibility(v);
          // If the active tab was hidden, switch to the first visible tab.
          if (!v[activeTab as keyof typeof v]) {
            setActiveTab(firstVisibleDashboardTab(v) as ViewTab);
          }
        }}
        onSettingsChange={setAppSettings}
      />

      <Suspense fallback={null}>
        <ConversationalDrawer
          open={showConversational && tabVisibility.conversational}
          onClose={() => setShowConversational(false)}
        />
      </Suspense>
    </div>
    </SpNameMapContext.Provider>
  );
}

class TabErrorBoundary extends React.Component<
  { children: React.ReactNode; tabName?: string },
  { error: Error | null }
> {
  state = { error: null as Error | null };
  static getDerivedStateFromError(error: Error) { return { error }; }
  render() {
    if (this.state.error) {
      return (
        <div className="flex flex-col items-center justify-center py-16 px-8 rounded-lg bg-white border " style={{ borderColor: '#E5E5E5' }}>
          <div className="text-3xl mb-3">⚠️</div>
          <h3 className="text-lg font-semibold text-gray-900 mb-1">
            {this.props.tabName ? `${this.props.tabName} encountered an error` : "Something went wrong"}
          </h3>
          <p className="text-sm text-gray-500 text-center max-w-md mb-4">
            This may happen when data is loading or system tables are not accessible. Other tabs should still work.
          </p>
          <button
            onClick={() => this.setState({ error: null })}
            className="px-4 py-2 rounded-lg text-sm font-medium text-white"
            style={{ backgroundColor: '#FF3621' }}
          >
            Try Again
          </button>
          <details className="mt-4 text-xs text-gray-500">
            <summary className="cursor-pointer">Error details</summary>
            <pre className="mt-2 whitespace-pre-wrap">{this.state.error.message}</pre>
          </details>
        </div>
      );
    }
    return this.props.children;
  }
}

class ErrorBoundary extends React.Component<
  { children: React.ReactNode },
  { error: Error | null }
> {
  state = { error: null as Error | null };
  static getDerivedStateFromError(error: Error) {
    return { error };
  }
  render() {
    if (this.state.error) {
      return (
        <div style={{ padding: 40, fontFamily: "sans-serif", background: "#0f172a", color: "#e2e8f0", minHeight: "100vh" }}>
          <h1 style={{ color: "#f97316", marginBottom: 16 }}>Something went wrong</h1>
          <p style={{ color: "#94a3b8", marginBottom: 24 }}>The app encountered an error. This usually happens when data is still loading or system tables are not accessible.</p>
          <button
            onClick={() => { this.setState({ error: null }); window.location.reload(); }}
            style={{ padding: "10px 24px", background: "#f97316", color: "#000", border: "none", borderRadius: 8, cursor: "pointer", fontWeight: 600, marginBottom: 24 }}
          >
            Reload App
          </button>
          <details style={{ marginTop: 16 }}>
            <summary style={{ cursor: "pointer", color: "#64748b" }}>Error details</summary>
            <pre style={{ whiteSpace: "pre-wrap", fontSize: 12, color: "#64748b", marginTop: 8 }}>
              {this.state.error.message}
              {"\n\n"}
              {this.state.error.stack}
            </pre>
          </details>
        </div>
      );
    }
    return this.props.children;
  }
}

function App() {
  return (
    <ErrorBoundary>
      <QueryClientProvider client={queryClient}>
        <PricingProvider>
          <Dashboard />
        </PricingProvider>
      </QueryClientProvider>
    </ErrorBoundary>
  );
}

export default App;
