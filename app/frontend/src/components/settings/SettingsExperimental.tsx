import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { AppSettings } from "../SettingsDialog";
import { usePricing } from "@/context/pricingState";

interface SettingsExperimentalProps {
  localSettings: AppSettings;
  updateSetting: <K extends keyof AppSettings>(key: K, value: AppSettings[K]) => void;
  saveStatus: string | null;
}

export function SettingsExperimental({ localSettings, updateSetting, saveStatus }: SettingsExperimentalProps) {
  const { useAccountPrices, setUseAccountPrices, discountPercent, available: pricingAvailable, loading: pricingLoading } = usePricing();
  const [pricingToggling, setPricingToggling] = useState(false);
  const [priceSearch, setPriceSearch] = useState("");
  const { data: accountPrices = null, isLoading: accountPricesLoading } = useQuery<{
    available: boolean;
    prices: Array<{ sku_name: string; cloud: string; currency_code: string; usage_unit: string; list_price: number; effective_list_price: number; start_time: string | null; end_time: string | null }>;
    source: string | null;
    count: number;
    message?: string;
  } | null>({
    queryKey: ["settings-account-prices"],
    queryFn: () => fetch("/api/settings/account-prices").then(r => r.json()).catch(() => ({ available: false, prices: [], source: null, count: 0 })),
    staleTime: 5 * 60 * 1000,
  });

  return (
    <div className="space-y-5">
      {saveStatus && (
        <div className="rounded-lg border border-green-200 bg-green-50 px-4 py-2 text-sm text-green-700">
          {saveStatus}
        </div>
      )}

      <div>
        <div className="flex items-center gap-2 mb-1">
          <svg className="h-4 w-4 text-amber-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19.428 15.428a2 2 0 00-1.022-.547l-2.387-.477a6 6 0 00-3.86.517l-.318.158a6 6 0 01-3.86.517L6.05 15.21a2 2 0 00-1.806.547M8 4h8l-1 1v5.172a2 2 0 00.586 1.414l5 5c1.26 1.26.367 3.414-1.415 3.414H4.828c-1.782 0-2.674-2.154-1.414-3.414l5-5A2 2 0 009 10.172V5L8 4z" />
          </svg>
          <h4 className="text-sm font-semibold text-gray-900">Experimental Features</h4>
        </div>
        <p className="mb-4 text-xs text-gray-500">
          These features are in preview and may change or be removed. Enable them to try out new functionality.
        </p>

        <div className="space-y-3">
          {/* App Hosting Cost Comparison — hidden from customers, preserved for future use
          <label className="flex items-start gap-3 rounded-lg border border-gray-200 bg-white p-4 cursor-pointer hover:border-gray-300 transition-colors">
            <input type="checkbox" checked={localSettings.enableAppHostingComparison} onChange={(e) => updateSetting("enableAppHostingComparison", e.target.checked)} className="mt-0.5 h-4 w-4 rounded border-gray-300 text-orange-600 focus:ring-orange-500" />
            <div>
              <div className="text-sm font-medium text-gray-900">App Hosting Cost Comparison</div>
              <div className="mt-0.5 text-xs text-gray-500">Show a comparison panel in the Apps tab that estimates hosting costs across alternative platforms (e.g. AWS, Azure, GCP) versus Databricks Apps.</div>
            </div>
          </label>
          */}

          {/* Cost Accuracy Checks — hidden from customers, preserved for future use
          <label className="flex items-start gap-3 rounded-lg border border-gray-200 bg-white p-4 cursor-pointer hover:border-gray-300 transition-colors">
            <input type="checkbox" checked={localSettings.enableAccuracyChecks} onChange={(e) => updateSetting("enableAccuracyChecks", e.target.checked)} className="mt-0.5 h-4 w-4 rounded border-gray-300 text-orange-600 focus:ring-orange-500" />
            <div>
              <div className="text-sm font-medium text-gray-900">Cost Accuracy Checks</div>
              <div className="mt-0.5 text-xs text-gray-500">Add an Accuracy Checks tab to Settings for running cross-validation queries that verify cost reporting is correct — detecting double-counting, missing attribution, and price lookup gaps.</div>
            </div>
          </label>
          */}

          {/* Account Pricing */}
          <div className="rounded-lg border border-gray-200 bg-white p-4 space-y-3">
            <div className="flex items-center gap-2">
              <svg className="h-4 w-4 text-gray-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M7 7h.01M7 3h5c.512 0 1.024.195 1.414.586l7 7a2 2 0 010 2.828l-7 7a2 2 0 01-2.828 0l-7-7A1.994 1.994 0 013 12V7a4 4 0 014-4z" />
              </svg>
              <span className="text-sm font-medium text-gray-900">Account Pricing</span>
              {accountPrices?.source && (
                <span className="rounded-full bg-gray-100 px-2 py-0.5 text-xs text-gray-500">
                  {accountPrices.source === "account_prices" ? "Negotiated rates" : "List prices"}
                </span>
              )}
              <div className="inline-flex items-center gap-1 rounded-full bg-amber-50 px-2 py-0.5 text-[10px] font-medium text-amber-700 border border-amber-200">
                <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19.428 15.428a2 2 0 00-1.022-.547l-2.387-.477a6 6 0 00-3.86.517l-.318.158a6 6 0 01-3.86.517L6.05 15.21a2 2 0 00-1.806.547M8 4h8l-1 1v5.172a2 2 0 00.586 1.414l5 5c1.26 1.26.367 3.414-1.415 3.414H4.828c-1.782 0-2.674-2.154-1.414-3.414l5-5A2 2 0 009 10.172V5L8 4z" />
                </svg>
                Preview
              </div>
            </div>
            {/* Use account prices toggle */}
            <div className="flex items-center justify-between rounded-lg border border-gray-100 bg-gray-50 px-3 py-2.5">
              <div>
                <p className="text-xs font-medium text-gray-800">Use account prices</p>
                <p className="text-xs text-gray-500 mt-0.5">
                  {useAccountPrices && pricingAvailable && discountPercent > 0
                    ? <span className="font-medium text-green-600">{discountPercent.toFixed(1)}% blended discount active on Overview and infrastructure spend. Other tabs show list prices.</span>
                    : useAccountPrices && !pricingAvailable && !pricingLoading
                    ? <span className="text-amber-600">No negotiated rates found (table missing or empty) — showing list prices.</span>
                    : <>Applies a usage-weighted blended discount from <code className="rounded bg-gray-100 px-0.5">system.billing.account_prices</code> (negotiated account rates) to Overview and infrastructure spend for the selected date range. SKUs without a negotiated rate count at list. Other tabs, budgets and alerts stay at list price (<code className="rounded bg-gray-100 px-0.5">system.billing.list_prices</code>).</>
                  }
                </p>
              </div>
              <button
                role="switch"
                aria-checked={useAccountPrices}
                disabled={pricingToggling}
                onClick={async () => {
                  setPricingToggling(true);
                  await setUseAccountPrices(!useAccountPrices);
                  setPricingToggling(false);
                }}
                className={`relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors focus:outline-none ${
                  useAccountPrices ? "bg-green-500" : "bg-gray-200"
                } ${pricingToggling ? "opacity-50" : ""}`}
              >
                <span
                  className={`pointer-events-none inline-block h-4 w-4 rounded-full bg-white shadow transform transition-transform ${
                    useAccountPrices ? "translate-x-4" : "translate-x-0"
                  }`}
                />
              </button>
            </div>
            {accountPricesLoading ? (
              <p className="text-xs text-gray-500">Loading...</p>
            ) : !accountPrices?.available ? (
              <span className="inline-flex items-center rounded-full bg-red-50 border border-red-200 px-2.5 py-1 text-xs font-medium text-red-600">
                {accountPrices?.message || "Pricing tables not accessible"}
              </span>
            ) : (
              <>
                <div className="flex items-center gap-2">
                  <input
                    type="text"
                    placeholder="Filter by SKU name..."
                    value={priceSearch}
                    onChange={e => setPriceSearch(e.target.value)}
                    className="rounded border border-gray-200 px-2 py-1.5 text-xs w-64 focus:outline-none focus:ring-1 focus:ring-[#FF3621]"
                  />
                  <span className="text-xs text-gray-500">{accountPrices.count} SKUs</span>
                </div>
                <div className="overflow-x-auto rounded-lg border border-gray-200 max-h-64">
                  <table className="min-w-full divide-y divide-gray-200 text-xs">
                    <thead className="bg-gray-50 sticky top-0">
                      <tr>
                        <th className="px-3 py-2 text-left text-xs font-medium text-gray-500">SKU Name</th>
                        <th className="px-3 py-2 text-left text-xs font-medium text-gray-500">Cloud</th>
                        <th className="px-3 py-2 text-right text-xs font-medium text-gray-500">List Price</th>
                        <th className="px-3 py-2 text-right text-xs font-medium text-gray-500">Effective Price</th>
                        <th className="px-3 py-2 text-left text-xs font-medium text-gray-500">Unit</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-gray-100 bg-white">
                      {accountPrices.prices
                        .filter(p => !priceSearch || p.sku_name.toLowerCase().includes(priceSearch.toLowerCase()))
                        .map((p, i) => (
                          <tr key={`${p.sku_name}-${p.cloud}-${i}`} className="hover:bg-gray-50">
                            <td className="px-3 py-2 font-mono text-gray-700">{p.sku_name}</td>
                            <td className="px-3 py-2 text-gray-500">{p.cloud}</td>
                            <td className="px-3 py-2 text-right text-gray-600">${p.list_price.toFixed(4)}</td>
                            <td className={`px-3 py-2 text-right font-medium ${p.effective_list_price < p.list_price ? "text-green-600" : "text-gray-900"}`}>
                              ${p.effective_list_price.toFixed(4)}
                              {p.effective_list_price < p.list_price && (
                                <span className="ml-1 text-green-500">
                                  ({((1 - p.effective_list_price / p.list_price) * 100).toFixed(0)}% off)
                                </span>
                              )}
                            </td>
                            <td className="px-3 py-2 text-gray-500">{p.usage_unit}</td>
                          </tr>
                        ))}
                    </tbody>
                  </table>
                </div>
              </>
            )}
          </div>

          {/* Anonymize Users */}
          <div className="rounded-lg border border-gray-200 bg-white p-4 space-y-3">
            <div className="flex items-center gap-2">
              <svg className="h-4 w-4 text-gray-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
              </svg>
              <span className="text-sm font-medium text-gray-900">Anonymize Users</span>
              <div className="inline-flex items-center gap-1 rounded-full bg-amber-50 px-2 py-0.5 text-[10px] font-medium text-amber-700 border border-amber-200">
                <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19.428 15.428a2 2 0 00-1.022-.547l-2.387-.477a6 6 0 00-3.86.517l-.318.158a6 6 0 01-3.86.517L6.05 15.21a2 2 0 00-1.806.547M8 4h8l-1 1v5.172a2 2 0 00.586 1.414l5 5c1.26 1.26.367 3.414-1.415 3.414H4.828c-1.782 0-2.674-2.154-1.414-3.414l5-5A2 2 0 009 10.172V5L8 4z" />
                </svg>
                Preview
              </div>
            </div>
            <div className="flex items-center justify-between rounded-lg border border-gray-100 bg-gray-50 px-3 py-2.5">
              <div>
                <p className="text-xs font-medium text-gray-800">Enable anonymization</p>
                <p className="text-xs text-gray-500 mt-0.5">
                  Replace human user emails with generic labels (User 1, User 2, …) throughout the Users tab. Service principals are not affected.
                </p>
              </div>
              <button
                role="switch"
                aria-checked={localSettings.anonymizeUsers}
                onClick={() => updateSetting("anonymizeUsers", !localSettings.anonymizeUsers)}
                className={`relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors focus:outline-none ${
                  localSettings.anonymizeUsers ? "bg-green-500" : "bg-gray-200"
                }`}
              >
                <span className={`pointer-events-none inline-block h-4 w-4 rounded-full bg-white shadow transform transition-transform ${
                  localSettings.anonymizeUsers ? "translate-x-4" : "translate-x-0"
                }`} />
              </button>
            </div>
          </div>

          {/* Use Case Tracking */}
          <div className="rounded-lg border border-gray-200 bg-white p-4 space-y-3">
            <div className="flex items-center gap-2">
              <svg className="h-4 w-4 text-gray-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10" />
              </svg>
              <span className="text-sm font-medium text-gray-900">Use Case Tracking</span>
              <div className="inline-flex items-center gap-1 rounded-full bg-amber-50 px-2 py-0.5 text-[10px] font-medium text-amber-700 border border-amber-200">
                <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19.428 15.428a2 2 0 00-1.022-.547l-2.387-.477a6 6 0 00-3.86.517l-.318.158a6 6 0 01-3.86.517L6.05 15.21a2 2 0 00-1.806.547M8 4h8l-1 1v5.172a2 2 0 00.586 1.414l5 5c1.26 1.26.367 3.414-1.415 3.414H4.828c-1.782 0-2.674-2.154-1.414-3.414l5-5A2 2 0 009 10.172V5L8 4z" />
                </svg>
                Preview
              </div>
            </div>
            <div className="flex items-center justify-between rounded-lg border border-gray-100 bg-gray-50 px-3 py-2.5">
              <div>
                <p className="text-xs font-medium text-gray-800">Enable use case tracking</p>
                <p className="text-xs text-gray-500 mt-0.5">
                  Add a Use Cases tab for tracking and categorizing Databricks workloads by business use case, including cost attribution and usage patterns per use case.
                </p>
              </div>
              <button
                role="switch"
                aria-checked={localSettings.enableUseCaseTracking}
                onClick={() => updateSetting("enableUseCaseTracking", !localSettings.enableUseCaseTracking)}
                className={`relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors focus:outline-none ${
                  localSettings.enableUseCaseTracking ? "bg-green-500" : "bg-gray-200"
                }`}
              >
                <span className={`pointer-events-none inline-block h-4 w-4 rounded-full bg-white shadow transform transition-transform ${
                  localSettings.enableUseCaseTracking ? "translate-x-4" : "translate-x-0"
                }`} />
              </button>
            </div>
          </div>

          <div className="rounded-lg border border-gray-100 bg-white p-4 space-y-2">
            <div className="flex items-center gap-2">
              <svg className="h-4 w-4 text-gray-700" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17H3a2 2 0 01-2-2V5a2 2 0 012-2h14a2 2 0 012 2v10a2 2 0 01-2 2h-2" />
              </svg>
              <span className="text-sm font-medium text-gray-900">Unity Gateway</span>
            </div>
            <p className="text-xs text-gray-500">
              Token traffic, requesters, and the service catalog live on the Unity Gateway tab.
              Usage still comes from <code className="rounded bg-gray-100 px-0.5">system.ai_gateway.usage</code> through the admin-shared table.
            </p>
          </div>

        </div>
      </div>
    </div>
  );
}
