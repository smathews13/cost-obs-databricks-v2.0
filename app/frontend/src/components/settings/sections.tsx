import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { AppSettings, TabVisibility, SettingsCapabilities } from "../SettingsDialog";
import { SettingsConfig } from "./SettingsConfig";
import type { AppConfigInfo } from "./SettingsConfig";
import { SettingsPermissions } from "./SettingsPermissions";
import { SettingsResources } from "./SettingsResources";
import {
  SectionTitle, Group, Row, Toggle, Select, TextInput, UnitInput, Badge, MonoChip,
  SecondaryButton, LinkButton, Callout, T, MONO,
} from "./dubois";
import { useToast } from "./toast";

// Tab labels for landing-tab select + visibility list (spec §6 order).
const TAB_LABELS: Record<keyof TabVisibility, string> = {
  dbu: "$DBU Spend", sql: "SQL", "ai-gateway": "Unity Gateway", genie: "Genie", apps: "Apps",
  tagging: "Tagging", "users-groups": "Users", kpis: "Platform KPIs & Trends",
  infra: "Cloud Costs", optimizer: "Optimize", "use-cases": "Use Cases",
  conversational: "Ask Genie (conversational surface)",
};
// Always-shown, landing-eligible main tabs (the ViewTab set, minus the conditional
// use-cases and the conversational overlay which isn't a landing destination).
const TAB_ORDER: (keyof TabVisibility)[] = ["dbu", "sql", "ai-gateway", "genie", "apps", "tagging", "users-groups", "kpis", "infra", "optimizer"];

const ext = (
  <svg style={{ width: 12, height: 12, flexShrink: 0 }} fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" /></svg>
);

interface CommonProps {
  localSettings: AppSettings;
  updateSetting: <K extends keyof AppSettings>(key: K, value: AppSettings[K]) => void;
  caps?: SettingsCapabilities;
}

