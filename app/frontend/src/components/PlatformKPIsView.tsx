import { useState, useEffect, memo } from "react";
import { useQueryClient } from "@tanstack/react-query";
import type { PlatformKPIsResponse, SpendAnomaliesResponse } from "@/types/billing";
import { SpendAnomalies } from "@/components/SpendAnomalies";
import { KPITrendModal } from "@/components/KPITrendModal";
import { KPICard as UiKPICard } from "@/components/ui/KPICard";
import { formatNumber, formatBytesNoDecimal, formatRowCount, formatDurationSeconds } from "@/utils/formatters";
import { useFeatureAvailability } from "@/hooks/useFeatureAvailability";
import { C } from "@/theme";
import { PageHero, InfoPanel } from "@/components/brand";

interface PlatformKPIsViewProps {
  data: PlatformKPIsResponse | undefined;
  isLoading: boolean;
  isFetching?: boolean;
  spendAnomalies: SpendAnomaliesResponse | undefined;
  anomaliesLoading: boolean;
  startDate?: string;
  endDate?: string;
  workspaceIds?: string[];
  workspaceNameMap?: Record<string, string>;
}

interface KPICardProps {
  title: string;
  value: string;
  subtitle?: string;
  infoTooltip?: string;
  icon: React.ReactNode;
  color?: string;
  onClick?: () => void;
  isLoading?: boolean;
  /** When set, renders an unavailable state with this reason instead of the value. */
  unavailableReason?: string;
}

const KPICard = memo(function KPICard({ title, value, subtitle, infoTooltip, icon, onClick, isLoading, unavailableReason }: KPICardProps) {
  return (
    <UiKPICard
      title={title}
      value={value}
      subtitle={subtitle}
      infoText={infoTooltip}
      icon={icon}
      onActivate={onClick}
      isLoading={isLoading}
      unavailableReason={unavailableReason}
    />
  );
});

const PLATFORM_KPI_KEYS = [
  "total_queries", "total_rows_read", "total_bytes_read", "total_compute_seconds",
  "total_jobs", "total_job_runs", "successful_runs", "active_notebooks",
  "active_workspaces", "models_served", "total_users",
] as const;

