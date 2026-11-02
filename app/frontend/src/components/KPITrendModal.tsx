import { useEffect, useCallback } from "react";
import { createPortal } from "react-dom";
import { format, parseISO } from "date-fns";
import { X, TrendingUp, TrendingDown, Minus } from "lucide-react";
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
} from "recharts";
import { useKPITrend, usePlatformKPITrend, useAppsKPITrend } from "@/hooks/useKPITrend";
import { formatCurrency, formatNumber, formatBytesNoDecimal, formatRowCount, formatComputeSecondsCompact } from "@/utils/formatters";
import { C, FONT_MONO } from "@/theme";
import { changeTone } from "@/components/changeTone";
import { InkTooltip } from "@/components/chartTheme";
import { axisTick, gridStroke, baselineStroke } from "@/components/chartTokens";
import { Spinner } from "@/components/Spinner";

interface KPITrendModalProps {
  kpi: string;
  kpiLabel: string;
  isOpen: boolean;
  onClose: () => void;
  startDate: string;
  endDate: string;
  formatValue?: (value: number, kpi: string) => string;
  variant?: "billing" | "platform" | "apps";
  workspaceIds?: string[];
}

const SPEND_KPIS = new Set(["total_spend", "avg_daily_spend", "aiml_spend", "apps_spend", "tagged_spend", "untagged_spend", "infra_cost", "avg_cost_per_cluster", "sql_spend", "aiml_avg_endpoint_cost", "apps_avg_cost_per_app", "cost_per_tag", "user_spend", "power_user_spend", "avg_spend_per_user"]);

function defaultBillingFormat(value: number, kpi: string): string {
  if (SPEND_KPIS.has(kpi)) {
    return formatCurrency(value);
  }
  if (kpi === "tag_coverage_pct") {
    return `${value.toFixed(1)}%`;
  }
  return formatNumber(value);
}

function defaultPlatformFormat(value: number, kpi: string): string {
  if (kpi === "total_bytes_read") return formatBytesNoDecimal(value);
  if (kpi === "total_rows_read") return formatRowCount(value);
  if (kpi === "total_compute_seconds") return formatComputeSecondsCompact(value);
  if (kpi === "avg_query_duration") return `${value.toFixed(1)}s`;
  if (kpi === "stickiness") return `${value.toFixed(1)}%`;
  return formatNumber(value);
}

