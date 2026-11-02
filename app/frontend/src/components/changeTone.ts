import { C } from "@/theme";

export function changeTone(pct: number): { bg: string; fg: string; label: string } {
  if (Math.abs(pct) < 0.5) return { bg: C.oatMed, fg: C.slate, label: "±0.0%" };
  if (pct > 0) return { bg: C.coralTint, fg: C.lavaHover, label: `+${pct.toFixed(1)}%` };
  return { bg: C.greenTint, fg: C.greenInk, label: `${pct.toFixed(1)}%` };
}