// ── General ──────────────────────────────────────────────────────────────────
export function GeneralSection({ localSettings, updateSetting, tabVisibility, caps }: CommonProps & { tabVisibility: TabVisibility }) {
  const toast = useToast();
  const qc = useQueryClient();
  const [spCopied, setSpCopied] = useState(false);

  const { data: appConfig } = useQuery<{ identity?: { display_name: string | null; user_name: string | null } | null }>({
    queryKey: ["app-config"], queryFn: () => fetch("/api/settings/config").then(r => r.json()).catch(() => null),
  });
  const { data: authStatus } = useQuery<{ sp_user_name?: string; sp_client_id?: string; sp_display_name?: string } | null>({
    queryKey: ["settings-auth-status"], queryFn: () => fetch("/api/settings/auth-status").then(r => r.json()).catch(() => null), staleTime: 60000,
  });
  const { data: appLinks } = useQuery<{ source_code_url: string; app_page_url: string; setup_job_url?: string } | null>({
    queryKey: ["settings-app-links"], queryFn: () => fetch("/api/settings/app-links").then(r => r.json()).catch(() => null), staleTime: 300000,
  });
  const { data: pricing } = useQuery<{ use_account_prices: boolean } | null>({
    queryKey: ["settings-pricing-mode"], queryFn: () => fetch("/api/settings/pricing-mode").then(r => r.json()).catch(() => null), staleTime: 300000,
  });

  const sp = authStatus?.sp_user_name || authStatus?.sp_client_id || appConfig?.identity?.display_name || appConfig?.identity?.user_name || "Service principal";
  const defaultName = authStatus?.sp_display_name || appConfig?.identity?.display_name || "service principal name";
  const visibleTabs = TAB_ORDER.filter((k) => tabVisibility[k]);

  const setPricing = async (useAccount: boolean) => {
    qc.setQueryData(["settings-pricing-mode"], { use_account_prices: useAccount });
    try {
      await fetch("/api/settings/pricing-mode", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ use_account_prices: useAccount }) });
      qc.invalidateQueries();
      toast(useAccount ? "Using account prices" : "Using list prices");
    } catch { toast("Could not update price basis"); }
  };

  return (
    <div>
      <SectionTitle title="General" subtitle="App identity and default dashboard behavior." />

      <Group label="App identity">
        <Row first label="Display name" helper={`Shown in the app header. Leave blank to use the default (${defaultName}).`}
          control={<TextInput value={localSettings.appDisplayName} onChange={(v) => updateSetting("appDisplayName", v)} placeholder={defaultName} />} />
        <Row label="Company name" helper="Appears in exported PDF report headers."
          control={<TextInput value={localSettings.companyName} onChange={(v) => updateSetting("companyName", v)} placeholder="e.g. Acme Corp" />} />
        <Row label="Service principal" helper="The identity this app runs as. Managed by Databricks Apps."
          control={
            <div className="flex items-center gap-2">
              <MonoChip>{sp}</MonoChip>
              <LinkButton onClick={() => { navigator.clipboard?.writeText(sp); setSpCopied(true); toast("Copied"); setTimeout(() => setSpCopied(false), 1500); }}>{spCopied ? "Copied" : "Copy"}</LinkButton>
            </div>
          } />
        {(appLinks?.source_code_url || appLinks?.app_page_url || appLinks?.setup_job_url) && (
          <Row label="Links"
            control={
              <div className="flex items-center gap-4 flex-wrap">
                {appLinks?.source_code_url && <LinkButton href={appLinks.source_code_url}><span className="inline-flex items-center gap-1">App source code {ext}</span></LinkButton>}
                {appLinks?.app_page_url && <LinkButton href={appLinks.app_page_url}><span className="inline-flex items-center gap-1">App backend {ext}</span></LinkButton>}
                {appLinks?.setup_job_url && <LinkButton href={appLinks.setup_job_url}><span className="inline-flex items-center gap-1">Setup job {ext}</span></LinkButton>}
              </div>
            } />
        )}
      </Group>

      <Group label="Dashboard defaults">
        <Row first label="Default date range" helper="Time window shown on dashboard load."
          control={<Select value={localSettings.defaultDateRangeDays} onChange={(v) => updateSetting("defaultDateRangeDays", v as AppSettings["defaultDateRangeDays"])}
            options={[{ value: 7, label: "Last 7 days" }, { value: 30, label: "Last 30 days" }, { value: 60, label: "Last 60 days" }, { value: 90, label: "Last 90 days" }]} />} />
        <Row label="Default landing tab" helper="Tab shown first when the dashboard opens."
          control={<Select value={localSettings.defaultLandingTab} onChange={(v) => updateSetting("defaultLandingTab", v)}
            options={visibleTabs.map((k) => ({ value: k as string, label: TAB_LABELS[k] }))} />} />
        <Row label="Auto-refresh" helper="Automatically refresh dashboard data."
          control={<Select value={localSettings.refreshIntervalMinutes} onChange={(v) => updateSetting("refreshIntervalMinutes", v as AppSettings["refreshIntervalMinutes"])}
            options={[{ value: 0, label: "Off" }, { value: 5, label: "Every 5 minutes" }, { value: 15, label: "Every 15 minutes" }, { value: 30, label: "Every 30 minutes" }]} />} />
        <Row label="Density" helper="Spacing of tables and cards."
          control={<Select value={localSettings.density} onChange={(v) => updateSetting("density", v as AppSettings["density"])}
            options={[{ value: "comfortable", label: "Comfortable" }, { value: "compact", label: "Compact" }]} />} />
        <Row label="Theme" helper="Color scheme."
          control={<Select value={localSettings.theme} onChange={(v) => updateSetting("theme", v as AppSettings["theme"])}
            options={[{ value: "light", label: "Light" }, { value: "dark", label: "Dark" }, { value: "system", label: "Match system" }]} />} />
        <Row label="Price basis"
          helper={caps && !caps.account_prices_available
            ? "system.billing.account_prices not accessible (private preview) — account prices fall back to list prices. Saves immediately."
            : "Account prices read from system.billing.account_prices (private preview). Saves immediately."}
          control={<Select value={pricing?.use_account_prices ? "account" : "list"} onChange={(v) => setPricing(v === "account")}
            options={[{ value: "list", label: "List prices" }, { value: "account", label: "Account prices" }]} />} />
      </Group>
    </div>
  );
}

