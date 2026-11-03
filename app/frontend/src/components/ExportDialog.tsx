import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import { createPortal } from "react-dom";
import type { TabVisibility } from "@/components/SettingsDialog";
import { C } from "@/theme";
import { useDocumentScrollLock } from "@/utils/scrolling";
import { CostObsMark } from "@/components/brand";
import { Spinner } from "@/components/Spinner";
import { SECTION_TO_TAB } from "@/components/exportSections";
import type { ExportFormat, ExportSections } from "@/components/exportSections";

export type { ExportFormat, ExportSections } from "@/components/exportSections";


const FOCUSABLE = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

interface ExportDialogProps {
  isOpen: boolean;
  onClose: () => void;
  onExport: (sections: ExportSections, format: ExportFormat) => void;
  tabVisibility: TabVisibility;
  enableArchitectureView?: boolean;
  enableMvShareRunbook?: boolean;
  isAdmin?: boolean;
  onExportArchitecture?: () => Promise<void>;
  dataLoading?: boolean;
  dataPrepared?: boolean;
  requiredTabs?: Array<keyof TabVisibility>;
  tabLoading?: Partial<Record<keyof TabVisibility, boolean>>;
  dataErrors?: Partial<Record<keyof TabVisibility, string>>;
  onPrepare?: (sections: ExportSections) => void;
  onSelectionChange?: (sections: ExportSections) => void;
  onRetryFailed?: () => Promise<void>;
}

const tabLabels: Record<keyof TabVisibility, string> = {
  dbu: "$DBU Spend",
  sql: "SQL",
  "ai-gateway": "Unity Gateway",
  genie: "Genie",
  apps: "Apps",
  tagging: "Tagging",
  "users-groups": "Users",
  kpis: "KPIs & Trends",
  infra: "Cloud Costs",
  optimizer: "Optimize",
  conversational: "Ask Genie",
};

const sectionLabels: Record<keyof ExportSections, { label: string; description: string }> = {
  summary: { label: "Executive Summary", description: "Total DBUs, spend, and key metrics" },
  products: { label: "Product Breakdown", description: "Spend by product category" },
  workspaces: { label: "Workspace Breakdown", description: "Top workspaces by spend" },
  skus: { label: "SKU Breakdown", description: "Spend by SKU/billing type" },
  pipelines: { label: "Jobs & Pipelines", description: "Top jobs and SDP pipelines" },
  interactive: { label: "Interactive Compute", description: "Notebook and cluster usage" },
  query360: { label: "Query", description: "SQL warehouse efficiency and query costs" },
  aiml: { label: "AI/ML", description: "Endpoints, models, ML clusters, and Agent Bricks" },
  apps: { label: "Apps", description: "Databricks Apps costs and connected artifacts" },
  tagging: { label: "Tagging", description: "Tag KPIs, coverage, top tags, untagged resources" },
  users: { label: "Users", description: "Top users by spend and product breakdown" },
  platformKPIs: { label: "Platform KPIs & Trends", description: "Platform-wide metrics and trends" },
  anomalies: { label: "Spend Anomalies", description: "Day-over-day spend changes" },
  awsCosts: { label: "Cloud Costs", description: "Cluster usage, DBU spend, and cloud cost integration status" },
  optimize: { label: "Optimize", description: "Warehouse rightsizing and idle-time opportunities" },
};

