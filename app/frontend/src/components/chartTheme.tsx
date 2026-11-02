import { ChartTooltipFrame } from "@/components/brand";

type Payload = { name?: string; value?: number; color?: string; dataKey?: string | number };

export function InkTooltip({
  active,
  label,
  payload,
  formatter,
}: {
  active?: boolean;
  label?: string;
  payload?: Payload[];
  formatter?: (value: number, name: string) => string;
}) {
  if (!active || !payload?.length) return null;
  const fmt = formatter ?? ((v: number) => String(v));
  return (
    <ChartTooltipFrame
      title={String(label ?? "")}
      rows={payload.map((p) => ({
        label: String(p.name ?? p.dataKey ?? ""),
        value: fmt(Number(p.value ?? 0), String(p.name ?? "")),
        color: p.color,
      }))}
    />
  );
}