// ── Dashboard tabs ────────────────────────────────────────────────────────────
export function DashboardTabsSection({ localVisibility, toggleTab, enableUseCaseTracking }: {
  localVisibility: TabVisibility; toggleTab: (k: keyof TabVisibility) => void; enableUseCaseTracking?: boolean;
}) {
  const toast = useToast();
  // Full toggle list: main tabs + conditional use-cases + the conversational overlay.
  const keys = [
    ...TAB_ORDER,
    ...(enableUseCaseTracking ? (["use-cases"] as (keyof TabVisibility)[]) : []),
    "conversational" as keyof TabVisibility,
  ];
  const visibleCount = keys.filter((k) => localVisibility[k]).length;
  return (
    <div>
      <SectionTitle title="Dashboard tabs" subtitle={`Choose which tabs viewers see. At least one tab must remain visible. ${visibleCount} of ${keys.length} visible.`} />
      <Group>
        {keys.map((k, i) => (
          <Row key={k} first={i === 0} label={TAB_LABELS[k]}
            control={<Toggle checked={localVisibility[k]} onChange={() => {
              if (localVisibility[k] && visibleCount <= 1) { toast("At least one tab must stay visible"); return; }
              toggleTab(k);
            }} />} />
        ))}
      </Group>
    </div>
  );
}

// ── Alerts & notifications ──────────────────────────────────────────────────
export function AlertsSection({ localSettings, updateSetting, caps }: CommonProps) {
  const toast = useToast();
  const [checking, setChecking] = useState(false);
  const { data: webhookStatus } = useQuery<{ configured: boolean } | null>({
    queryKey: ["settings-webhook-status"], queryFn: () => fetch("/api/settings/webhook").then(r => r.json()).catch(() => null), staleTime: 300000,
  });
  const masked = webhookStatus?.configured && !localSettings.slackWebhookUrl;
  return (
    <div>
      <SectionTitle title="Alerts & notifications" subtitle="Thresholds that drive spike detection and where alerts are delivered." />
      <Group label="Thresholds">
        <Row first label="Spike threshold" helper="Day-over-day change that triggers an alert."
          control={<UnitInput affix="%" value={localSettings.alertSpikePercent} min={1} max={500} onChange={(v) => updateSetting("alertSpikePercent", v || 20)} />} />
        <Row label="Daily budget" helper="Alert when account-wide daily spend exceeds this."
          control={<UnitInput affix="$" affixSide="left" width={160} value={localSettings.alertDailyBudget} min={0} step={1000} onChange={(v) => updateSetting("alertDailyBudget", v || 0)} />} />
        <Row label="Workspace budget" helper="Per-workspace daily limit."
          control={<UnitInput affix="$" affixSide="left" width={160} value={localSettings.alertWorkspaceBudget} min={0} step={1000} onChange={(v) => updateSetting("alertWorkspaceBudget", v || 0)} />} />
        <Row label="Anomaly sensitivity" helper="Maps to spike-detection thresholds in alerting."
          control={<Select value={localSettings.anomalySensitivity ?? "medium"} onChange={(v) => updateSetting("anomalySensitivity", v as AppSettings["anomalySensitivity"])}
            options={[{ value: "low", label: "Low" }, { value: "medium", label: "Medium" }, { value: "high", label: "High" }]} />} />
      </Group>
      <Group label="Delivery">
        <Row first label="Slack webhook" helper={masked ? "A webhook is configured on the server (masked). Enter a new URL to replace it." : "Post alerts to a Slack channel."}
          control={
            <div className="flex items-center gap-2">
              <TextInput mono value={localSettings.slackWebhookUrl} onChange={(v) => updateSetting("slackWebhookUrl", v)}
                placeholder={masked ? "https://hooks.slack.com/…••••" : "https://hooks.slack.com/services/…"} width={260} />
              <SecondaryButton disabled={!localSettings.slackWebhookUrl && !webhookStatus?.configured}
                onClick={async () => {
                  if (localSettings.slackWebhookUrl) await fetch("/api/settings/webhook", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ slack_webhook_url: localSettings.slackWebhookUrl }) });
                  const r = await fetch("/api/settings/webhook/test", { method: "POST" }); const d = await r.json().catch(() => ({}));
                  toast(d.success ? "Test message sent to Slack" : `Test failed: ${d.error ?? "unknown"}`);
                }}>Send test</SecondaryButton>
            </div>
          } />
        <Row label="Email recipients"
          helper={caps?.smtp_configured ? "Comma-separated addresses for alert emails." : "Requires SMTP configuration (SMTP_* environment variables)."}
          control={<TextInput value="" onChange={() => {}} placeholder="Comma-separated addresses" disabled={!caps?.smtp_configured} width={260} />} />
        <Row label="Run alert check now"
          helper="Evaluate saved thresholds against the latest day's spend; posts any breaches to the Slack webhook. Runs automatically each night too."
          control={<SecondaryButton disabled={checking} onClick={async () => {
            setChecking(true);
            try {
              const r = await fetch("/api/settings/alerts/run", { method: "POST" });
              const d = await r.json().catch(() => ({}));
              const n = (d.breaches || []).length;
              if (!r.ok) toast("Alert check failed");
              else if (n === 0) toast("No threshold breaches in the latest data");
              else toast(`${n} breach${n === 1 ? "" : "es"} found${d.sent ? " — posted to Slack" : " (configure a webhook to deliver)"}`);
            } catch { toast("Alert check failed"); }
            finally { setChecking(false); }
          }}>{checking ? "Checking…" : "Run check"}</SecondaryButton>} />
      </Group>
    </div>
  );
}

