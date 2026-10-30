import { useState, useEffect, useCallback, useRef, Fragment } from "react";
import { createPortal } from "react-dom";
import { useQueryClient } from "@tanstack/react-query";
import { Spinner } from "./Spinner";
import { ReadinessChecks, normalizeReadinessResult } from "./settings/ReadinessChecks";
import { VirtualizedList } from "./VirtualizedList";
import type { ReadinessResult } from "./settings/ReadinessChecks";
import { isAccessError, isGrantableCatalogError, setupBuildOutcome } from "../utils/setupBuild";
import type { SetupBuildTask, SetupUserGrants } from "../utils/setupBuild";

interface SetupWizardProps {
  onComplete: () => void;
  onClose?: () => void;
  embedded?: boolean;
}

interface ConfigData {
  warehouse: { id: string; name: string | null; size: string | null; state: string } | null;
  identity: { display_name: string; user_name: string } | null;
  storage_location: { catalog: string; schema: string } | null;
}

interface CloudData {
  provider: "aws" | "azure" | "gcp";
  host: string;
}


interface SetupStatus {
  catalog: string;
  schema: string;
  tables: Record<string, boolean>;
  all_tables_exist: boolean;
  missing_tables: string[];
  status: "ready" | "setup_required";
  task?: SetupBuildTask;
  next_poll_ms?: number;
}

type WizardStep = "welcome" | "storage-location" | "permissions" | "create-tables" | "workspace-filter" | "complete";

const STEPS: WizardStep[] = ["welcome", "storage-location", "permissions", "create-tables", "workspace-filter", "complete"];

const STEP_LABELS: Record<WizardStep, string> = {
  welcome: "Environment",
  "storage-location": "Storage",
  permissions: "Permissions",
  "create-tables": "Create Tables",
  "workspace-filter": "Workspaces",
  complete: "Complete",
};

const CLOUD_LABELS: Record<string, string> = {
  aws: "Amazon Web Services",
  azure: "Microsoft Azure",
  gcp: "Google Cloud Platform",
};

function PermissionErrorBlock({ error, onGranted }: { error: string; onGranted: () => void }) {
  const [copied, setCopied] = useState(false);
  const [grantStatus, setGrantStatus] = useState<"idle" | "running" | "ok" | "fail">("idle");
  const [grantMessage, setGrantMessage] = useState("");

  // Extract the GRANT SQL from the error message — use [\s\S] so periods in
  // email addresses (e.g. sam.mathews@databricks.com) don't break the match
  const grantMatch = error.match(/(GRANT [\s\S]+)$/);
  const rawGrants = grantMatch ? grantMatch[1].trim() : null;
  // Normalise to one statement per line
  const grantSql = rawGrants ? rawGrants.replace(/;\s*/g, ";\n").trim() : null;

  const msgPart = grantMatch ? error.slice(0, error.indexOf(grantMatch[1])).trim() : error;

  const handleCopy = () => {
    if (grantSql) {
      navigator.clipboard.writeText(grantSql).then(() => {
        setCopied(true);
        setTimeout(() => setCopied(false), 2000);
      });
    }
  };

  const handleGrant = async () => {
    setGrantStatus("running");
    setGrantMessage("");
    try {
      const res = await fetch("/api/setup/grant-catalog-access", { method: "POST" });
      const data = await res.json();
      if (data.ok) {
        setGrantStatus("ok");
        setGrantMessage(data.message);
        setTimeout(onGranted, 1500);
      } else {
        setGrantStatus("fail");
        setGrantMessage(data.message);
      }
    } catch {
      setGrantStatus("fail");
      setGrantMessage("Request failed — check server logs");
    }
  };

  return (
    <div className="mb-4 rounded-lg border border-red-200 bg-red-50 p-4 space-y-3">
      <p className="text-sm text-red-700">{msgPart}</p>

      {grantSql && (
        <div className="rounded border border-gray-700 bg-gray-900 overflow-hidden">
          <div className="flex items-center justify-between px-3 py-1.5 border-b border-gray-700">
            <span className="text-[10px] font-medium text-gray-500 uppercase tracking-wide">SQL — run as metastore admin</span>
            <button
              onClick={handleCopy}
              className="text-[11px] font-medium text-gray-500 hover:text-white transition-colors"
            >
              {copied ? "Copied!" : "Copy"}
            </button>
          </div>
          <pre className="px-3 py-2.5 font-mono text-xs text-green-400 whitespace-pre overflow-x-auto">
            {grantSql}
          </pre>
        </div>
      )}

      <div className="flex items-center gap-3 flex-wrap">
        <button
          onClick={handleGrant}
          disabled={grantStatus === "running" || grantStatus === "ok"}
          className="inline-flex items-center gap-1.5 rounded px-3 py-1.5 text-xs font-medium text-white disabled:opacity-60 transition-colors"
          style={{ backgroundColor: grantStatus === "ok" ? "#16a34a" : "#FF3621" }}
        >
          {grantStatus === "running" && <Spinner size="xs" />}
          {grantStatus === "ok" ? "Granted!" : grantStatus === "running" ? "Granting…" : "Try to grant access"}
        </button>
        <p className="text-[11px] text-red-600">
          {grantStatus === "ok" || grantStatus === "running"
            ? grantMessage
            : grantStatus === "fail"
            ? <span className="text-red-700">{grantMessage} — copy the SQL above and ask a metastore admin to run it.</span>
            : "Requires metastore admin rights. If this fails, copy the SQL above and ask your admin."}
        </p>
      </div>
    </div>
  );
}

const SCOPE_INDEPENDENT_QUERIES = new Set([
  "user", "user-permissions", "unified-settings", "app-config", "warehouses",
  "cloud-provider", "cloud-connections", "health", "debug-run", "mv-sources",
]);

function isScopeIndependentQuery(head: string): boolean {
  return SCOPE_INDEPENDENT_QUERIES.has(head) || head.startsWith("settings-") || head.startsWith("setup-");
}