export function KPITrendModal({
  kpi,
  kpiLabel,
  isOpen,
  onClose,
  startDate,
  endDate,
  formatValue,
  variant = "billing",
  workspaceIds,
}: KPITrendModalProps) {
  const billingTrend = useKPITrend(kpi, startDate, endDate, "daily", workspaceIds);
  const platformTrend = usePlatformKPITrend(kpi, startDate, endDate, "daily", workspaceIds);
  const appsTrend = useAppsKPITrend(kpi, startDate, endDate, "daily");
  const { data, isLoading } = variant === "platform" ? platformTrend : variant === "apps" ? appsTrend : billingTrend;

  const fmt = formatValue ?? (variant === "platform" ? defaultPlatformFormat : defaultBillingFormat);

  const handleKeyDown = useCallback((e: KeyboardEvent) => {
    if (e.key === "Escape") onClose();
  }, [onClose]);

  useEffect(() => {
    if (isOpen) {
      document.addEventListener("keydown", handleKeyDown);
      document.body.style.overflow = "hidden";
    }
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      document.body.style.overflow = "";
    };
  }, [isOpen, handleKeyDown]);

  if (!isOpen) return null;

  const formattedStart = format(parseISO(startDate), "MMM d, yyyy");
  const formattedEnd = format(parseISO(endDate), "MMM d, yyyy");
  const changePct = data?.summary?.change_percent ?? 0;
  const tone = changeTone(changePct);

  return createPortal(
    <div
      className="animate-backdrop fixed inset-0 z-50 overflow-y-auto"
      style={{ background: "rgba(11,32,38,.45)" }}
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <div className="flex min-h-full items-center justify-center p-4" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div
        className="animate-dialog relative w-full max-w-4xl bg-white"
        style={{ borderRadius: 12, boxShadow: "var(--sh-modal)" }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-6 py-4" style={{ borderBottom: `1px solid ${C.hairline}` }}>
          <div>
            <h2 className="text-xl font-semibold" style={{ color: C.ink }}>{kpiLabel}</h2>
            <p className="text-sm" style={{ color: C.slate }}>Trend Analysis</p>
          </div>
          <button
            onClick={onClose}
            className="rounded-lg p-2 transition-colors"
            style={{ color: C.slate }}
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        <div className="p-6">
          {isLoading ? (
            <div className="flex h-80 items-center justify-center">
              <Spinner size="md" />
            </div>
          ) : data?.data_points && data.data_points.length > 0 ? (
            <>
              <div className="mb-6 grid grid-cols-4 gap-4">
                {[
                  ["Start", fmt(data.summary.period_start_value, kpi)],
                  ["End", fmt(data.summary.period_end_value, kpi)],
                  ["Average", fmt(data.summary.avg_value, kpi)],
                ].map(([label, value]) => (
                  <div key={label} className="p-4" style={{ background: C.oatPage, borderRadius: 8 }}>
                    <p className="text-[11px] font-bold uppercase tracking-wide" style={{ color: C.slate }}>{label}</p>
                    <p className="mt-1 text-[22px] font-medium" style={{ color: C.ink, fontFamily: FONT_MONO }}>{value}</p>
                  </div>
                ))}
                <div className="p-4" style={{ background: tone.bg, borderRadius: 8 }}>
                  <p className="text-[11px] font-bold uppercase tracking-wide" style={{ color: C.slate }}>Change</p>
                  <p className="mt-1 text-[22px] font-medium" style={{ color: tone.fg, fontFamily: FONT_MONO }}>
                    {tone.label === "±0.0%" ? "±0.0%" : `${changePct > 0 ? "+" : ""}${changePct.toFixed(1)}%`}
                  </p>
                </div>
              </div>

              <div className="h-64">
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart data={data.data_points} margin={{ top: 10, right: 10, left: 0, bottom: 0 }}>
                    <CartesianGrid vertical={false} stroke={gridStroke} />
                    <XAxis
                      dataKey="date"
                      tickFormatter={(value) => format(parseISO(value), "MMM d")}
                      tick={axisTick}
                      axisLine={{ stroke: baselineStroke }}
                    />
                    <YAxis
                      tickFormatter={(value) => fmt(value, kpi)}
                      tick={axisTick}
                      axisLine={{ stroke: baselineStroke }}
                      width={80}
                    />
                    <Tooltip content={<InkTooltip formatter={(v) => fmt(v, kpi)} />} />
                    <Area isAnimationActive={false}
                      type="monotone"
                      dataKey="value"
                      stroke={C.lava}
                      strokeWidth={2.2}
                      fill={C.lava}
                      fillOpacity={0.16}
                      dot={false}
                      activeDot={{ r: 3 }}
                    />
                  </AreaChart>
                </ResponsiveContainer>
              </div>

              <div className="mt-4 flex items-center gap-2 px-4 py-3" style={{ background: tone.bg, borderRadius: 8 }}>
                {data.summary.trend === "increasing" && (
                  <>
                    <TrendingUp className="h-5 w-5" style={{ color: C.lavaHover }} />
                    <span className="text-sm font-medium" style={{ color: C.ink }}>
                      Trending upward by {data.summary.change_percent.toFixed(1)}% from {formattedStart} to {formattedEnd}
                    </span>
                  </>
                )}
                {data.summary.trend === "decreasing" && (
                  <>
                    <TrendingDown className="h-5 w-5" style={{ color: C.greenInk }} />
                    <span className="text-sm font-medium" style={{ color: C.ink }}>
                      Trending downward by {Math.abs(data.summary.change_percent).toFixed(1)}% from {formattedStart} to {formattedEnd}
                    </span>
                  </>
                )}
                {data.summary.trend === "flat" && (
                  <>
                    <Minus className="h-5 w-5" style={{ color: C.slate }} />
                    <span className="text-sm font-medium" style={{ color: C.ink }}>
                      Relatively stable (±{Math.abs(data.summary.change_percent).toFixed(1)}%) from {formattedStart} to {formattedEnd}
                    </span>
                  </>
                )}
              </div>
            </>
          ) : (
            <div className="flex h-64 flex-col items-center justify-center" style={{ color: C.slate }}>
              <p className="text-lg font-medium">No data available</p>
              <p className="text-sm">Try selecting a different date range</p>
            </div>
          )}
        </div>
      </div>
      </div>
    </div>,
    document.body
  );
}