export function PlatformKPIsView({ data, isLoading, isFetching, spendAnomalies, anomaliesLoading, startDate, endDate, workspaceIds, workspaceNameMap }: PlatformKPIsViewProps) {
  const queryClient = useQueryClient();

  // Per-feature availability from the shared hook (caches under READINESS_QUERY_KEY).
  const { tableGranted } = useFeatureAvailability();

  const lakeflowGranted     = tableGranted("system.lakeflow.pipelines");
  const computeGranted      = tableGranted("system.compute.clusters");
  const servingGranted      = tableGranted("system.serving.served_entities");
  const queryHistoryGranted = tableGranted("system.query.history");

  // Only suppress a card when the dependency is **explicitly** denied, not when unknown.
  const jobsUnavailable       = lakeflowGranted === false    ? "lakeflow grants required — run SP grants to fix" : undefined;
  const clustersUnavailable   = computeGranted === false     ? "compute.clusters grant required" : undefined;
  const servingUnavailable    = servingGranted === false     ? "serving.served_entities grant required" : undefined;
  const queryHistUnavailable  = queryHistoryGranted === false ? "query.history grant required" : undefined;

  const [selectedKPI, setSelectedKPI] = useState<{
    kpi: "total_queries" | "total_rows_read" | "total_bytes_read" | "total_compute_seconds" | "total_jobs" | "total_job_runs" | "successful_runs" | "active_notebooks" | "active_workspaces" | "models_served" | "total_users" | "avg_query_duration" | "unique_warehouses" | "stickiness";
    label: string;
  } | null>(null);

  const wsKey = workspaceIds?.join(",") ?? "";

  // Pre-warm the first 3 KPI trends in the background; the rest load on card click
  useEffect(() => {
    if (!startDate || !endDate) return;
    for (const kpi of PLATFORM_KPI_KEYS.slice(0, 3)) {
      queryClient.prefetchQuery({
        queryKey: ["platform-kpi-trend", kpi, startDate, endDate, "daily", wsKey],
        queryFn: async () => {
          const params = new URLSearchParams({ kpi, start_date: startDate, end_date: endDate, granularity: "daily" });
          if (wsKey) params.set("workspace_ids", wsKey);
          const res = await fetch(`/api/billing/platform-kpi-trend?${params}`);
          if (!res.ok) throw new Error("prefetch failed");
          return res.json();
        },
        staleTime: 5 * 60 * 1000,
      });
    }
  }, [startDate, endDate, wsKey, queryClient]);

  // Info box minimize state with localStorage persistence
  const MINIMIZE_KEY = "cost-obs-minimize-kpis-info";
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

  if (isLoading) {
    return (
      <div className="flex h-64 flex-col items-center justify-center gap-3">
        <div className="h-12 w-12 animate-spin rounded-full border-4 border-gray-200" style={{ borderTopColor: C.lava }} />
        <p className="text-sm text-gray-500">Loading platform KPIs...</p>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="rounded-lg bg-white p-6 border " style={{ borderColor: C.hairline }}>
        <div className="flex h-32 flex-col items-center justify-center gap-2 text-gray-500">
          <p className="text-base font-medium">No platform KPI data available</p>
          <p className="text-sm">Try adjusting the date range or verify system tables are accessible</p>
        </div>
      </div>
    );
  }

  // Success rate requires job result state data from system.lakeflow tables
  // If successful_runs is 0 but we have job runs, it means we don't have the result state data
  const hasSuccessRateData = data.successful_runs > 0;

  const periodDays = startDate && endDate
    ? Math.round((new Date(endDate).getTime() - new Date(startDate).getTime()) / 86400000) + 1
    : 0;

  const successRatePct = hasSuccessRateData && data.total_job_runs > 0
    ? ((data.successful_runs / data.total_job_runs) * 100).toFixed(1)
    : null;

  const stickinessPct = data.stickiness_pct != null && data.stickiness_pct > 0
    ? Math.round(data.stickiness_pct)
    : (data.avg_daily_query_users && data.unique_query_users > 0
        ? Math.min(100, Math.round((data.avg_daily_query_users / data.unique_query_users) * 100))
        : null);

  const workspacePct = data.total_workspace_count && data.total_workspace_count > 0
    ? Math.round(((data.avg_daily_workspaces ?? data.active_workspaces) / data.total_workspace_count) * 100)
    : null;

  const handleKPIClick = (kpi: "total_queries" | "total_rows_read" | "total_bytes_read" | "total_compute_seconds" | "total_jobs" | "total_job_runs" | "successful_runs" | "active_notebooks" | "active_workspaces" | "models_served" | "total_users" | "avg_query_duration" | "unique_warehouses" | "stickiness", label: string) => {
    setSelectedKPI({ kpi, label });
  };

  return (
    <div className="animate-fade-in space-y-6">
      <InfoPanel
        title="KPIs & Trends tab methodology"
        minimized={infoMinimized}
        onToggle={handleMinimizeToggle}
      >
        <ul className="list-inside list-disc space-y-1">
          <li><strong>Query and Data Processing</strong>: SQL execution, data scanned, and compute time across all warehouses</li>
          <li><strong>Jobs and Workflows</strong>: Automated job executions, success rates, and notebook usage</li>
          <li><strong>Platform Utilization</strong>: Active workspaces, model serving endpoints, user adoption, and usage stickiness (DAU/MAU)</li>
          <li>Use each metric card's See trend button to view historical trends and patterns</li>
        </ul>
      </InfoPanel>

      {/* Header */}
      <PageHero
        icon={
          <svg fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" />
          </svg>
        }
        title="Platform KPIs & Trends"
        subtitle="Platform health, usage metrics, and adoption tracking"
        dateRange={{ startDate, endDate }}
        workspaceScope={workspaceIds && workspaceIds.length > 0
          ? workspaceIds.length === 1
            ? (workspaceNameMap?.[workspaceIds[0]] || workspaceIds[0])
            : `${workspaceIds.length} workspaces`
          : undefined}
      />

      {data.data_stale && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-2.5 text-xs text-amber-700">
          Showing cached data — live query returned no results. Values reflect the last successful load.
        </div>
      )}

      {/* Query & Data Processing Metrics */}
      <div>
        <h3 className="mb-4 text-lg font-semibold text-gray-900">Query & Data Processing</h3>
        <div className="grid grid-cols-1 gap-6 sm:grid-cols-2 lg:grid-cols-4">
          <KPICard
            title="Total Queries Executed"
            value={formatNumber(data.total_queries)}
            subtitle={`from ${formatNumber(data.unique_query_users)} unique users`}
            isLoading={isLoading || isFetching}
            color="bg-orange-100"
            unavailableReason={queryHistUnavailable}
            onClick={!queryHistUnavailable && startDate && endDate ? () => handleKPIClick("total_queries", "Daily Queries Executed") : undefined}
            icon={
              <svg className="h-6 w-6 text-lava" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-3 7h3m-3 4h3m-6-4h.01M9 16h.01" />
              </svg>
            }
          />

          <KPICard
            title="Rows Processed"
            value={formatRowCount(data.total_rows_read)}
            subtitle="total data scanned"
            isLoading={isLoading || isFetching}
            color="bg-orange-100"
            unavailableReason={queryHistUnavailable}
            onClick={!queryHistUnavailable && startDate && endDate ? () => handleKPIClick("total_rows_read", "Rows Processed") : undefined}
            icon={
              <svg className="h-6 w-6 text-lava" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 7v10c0 2.21 3.582 4 8 4s8-1.79 8-4V7M4 7c0 2.21 3.582 4 8 4s8-1.79 8-4M4 7c0-2.21 3.582-4 8-4s8 1.79 8 4m0 5c0 2.21-3.582 4-8 4s-8-1.79-8-4" />
              </svg>
            }
          />

          <KPICard
            title="Data Processed"
            value={formatBytesNoDecimal(data.total_bytes_read)}
            subtitle="total throughput"
            isLoading={isLoading || isFetching}
            color="bg-orange-100"
            unavailableReason={queryHistUnavailable}
            onClick={!queryHistUnavailable && startDate && endDate ? () => handleKPIClick("total_bytes_read", "Data Processed") : undefined}
            icon={
              <svg className="h-6 w-6 text-lava" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z" />
              </svg>
            }
          />

          <KPICard
            title="Compute Time"
            value={formatDurationSeconds(data.total_compute_seconds)}
            subtitle="total processing time"
            isLoading={isLoading || isFetching}
            color="bg-orange-100"
            unavailableReason={queryHistUnavailable}
            onClick={!queryHistUnavailable && startDate && endDate ? () => handleKPIClick("total_compute_seconds", "Daily Active Processing Time") : undefined}
            icon={
              <svg className="h-6 w-6 text-lava" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
            }
          />
        </div>
      </div>

      {/* Jobs & Workflows */}
      <div>
        <h3 className="mb-4 text-lg font-semibold text-gray-900">Jobs & Workflows</h3>
        <div className="grid grid-cols-1 gap-6 sm:grid-cols-2 lg:grid-cols-4">
          <KPICard
            title="Total Active Jobs"
            value={formatNumber(data.total_jobs)}
            subtitle={`${formatNumber(data.unique_job_owners)} unique owners`}
            infoTooltip="Distinct jobs with billing usage across the full selected period. The daily trend shows unique jobs active each day, which is typically lower — the same job counts once per day vs. once for the entire period."
            color="bg-orange-100"
            isLoading={isLoading || isFetching}
            unavailableReason={jobsUnavailable}
            onClick={!jobsUnavailable && startDate && endDate ? () => handleKPIClick("total_jobs", "Daily Active Jobs") : undefined}
            icon={
              <svg className="h-6 w-6 text-lava" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 13.255A23.931 23.931 0 0112 15c-3.183 0-6.22-.62-9-1.745M16 6V4a2 2 0 00-2-2h-4a2 2 0 00-2 2v2m4 6h.01M5 20h14a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" />
              </svg>
            }
          />

          <KPICard
            title="Job Runs"
            value={formatNumber(data.total_job_runs)}
            subtitle={(data.total_job_run_hours ?? 0) > 0 ? `${formatNumber(data.total_job_run_hours ?? 0)} hrs total run time` : "Total executions"}
            color="bg-orange-100"
            isLoading={isLoading || isFetching}
            unavailableReason={jobsUnavailable}
            onClick={!jobsUnavailable && startDate && endDate ? () => handleKPIClick("total_job_runs", "Daily Job Runs") : undefined}
            icon={
              <svg className="h-6 w-6 text-lava" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 10V3L4 14h7v7l9-11h-7z" />
              </svg>
            }
          />

          <KPICard
            title="Successful Runs"
            value={hasSuccessRateData ? formatNumber(data.successful_runs) : "N/A"}
            subtitle={successRatePct !== null ? `${successRatePct}% success rate` : "Result states unavailable"}
            color={hasSuccessRateData ? "bg-orange-100" : "bg-gray-100"}
            isLoading={isLoading || isFetching}
            onClick={startDate && endDate && hasSuccessRateData ? () => handleKPIClick("successful_runs", "Successful Runs") : undefined}
            icon={
              <svg className={`h-6 w-6 ${hasSuccessRateData ? "text-lava" : "text-gray-500"}`} fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
            }
          />

          <KPICard
            title="Total Unique Clusters"
            value={formatNumber(data.active_notebooks)}
            subtitle={periodDays > 0 ? `active across ${periodDays} day${periodDays === 1 ? "" : "s"}` : "Unique clusters, period total"}
            infoTooltip="Distinct clusters with billing usage across the full selected period. The daily trend shows unique clusters active each day, which is typically lower — the same cluster counts once per day vs. once for the entire period."
            color="bg-orange-100"
            isLoading={isLoading || isFetching}
            unavailableReason={clustersUnavailable}
            onClick={!clustersUnavailable && startDate && endDate ? () => handleKPIClick("active_notebooks", "Daily Active Clusters") : undefined}
            icon={
              <svg className="h-6 w-6 text-lava" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 12h14M5 12a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v4a2 2 0 01-2 2M5 12a2 2 0 00-2 2v4a2 2 0 002 2h14a2 2 0 002-2v-4a2 2 0 00-2-2m-2-4h.01M17 16h.01" />
              </svg>
            }
          />
        </div>
      </div>

      {/* Platform Utilization */}
      <div>
        <h3 className="mb-4 text-lg font-semibold text-gray-900">Platform Utilization</h3>
        <div className="grid grid-cols-1 gap-6 sm:grid-cols-2 lg:grid-cols-4">
          <KPICard
            title="Active Workspaces"
            value={formatNumber(data.avg_daily_workspaces != null && data.avg_daily_workspaces !== 0 ? data.avg_daily_workspaces : data.active_workspaces)}
            subtitle={workspacePct !== null ? `${workspacePct}% active of ${formatNumber(data.total_workspace_count ?? 0)} total` : "Avg workspaces active per day"}
            infoTooltip="Average number of distinct workspaces with billable usage per day in the selected period. Matches the daily trend average."
            color="bg-orange-100"
            isLoading={isLoading || isFetching}
            onClick={startDate && endDate ? () => handleKPIClick("active_workspaces", "Daily Active Workspaces") : undefined}
            icon={
              <svg className="h-6 w-6 text-lava" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10" />
              </svg>
            }
          />

          {(data.models_served > 0 || servingUnavailable) && (
            <KPICard
              title="Unique Models Served"
              value={formatNumber(data.models_served)}
              subtitle={(data.avg_daily_models ?? 0) > 0 ? `${formatNumber(data.avg_daily_models ?? 0)} served daily` : `${formatNumber(data.total_serving_dbus)} DBUs`}
              infoTooltip="Distinct model-serving endpoints active at any point during the period. The daily trend shows unique endpoints per day, which is typically lower — the same endpoint counts once per day vs. once for the entire period."
              color="bg-orange-100"
              isLoading={isLoading || isFetching}
              unavailableReason={servingUnavailable}
              onClick={!servingUnavailable && startDate && endDate ? () => handleKPIClick("models_served", "Daily Models Served") : undefined}
              icon={
                <svg className="h-6 w-6 text-lava" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
                </svg>
              }
            />
          )}

          <KPICard
            title="Unique Active Users"
            value={formatNumber(data.unique_query_users)}
            subtitle={`${formatNumber(data.unique_job_owners ?? 0)} job owners`}
            isLoading={isLoading || isFetching}
            infoTooltip="Distinct SQL query executors in the selected period (matches the daily trend). Job owners shown separately — some users may appear in both groups."
            color="bg-orange-100"
            onClick={startDate && endDate ? () => handleKPIClick("total_users", "Daily Active Users") : undefined}
            icon={
              <svg className="h-6 w-6 text-lava" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z" />
              </svg>
            }
          />

          <KPICard
            title="Usage Stickiness"
            value={stickinessPct !== null ? `${stickinessPct}%` : "N/A"}
            subtitle="DAU / total users ratio"
            infoTooltip="Avg daily active users divided by total unique users in the period. Higher = more habitual usage. 20%+ is strong engagement; 10% suggests occasional usage."
            color={stickinessPct !== null ? "bg-orange-100" : "bg-gray-100"}
            isLoading={isLoading || isFetching}
            onClick={stickinessPct !== null && startDate && endDate ? () => handleKPIClick("stickiness", "Daily Usage Stickiness") : undefined}
            icon={
              <svg className={`h-6 w-6 ${stickinessPct !== null ? "text-lava" : "text-gray-500"}`} fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" />
              </svg>
            }
          />
        </div>
      </div>

      {/* Spend Changes & Trends */}
      <SpendAnomalies data={spendAnomalies} isLoading={anomaliesLoading} />

      {/* Platform KPI Trend Modal */}
      {selectedKPI && startDate && endDate && (
        <KPITrendModal
          variant="platform"
          kpi={selectedKPI.kpi}
          kpiLabel={selectedKPI.label}
          isOpen={!!selectedKPI}
          onClose={() => setSelectedKPI(null)}
          startDate={startDate}
          endDate={endDate}
          workspaceIds={workspaceIds}
        />
      )}
    </div>
  );
}