export function ExportDialog({
  isOpen,
  onClose,
  onExport,
  tabVisibility,
  enableArchitectureView = false,
  enableMvShareRunbook = false,
  isAdmin = false,
  onExportArchitecture,
  dataLoading = false,
  dataPrepared,
  requiredTabs = [],
  tabLoading = {},
  dataErrors = {},
  onPrepare,
  onSelectionChange,
  onRetryFailed,
}: ExportDialogProps) {
  const prepared = dataPrepared ?? !onPrepare;
  const visibleSections = useMemo(() => {
    const result: ExportSections = {} as ExportSections;
    for (const key of Object.keys(SECTION_TO_TAB) as Array<keyof ExportSections>) {
      const tab = SECTION_TO_TAB[key];
      result[key] = tab === null || tabVisibility[tab];
    }
    return result;
  }, [tabVisibility]);

  const [sections, setSections] = useState<ExportSections>(visibleSections);
  const [format, setFormat] = useState<ExportFormat>("pdf");
  const [architectureBusy, setArchitectureBusy] = useState(false);
  const [architectureError, setArchitectureError] = useState<string | null>(null);
  const [runbookBusy, setRunbookBusy] = useState(false);
  const [runbookError, setRunbookError] = useState<string | null>(null);
  const [retryingFailed, setRetryingFailed] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);
  useDocumentScrollLock(isOpen);

  const handleKeyDown = useCallback((e: KeyboardEvent) => {
    if (e.key === "Escape" && !architectureBusy) onClose();
  }, [architectureBusy, onClose]);

  useEffect(() => {
    if (isOpen) {
      setSections(visibleSections);
      setArchitectureError(null);
    }
  }, [isOpen, visibleSections]);

  useEffect(() => {
    if (isOpen) {
      document.addEventListener("keydown", handleKeyDown);
    }
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [isOpen, handleKeyDown]);

  useEffect(() => {
    if (!isOpen) return;
    const returnFocusTo = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    panelRef.current?.focus();
    const trap = (event: KeyboardEvent) => {
      if (event.key !== "Tab" || !panelRef.current) return;
      const focusable = Array.from(
        panelRef.current.querySelectorAll<HTMLElement>(FOCUSABLE),
      ).filter((element) => !element.hidden && element.getAttribute("aria-hidden") !== "true");
      if (focusable.length === 0) {
        event.preventDefault();
        panelRef.current.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", trap);
    return () => {
      document.removeEventListener("keydown", trap);
      returnFocusTo?.focus();
    };
  }, [isOpen]);

  if (!isOpen) return null;

  const updateSections = (next: ExportSections) => {
    setSections(next);
    onSelectionChange?.(next);
  };

  const toggleSection = (key: keyof ExportSections) => {
    updateSections({ ...sections, [key]: !sections[key] });
  };

  const selectAll = () => {
    updateSections({ ...visibleSections });
  };

  const selectNone = () => {
    const none: ExportSections = {} as ExportSections;
    for (const key of Object.keys(SECTION_TO_TAB) as Array<keyof ExportSections>) none[key] = false;
    updateSections(none);
  };

  const visibleKeys = (Object.keys(SECTION_TO_TAB) as Array<keyof ExportSections>).filter((k) => visibleSections[k]);
  const selectedCount = visibleKeys.filter((k) => sections[k]).length;
  const failedSelectedTabs = Array.from(new Set(
    requiredTabs.filter((tab) => Boolean(dataErrors[tab])),
  ));
  const hasSelectedErrors = failedSelectedTabs.length > 0;
  const readyTabCount = requiredTabs.filter((tab) => !tabLoading[tab] && !dataErrors[tab]).length;

  const handleExport = () => {
    if (dataLoading || architectureBusy || hasSelectedErrors) return;
    if (!prepared && onPrepare) {
      onPrepare(sections);
      return;
    }
    onExport(sections, format);
    onClose();
  };

  const retryFailed = async () => {
    if (!onRetryFailed || retryingFailed) return;
    setRetryingFailed(true);
    try {
      await onRetryFailed();
    } finally {
      setRetryingFailed(false);
    }
  };

  const handleRunbookDownload = async () => {
    if (runbookBusy) return;
    setRunbookBusy(true);
    setRunbookError(null);
    try {
      const response = await fetch("/api/settings/materialized-view-runbook");
      if (!response.ok) {
        throw new Error(`Runbook download failed with status ${response.status}`);
      }
      const blob = await response.blob();
      const objectUrl = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = objectUrl;
      link.download = "cost_obs_mv_share_publisher.py";
      link.style.display = "none";
      document.body.appendChild(link);
      try {
        link.click();
        await new Promise((resolve) => window.setTimeout(resolve, 400));
      } finally {
        link.remove();
        URL.revokeObjectURL(objectUrl);
      }
    } catch (error) {
      console.error("MV share runbook download failed", error);
      setRunbookError("The runbook could not be downloaded. Check the experimental setting and try again.");
    } finally {
      setRunbookBusy(false);
    }
  };

  const handleArchitectureExport = async () => {
    if (!onExportArchitecture || architectureBusy) return;
    setArchitectureBusy(true);
    setArchitectureError(null);
    try {
      await onExportArchitecture();
      onClose();
    } catch (error) {
      console.error("Architecture PDF download failed", error);
      setArchitectureError("The architecture PDF could not be downloaded. Please try again.");
    } finally {
      setArchitectureBusy(false);
    }
  };

  return createPortal(
    <div
      className="animate-backdrop fixed inset-0 z-50 overflow-y-auto bg-black/40"
      onClick={(e) => e.target === e.currentTarget && !architectureBusy && onClose()}
    >
      <div className="flex min-h-full items-center justify-center p-4 sm:p-6" onClick={(e) => e.target === e.currentTarget && !architectureBusy && onClose()}>
        <div
          ref={panelRef}
          tabIndex={-1}
          role="dialog"
          aria-modal="true"
          aria-labelledby="export-dialog-title"
          aria-describedby="export-dialog-description"
          className="animate-dialog relative flex h-[92vh] max-h-208 w-full max-w-4xl flex-col overflow-hidden"
          style={{
            background: C.card,
            border: "none",
            borderRadius: 8,
            boxShadow: "0 20px 60px rgba(0,0,0,0.25)",
          }}
          onClick={(e) => e.stopPropagation()}
        >
          <div className="px-6 py-5" style={{ background: C.navy, borderBottom: `1px solid ${C.hairline}` }}>
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-3">
                <div className="flex h-11 w-11 shrink-0 items-center justify-center">
                  <CostObsMark className="h-9 w-9" whiteOrbit />
                </div>
                <div>
                  <h3 id="export-dialog-title" className="text-lg font-bold leading-tight" style={{ color: C.white }}>
                    Export report
                  </h3>
                  <p id="export-dialog-description" className="mt-0.5 text-sm" style={{ color: C.muted }}>
                    Choose a format and the sections to include.
                  </p>
                </div>
              </div>
              <button
                type="button"
                onClick={onClose}
                disabled={architectureBusy}
                aria-label="Close export dialog"
                className="flex h-9 w-9 items-center justify-center transition-colors focus-visible:outline-none focus-visible:shadow-(--focus)"
                style={{ color: C.white, borderRadius: 8 }}
                onMouseEnter={(e) => { e.currentTarget.style.backgroundColor = "rgba(255,255,255,0.12)"; }}
                onMouseLeave={(e) => { e.currentTarget.style.backgroundColor = "transparent"; }}
              >
                <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>
          </div>

          {(enableArchitectureView || (isAdmin && enableMvShareRunbook)) && (
            <div className="px-6 pt-5" style={{ background: C.oatPage }}>
              <div className={`grid grid-cols-1 gap-3 ${enableArchitectureView && isAdmin && enableMvShareRunbook ? "lg:grid-cols-2" : ""}`}>
              {enableArchitectureView && (
                <div
                  className="flex flex-col justify-between gap-4 px-4 py-3.5"
                  style={{
                    background: C.card,
                    border: `1px solid ${C.navy}`,
                    borderLeft: `4px solid ${C.lava}`,
                    borderRadius: 8,
                  }}
                >
                <div>
                  <div className="flex items-center gap-2">
                    <h4 className="text-sm font-bold" style={{ color: C.navy }}>Architecture PDF</h4>
                    <span className="rounded-full px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide" style={{ color: C.lava, background: C.coralTint }}>
                      Experimental
                    </span>
                  </div>
                  <p className="mt-1 text-xs leading-5" style={{ color: C.slate }}>
                    Download a customer-facing overview of components, data flow, governance, refresh behavior, and source tables.
                  </p>
                  {architectureError && (
                    <p role="alert" className="mt-1 text-xs font-semibold" style={{ color: "#B91C1C" }}>
                      {architectureError}
                    </p>
                  )}
                </div>
                <button
                  type="button"
                  onClick={handleArchitectureExport}
                  disabled={architectureBusy || !onExportArchitecture}
                  className="inline-flex items-center gap-2 px-4 py-2 text-sm font-semibold text-white focus-visible:outline-none focus-visible:shadow-(--focus)"
                  style={{ background: architectureBusy ? C.slate : C.navy, borderRadius: 8 }}
                >
                  {architectureBusy ? <Spinner size="xs" /> : (
                    <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.293.707l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                    </svg>
                  )}
                  {architectureBusy ? "Downloading architecture PDF…" : "Download Architecture PDF"}
                </button>
                </div>
              )}
              {isAdmin && enableMvShareRunbook && (
                <div
                  className="flex flex-col justify-between gap-4 px-4 py-3.5"
                  style={{
                    background: C.card,
                    border: `1px solid ${C.navy}`,
                    borderLeft: `4px solid ${C.s3}`,
                    borderRadius: 8,
                  }}
                >
                  <div>
                    <div className="flex items-center gap-2">
                      <h4 className="text-sm font-bold" style={{ color: C.navy }}>MV Share Runbook</h4>
                      <span className="rounded-full px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide" style={{ color: C.s3, background: "#E8F7F0" }}>
                        Experimental
                      </span>
                    </div>
                    <p className="mt-1 text-xs leading-5" style={{ color: C.slate }}>
                      Download the Run All notebook that builds compatible cost-obs aggregates and publishes them through Delta Sharing.
                    </p>
                    {runbookError && (
                      <p role="alert" className="mt-1 text-xs font-semibold" style={{ color: "#B91C1C" }}>
                        {runbookError}
                      </p>
                    )}
                  </div>
                  <button
                    type="button"
                    onClick={() => { void handleRunbookDownload(); }}
                    disabled={runbookBusy}
                    className="inline-flex w-fit items-center gap-2 px-4 py-2 text-sm font-semibold text-white focus-visible:outline-none focus-visible:shadow-(--focus)"
                    style={{ background: C.navy, borderRadius: 8 }}
                  >
                    <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                    </svg>
                    {runbookBusy ? "Downloading runbook…" : "Download Runbook"}
                  </button>
                </div>
              )}
              </div>
            </div>
          )}

          <section
            aria-labelledby="cost-report-heading"
            className="px-6 pb-4 pt-5"
            style={{ background: C.oatPage, borderBottom: `1px solid ${C.hairline}` }}
          >
            <h4 id="cost-report-heading" className="text-sm font-bold" style={{ color: C.navy }}>
              Cost report
            </h4>
            <p className="mt-1 mb-4 text-xs leading-5" style={{ color: C.slate }}>
              Export selected cost and usage sections with the active workspace and date filters.
            </p>
            <div className="mb-2 text-xs font-bold uppercase tracking-[0.12em]" style={{ color: C.slate }}>
              File format
            </div>
            <div
              className="inline-flex overflow-hidden"
              role="group"
              aria-label="Export format"
              style={{ border: `1px solid ${C.hairline}`, borderRadius: 8, background: C.oatMed }}
            >
              {(["pdf", "csv"] as ExportFormat[]).map((f) => (
                <button
                  type="button"
                  key={f}
                  onClick={() => setFormat(f)}
                  aria-pressed={format === f}
                  className="min-w-20 px-4 py-2 text-sm font-semibold transition-colors focus-visible:z-10 focus-visible:outline-none focus-visible:shadow-(--focus)"
                  style={{
                    background: format === f ? C.card : "transparent",
                    color: format === f ? C.navy : C.slate,
                    borderLeft: f === "csv" ? `1px solid ${C.hairline}` : undefined,
                    boxShadow: format === f ? `inset 0 -2px 0 ${C.lava}` : "none",
                  }}
                >
                  {f.toUpperCase()}
                </button>
              ))}
            </div>
            {format === "csv" && (
              <p className="mt-2 text-xs leading-5" style={{ color: C.slate }}>
                Downloads a multi-sheet Excel workbook (.xls), with one sheet per app tab and the active workspace and date filters applied.
              </p>
            )}
          </section>

          <div className="min-h-0 flex-1 overflow-y-auto px-6 py-4" style={{ background: C.card }}>
            <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
              <div>
                <div className="text-sm font-semibold" style={{ color: C.ink }}>Report sections</div>
                <div className="mt-0.5 text-xs" style={{ color: C.slate }}>
                  {selectedCount} of {visibleKeys.length} selected
                </div>
              </div>
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={selectAll}
                  className="px-2 py-1 text-sm font-semibold transition-colors focus-visible:outline-none focus-visible:shadow-(--focus)"
                  style={{ color: C.lava, borderRadius: 4 }}
                >
                  Select all
                </button>
                <button
                  type="button"
                  onClick={selectNone}
                  className="px-2 py-1 text-sm font-semibold transition-colors focus-visible:outline-none focus-visible:shadow-(--focus)"
                  style={{ color: C.lava, borderRadius: 4 }}
                >
                  Select none
                </button>
              </div>
            </div>

            <div className="grid gap-1.5 sm:grid-cols-2">
              {visibleKeys.map((key) => {
                const { label, description } = sectionLabels[key];
                const checked = sections[key];
                const tab = SECTION_TO_TAB[key];
                const sectionError = tab ? dataErrors[tab] : undefined;

                return (
                  <label
                    key={key}
                    className="flex cursor-pointer items-start gap-2.5 px-3 py-2.5 transition-colors"
                    style={{
                      background: checked ? C.coralTint : C.card,
                      border: `1px solid ${checked ? C.coralBrd : C.hairline}`,
                      borderLeft: `3px solid ${checked ? C.lava : C.hairline}`,
                      borderRadius: 8,
                    }}
                  >
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() => toggleSection(key)}
                      className="mt-0.5 h-4 w-4 shrink-0 rounded-[3px] focus-visible:outline-none focus-visible:shadow-(--focus)"
                      style={{ accentColor: C.lava }}
                    />
                    <div className="flex-1">
                      <div className="text-sm font-semibold leading-4" style={{ color: C.ink }}>{label}</div>
                      <div className="mt-0.5 truncate text-xs leading-4" style={{ color: C.slate }} title={description}>{description}</div>
                      {sectionError && (
                        <div role="status" className="mt-1 text-[11px] font-semibold text-red-700">
                          Data failed to load. Deselect this section or retry.
                        </div>
                      )}
                    </div>
                  </label>
                );
              })}
            </div>
            {requiredTabs.length > 0 && (
              <div className="mt-4 rounded-lg border px-3 py-2.5" style={{ borderColor: C.hairline, background: C.oatPage }}>
                <div className="text-xs font-semibold" style={{ color: C.ink }}>
                  Required tabs: {readyTabCount} of {requiredTabs.length} ready
                </div>
                <div className="mt-2 flex flex-wrap gap-2">
                  {requiredTabs.map((tab) => {
                    const error = dataErrors[tab];
                    const loading = tabLoading[tab];
                    const status = error ? "Failed" : loading ? "Loading" : "Ready";
                    return (
                      <span
                        key={tab}
                        role="status"
                        className="rounded-full border px-2 py-1 text-[11px] font-semibold"
                        style={{
                          borderColor: error ? "#FCA5A5" : loading ? C.coralBrd : "#A7D7B7",
                          color: error ? "#B91C1C" : loading ? C.lava : "#166534",
                          background: C.card,
                        }}
                      >
                        {tabLabels[tab]} · {status}
                      </span>
                    );
                  })}
                </div>
              </div>
            )}
          </div>

          <div
            className="flex flex-wrap items-center justify-between gap-3 px-6 py-4"
            style={{ background: C.oatPage, borderTop: `1px solid ${C.hairline}` }}
          >
            {hasSelectedErrors ? (
              <div role="alert" className="flex items-center gap-3 text-xs text-red-700">
                <span>Selected report sections failed to load. Export is blocked to prevent a partial report.</span>
                <button type="button" onClick={() => void retryFailed()} disabled={!onRetryFailed || retryingFailed} className="font-semibold underline disabled:opacity-50">
                  {retryingFailed ? "Retrying…" : "Retry failed sections"}
                </button>
              </div>
            ) : (
              <span className="text-xs" style={{ color: C.slate }}>
                Active workspace and date filters are applied.
              </span>
            )}
            <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={onClose}
              disabled={architectureBusy}
              className="px-4 py-2 text-sm font-semibold transition-colors focus-visible:outline-none focus-visible:shadow-(--focus)"
              style={{ color: C.ink, background: C.card, border: `1px solid ${C.hairline}`, borderRadius: 8 }}
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={handleExport}
              disabled={selectedCount === 0 || dataLoading || architectureBusy || hasSelectedErrors}
              aria-label={dataLoading
                ? `Preparing ${selectedCount} report sections: ${readyTabCount} of ${requiredTabs.length} dashboard tabs ready`
                : hasSelectedErrors
                  ? "Export blocked by failed report data"
                  : !prepared && onPrepare
                    ? "Prepare report data"
                    : undefined}
              className="btn-brand inline-flex items-center gap-2 px-4 py-2 text-sm focus-visible:outline-none"
            >
              {dataLoading ? <Spinner size="xs" /> : (
                <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.293.707l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                </svg>
              )}
              {dataLoading
                ? `Preparing ${selectedCount} report sections · ${readyTabCount} of ${requiredTabs.length} dashboard tabs ready…`
                : !prepared && onPrepare
                  ? `Prepare ${selectedCount} section${selectedCount !== 1 ? "s" : ""}`
                  : `Export ${selectedCount} section${selectedCount !== 1 ? "s" : ""} as ${format.toUpperCase()}`}
            </button>
            </div>
          </div>
        </div>
      </div>
    </div>,
    document.body
  );
}