export function SetupWizard({ onComplete, onClose, embedded }: SetupWizardProps) {
  const queryClient = useQueryClient();
  const [step, setStep] = useState<WizardStep>("welcome");
  const [config, setConfig] = useState<ConfigData | null>(null);
  const [cloud, setCloud] = useState<CloudData | null>(null);
  const [readiness, setReadiness] = useState<ReadinessResult | null>(null);
  const [readinessError, setReadinessError] = useState<string | null>(null);
  const [setupStatus, setSetupStatus] = useState<SetupStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [tablesJustCreated, setTablesJustCreated] = useState(false);
  // A table failed but the server is still finishing the ones already running;
  // Retry stays disabled until it lets go, because it would refuse a second build.
  const [buildDraining, setBuildDraining] = useState(false);
  const failedPollsRef = useRef(0);
  // Poll-loop handles, held in refs so Skip (and unmount) can cancel the loop
  // that handleCreateTables started — the loop is otherwise a closure with no
  // external cancellation handle, which is why a frozen build had no way out.
  const pollCancelledRef = useRef(false);
  const pollTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const safetyTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [storagePhase, setStoragePhase] = useState<'idle' | 'saving' | 'creating-catalog' | 'creating-schema' | 'done' | 'error'>('idle');
  const [storageChecks, setStorageChecks] = useState<{ config: boolean | null; catalog: boolean | null; schema: boolean | null }>({ config: null, catalog: null, schema: null });
  const [error, setError] = useState<string | null>(null);
  const [preflightResult, setPreflightResult] = useState<{ ok: boolean; status: string; message: string } | null>(null);
  const [preflightLoading, setPreflightLoading] = useState(false);
  const preflightFiredRef = useRef(false);

  // Storage location step state
  const [catalogInput, setCatalogInput] = useState("");
  const [schemaInput, setSchemaInput] = useState("");
  const [storageEnvVarLocked, setStorageEnvVarLocked] = useState(false);

  // Workspace filter step state
  const [wsLoading, setWsLoading] = useState(false);
  const [allWorkspaces, setAllWorkspaces] = useState<{ id: string; name: string; historical?: boolean }[]>([]);
  const [selectedWsIds, setSelectedWsIds] = useState<Set<string>>(new Set());
  const [wsSaved, setWsSaved] = useState(false);
  const [wsSaving, setWsSaving] = useState(false);
  // The pool saved by a previous run of this step. [] = all workspaces.
  const [savedWsIds, setSavedWsIds] = useState<string[]>([]);
  // Set when the saved pool or the workspace list couldn't be loaded. Saving is
  // blocked then: the selection would be empty or "all", silently widening the pool.
  const [wsPoolError, setWsPoolError] = useState<string | null>(null);

  // Load initial data
  useEffect(() => {
    async function loadData() {
      setLoading(true);
      setError(null);
      try {
        const [configRes, cloudRes] = await Promise.all([
          fetch("/api/settings/config"),
          fetch("/api/settings/cloud-provider"),
        ]);
        if (configRes.ok) {
          const cfg = await configRes.json();
          setConfig(cfg);
          const rawCat = cfg?.storage_location?.catalog || "";
          const rawSch = cfg?.storage_location?.schema || "";
          const isEnvVar = cfg?.storage_location?.catalog_source === "env_var";
          setStorageEnvVarLocked(isEnvVar);
          // Never pre-fill with the forbidden defaults — force the user to choose explicitly
          setCatalogInput(rawCat.toLowerCase() === "main" ? "" : rawCat);
          setSchemaInput(rawSch.toLowerCase() === "cost_obs" && rawCat.toLowerCase() === "main" ? "" : rawSch);
        }
        if (cloudRes.ok) setCloud(await cloudRes.json());
      } catch (e) {
        setError(`Failed to load environment info: ${e}`);
      } finally {
        setLoading(false);
      }
    }
    loadData();
  }, []);

  useEffect(() => {
    setStoragePhase('idle');
    setStorageChecks({ config: null, catalog: null, schema: null });
  }, [step]);



  const loadReadiness = useCallback(async (forceRefresh = false) => {
    setLoading(true);
    setReadinessError(null);
    try {
      const url = forceRefresh ? "/api/setup/readiness?refresh=true" : "/api/setup/readiness";
      const res = await fetch(url);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setReadiness(normalizeReadinessResult(await res.json()));
    } catch (e) {
      setReadinessError(`Failed to check system readiness: ${e}`);
    } finally {
      setLoading(false);
    }
  }, []);

  const loadPreflight = useCallback(async () => {
    setPreflightLoading(true);
    setPreflightResult(null);
    try {
      const res = await fetch("/api/setup/preflight-catalog");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setPreflightResult(await res.json());
    } catch (e) {
      setPreflightResult({ ok: false, status: "catalog_check_failed", message: `Preflight check failed: ${e}` });
    } finally {
      setPreflightLoading(false);
    }
  }, []);

  // Auto-fire preflight when arriving at create-tables with no result — covers
  // pod-restart recovery where the wizard remounts mid-flow and goNext is never called.
  // Ref guard ensures we fire at most once per mount even if loadPreflight is redefined.
  useEffect(() => {
    if (step !== "create-tables") { preflightFiredRef.current = false; return; }
    if (preflightResult || preflightLoading || preflightFiredRef.current) return;
    preflightFiredRef.current = true;
    loadPreflight();
  }, [step, preflightResult, preflightLoading, loadPreflight]);


  const pollSetupStatus = useCallback(async () => {
    try {
      const res = await fetch("/api/setup/status");
      if (res.ok) {
        const data: SetupStatus = await res.json();
        setSetupStatus(data);
        return data;
      }
    } catch {
      // ignore polling errors
    }
    return null;
  }, []);

  const clearPollTimers = useCallback(() => {
    if (pollTimeoutRef.current) { clearTimeout(pollTimeoutRef.current); pollTimeoutRef.current = null; }
    if (safetyTimeoutRef.current) { clearTimeout(safetyTimeoutRef.current); safetyTimeoutRef.current = null; }
  }, []);

  // Stop the build poll and move on. The background build keeps running on the
  // server (tables still get built; the dashboard falls back to direct system
  // queries meanwhile) — Skip only releases the wizard so a stuck/slow build
  // can never trap the user on this step. Plain function (not memoized) so it
  // always calls the current-render goNext with the live `step`.
  const handleSkipTables = () => {
    pollCancelledRef.current = true;
    clearPollTimers();
    setCreating(false);
    setBuildDraining(false);
    goNext();
  };

  // Cancel any in-flight poll loop when the wizard unmounts.
  useEffect(() => () => { pollCancelledRef.current = true; clearPollTimers(); }, [clearPollTimers]);

  const handleCreateTables = async () => {
    setCreating(true);
    setError(null);
    setBuildDraining(false);
    failedPollsRef.current = 0;
    pollCancelledRef.current = false;
    clearPollTimers();
    try {
      const res = await fetch("/api/setup/create-tables?run_in_background=true", { method: "POST", signal: AbortSignal.timeout(30000) });
      if (!res.ok) {
        const body = await res.text();
        throw new Error(`HTTP ${res.status}: ${body}`);
      }

      // Poll for completion. task.status is the authority, but we ALSO treat
      // "every table reported done" as completion: if the terminal status write
      // is ever missed, an all-done progress map still releases the step instead
      // of hanging until the safety timeout.
      // The status endpoint returns all_tables_exist=false until setup_done.json
      // is written (post-wizard), so it is NOT a reliable success signal here.
      // Use next_poll_ms hint from server (5s during active build, 30s idle).
      const schedulePoll = async () => {
        if (pollCancelledRef.current) return;
        const status = await pollSetupStatus();
        if (pollCancelledRef.current) return;
        failedPollsRef.current = status ? 0 : failedPollsRef.current + 1;
        const outcome = setupBuildOutcome(status?.task, failedPollsRef.current);
        if (outcome.kind === "done") {
          clearPollTimers();
          setCreating(false);
          setBuildDraining(false);
          setTablesJustCreated(true);
        } else if (outcome.kind === "failed" || outcome.kind === "lost") {
          if (safetyTimeoutRef.current) { clearTimeout(safetyTimeoutRef.current); safetyTimeoutRef.current = null; }
          setCreating(false);
          setError(outcome.message);
          if (outcome.kind === "failed" && outcome.serverBusy) {
            // Keep polling quietly so Retry unlocks once the server finishes.
            setBuildDraining(true);
            pollTimeoutRef.current = setTimeout(schedulePoll, status?.next_poll_ms ?? 5000);
          } else {
            clearPollTimers();
            setBuildDraining(false);
          }
        } else if (outcome.kind === "interrupted") {
          clearPollTimers();
          setCreating(false);
          setBuildDraining(false);
        } else {
          const delay = status?.next_poll_ms ?? 5000;
          pollTimeoutRef.current = setTimeout(schedulePoll, delay);
        }
      };
      pollTimeoutRef.current = setTimeout(schedulePoll, 2000);

      // Safety timeout after 10 minutes — cancelled on normal completion so it
      // doesn't fire on the Complete step after a successful build.
      safetyTimeoutRef.current = setTimeout(() => {
        pollCancelledRef.current = true;
        clearPollTimers();
        setCreating(false);
        setError("Table creation is taking longer than expected — you can Skip to continue; tables keep building in the background. Check /api/setup/status for progress.");
      }, 600000);
    } catch (e) {
      setCreating(false);
      setError(`Failed to create tables: ${e}`);
    }
  };

  const loadWorkspaces = useCallback(async () => {
    setWsLoading(true);
    setWsSaved(false);
    setWsPoolError(null);
    try {
      const [filterRes, listRes] = await Promise.all([
        fetch("/api/setup/workspace-filter").catch(() => null),
        fetch("/api/setup/list-workspaces").catch(() => null),
      ]);
      if (!filterRes?.ok) {
        const body = await filterRes?.json().catch(() => ({}));
        setWsPoolError(body?.detail || "Could not read the saved workspace scope.");
        return;
      }
      const saved: string[] = (await filterRes.json()).workspace_ids ?? [];
      setSavedWsIds(saved);
      // Without the list nothing is selectable, and saving an empty selection means
      // "all workspaces", so this blocks saving the same way an unreadable pool does.
      if (!listRes?.ok) {
        const body = await listRes?.json().catch(() => ({}));
        setWsPoolError(body?.detail || "Could not load the workspace list.");
        return;
      }
      {
        const data = await listRes.json();
        const list: { id: string; name: string; historical?: boolean }[] = data.workspaces ?? [];
        // Keep saved ids the list didn't return selectable, so saving again can't drop them silently.
        const missing = saved.filter((id) => !list.some((w) => w.id === id));
        setAllWorkspaces([...list, ...missing.map((id) => ({ id, name: id, historical: true }))]);
        // Start from the saved pool; with none saved, everything is selected.
        setSelectedWsIds(new Set(saved.length > 0 ? saved : list.map((w) => w.id)));
      }
    } catch (e) {
      setWsPoolError(`Could not load the workspaces: ${e}`);
    } finally {
      setWsLoading(false);
    }
  }, []);

  // Returns false (and shows the error) when the save failed, so the wizard stays on
  // this step instead of moving on as if the filter had been saved.
  const saveWorkspaceFilter = async (): Promise<boolean> => {
    if (wsPoolError) return false;
    // Every workspace selected means "no filter". Sending the explicit list would lock
    // the pool to today's workspaces and hide any created later.
    const ids = selectedWsIds.size === allWorkspaces.length ? [] : Array.from(selectedWsIds);
    setError(null);
    setWsSaving(true);
    try {
      const res = await fetch("/api/setup/save-workspace-filter", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workspace_ids: ids }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        setError(body.detail || `Could not save the workspace filter (HTTP ${res.status})`);
        return false;
      }
      setWsSaved(true);
      setSavedWsIds(ids);
      // Every cached tab payload belongs to the previous scope. Reset rather than
      // invalidate so tabs show their loading state, not the old numbers, while
      // they refetch. Identity and settings queries don't depend on the scope;
      // resetting them would blank the dialog this wizard runs in.
      await queryClient.resetQueries({
        predicate: (q) => !isScopeIndependentQuery(String(q.queryKey[0] ?? "")),
      });
      queryClient.invalidateQueries({ queryKey: ["setup-workspace-filter"] });
      return true;
    } catch (e) {
      setError(`Could not save the workspace filter: ${e}`);
      return false;
    } finally {
      setWsSaving(false);
    }
  };

  const goNext = () => {
    const idx = STEPS.indexOf(step);
    if (idx < STEPS.length - 1) {
      const next = STEPS[idx + 1];
      setStep(next);
      if (next === "permissions") loadReadiness();
      if (next === "create-tables") { pollSetupStatus(); loadPreflight(); }
      if (next === "workspace-filter") loadWorkspaces();
    }
  };

  const goBack = () => {
    const idx = STEPS.indexOf(step);
    if (idx > 0) setStep(STEPS[idx - 1]);
  };

  const currentIdx = STEPS.indexOf(step);

  const wizardInner = (
    <>
        {/* Header — hidden in embedded mode (already inside App Settings dialog) */}
        {!embedded && (
        <div className="relative rounded-t-xl px-8 py-6" style={{ backgroundColor: '#1B3139' }}>
          {onClose && (
            <button
              onClick={onClose}
              className="absolute right-4 top-4 rounded-full p-1 text-white/60 hover:bg-white/10 hover:text-white transition-colors"
            >
              <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          )}
          <h2 className="text-xl font-bold text-white">cost-obs setup</h2>
          <p className="mt-1 text-sm text-white/70">Configure your environment to get started</p>
        </div>
        )}

        {/* Step indicator */}
        <div className="shrink-0 flex items-start border-b px-6 py-3" style={{ borderColor: '#E5E5E5' }}>
          {STEPS.map((s, i) => (
            <Fragment key={s}>
              {i > 0 && <div className="mt-3 h-px flex-1 self-start bg-gray-200" />}
              <div className="flex flex-col items-center">
                <div className={`flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-[10px] font-bold ${
                  i < currentIdx ? "bg-green-500 text-white" :
                  i === currentIdx ? "text-white" : "bg-gray-200 text-gray-500"
                }`} style={i === currentIdx ? { backgroundColor: '#FF3621' } : undefined}>
                  {i < currentIdx ? (
                    <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" /></svg>
                  ) : i + 1}
                </div>
                <span className={`mt-1 w-14 text-center text-[10px] font-medium leading-tight ${i === currentIdx ? "text-gray-900" : "text-gray-500"}`}>
                  {STEP_LABELS[s]}
                </span>
              </div>
            </Fragment>
          ))}
        </div>

        {/* Body */}
        <div className="flex-1 min-h-0 overflow-y-auto px-8 py-6">
          {error && (() => {
            if (isGrantableCatalogError(error)) {
              return <PermissionErrorBlock error={error} onGranted={() => { setError(null); }} />;
            }
            const accessProblem = step === "create-tables" && isAccessError(error);
            return (
              <div className="mb-4 rounded-lg bg-red-50 p-3 text-sm text-red-700">
                <p className="break-words">{error}</p>
                {accessProblem && (
                  <div className="mt-2 flex flex-wrap items-center gap-2">
                    <span>This looks like missing access. Check what the app can reach, grant it, then retry.</span>
                    <button
                      onClick={() => { setError(null); setStep("permissions"); loadReadiness(); }}
                      className="rounded-md border border-red-300 bg-white px-3 py-1 text-xs font-medium text-red-700 hover:bg-red-100"
                    >
                      Review permissions
                    </button>
                  </div>
                )}
                {step === "create-tables" && buildDraining && (
                  <p className="mt-2 text-xs text-red-600">Finishing the tables already in progress. Retry unlocks when they are done.</p>
                )}
              </div>
            );
          })()}

          {step === "welcome" && (
            <WelcomeStep config={config} cloud={cloud} loading={loading} />
          )}

          {step === "storage-location" && (
            <StorageLocationStep
              catalog={catalogInput}
              schema={schemaInput}
              onCatalogChange={setCatalogInput}
              onSchemaChange={setSchemaInput}
              phase={storagePhase}
              checks={storageChecks}
              envVarLocked={storageEnvVarLocked}
            />
          )}

          {step === "permissions" && (
            <WizardPermissionsStep
              readiness={readiness}
              loading={loading}
              fetchError={readinessError}
              onRecheck={loadReadiness}
            />
          )}

          {step === "create-tables" && (
            <CreateTablesStep
              setupStatus={setupStatus}
              creating={creating}
              tablesJustCreated={tablesJustCreated}
              preflightResult={preflightResult}
              preflightLoading={preflightLoading}
              onRecheck={loadPreflight}
            />
          )}

          {step === "workspace-filter" && (
            <WorkspaceFilterStep
              loading={wsLoading}
              workspaces={allWorkspaces}
              selectedIds={selectedWsIds}
              onToggle={(id) => setSelectedWsIds((prev) => {
                const next = new Set(prev);
                next.has(id) ? next.delete(id) : next.add(id);
                return next;
              })}
              onSelectAll={() => setSelectedWsIds(new Set(allWorkspaces.map((w) => w.id)))}
              onClearAll={() => setSelectedWsIds(new Set())}
              saved={wsSaved}
              savedIds={savedWsIds}
              poolError={wsPoolError}
              onRetry={loadWorkspaces}
            />
          )}

          {step === "complete" && <CompleteStep />}
        </div>

        {/* Footer */}
        <div className="shrink-0 flex items-center justify-between rounded-b-xl border-t px-8 py-4" style={{ borderColor: '#E5E5E5' }}>
          <div>
            {currentIdx > 0 && (
              <button
                onClick={goBack}
                className="rounded-lg px-4 py-2 text-sm font-medium text-gray-600 hover:bg-gray-100"
              >
                Back
              </button>
            )}
          </div>
          <div>
            {step === "complete" ? (
              <button
                onClick={onComplete}
                className="btn-brand rounded-lg px-6 py-2 text-sm font-bold text-white transition-colors"
              >
                Go to Dashboard
              </button>
            ) : step === "workspace-filter" ? (
              <div className="flex items-center gap-2">
                <button
                  onClick={goNext}
                  className="rounded-lg border border-gray-300 px-4 py-2 text-sm font-medium text-gray-600 hover:bg-gray-50"
                >
                  Skip
                </button>
                <button
                  onClick={async () => { if (await saveWorkspaceFilter()) goNext(); }}
                  disabled={wsLoading || wsSaving || !!wsPoolError}
                  className="btn-brand rounded-lg px-6 py-2 text-sm font-bold text-white transition-colors disabled:opacity-50"
                >
                  {selectedWsIds.size === 0 || selectedWsIds.size === allWorkspaces.length ? "Continue (all workspaces)" : `Save & Continue (${selectedWsIds.size} workspace${selectedWsIds.size !== 1 ? "s" : ""})`}
                </button>
              </div>
            ) : step === "create-tables" ? (
              creating ? (
                <button
                  onClick={handleSkipTables}
                  className="rounded-lg border border-gray-300 px-4 py-2 text-sm font-medium text-gray-600 hover:bg-gray-50"
                  title="Continue setup — tables keep building in the background"
                >
                  Skip
                </button>
              )
              : (tablesJustCreated || setupStatus?.all_tables_exist) ? (
                <button
                  onClick={goNext}
                  className="btn-brand rounded-lg px-6 py-2 text-sm font-bold text-white transition-colors"
                >
                  Next
                </button>
              ) : (
                <button
                  onClick={handleCreateTables}
                  disabled={preflightLoading || !preflightResult?.ok || buildDraining}
                  className="btn-brand rounded-lg px-6 py-2 text-sm font-bold text-white transition-colors disabled:opacity-50"
                >
                  {buildDraining ? "Finishing…" : error ? "Retry" : "Create Tables"}
                </button>
              )
            ) : step === "storage-location" ? (
              storagePhase === "done" ? (
                <button
                  onClick={goNext}
                  className="btn-brand rounded-lg px-6 py-2 text-sm font-bold text-white transition-colors"
                >
                  Next
                </button>
              ) : (
                <button
                  onClick={async () => {
                    const cat = catalogInput.trim();
                    const sch = schemaInput.trim();
                    if (!cat || !sch) return;
                    setError(null);

                    // Step 1: Save config — skip when catalog/schema come from env vars
                    // (env vars always win in get_catalog_schema(); saving to file is a no-op).
                    setStorageChecks({ config: null, catalog: null, schema: null });
                    if (storageEnvVarLocked) {
                      setStoragePhase("creating-catalog");
                      setStorageChecks(c => ({ ...c, config: true }));
                    } else {
                      setStoragePhase("saving");
                      let saved = false;
                      for (let attempt = 1; attempt <= 3 && !saved; attempt++) {
                        try {
                          const res = await fetch("/api/settings/catalog", {
                            method: "POST",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({ catalog: cat, schema: sch }),
                            signal: AbortSignal.timeout(30000),
                          });
                          if (!res.ok) {
                            const body = await res.json().catch(() => ({}));
                            setError(body.detail || `Failed to save (HTTP ${res.status})`);
                            setStorageChecks(c => ({ ...c, config: false }));
                            setStoragePhase("error");
                            return;
                          }
                          saved = true;
                        } catch (e: unknown) {
                          if (e instanceof Error && e.name === "TimeoutError" && attempt < 3) {
                            await new Promise(r => setTimeout(r, 4000));
                          } else {
                            setError(e instanceof Error && e.name === "TimeoutError"
                              ? "Server is not responding. Wait a moment and try again."
                              : `Failed to save: ${e}`);
                            setStorageChecks(c => ({ ...c, config: false }));
                            setStoragePhase("error");
                            return;
                          }
                        }
                      }
                      setStorageChecks(c => ({ ...c, config: true }));
                    }

                    // Step 2: Create catalog
                    setStoragePhase("creating-catalog");
                    try {
                      const res = await fetch("/api/setup/ensure-catalog", {
                        method: "POST",
                        signal: AbortSignal.timeout(45000),
                      });
                      const body = await res.json().catch(() => ({}));
                      if (!res.ok || body.ok === false) {
                        setError(body.message || body.detail || `Could not create catalog \`${cat}\``);
                        setStorageChecks(c => ({ ...c, catalog: false }));
                        setStoragePhase("error");
                        return;
                      }
                      setStorageChecks(c => ({ ...c, catalog: true }));
                    } catch (e: unknown) {
                      setError(`Could not create catalog: ${e}`);
                      setStorageChecks(c => ({ ...c, catalog: false }));
                      setStoragePhase("error");
                      return;
                    }

                    // Step 3: Create schema
                    setStoragePhase("creating-schema");
                    try {
                      const res = await fetch("/api/setup/ensure-schema", {
                        method: "POST",
                        signal: AbortSignal.timeout(45000),
                      });
                      const body = await res.json().catch(() => ({}));
                      if (!res.ok || body.ok === false) {
                        setError(body.message || body.detail || `Could not create schema \`${cat}.${sch}\``);
                        setStorageChecks(c => ({ ...c, schema: false }));
                        setStoragePhase("error");
                        return;
                      }
                      setStorageChecks(c => ({ ...c, schema: true }));
                    } catch (e: unknown) {
                      setError(`Could not create schema: ${e}`);
                      setStorageChecks(c => ({ ...c, schema: false }));
                      setStoragePhase("error");
                      return;
                    }

                    setStoragePhase("done");
                  }}
                  disabled={!catalogInput.trim() || !schemaInput.trim() || (storagePhase !== "idle" && storagePhase !== "error")}
                  className="btn-brand rounded-lg px-6 py-2 text-sm font-bold text-white transition-colors disabled:opacity-50"
                >
                  {storagePhase !== "idle" && storagePhase !== "error" ? (
                    <span className="flex items-center gap-2">
                      <Spinner size="sm" />
                      Working…
                    </span>
                  ) : storagePhase === "error" ? "Retry" : "Save & Continue"}
                </button>
              )
            ) : step === "permissions" ? (
              <button
                onClick={goNext}
                disabled={loading || readiness == null || (readiness.overall !== "ready" && readiness.overall !== "core_ready")}
                className="btn-brand rounded-lg px-6 py-2 text-sm font-bold text-white transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              >
                Next
              </button>
            ) : (
              <button
                onClick={goNext}
                disabled={loading}
                className="btn-brand rounded-lg px-6 py-2 text-sm font-bold text-white transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              >
                Next
              </button>
            )}
          </div>
        </div>
    </>
  );

  if (embedded) {
    return <div className="flex flex-col h-full">{wizardInner}</div>;
  }

  return createPortal(
    <div className="animate-backdrop fixed inset-0 z-50 overflow-y-auto bg-black/50">
      <div className="flex min-h-full items-center justify-center p-4">
        <div className="animate-dialog mx-4 w-full max-w-2xl rounded-xl bg-white shadow-2xl">
          {wizardInner}
        </div>
      </div>
    </div>,
    document.body
  );
}

