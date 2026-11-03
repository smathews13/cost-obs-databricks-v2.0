import type { TabVisibility } from "../SettingsDialog";

const TAB_LABELS: Record<keyof TabVisibility, { label: string; color: string }> = {
  dbu: { label: "$DBU Spend", color: "#FF3621" },
  sql: { label: "SQL", color: "#FF3621" },
  "ai-gateway": { label: "Unity Gateway", color: "#FF3621" },
  genie: { label: "Genie", color: "#FF3621" },
  apps: { label: "Apps", color: "#FF3621" },
  tagging: { label: "Tagging", color: "#FF3621" },
  "users-groups": { label: "Users", color: "#FF3621" },
  kpis: { label: "Platform KPIs & Trends", color: "#FF3621" },
  infra: { label: "Cloud Costs", color: "#FF3621" },
  optimizer: { label: "Optimize", color: "#FF3621" },
  conversational: { label: "Ask Genie (conversational surface)", color: "#FF3621" },
};

interface SettingsTabsProps {
  localVisibility: TabVisibility;
  toggleTab: (key: keyof TabVisibility) => void;
  visibleCount: number;
}

export function SettingsTabs({ localVisibility, toggleTab, visibleCount }: SettingsTabsProps) {
  return (
    <div>
      <p className="mb-3 text-sm text-gray-500">
        Toggle which tabs are visible in the dashboard. At least one tab must remain visible.
      </p>
      <div className="space-y-1.5">
        {(Object.keys(TAB_LABELS) as Array<keyof TabVisibility>).map((key) => {
          const { label, color } = TAB_LABELS[key];
          const checked = localVisibility[key];
          return (
            <label
              key={key}
              className={`flex cursor-pointer items-center justify-between rounded-lg border-2 px-3 py-2 transition-colors ${
                checked ? "border-gray-200 bg-white" : "border-gray-100 bg-gray-50 opacity-60"
              }`}
            >
              <div className="flex items-center gap-3">
                <div className="h-3 w-3 rounded-full" style={{ backgroundColor: color }} />
                <span className={`font-medium ${checked ? "text-gray-900" : "text-gray-500"}`}>{label}</span>
              </div>
              <div className="relative">
                <input type="checkbox" checked={checked} onChange={() => toggleTab(key)} className="sr-only" disabled={checked && visibleCount <= 1} />
                <div className={`h-6 w-11 rounded-full transition-colors ${checked ? "" : "bg-gray-300"}`} style={checked ? { backgroundColor: color } : {}} />
                <div className={`absolute left-0.5 top-0.5 h-5 w-5 rounded-full bg-white shadow transition-transform ${checked ? "translate-x-5" : "translate-x-0"}`} />
              </div>
            </label>
          );
        })}
      </div>
    </div>
  );
}
