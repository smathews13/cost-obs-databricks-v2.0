import { createContext, useContext } from "react";

export interface PricingState {
  useAccountPrices: boolean;
  multiplier: number;
  discountPercent: number;
  skuCount: number;
  available: boolean;
  loading: boolean;
  /** Scale a spend amount by the active pricing multiplier */
  applyPricing: (amount: number) => number;
  /** Toggle the setting and reload multiplier */
  setUseAccountPrices: (enabled: boolean) => Promise<void>;
  /** Weight the multiplier by usage in this date range / workspace selection */
  setPricingScope: (startDate?: string, endDate?: string, workspaceIds?: string[]) => void;
}

export const PricingContext = createContext<PricingState>({
  useAccountPrices: false,
  multiplier: 1.0,
  discountPercent: 0,
  skuCount: 0,
  available: false,
  loading: true,
  applyPricing: (x) => x,
  setUseAccountPrices: async () => {},
  setPricingScope: () => {},
});

export function usePricing() {
  return useContext(PricingContext);
}