function WelcomeStep({ config, cloud, loading }: { config: ConfigData | null; cloud: CloudData | null; loading: boolean }) {
  return (
    <div className="space-y-4">
      <p className="text-sm text-gray-600">
        We detected the following environment. Verify this looks correct before proceeding.
      </p>

      <div className="space-y-3">
        <InfoRow
          label="Cloud Provider"
          value={loading ? "Detecting…" : (cloud ? CLOUD_LABELS[cloud.provider] || cloud.provider : "Unknown")}
          loading={loading}
        />
        <InfoRow
          label="Workspace"
          value={loading ? "Detecting…" : (cloud?.host || "Unknown")}
          loading={loading}
        />
        {loading ? (
          <InfoRow label="SQL Warehouse" value="Detecting…" loading={true} />
        ) : config?.warehouse ? (
          <InfoRow
            label="SQL Warehouse"
            value={config.warehouse.name || config.warehouse.id || "Unknown"}
            status={config.warehouse.state === "RUNNING" ? "ok" : config.warehouse.state === "UNKNOWN" ? undefined : "warn"}
          />
        ) : (
          <div className="rounded-lg bg-amber-50 px-4 py-3">
            <p className="text-sm font-medium text-amber-800 mb-1">No SQL warehouse detected</p>
            <p className="text-xs text-amber-700">
              The warehouse is set via the <span className="font-mono">DATABRICKS_WAREHOUSE_ID</span> app resource binding or the <span className="font-mono">DATABRICKS_HTTP_PATH</span> env var in <span className="font-mono">app.yaml</span>.
              Add a SQL warehouse resource in the Databricks Apps UI and redeploy, then restart setup.
            </p>
          </div>
        )}
        <InfoRow
          label="Identity"
          value={loading ? "Detecting…" : (config?.identity
            ? (config.identity.display_name === config.identity.user_name
                ? config.identity.user_name
                : `${config.identity.display_name} (${config.identity.user_name})`)
            : "Unknown")}
          loading={loading}
        />
      </div>
    </div>
  );
}

