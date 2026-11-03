export interface TabVisibility {
  dbu: boolean;
  infra: boolean;
  optimizer: boolean;
  kpis: boolean;
  "ai-gateway": boolean;
  genie: boolean;
  sql: boolean;
  apps: boolean;
  tagging: boolean;
  "users-groups": boolean;
  conversational: boolean;
}

export const DASHBOARD_TAB_ORDER: Array<keyof TabVisibility> = [
  "dbu",
  "sql",
  "ai-gateway",
  "genie",
  "apps",
  "tagging",
  "users-groups",
  "kpis",
  "infra",
  "optimizer",
];

export function firstVisibleDashboardTab(visibility: TabVisibility): keyof TabVisibility {
  return DASHBOARD_TAB_ORDER.find((key) => visibility[key]) ?? "dbu";
}

export const DEFAULT_VISIBILITY: TabVisibility = {
  dbu: true, infra: true, optimizer: true, kpis: true, "ai-gateway": true, genie: true,
  sql: true, apps: true, tagging: true, "users-groups": true, conversational: true,
};

const STORAGE_KEY = "coc-tab-visibility";

export function loadTabVisibility(): TabVisibility {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored) {
      // Saved settings can hold tabs that no longer exist (e.g. the retired Use Cases tab).
      const parsed = JSON.parse(stored) as Record<string, unknown>;
      const merged = { ...DEFAULT_VISIBILITY };
      for (const key of Object.keys(DEFAULT_VISIBILITY) as (keyof TabVisibility)[]) {
        if (typeof parsed[key] === "boolean") merged[key] = parsed[key] as boolean;
      }
      return merged;
    }
  } catch { /* ignore */ }
  return DEFAULT_VISIBILITY;
}

export function saveTabVisibility(visibility: TabVisibility) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(visibility));
}

// ── App Settings ────────────────────────────────────────────────────────────
export interface AppSettings {
  defaultDateRangeDays: 7 | 14 | 30 | 60 | 90;
  refreshIntervalMinutes: 0 | 5 | 15 | 30;
  compactMode: boolean;              // deprecated — migrated to `density`
  darkMode: boolean;                 // deprecated — migrated to `theme`
  density: "comfortable" | "compact";
  theme: "light" | "dark" | "system";
  defaultLandingTab: string;
  showWorkspaceNames: boolean;
  anomalySensitivity: "low" | "medium" | "high";
  expSetupWizardLink: boolean;
  expDebuggerLink: boolean;
  enableArchitectureView: boolean;
  enableMvShareRunbook: boolean;
  companyName: string;
  appDisplayName: string;
  monthlyBudget: number;
  costAllocationTags: string;
  alertSpikePercent: number;
  alertDailyBudget: number;
  alertWorkspaceBudget: number;
  slackWebhookUrl: string;
  enableAppHostingComparison: boolean;
  enableAccuracyChecks: boolean;
  anonymizeUsers: boolean;
}

export const DEFAULT_APP_SETTINGS: AppSettings = {
  defaultDateRangeDays: 30,
  refreshIntervalMinutes: 0,
  compactMode: false,
  darkMode: false,
  density: "comfortable",
  theme: "light",
  defaultLandingTab: "dbu",
  showWorkspaceNames: true,
  anomalySensitivity: "medium",
  expSetupWizardLink: false,
  expDebuggerLink: false,
  enableArchitectureView: true,
  enableMvShareRunbook: true,
  companyName: "",
  appDisplayName: "",
  monthlyBudget: 0,
  costAllocationTags: "",
  alertSpikePercent: 20,
  alertDailyBudget: 50000,
  alertWorkspaceBudget: 10000,
  slackWebhookUrl: "",
  enableAppHostingComparison: false,
  enableAccuracyChecks: false,
  anonymizeUsers: false,
};

const APP_SETTINGS_KEY = "coc-app-settings";

export function loadAppSettings(): AppSettings {
  try {
    const stored = localStorage.getItem(APP_SETTINGS_KEY);
    if (stored) {
      const parsed = JSON.parse(stored);
      const merged = { ...DEFAULT_APP_SETTINGS, ...parsed } as AppSettings;
      // Migrate deprecated boolean toggles into the new selects (only when the new
      // keys are absent, so an explicit new choice always wins).
      if (parsed.theme === undefined) merged.theme = parsed.darkMode ? "dark" : "light";
      if (parsed.density === undefined) merged.density = parsed.compactMode ? "compact" : "comfortable";
      return merged;
    }
  } catch { /* ignore */ }
  return { ...DEFAULT_APP_SETTINGS };
}

export function saveAppSettings(settings: AppSettings) {
  // Keep the deprecated booleans in sync so any un-migrated reader still works.
  const out = { ...settings, darkMode: settings.theme === "dark", compactMode: settings.density === "compact" };
  localStorage.setItem(APP_SETTINGS_KEY, JSON.stringify(out));
}
