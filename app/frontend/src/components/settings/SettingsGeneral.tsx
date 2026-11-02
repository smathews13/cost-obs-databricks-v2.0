import { useState, useEffect, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import type { AppSettings } from "../SettingsDialog";

interface ScheduleSettings {
  enabled: boolean;
  frequency: "nightly" | "weekly" | "monthly";
  hour_utc: number;
  lookback_days: number;
}

export type { ScheduleSettings };

interface SettingsGeneralProps {
  localSettings: AppSettings;
  updateSetting: <K extends keyof AppSettings>(key: K, value: AppSettings[K]) => void;
  saveStatus: string | null;
  setSaveStatus: (status: string | null) => void;
  onScheduleChange: (s: ScheduleSettings) => void;
}

const SCHEDULE_DEFAULTS: ScheduleSettings = { enabled: true, frequency: "nightly", hour_utc: 5, lookback_days: 180 };

export function SettingsGeneral({ localSettings, updateSetting, saveStatus, setSaveStatus, onScheduleChange }: SettingsGeneralProps) {
  const { data: scheduleData } = useQuery<ScheduleSettings>({
    queryKey: ["settings-schedule"],
    queryFn: () => fetch("/api/settings/schedule").then(r => r.ok ? r.json() : SCHEDULE_DEFAULTS).catch(() => SCHEDULE_DEFAULTS),
    staleTime: 5 * 60 * 1000,
  });

  const { data: savedThresholds } = useQuery<{ spike_threshold_percent: number; daily_budget: number; workspace_budget: number } | null>({
    queryKey: ["settings-alert-thresholds"],
    queryFn: () => fetch("/api/settings/alert-thresholds").then(r => r.ok ? r.json() : null).catch(() => null),
    staleTime: 5 * 60 * 1000,
  });

  const { data: webhookStatus } = useQuery<{ configured: boolean } | null>({
    queryKey: ["settings-webhook-status"],
    queryFn: () => fetch("/api/settings/webhook").then(r => r.ok ? r.json() : null).catch(() => null),
    staleTime: 5 * 60 * 1000,
  });

  // Sync backend threshold values into localSettings once on load (if localStorage was cleared).
  const thresholdsSynced = useRef(false);
  useEffect(() => {
    if (!savedThresholds || thresholdsSynced.current) return;
    thresholdsSynced.current = true;
    updateSetting("alertSpikePercent", savedThresholds.spike_threshold_percent);
    updateSetting("alertDailyBudget", savedThresholds.daily_budget);
    updateSetting("alertWorkspaceBudget", savedThresholds.workspace_budget);
  }, [savedThresholds]); // eslint-disable-line react-hooks/exhaustive-deps

  // The user's edit wins; until then show the saved schedule, or defaults while it loads.
  const [editedSchedule, setEditedSchedule] = useState<ScheduleSettings | null>(null);
  const localSchedule = editedSchedule ?? scheduleData ?? SCHEDULE_DEFAULTS;

  const updateSchedule = (next: ScheduleSettings) => {
    setEditedSchedule(next);
    onScheduleChange(next);
  };

  // UI shows PST (UTC-8). hour_utc is what the backend stores and schedules on.
  const PST_OFFSET = 8;
  const utcToPst = (utc: number) => (utc - PST_OFFSET + 24) % 24;
  const pstToUtc = (pst: number) => (pst + PST_OFFSET) % 24;
  const PST_HOUR_OPTIONS = [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23];
  const formatHourPst = (pst: number) => {
    const period = pst < 12 ? "AM" : "PM";
    const display = pst === 0 ? 12 : pst > 12 ? pst - 12 : pst;
    return `${display}:00 ${period} PST`;
  };
  return (
    <div className="space-y-5">
      {saveStatus && (
        <div className="rounded-lg border border-green-200 bg-green-50 px-4 py-2 text-sm text-green-700">
          {saveStatus}
        </div>
      )}

      {/* ── Dashboard Defaults ────────────────── */}
      <div>
        <div className="flex items-center gap-2 mb-3">
          <svg className="h-4 w-4 text-gray-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
          </svg>
          <h4 className="text-sm font-semibold text-gray-900">Dashboard Defaults</h4>
        </div>
        <div className="divide-y divide-gray-100 rounded-lg border border-gray-200">
          {/* Default Date Range */}
          <div className="flex items-center justify-between px-4 py-3">
            <div>
              <div className="text-sm font-medium text-gray-900">Default Date Range</div>
              <div className="text-xs text-gray-500">Time window shown on dashboard load</div>
            </div>
            <select
              value={localSettings.defaultDateRangeDays}
              onChange={(e) => updateSetting("defaultDateRangeDays", Number(e.target.value) as AppSettings["defaultDateRangeDays"])}
              className="rounded-md border border-gray-300 px-3 py-1.5 text-sm focus:border-orange-500 focus:outline-none focus:ring-1 focus:ring-orange-500"
            >
              <option value={7}>Last 7 days</option>
              <option value={14}>Last 14 days</option>
              <option value={30}>Last 30 days</option>
              <option value={60}>Last 60 days</option>
              <option value={90}>Last 90 days</option>
            </select>
          </div>

          {/* Auto-Refresh Interval */}
          <div className="flex items-center justify-between px-4 py-3">
            <div>
              <div className="text-sm font-medium text-gray-900">Auto-Refresh Interval</div>
              <div className="text-xs text-gray-500">Automatically refresh dashboard data</div>
            </div>
            <select
              value={localSettings.refreshIntervalMinutes}
              onChange={(e) => updateSetting("refreshIntervalMinutes", Number(e.target.value) as AppSettings["refreshIntervalMinutes"])}
              className="rounded-md border border-gray-300 px-3 py-1.5 text-sm focus:border-orange-500 focus:outline-none focus:ring-1 focus:ring-orange-500"
            >
              <option value={0}>Off</option>
              <option value={5}>Every 5 minutes</option>
              <option value={15}>Every 15 minutes</option>
              <option value={30}>Every 30 minutes</option>
            </select>
          </div>

          {/* Compact Mode */}
          <div className="flex items-center justify-between px-4 py-3 dark-mode:border-dm-border dark-mode:bg-dm-surface">
            <div>
              <div className="text-sm font-medium text-gray-900">Compact Mode</div>
              <div className="text-xs text-gray-500">Reduce spacing for denser data display</div>
            </div>
            <label className="relative cursor-pointer">
              <input type="checkbox" checked={localSettings.compactMode} onChange={(e) => updateSetting("compactMode", e.target.checked)} className="sr-only" />
              <div className={`h-6 w-11 rounded-full transition-colors ${localSettings.compactMode ? "" : "bg-gray-300"}`} style={localSettings.compactMode ? { backgroundColor: '#FF3621' } : {}} />
              <div className={`absolute left-0.5 top-0.5 h-5 w-5 rounded-full bg-white shadow transition-transform ${localSettings.compactMode ? "translate-x-5" : "translate-x-0"}`} />
            </label>
          </div>

          {/* Dark Mode */}
          <div className="flex items-center justify-between px-4 py-3">
            <div>
              <div className="text-sm font-medium text-gray-900">Dark Mode</div>
              <div className="text-xs text-gray-500">Switch to a dark color scheme</div>
            </div>
            <label className="relative cursor-pointer">
              <input type="checkbox" checked={localSettings.darkMode} onChange={(e) => updateSetting("darkMode", e.target.checked)} className="sr-only" />
              <div className={`h-6 w-11 rounded-full transition-colors ${localSettings.darkMode ? "" : "bg-gray-300"}`} style={localSettings.darkMode ? { backgroundColor: '#FF3621' } : {}} />
              <div className={`absolute left-0.5 top-0.5 h-5 w-5 rounded-full bg-white shadow transition-transform ${localSettings.darkMode ? "translate-x-5" : "translate-x-0"}`} />
            </label>
          </div>
        </div>
      </div>

      {/* ── Rebuild Schedule ────────────────── */}
      <div>
        <div className="flex items-center gap-2 mb-3">
          <svg className="h-4 w-4 text-gray-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
          </svg>
          <h4 className="text-sm font-semibold text-gray-900">Rebuild Schedule</h4>
        </div>
        <p className="mb-2 text-xs text-gray-500">
          Rebuild history is tracked in the <span className="font-medium text-gray-600">Storage Location &amp; Tables</span> section of the <span className="font-medium text-gray-600">Config</span> tab.
        </p>
        <div className="divide-y divide-gray-100 rounded-lg border border-gray-200">
          {/* Enable toggle */}
          <div className="flex items-center justify-between px-4 py-3">
            <div>
              <div className="text-sm font-medium text-gray-900">Auto-Rebuild Tables</div>
              <div className="text-xs text-gray-500">Automatically rebuild materialized views on a schedule</div>
            </div>
            <label className="relative cursor-pointer">
              <input
                type="checkbox"
                checked={localSchedule.enabled}
                onChange={e => updateSchedule({ ...localSchedule, enabled: e.target.checked })}
                className="sr-only"
              />
              <div className={`h-6 w-11 rounded-full transition-colors ${localSchedule.enabled ? "" : "bg-gray-300"}`} style={localSchedule.enabled ? { backgroundColor: '#FF3621' } : {}} />
              <div className={`absolute left-0.5 top-0.5 h-5 w-5 rounded-full bg-white shadow transition-transform ${localSchedule.enabled ? "translate-x-5" : "translate-x-0"}`} />
            </label>
          </div>
          {/* Frequency */}
          <div className="flex items-center justify-between px-4 py-3">
            <div>
              <div className="text-sm font-medium text-gray-900">Frequency</div>
              <div className="text-xs text-gray-500">How often to rebuild tables</div>
            </div>
            <select
              value={localSchedule.frequency}
              onChange={e => updateSchedule({ ...localSchedule, frequency: e.target.value as ScheduleSettings["frequency"] })}
              disabled={!localSchedule.enabled}
              className="rounded-md border border-gray-300 px-3 py-1.5 text-sm focus:border-orange-500 focus:outline-none focus:ring-1 focus:ring-orange-500 disabled:opacity-50"
            >
              <option value="nightly">Nightly</option>
              <option value="weekly">Weekly (Mondays)</option>
              <option value="monthly">Monthly (1st of month)</option>
            </select>
          </div>
          {/* Hour */}
          <div className="flex items-center justify-between px-4 py-3">
            <div>
              <div className="text-sm font-medium text-gray-900">Time</div>
              <div className="text-xs text-gray-500">Hour to run the rebuild (PST)</div>
            </div>
            <select
              value={utcToPst(localSchedule.hour_utc)}
              onChange={e => updateSchedule({ ...localSchedule, hour_utc: pstToUtc(Number(e.target.value)) })}
              disabled={!localSchedule.enabled}
              className="rounded-md border border-gray-300 px-3 py-1.5 text-sm focus:border-orange-500 focus:outline-none focus:ring-1 focus:ring-orange-500 disabled:opacity-50"
            >
              {PST_HOUR_OPTIONS.map(h => (
                <option key={h} value={h}>{formatHourPst(h)}</option>
              ))}
            </select>
          </div>
          {/* Rebuild window */}
          <div className="flex items-center justify-between px-4 py-3">
            <div>
              <div className="text-sm font-medium text-gray-900">Rebuild Window</div>
              <div className="text-xs text-gray-500">How far back to pull data on each rebuild</div>
            </div>
            <select
              value={localSchedule.lookback_days}
              onChange={e => updateSchedule({ ...localSchedule, lookback_days: Number(e.target.value) })}
              disabled={!localSchedule.enabled}
              className="rounded-md border border-gray-300 px-3 py-1.5 text-sm focus:border-orange-500 focus:outline-none focus:ring-1 focus:ring-orange-500 disabled:opacity-50"
            >
              <option value={180}>6 months (default)</option>
              <option value={365}>1 year</option>
              <option value={730}>2 years</option>
              <option value={1095}>3 years</option>
            </select>
          </div>

        </div>
      </div>

      {/* ── Export & Branding ────────────────── */}
      <div>
        <div className="flex items-center gap-2 mb-3">
          <svg className="h-4 w-4 text-gray-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
          </svg>
          <h4 className="text-sm font-semibold text-gray-900">Export & Branding</h4>
        </div>
        <div className="rounded-lg border border-gray-200 px-4 py-3">
          <div className="mb-2">
            <div className="text-sm font-medium text-gray-900">Company Name</div>
            <div className="text-xs text-gray-500">Appears in PDF report headers</div>
          </div>
          <input
            type="text"
            value={localSettings.companyName}
            onChange={(e) => updateSetting("companyName", e.target.value)}
            placeholder="e.g., Acme Corp"
            className="w-full rounded-md border border-gray-300 px-3 py-1.5 text-sm focus:border-orange-500 focus:outline-none focus:ring-1 focus:ring-orange-500"
          />
        </div>
      </div>

      {/* ── Alert Thresholds ────────────────── */}
      <div>
        <div className="flex items-center gap-2 mb-3">
          <svg className="h-4 w-4 text-gray-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
          </svg>
          <h4 className="text-sm font-semibold text-gray-900">Alert Thresholds</h4>
        </div>
        <div className="grid grid-cols-3 gap-4 rounded-lg border border-gray-200 px-4 py-3">
          <div>
            <div className="text-xs font-medium text-gray-700 mb-1.5">Spike Threshold</div>
            <div className="flex items-center gap-1">
              <input
                type="number"
                min={5}
                max={100}
                value={localSettings.alertSpikePercent}
                onChange={(e) => updateSetting("alertSpikePercent", Number(e.target.value) || 20)}
                className="w-full rounded-md border border-gray-300 px-3 py-1.5 text-sm text-right focus:border-orange-500 focus:outline-none focus:ring-1 focus:ring-orange-500"
              />
              <span className="text-sm text-gray-500">%</span>
            </div>
            <div className="text-xs text-gray-500 mt-1">Day-over-day change</div>
          </div>
          <div>
            <div className="text-xs font-medium text-gray-700 mb-1.5">Daily Budget</div>
            <div className="flex items-center gap-1">
              <span className="text-sm text-gray-500">$</span>
              <input
                type="number"
                min={0}
                step={1000}
                value={localSettings.alertDailyBudget}
                onChange={(e) => updateSetting("alertDailyBudget", Number(e.target.value) || 0)}
                className="w-full rounded-md border border-gray-300 px-3 py-1.5 text-sm text-right focus:border-orange-500 focus:outline-none focus:ring-1 focus:ring-orange-500"
              />
            </div>
            <div className="text-xs text-gray-500 mt-1">Alert above this amount</div>
          </div>
          <div>
            <div className="text-xs font-medium text-gray-700 mb-1.5">Workspace Budget</div>
            <div className="flex items-center gap-1">
              <span className="text-sm text-gray-500">$</span>
              <input
                type="number"
                min={0}
                step={1000}
                value={localSettings.alertWorkspaceBudget}
                onChange={(e) => updateSetting("alertWorkspaceBudget", Number(e.target.value) || 0)}
                className="w-full rounded-md border border-gray-300 px-3 py-1.5 text-sm text-right focus:border-orange-500 focus:outline-none focus:ring-1 focus:ring-orange-500"
              />
            </div>
            <div className="text-xs text-gray-500 mt-1">Per-workspace limit</div>
          </div>
        </div>
      </div>

      {/* ── Notifications ────────────────── */}
      <div>
        <div className="flex items-center gap-2 mb-3">
          <svg className="h-4 w-4 text-gray-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9" />
          </svg>
          <h4 className="text-sm font-semibold text-gray-900">Notifications</h4>
        </div>
        <div className="rounded-lg border border-gray-200 px-4 py-3">
          <div className="mb-2 flex items-center justify-between">
            <div>
              <div className="text-sm font-medium text-gray-900">Slack Webhook URL</div>
              <div className="text-xs text-gray-500">Receive alert notifications in Slack</div>
            </div>
            {webhookStatus?.configured && !localSettings.slackWebhookUrl && (
              <span className="inline-flex items-center gap-1 rounded-full bg-green-50 border border-green-200 px-2 py-0.5 text-[11px] font-medium text-green-700">
                <span className="h-1.5 w-1.5 rounded-full bg-green-500" />Configured on server
              </span>
            )}
          </div>
          <div className="flex gap-2">
            <input
              type="url"
              value={localSettings.slackWebhookUrl}
              onChange={(e) => updateSetting("slackWebhookUrl", e.target.value)}
              placeholder={webhookStatus?.configured && !localSettings.slackWebhookUrl ? "Leave blank to keep existing webhook" : "https://hooks.slack.com/services/..."}
              className="flex-1 rounded-md border border-gray-300 px-3 py-1.5 text-sm focus:border-orange-500 focus:outline-none focus:ring-1 focus:ring-orange-500"
            />
            <button
              onClick={async () => {
                if (localSettings.slackWebhookUrl) {
                  await fetch("/api/settings/webhook", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ slack_webhook_url: localSettings.slackWebhookUrl }),
                  });
                }
                const res = await fetch("/api/settings/webhook/test", { method: "POST" });
                const data = await res.json();
                setSaveStatus(data.success ? "Test message sent to Slack!" : `Webhook test failed: ${data.error}`);
                setTimeout(() => setSaveStatus(null), 4000);
              }}
              disabled={!localSettings.slackWebhookUrl && !webhookStatus?.configured}
              className="rounded-md border border-gray-300 px-3 py-1.5 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              Test
            </button>
          </div>
        </div>
      </div>

    </div>
  );
}
