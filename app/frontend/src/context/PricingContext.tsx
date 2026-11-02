/**
 * PricingContext — provides the active pricing multiplier to all components.
 *
 * When `use_account_prices` is enabled in Settings, the backend computes a
 * usage-weighted blended multiplier (account_price / list_price) from
 * system.billing.account_prices, weighted by usage in the dashboard's current
 * date range and workspaces (see setPricingScope). The Overview and
 * infrastructure spend figures are scaled by it; other tabs stay at list price.
 */
import React, { useCallback, useEffect, useState } from "react";
import { PricingContext } from "./pricingState";


export function PricingProvider({ children }: { children: React.ReactNode }) {
  const [useAccountPrices, setUseAccountPricesState] = useState(false);
  const [multiplier, setMultiplier] = useState(1.0);
  const [discountPercent, setDiscountPercent] = useState(0);
  const [skuCount, setSkuCount] = useState(0);
  const [available, setAvailable] = useState(false);
  const [loading, setLoading] = useState(true);
  const [prefLoaded, setPrefLoaded] = useState(false);
  const [scope, setScope] = useState<{ startDate?: string; endDate?: string; workspaceIds?: string }>({});

  const setPricingScope = useCallback((startDate?: string, endDate?: string, workspaceIds?: string[]) => {
    const ws = workspaceIds?.length ? workspaceIds.join(",") : undefined;
    setScope((prev) =>
      prev.startDate === startDate && prev.endDate === endDate && prev.workspaceIds === ws
        ? prev
        : { startDate, endDate, workspaceIds: ws },
    );
  }, []);

  const loadMultiplier = async () => {
    try {
      const params = new URLSearchParams();
      if (scope.startDate) params.set("start_date", scope.startDate);
      if (scope.endDate) params.set("end_date", scope.endDate);
      if (scope.workspaceIds) params.set("workspace_ids", scope.workspaceIds);
      const res = await fetch(`/api/settings/account-price-multiplier?${params}`);
      if (!res.ok) return;
      const data = await res.json();
      setMultiplier(data.multiplier ?? 1.0);
      setDiscountPercent(data.discount_percent ?? 0);
      setSkuCount(data.sku_count ?? 0);
      setAvailable(data.available ?? false);
    } catch {
      setMultiplier(1.0);
      setAvailable(false);
    }
  };

  useEffect(() => {
    // Load pricing mode preference from server
    fetch("/api/settings/pricing-mode")
      .then((r) => r.json())
      .then((d) => {
        setUseAccountPricesState(d.use_account_prices ?? false);
      })
      .catch(() => {})
      .finally(() => setPrefLoaded(true));
  }, []);

  // (Re)load whenever the mode or the dashboard's date range / workspaces change.
  useEffect(() => {
    if (!prefLoaded) return;
    if (!useAccountPrices) {
      setLoading(false);
      return;
    }
    setLoading(true);
    loadMultiplier().finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [prefLoaded, useAccountPrices, scope]);

  const setUseAccountPrices = async (enabled: boolean) => {
    await fetch("/api/settings/pricing-mode", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ use_account_prices: enabled }),
    });
    setUseAccountPricesState(enabled);
    if (!enabled) {
      setMultiplier(1.0);
      setDiscountPercent(0);
      setAvailable(false);
    }
  };

  return (
    <PricingContext.Provider
      value={{
        useAccountPrices,
        multiplier,
        discountPercent,
        skuCount,
        available,
        loading,
        applyPricing: (amount: number) => amount * multiplier,
        setUseAccountPrices,
        setPricingScope,
      }}
    >
      {children}
    </PricingContext.Provider>
  );
}