function StorageCheckRow({ label, state }: { label: string; state: "pending" | "running" | "done" | "error" }) {
  return (
    <div className="flex items-center gap-2.5 text-sm">
      {state === "running" ? (
        <Spinner size="sm" />
      ) : state === "done" ? (
        <svg className="h-4 w-4 shrink-0 text-green-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M5 13l4 4L19 7" />
        </svg>
      ) : state === "error" ? (
        <svg className="h-4 w-4 shrink-0 text-red-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
        </svg>
      ) : (
        <div className="h-4 w-4 shrink-0 rounded-full border-2 border-gray-300" />
      )}
      <span className={state === "error" ? "text-red-600" : state === "done" ? "text-gray-800 font-medium" : "text-gray-500"}>{label}</span>
    </div>
  );
}

// A text field with a "Browse" dropdown of values the current user can see.
// Picking from the list avoids typos; typing a name the list does not contain
// is how the user signals a brand-new (net-new) catalog/schema. The list is
// fetched lazily on first open so an env-var-locked field never calls the API.
function BrowseField({
  label,
  value,
  onChange,
  disabled,
  placeholder,
  browseHint,
  fetchOptions,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  disabled?: boolean;
  placeholder: string;
  browseHint?: string; // when set, Browse is disabled with this tooltip
  fetchOptions: () => Promise<{ options: string[]; error?: string }>;
}) {
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [options, setOptions] = useState<string[]>([]);
  const [failed, setFailed] = useState(false);
  const canBrowse = !disabled && !browseHint;

  const toggle = async () => {
    if (open) { setOpen(false); return; }
    setOpen(true);
    setLoading(true);
    setFailed(false);
    try {
      const r = await fetchOptions();
      setOptions(r.options ?? []);
      setFailed(Boolean(r.error));
    } catch {
      setOptions([]);
      setFailed(true);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between">
        <label className="block text-xs font-medium text-gray-700">{label}</label>
        <button
          type="button"
          onClick={toggle}
          disabled={!canBrowse}
          title={browseHint}
          className="text-xs font-medium text-[#FF3621] hover:underline disabled:cursor-not-allowed disabled:text-gray-400 disabled:no-underline"
        >
          {open ? "Close" : "Browse"}
        </button>
      </div>
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
        placeholder={placeholder}
        className="w-full rounded-md border border-gray-300 px-3 py-2 text-sm font-mono text-gray-900 placeholder-gray-400 focus:border-[#FF3621] focus:outline-none focus:ring-1 focus:ring-[#FF3621] disabled:bg-gray-100 disabled:text-gray-500"
      />
      {open && (
        <div className="max-h-44 overflow-y-auto rounded-md border border-gray-200 bg-white">
          {loading ? (
            <div className="flex items-center gap-2 px-3 py-2 text-xs text-gray-500"><Spinner size="xs" /> Loading…</div>
          ) : options.length === 0 ? (
            <div className="px-3 py-2 text-xs text-gray-500">
              {failed
                ? "Couldn't list here — type the name in instead."
                : "Nothing you can access here — type a name to create a new one."}
            </div>
          ) : (
            options.map((o) => (
              <button
                key={o}
                type="button"
                onClick={() => { onChange(o); setOpen(false); }}
                className={`block w-full truncate px-3 py-1.5 text-left text-sm font-mono hover:bg-gray-50 ${o === value ? "bg-orange-50 text-[#FF3621]" : "text-gray-700"}`}
              >
                {o}
              </button>
            ))
          )}
        </div>
      )}
    </div>
  );
}

function StorageLocationStep({
  catalog,
  schema,
  onCatalogChange,
  onSchemaChange,
  phase,
  checks,
  envVarLocked,
}: {
  catalog: string;
  schema: string;
  onCatalogChange: (v: string) => void;
  onSchemaChange: (v: string) => void;
  phase: string;
  checks: { config: boolean | null; catalog: boolean | null; schema: boolean | null };
  envVarLocked?: boolean;
}) {
  const locked = phase !== "idle" && phase !== "error";

  const rowState = (
    checkVal: boolean | null,
    activePhase: string,
  ): "pending" | "running" | "done" | "error" => {
    if (phase === activePhase) return "running";
    if (checkVal === true) return "done";
    if (checkVal === false) return "error";
    return "pending";
  };

  const showProgress = phase !== "idle";

  return (
    <div className="space-y-5">
      <p className="text-sm text-gray-600">
        Choose where the app will store its pre-aggregated cost tables.
        The catalog and schema will be created for you if they don't already exist.
      </p>

      {envVarLocked && (
        <div className="flex items-start gap-2.5 rounded-lg border border-amber-200 bg-amber-50 px-4 py-3">
          <svg className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 15v2m0 0v2m0-2h2m-2 0H10m2-6V7a4 4 0 00-8 0v4H3a1 1 0 00-1 1v6a2 2 0 002 2h14a2 2 0 002-2v-6a1 1 0 00-1-1h-1V7a4 4 0 00-8 0z" />
          </svg>
          <p className="text-xs text-amber-800">
            Catalog and schema are set by environment variable (<code className="font-mono">COST_OBS_CATALOG</code> / <code className="font-mono">COST_OBS_SCHEMA</code>). To change them, update the environment variables in the Databricks Apps configuration.
          </p>
        </div>
      )}

      <div className={`rounded-lg border p-4 space-y-4 ${locked || envVarLocked ? "border-gray-100 bg-gray-50" : "border-gray-200 bg-white"}`}>
        <BrowseField
          label="Catalog"
          value={catalog}
          onChange={onCatalogChange}
          disabled={locked || envVarLocked}
          placeholder="e.g. my_catalog"
          fetchOptions={async () => {
            const r = await fetch("/api/setup/list-catalogs").then((x) => x.json()).catch(() => ({}));
            return { options: r.catalogs ?? [], error: r.error };
          }}
        />
        <BrowseField
          label="Schema"
          value={schema}
          onChange={onSchemaChange}
          disabled={locked || envVarLocked}
          placeholder="e.g. cost_obs_app"
          browseHint={!catalog.trim() ? "Enter or pick a catalog first" : undefined}
          fetchOptions={async () => {
            const r = await fetch(`/api/setup/list-schemas?catalog=${encodeURIComponent(catalog.trim())}`)
              .then((x) => x.json()).catch(() => ({}));
            return { options: r.schemas ?? [], error: r.error };
          }}
        />
        {!envVarLocked && (
          <p className="text-xs text-gray-500">
            Browse picks from catalogs &amp; schemas you can access. Type a name that isn't listed to create a new one.
          </p>
        )}
      </div>

      {showProgress ? (
        <div className="space-y-3 rounded-lg border border-gray-100 bg-gray-50 px-4 py-3">
          {!envVarLocked && (
            <StorageCheckRow label="Saving configuration" state={rowState(checks.config, "saving")} />
          )}
          {(checks.catalog !== null || phase === "creating-catalog" || phase === "creating-schema" || phase === "done" || phase === "error") && (
            <StorageCheckRow label={`Creating catalog \`${catalog}\``} state={rowState(checks.catalog, "creating-catalog")} />
          )}
          {(checks.schema !== null || phase === "creating-schema" || phase === "done" || (phase === "error" && checks.catalog === true)) && (
            <StorageCheckRow label={`Creating schema \`${catalog}.${schema}\``} state={rowState(checks.schema, "creating-schema")} />
          )}
        </div>
      ) : (
        <p className="text-xs text-gray-500">
          Tables will be placed at{" "}
          <span className="font-mono">{catalog || "…"}.{schema || "…"}</span>.
        </p>
      )}
    </div>
  );
}

interface WizardPermissionsStepProps {
  readiness: ReadinessResult | null;
  loading: boolean;
  fetchError: string | null;
  onRecheck: (forceRefresh?: boolean) => void;
}

function WizardPermissionsStep({
  readiness,
  loading,
  fetchError,
  onRecheck,
}: WizardPermissionsStepProps) {
  const coreReady = readiness != null &&
    (readiness.overall === "ready" || readiness.overall === "core_ready");

  return (
    <div className="space-y-4">
      <p className="text-sm text-gray-600">
        Verifying the app's service principal has access to required Databricks system tables.
        Core tables (billing) must pass before you can continue.
      </p>
      {!loading && coreReady && (
        <div className="rounded-lg border border-green-200 bg-green-50 px-4 py-2.5 text-sm text-green-700">
          Core access confirmed — you can proceed to Create Tables.
        </div>
      )}
      <ReadinessChecks
        result={readiness}
        loading={loading}
        fetchError={fetchError}
        onRecheck={onRecheck}
      />
    </div>
  );
}

function UserGrantsNotice({ grants }: { grants?: SetupUserGrants | null }) {
  if (!grants) return null;
  if (grants.ok) {
    return (
      <p className="px-1 text-xs text-gray-600">
        Granted <span className="font-medium">{grants.grantee}</span> USE CATALOG, USE SCHEMA and MANAGE on the app catalog and schema.
      </p>
    );
  }
  return (
    <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 space-y-2">
      <p className="text-sm font-medium text-amber-800">
        Could not grant {grants.grantee || "you"} access to the app catalog
      </p>
      <p className="text-xs text-amber-700">
        {grants.error || grants.failed[0]?.error || "The grant was refused."} The dashboard still works; you just can't browse or manage these tables in Catalog Explorer until a catalog owner runs:
      </p>
      {grants.manual_sql && (
        <pre className="overflow-x-auto whitespace-pre rounded border border-amber-200 bg-white px-2 py-1.5 font-mono text-[11px] text-gray-800">{grants.manual_sql}</pre>
      )}
    </div>
  );
}

type CreatedTableRow = { name: string; label: string; tone: "ok" | "muted" | "warn" | "error"; detail?: string };

function describeTableResult(result: string): Omit<CreatedTableRow, "name"> {
  if (result === "created") return { label: "Created", tone: "ok" };
  if (result === "ensured") return { label: "Ready", tone: "ok" };
  if (result === "refreshed") return { label: "Updated", tone: "muted", detail: "Already existed; refreshed with recent data" };
  if (result.startsWith("skipped")) return { label: "Skipped", tone: "warn", detail: "Optional source not connected" };
  return { label: "Failed", tone: "error", detail: result.replace(/^error:\s*/, "") };
}

const TABLE_TONE_CLASS: Record<CreatedTableRow["tone"], string> = {
  ok: "border-green-200 bg-green-50 text-green-700",
  muted: "border-gray-200 bg-gray-50 text-gray-600",
  warn: "border-amber-200 bg-amber-50 text-amber-700",
  error: "border-red-200 bg-red-50 text-red-700",
};

// The tables Create Tables manages, with what this build did to each. When no
// build ran in this session (e.g. setup finished earlier), shows which exist.
function CreatedTablesList({ catalog, schema, results }: {
  catalog?: string;
  schema?: string;
  results?: Record<string, string>;
}) {
  const hasResults = !!results && Object.keys(results).length > 0;
  const [existing, setExisting] = useState<{ catalog: string; schema: string; tables: { name: string; exists: boolean }[] } | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    if (hasResults) return;
    let cancelled = false;
    fetch("/api/setup/created-tables")
      .then(async (r) => {
        if (r.ok) return r.json();
        const body = await r.json().catch(() => ({}));
        throw new Error(body?.detail || `HTTP ${r.status}`);
      })
      .then((data) => { if (!cancelled) setExisting(data); })
      .catch((e) => { if (!cancelled) setLoadError(e instanceof Error ? e.message : String(e)); });
    return () => { cancelled = true; };
  }, [hasResults]);

  const rows: CreatedTableRow[] = hasResults
    ? Object.entries(results!).map(([name, result]) => ({ name, ...describeTableResult(result) }))
    : (existing?.tables ?? []).map(({ name, exists }) => (
      // Optional sources (cloud invoices, use-case objects) are legitimately absent,
      // so an absent table is "not created", not a warning.
      exists ? { name, label: "Present", tone: "ok" as const } : { name, label: "Not created", tone: "muted" as const }
    ));
  const cat = catalog || existing?.catalog;
  const sch = schema || existing?.schema;

  if (!hasResults && !existing) {
    return loadError
      ? <p className="px-1 text-xs text-amber-700">{loadError}</p>
      : <LoadingSpinner text="Listing tables…" />;
  }
  const counts = rows.reduce<Record<string, number>>((acc, r) => ({ ...acc, [r.label]: (acc[r.label] ?? 0) + 1 }), {});

  return (
    <div className="rounded-lg border border-gray-200">
      <div className="flex flex-wrap items-baseline justify-between gap-2 border-b border-gray-100 px-3 py-2">
        <span className="text-xs font-semibold text-gray-700">
          {rows.length} tables{cat && sch ? <> in <span className="font-mono font-normal">{cat}.{sch}</span></> : null}
        </span>
        <span className="text-[11px] text-gray-500">
          {Object.entries(counts).map(([label, n]) => `${n} ${label.toLowerCase()}`).join(" · ")}
        </span>
      </div>
      <div className="max-h-64 overflow-y-auto divide-y divide-gray-100">
        {rows.map((r) => (
          <div key={r.name} className="flex items-center justify-between gap-3 px-3 py-1.5" title={r.detail}>
            <span className="min-w-0 truncate font-mono text-xs text-gray-800">{r.name}</span>
            <span className={`shrink-0 rounded-full border px-2 py-0.5 text-[10px] font-medium ${TABLE_TONE_CLASS[r.tone]}`}>
              {r.label}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

function CreateTablesStep({ setupStatus, creating, tablesJustCreated, preflightResult, preflightLoading, onRecheck }: {
  setupStatus: SetupStatus | null;
  creating: boolean;
  tablesJustCreated: boolean;
  preflightResult: { ok: boolean; status: string; message: string } | null;
  preflightLoading: boolean;
  onRecheck: () => void;
}) {
  if (preflightLoading) {
    return <LoadingSpinner text="Checking catalog access…" />;
  }

  if (preflightResult && !preflightResult.ok) {
    return (
      <div className="space-y-4">
        <div className="rounded-lg border border-red-200 bg-red-50 px-4 py-3">
          <p className="text-sm font-medium text-red-800 mb-1">Catalog not accessible</p>
          <p className="text-sm text-red-700">{preflightResult.message}</p>
        </div>
        <button
          onClick={onRecheck}
          className="text-sm font-medium hover:underline"
          style={{ color: '#FF3621' }}
        >
          Re-check catalog
        </button>
      </div>
    );
  }

  if (creating) {
    const tableProgress = setupStatus?.task?.table_progress ?? {};
    const progressEntries = Object.entries(tableProgress);
    return (
      <div className="space-y-4">
        <LoadingSpinner text="Creating materialized views... This may take a few minutes." />
        {progressEntries.length > 0 && (
          <div className="space-y-1">
            {progressEntries.map(([table, state]) => (
              <div key={table} className="flex items-center gap-2 px-3 py-1 text-sm">
                {state === "done" ? (
                  <svg className="h-4 w-4 text-green-500" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" /></svg>
                ) : state === "running" ? (
                  <Spinner size="sm" />
                ) : state === "error" ? (
                  <svg className="h-4 w-4 text-red-500" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" /></svg>
                ) : (
                  <div className="h-4 w-4 rounded-full border-2 border-gray-200" />
                )}
                <span className="font-mono text-xs text-gray-700">{table}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    );
  }

  if (tablesJustCreated || setupStatus?.all_tables_exist) {
    return (
      <div className="space-y-4">
        <div className="rounded-lg border border-green-200 bg-green-50 px-4 py-3 flex items-center gap-3">
          <svg className="h-5 w-5 text-green-500 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
          </svg>
          <p className="text-sm font-medium text-green-800">Tables created successfully. Click <strong>Next</strong> to continue.</p>
        </div>
        <UserGrantsNotice grants={setupStatus?.task?.user_grants} />
        <CreatedTablesList
          catalog={setupStatus?.catalog}
          schema={setupStatus?.schema}
          results={setupStatus?.task?.table_results}
        />
      </div>
    );
  }

  const interrupted = setupStatus?.task?.status === "interrupted";
  const tableProgress = setupStatus?.task?.table_progress ?? {};
  const builtCount = Object.values(tableProgress).filter((s) => s === "done").length;
  const totalCount = Object.keys(tableProgress).length;

  return (
    <div className="space-y-4">
      {interrupted && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3">
          <p className="text-sm font-semibold text-amber-800 mb-1">Previous build was interrupted</p>
          <p className="text-sm text-amber-700">
            The app restarted while building materialized views.
            {totalCount > 0 && ` ${builtCount} of ${totalCount} tables were built before the interruption.`}
            {" "}Click "Create Tables" to rebuild from scratch.
          </p>
        </div>
      )}

      {!interrupted && (
        <p className="text-sm text-gray-600">
          The app uses pre-aggregated materialized views for fast dashboard loading.
          This step creates them with 6 months of historical data.
        </p>
      )}

      {setupStatus && setupStatus.missing_tables.length > 0 && (
        <div className="rounded-lg bg-amber-50 p-3 text-sm text-amber-700">
          {setupStatus.missing_tables.length} table(s) need to be created in{" "}
          <span className="font-mono">{setupStatus.catalog}.{setupStatus.schema}</span>.
        </div>
      )}

      <p className="text-xs text-gray-500">
        This typically takes 2-5 minutes depending on data volume.
        Click "Create Tables" to begin.
      </p>
    </div>
  );
}

function WorkspaceFilterStep({
  loading,
  workspaces,
  selectedIds,
  onToggle,
  onSelectAll,
  onClearAll,
  saved,
  savedIds,
  poolError,
  onRetry,
}: {
  loading: boolean;
  workspaces: { id: string; name: string; historical?: boolean }[];
  selectedIds: Set<string>;
  onToggle: (id: string) => void;
  onSelectAll: () => void;
  onClearAll: () => void;
  saved: boolean;
  savedIds: string[];
  poolError: string | null;
  onRetry: () => void;
}) {
  if (loading) return <LoadingSpinner text="Loading workspaces..." />;

  if (poolError) {
    return (
      <div className="space-y-3 rounded-lg border border-red-200 bg-red-50 px-4 py-3">
        <p className="text-sm font-medium text-red-800">{poolError}</p>
        <p className="text-xs text-red-700">
          Saving is disabled so the current scope isn't replaced by mistake. Retry, or Skip to keep it as it is.
        </p>
        <button onClick={onRetry} className="text-sm font-medium hover:underline" style={{ color: '#FF3621' }}>
          Retry
        </button>
      </div>
    );
  }

  const nameOf = (id: string) => workspaces.find((w) => w.id === id)?.name || id;
  const currentScope = savedIds.length === 0
    ? "All workspaces"
    : savedIds.length === 1
    ? nameOf(savedIds[0])
    : `${savedIds.length} workspaces`;

  return (
    <div className="space-y-4">
      <p className="text-sm text-gray-600">
        Choose which workspaces this app should display data for. Admins can come back to this step at any time to change it.
      </p>
      <div className="rounded-lg border border-gray-200 bg-gray-50 px-4 py-2.5 text-xs text-gray-600">
        Currently scoped to: <span className="font-medium text-gray-800">{currentScope}</span>
      </div>

      {workspaces.length === 0 ? (
        <div className="rounded-lg bg-amber-50 p-3 text-sm text-amber-700">
          No workspaces with billing activity were found. Click <strong>Continue (all workspaces)</strong> to show data for all workspaces, or go back and check that system table permissions are granted.
        </div>
      ) : (
        <>
          <div className="flex items-center justify-between">
            <span className="text-xs text-gray-500">
              {selectedIds.size === 0 || selectedIds.size === workspaces.length ? "All workspaces will be shown" : `${selectedIds.size} of ${workspaces.length} selected`}
            </span>
            <div className="flex gap-3">
              <button onClick={onSelectAll} className="text-xs font-medium hover:underline" style={{ color: '#FF3621' }}>Select All</button>
              <button onClick={onClearAll} className="text-xs font-medium hover:underline" style={{ color: '#FF3621' }}>Clear All</button>
            </div>
          </div>

          <div className="rounded-lg border border-gray-200 p-2">
            <VirtualizedList
              items={workspaces}
              itemHeight={48}
              maxHeight={208}
              getKey={(ws) => ws.id}
              renderItem={(ws) => {
                const checked = selectedIds.has(ws.id);
                return (
                  <label className={`flex cursor-pointer items-center gap-3 rounded-md px-3 py-2 transition-colors ${checked ? "bg-orange-50" : "hover:bg-gray-50"}`}>
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() => onToggle(ws.id)}
                      className="h-3.5 w-3.5 rounded border-gray-300 text-orange-500 focus:ring-orange-500"
                    />
                    <div className="min-w-0">
                      <div className="flex items-center gap-1.5">
                        <span className="text-sm font-medium text-gray-800 truncate">{ws.name}</span>
                        {ws.historical && (
                          <span className="shrink-0 rounded-full border border-amber-200 bg-amber-50 px-1.5 py-0.5 text-[10px] font-medium text-amber-700" title="This workspace no longer exists in the account — data is historical.">
                            historical
                          </span>
                        )}
                      </div>
                      <div className="text-xs font-mono text-gray-500">{ws.id}</div>
                    </div>
                  </label>
                );
              }}
            />
          </div>

          {selectedIds.size === 0 && (
            <p className="text-xs text-gray-500">
              No workspaces selected — all workspaces will be visible in the dashboard. Select specific workspaces to restrict the data shown.
            </p>
          )}
        </>
      )}

      {saved && (
        <div className="rounded-lg border border-green-200 bg-green-50 px-4 py-2.5">
          <p className="text-xs font-medium text-green-800">Workspace scope saved. It persists across redeploys.</p>
        </div>
      )}
    </div>
  );
}

function CompleteStep() {
  return (
    <div className="flex flex-col items-center py-8">
      <div className="mb-4 flex h-16 w-16 items-center justify-center rounded-full bg-green-100">
        <svg className="h-8 w-8 text-green-600" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
        </svg>
      </div>
      <h3 className="text-lg font-bold text-gray-900 text-center">Setup Complete</h3>
      <p className="mt-2 max-w-sm text-sm text-gray-600 text-center">
        Your environment is configured and materialized views are ready.
        Click below to start exploring your cost data.
      </p>
      <div className="mt-6 w-full rounded-lg border border-amber-200 bg-amber-50 p-4">
        <p className="text-xs font-semibold text-amber-800 mb-1">Important Disclaimer</p>
        <p className="text-xs text-amber-700 leading-relaxed">
          This application is provided as a reference implementation and is not official production software from Databricks.
          It is not covered by Databricks support SLAs. If you encounter issues or have questions, your Solutions Architect
          (SA) and account team are available to assist. We encourage you to customize and tune this application to meet
          your organization's specific requirements. Databricks customers using this reference architecture should treat
          their deployment and use like OSS software.
        </p>
      </div>
    </div>
  );
}

function LoadingSpinner({ text }: { text: string }) {
  return (
    <div className="flex flex-col items-center justify-center py-12">
      <Spinner size="md" />
      <p className="mt-3 text-sm text-gray-500">{text}</p>
    </div>
  );
}

function InfoRow({ label, value, status, loading }: { label: string; value: string; status?: "ok" | "warn" | "error"; loading?: boolean }) {
  return (
    <div className="flex items-center gap-4 rounded-lg bg-gray-50 px-4 py-2.5">
      <span className="shrink-0 text-sm font-medium text-gray-500">{label}</span>
      <div className="flex min-w-0 flex-1 items-center justify-end gap-2">
        {loading ? (
          <Spinner size="sm" />
        ) : (
          <span className="truncate text-right text-sm font-medium text-gray-900" title={value}>{value}</span>
        )}
        {!loading && status === "ok" && <span className="shrink-0 h-2 w-2 rounded-full bg-green-500" />}
        {!loading && status === "warn" && <span className="shrink-0 h-2 w-2 rounded-full bg-amber-500" />}
        {!loading && status === "error" && <span className="shrink-0 h-2 w-2 rounded-full bg-red-500" />}
      </div>
    </div>
  );
}
