import { memo } from "react";
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  Legend,
  CartesianGrid,
} from "recharts";
import { format, parseISO } from "date-fns";
import type { TimeseriesResponse } from "@/types/billing";
import { formatCurrencyCompact as formatCurrency } from "@/utils/formatters";
import { C, productColor } from "@/theme";
import { InkTooltip } from "@/components/chartTheme";
import { axisTick, gridStroke, baselineStroke } from "@/components/chartTokens";

interface SpendChartProps {
  data: TimeseriesResponse | undefined;
  isLoading: boolean;
}

function categoryColor(category: string): string {
  if (category === "SQL - Genie") return C.s5;
  if (category === "ETL - Batch" || category === "ETL - Streaming") return category.includes("Stream") ? C.s1 : C.s3;
  if (category === "Model Serving") return C.s1;
  return productColor(category);
}

function formatDate(dateStr: string): string {
  try {
    return format(parseISO(dateStr), "MMM d");
  } catch {
    return dateStr;
  }
}

function CardShell({ children, title }: { children: React.ReactNode; title: string }) {
  return (
    <div className="co-card p-6">
      <h3 className="mb-4 text-base font-semibold" style={{ color: C.ink }}>{title}</h3>
      {children}
    </div>
  );
}

export const SpendChart = memo(function SpendChart({ data, isLoading }: SpendChartProps) {
  if (isLoading) {
    return (
      <CardShell title="Spend Over Time">
        <div className="h-80 animate-pulse rounded" style={{ backgroundColor: C.hairline }} />
      </CardShell>
    );
  }

  if (!data || data.timeseries.length === 0) {
    return (
      <CardShell title="Spend Over Time">
        <div className="flex h-80 flex-col items-center justify-center gap-2" style={{ color: C.slate }}>
          <p className="text-base font-medium">No spend data available</p>
          <p className="text-sm">Try adjusting the date range or check that billing data is being collected</p>
        </div>
      </CardShell>
    );
  }

  return (
    <div className="animate-fade-in co-card p-6">
      <h3 className="mb-4 text-base font-semibold" style={{ color: C.ink }}>Spend Over Time</h3>
      <ResponsiveContainer width="100%" height={320}>
        <AreaChart data={data.timeseries} margin={{ top: 10, right: 30, left: 0, bottom: 0 }}>
          <CartesianGrid vertical={false} stroke={gridStroke} />
          <XAxis
            dataKey="date"
            tickFormatter={formatDate}
            stroke={baselineStroke}
            tick={axisTick}
            tickMargin={8}
          />
          <YAxis tickFormatter={formatCurrency} stroke={baselineStroke} tick={axisTick} width={80} />
          <Tooltip
            content={<InkTooltip formatter={(v) => formatCurrency(v)} />}
          />
          <Legend wrapperStyle={{ fontSize: 12, color: C.slate }} />
          {data.categories.map((category) => {
            const color = categoryColor(category);
            return (
              <Area
                isAnimationActive={false}
                key={category}
                type="monotone"
                dataKey={category}
                stackId="1"
                stroke={color}
                strokeWidth={2.2}
                fill={color}
                fillOpacity={0.16}
                dot={false}
                activeDot={{ r: 3 }}
              />
            );
          })}
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
});