// ── Resources (read-only) ─────────────────────────────────────────────────────
export function ResourcesSection() {
  return <SettingsResources />;
}

// ── Experimental (admin-only) ─────────────────────────────────────────────────
export function ExperimentalSection({ localSettings, updateSetting }: CommonProps) {
  const toast = useToast();
  const qc = useQueryClient();
  const [clearing, setClearing] = useState(false);
  return (
    <div>
      <SectionTitle title="Experimental" badge={<Badge>Admin only</Badge>} subtitle="Early features. Off by default; may change or be removed in a future version." />
      <Group>
        <Row first label={<span className="inline-flex items-center gap-2">Setup wizard link <Badge>Experimental</Badge></span>}
          helper="Show the Setup wizard as a link in the settings nav footer."
          control={<Toggle checked={localSettings.expSetupWizardLink} onChange={(v) => updateSetting("expSetupWizardLink", v)} />} />
        <Row label={<span className="inline-flex items-center gap-2">Debugger link <Badge>Experimental</Badge></span>}
          helper="Show the query debugger (SQL + timing per tile) as a nav-footer link."
          control={<Toggle checked={localSettings.expDebuggerLink} onChange={(v) => updateSetting("expDebuggerLink", v)} />} />
        <Row label="Architecture PDF"
          helper="Offer the customer-facing v2 architecture document, including Lakebase serving and state."
          control={<Toggle checked={localSettings.enableArchitectureView} onChange={(v) => updateSetting("enableArchitectureView", v)} />} />
        <Row label="Materialized-view share runbook"
          helper="Let administrators download the Run All notebook for publishing gold aggregates through Delta Sharing."
          control={<Toggle checked={localSettings.enableMvShareRunbook} onChange={(v) => updateSetting("enableMvShareRunbook", v)} />} />
        <Row label="Clear query cache" helper="Next loads re-query the warehouse."
          control={<SecondaryButton disabled={clearing} onClick={async () => {
            setClearing(true);
            try { await fetch("/api/cache/clear", { method: "POST" }); await qc.invalidateQueries(); toast("Query cache cleared — next loads re-query the warehouse"); }
            catch { toast("Could not clear cache"); }
            finally { setClearing(false); }
          }}>{clearing ? "Clearing…" : "Clear cache"}</SecondaryButton>} />
      </Group>
    </div>
  );
}

// ── Refresh schedule (self-contained, saves immediately) ──────────────────────
interface Schedule { enabled: boolean; frequency: "nightly" | "weekly" | "monthly"; hour_utc: number; lookback_days: number; }
const SCHED_DEFAULTS: Schedule = { enabled: true, frequency: "nightly", hour_utc: 5, lookback_days: 180 };

function ScheduleGroup() {
  const toast = useToast();
  const { data } = useQuery<Schedule>({
    queryKey: ["settings-schedule"], queryFn: () => fetch("/api/settings/schedule").then(r => r.ok ? r.json() : SCHED_DEFAULTS).catch(() => SCHED_DEFAULTS), staleTime: 300000,
  });
  const [edited, setEdited] = useState<Schedule | null>(null);
  const s = edited ?? data ?? SCHED_DEFAULTS;
  const save = (next: Schedule) => {
    setEdited(next);
    fetch("/api/settings/schedule", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(next) }).then(() => toast("Refresh schedule saved")).catch(() => toast("Could not save schedule"));
  };
  const hours = Array.from({ length: 24 }, (_, h) => ({ value: h, label: `${String(h).padStart(2, "0")}:00 UTC` }));
  return (
    <Group label="Refresh schedule">
      <Row first label="Scheduled refresh" helper="Rebuilds warehouse gold, then copies into Lakebase. Full rebuilds take 3–8 minutes; incremental refreshes finish in under a minute."
        control={<Toggle checked={s.enabled} onChange={(v) => save({ ...s, enabled: v })} />} />
      {s.enabled && <Row label="Frequency" helper="How often to rebuild the managed tables."
        control={<Select value={s.frequency} onChange={(v) => save({ ...s, frequency: v as Schedule["frequency"] })}
          options={[{ value: "nightly", label: "Nightly" }, { value: "weekly", label: "Weekly (Mondays)" }, { value: "monthly", label: "Monthly (1st)" }]} />} />}
      {s.enabled && <Row label="Run at" helper="Hour the rebuild runs."
        control={<Select value={s.hour_utc} onChange={(v) => save({ ...s, hour_utc: Number(v) })} options={hours} />} />}
      {s.enabled && <Row label="Rebuild window" helper="How far back to pull data on each rebuild."
        control={<Select value={s.lookback_days} onChange={(v) => save({ ...s, lookback_days: Number(v) })}
          options={[{ value: 180, label: "6 months" }, { value: 365, label: "1 year" }, { value: 730, label: "2 years" }, { value: 1095, label: "3 years" }]} />} />}
    </Group>
  );
}

