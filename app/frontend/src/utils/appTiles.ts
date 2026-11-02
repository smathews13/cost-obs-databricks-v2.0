const MIN_ICON_PX = 32;
const MAX_ICON_PX = 56;

/** Icon edge for an app tile: 32px at no spend, 56px for the top spender. */
export function appIconSize(spend: number, maxSpend: number): number {
  const s = Number(spend) || 0;
  const ratio = maxSpend > 0 ? Math.min(1, Math.max(0, s / maxSpend)) : 0;
  return Math.round(MIN_ICON_PX + ratio * (MAX_ICON_PX - MIN_ICON_PX));
}