// ── Data & tables ─────────────────────────────────────────────────────────────
export function DataTablesSection({ localSettings, updateSetting, caps }: CommonProps) {
  const { data: appConfig } = useQuery<AppConfigInfo | undefined>({
    queryKey: ["app-config"], queryFn: () => fetch("/api/settings/config").then(r => r.json()).catch(() => undefined),
  });
  const { data: appLinks } = useQuery<{ setup_job_url?: string } | null>({
    queryKey: ["settings-app-links"], queryFn: () => fetch("/api/settings/app-links").then(r => r.json()).catch(() => null), staleTime: 300000,
  });
  const loc = appConfig?.storage_location;
  return (
    <div>
      <SectionTitle title="Data & tables" subtitle="Where the app stores its managed tables and how they refresh." />
      <Group label="Storage location">
        <Row first label="Catalog & schema" helper="Fixed after setup. Redeploy or re-run setup to change."
          control={<MonoChip>{loc?.catalog && loc?.schema ? `${loc.catalog}.${loc.schema}` : "—"}</MonoChip>} />
        <Row label="Workspace filter" helper="The workspace pool is shown on Resources. Change it from setup, not by redeploying."
          control={<span style={{ fontSize: 12, color: T.textSecondary }}>See Resources</span>} />
        {appLinks?.setup_job_url && (
          <Row label="Setup job" helper="One-time onboarding and Lakebase bootstrap. Nightly gold refreshes run in the app, not this job."
            control={<LinkButton href={appLinks.setup_job_url}><span className="inline-flex items-center gap-1">Open setup job {ext}</span></LinkButton>} />
        )}
        <Row label="Workspace display names"
          helper={caps && !caps.workspace_names_available
            ? "Grant missing — showing IDs. Names come from system.access.workspaces_latest once the SP can read it."
            : "Show resolved names from system.access.workspaces_latest instead of IDs."}
          control={<Toggle checked={localSettings.showWorkspaceNames} disabled={caps ? !caps.workspace_names_available : false} onChange={(v) => updateSetting("showWorkspaceNames", v)} />} />
      </Group>
      <ScheduleGroup />
      {/* Shared Delta-Sharing sources + managed-tables status/rebuild/history/danger —
          DuBois-styled, self-contained (owns the table-status polling + rebuild + drop). */}
      <SettingsConfig />
    </div>
  );
}

// ── Access ────────────────────────────────────────────────────────────────────
export function AccessSection() {
  return (
    <div>
      <SectionTitle title="Access" subtitle="System-table readiness, who can administer the app, and how it authenticates." />
      <SettingsPermissions />
    </div>
  );
}

// ── Access grants-pending fallback helper (used inline by shell if needed) ─────
export function GrantsPendingCallout({ sql }: { sql: string }) {
  return (
    <Callout tone="warning">
      <div style={{ fontWeight: 600, marginBottom: 4 }}>System-table grants pending</div>
      <div style={{ marginBottom: 8 }}>Each new app deploy gets a fresh service principal. Until these grants run, affected metrics show <em>unavailable</em> (never $0.00). Run this as a metastore admin, then re-check:</div>
      <pre style={{ backgroundColor: "#11171C", color: "#E8ECF0", borderRadius: 6, padding: 10, fontFamily: MONO, fontSize: 11.5, overflowX: "auto" }}>{sql}</pre>
    </Callout>
  );
}
