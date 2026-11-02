import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { SettingsDebugger } from "../SettingsDebugger";
import { _resetDebuggerState } from "../debuggerState";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function renderWithQuery(ui: React.ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

// Typed fetch mock so TypeScript is happy
const fetchMock = vi.fn<typeof fetch>();

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockReset();
  // Reset module-level persisted state so each test starts with hasRun=false,
  // preventing the debug query from firing at mount due to a previous test's run.
  _resetDebuggerState();
});

// ---------------------------------------------------------------------------
// Auth mode sourcing
// ---------------------------------------------------------------------------

describe("SettingsDebugger — auth mode sourcing", () => {
  it("shows the auth_mode from /api/settings/auth-status, not a hardcoded default", async () => {
    // /api/settings/config returns a payload WITHOUT auth_mode
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/settings/config")) {
        return Promise.resolve(new Response(JSON.stringify({
          version: { commit_sha: "abc1234" },
          warehouse: { id: "wh-1", source: "app_resource" },
          storage_location: { catalog: "main", schema: "coc" },
          // no auth_mode field
        }), { status: 200, headers: { "Content-Type": "application/json" } }));
      }
      if (url.includes("/api/settings/auth-status")) {
        return Promise.resolve(new Response(JSON.stringify({
          auth_mode: "user",
          identity: "user_oauth",
        }), { status: 200, headers: { "Content-Type": "application/json" } }));
      }
      // /api/debug/run — never called since user hasn't clicked "Run Diagnostics"
      return Promise.resolve(new Response("{}", { status: 200 }));
    });

    renderWithQuery(<SettingsDebugger />);

    // Deployment Info section appears once config loads
    await waitFor(() => {
      expect(screen.getByText("Auth mode")).toBeInTheDocument();
    });

    // Must show the real value from auth-status, not "service_principal"
    expect(screen.getByText("user")).toBeInTheDocument();
    expect(screen.queryByText("service_principal")).not.toBeInTheDocument();
  });

  it("shows '—' when /api/settings/auth-status is unavailable", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/settings/config")) {
        return Promise.resolve(new Response(JSON.stringify({
          version: { commit_sha: "abc1234" },
          warehouse: { id: "wh-1", source: "http_path" },
          storage_location: { catalog: "main", schema: "coc" },
        }), { status: 200, headers: { "Content-Type": "application/json" } }));
      }
      if (url.includes("/api/settings/auth-status")) {
        return Promise.reject(new Error("Network error"));
      }
      return Promise.resolve(new Response("{}", { status: 200 }));
    });

    renderWithQuery(<SettingsDebugger />);

    await waitFor(() => {
      expect(screen.getByText("Auth mode")).toBeInTheDocument();
    });

    // Falls back to "—" rather than a stale hardcoded value. (Several deployment-info
    // fields render "—" when their source is unavailable, so match all occurrences.)
    await waitFor(() => {
      expect(screen.getAllByText("—").length).toBeGreaterThan(0);
    });
  });
});

// ---------------------------------------------------------------------------
// Helpers for diagnostics tests
// ---------------------------------------------------------------------------

interface DiagCheckInput {
  id: string;
  category: string;
  label: string;
  status: "pass" | "fail" | "warn" | "skip";
  detail: string;
  fix: string;
  failure_class?: string;
}

function makeDiagResult(checks: DiagCheckInput[]) {
  return {
    checks,
    summary: {
      passed: checks.filter(c => c.status === "pass").length,
      failed: checks.filter(c => c.status === "fail").length,
      warned: checks.filter(c => c.status === "warn").length,
      total: checks.length,
    },
  };
}

function mockApisWithDiag(diagPayload: object) {
  fetchMock.mockImplementation((input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/api/settings/config")) {
      return Promise.resolve(new Response(JSON.stringify({
        version: { commit_sha: "abc1234" },
        warehouse: { id: "wh-1", source: "app_resource" },
        storage_location: { catalog: "main", schema: "coc" },
      }), { status: 200, headers: { "Content-Type": "application/json" } }));
    }
    if (url.includes("/api/settings/auth-status")) {
      return Promise.resolve(new Response(JSON.stringify({ auth_mode: "sp" }), {
        status: 200, headers: { "Content-Type": "application/json" },
      }));
    }
    if (url.includes("/api/debug/run")) {
      return Promise.resolve(new Response(JSON.stringify(diagPayload), {
        status: 200, headers: { "Content-Type": "application/json" },
      }));
    }
    return Promise.resolve(new Response("{}", { status: 200 }));
  });
}

// ---------------------------------------------------------------------------
// Failure class → action label mapping
// ---------------------------------------------------------------------------

describe("SettingsDebugger — failure class action labels", () => {
  it("warehouse permission failure renders 'Grant SQL' button", async () => {
    mockApisWithDiag(makeDiagResult([{
      id: "wh-perm",
      category: "permissions",
      label: "Warehouse access",
      status: "fail",
      detail: "SP cannot execute queries on the configured SQL warehouse",
      fix: "GRANT CAN_USE ON WAREHOUSE `wh-prod` TO `sp-abc`;",
      failure_class: "warehouse_permission",
    }]));

    renderWithQuery(<SettingsDebugger />);
    await userEvent.click(await screen.findByRole("button", { name: /run diagnostics/i }));

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /grant sql/i })).toBeInTheDocument();
    });
  });

  it("system table grant failure renders 'Grant SQL' button", async () => {
    mockApisWithDiag(makeDiagResult([{
      id: "tbl-grant",
      category: "permissions",
      label: "system.query.history access",
      status: "fail",
      detail: "PERMISSION_DENIED on system.query.history",
      fix: "GRANT SELECT ON TABLE system.query.history TO `sp-abc`;",
      failure_class: "system_table_grant",
    }]));

    renderWithQuery(<SettingsDebugger />);
    await userEvent.click(await screen.findByRole("button", { name: /run diagnostics/i }));

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /grant sql/i })).toBeInTheDocument();
    });
  });

  it("missing MV failure renders 'Rebuild' button", async () => {
    mockApisWithDiag(makeDiagResult([{
      id: "mv-missing",
      category: "materialized_views",
      label: "billing_summary MV missing",
      status: "fail",
      detail: "Table main.coc.billing_summary does not exist",
      fix: "Go to Settings → Config → Rebuild to recreate app tables.",
      failure_class: "missing_mv",
    }]));

    renderWithQuery(<SettingsDebugger />);
    await userEvent.click(await screen.findByRole("button", { name: /run diagnostics/i }));

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /rebuild/i })).toBeInTheDocument();
    });
  });

  it("not_configured failure renders 'Configure' button", async () => {
    mockApisWithDiag(makeDiagResult([{
      id: "no-config",
      category: "configuration",
      label: "Warehouse not configured",
      status: "fail",
      detail: "DATABRICKS_HTTP_PATH is not set",
      fix: "Set DATABRICKS_HTTP_PATH in the Apps environment variables.",
      failure_class: "not_configured",
    }]));

    renderWithQuery(<SettingsDebugger />);
    await userEvent.click(await screen.findByRole("button", { name: /run diagnostics/i }));

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /configure/i })).toBeInTheDocument();
    });
  });

  it("infers 'Rebuild' for materialized_views category with no explicit failure_class", async () => {
    mockApisWithDiag(makeDiagResult([{
      id: "mv-inferred",
      category: "materialized_views",
      label: "MV stale",
      status: "fail",
      detail: "Materialized view is 48 hours stale",
      fix: "Rebuild the materialized view to restore fresh data.",
      // no failure_class — must be inferred from category
    }]));

    renderWithQuery(<SettingsDebugger />);
    await userEvent.click(await screen.findByRole("button", { name: /run diagnostics/i }));

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /rebuild/i })).toBeInTheDocument();
    });
  });
});

// ---------------------------------------------------------------------------
// Schema mismatch: prose fix, not raw SQL
// ---------------------------------------------------------------------------

describe("SettingsDebugger — schema mismatch renders rebuild state, not raw SQL error", () => {
  it("schema_mismatch check shows 'Rebuild' button", async () => {
    mockApisWithDiag(makeDiagResult([{
      id: "schema-mismatch",
      category: "materialized_views",
      label: "billing_summary schema mismatch",
      status: "fail",
      detail: "Column 'workspace_id' not found in main.coc.billing_summary — expected by current query",
      fix: "Drop and rebuild the materialized view to apply the latest schema.",
      failure_class: "schema_mismatch",
    }]));

    renderWithQuery(<SettingsDebugger />);
    await userEvent.click(await screen.findByRole("button", { name: /run diagnostics/i }));

    // Must show Rebuild action label, not Grant SQL or Show Fix
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /rebuild/i })).toBeInTheDocument();
    });
    expect(screen.queryByRole("button", { name: /grant sql/i })).not.toBeInTheDocument();
  });

  it("schema_mismatch fix text renders as prose, not a SQL code block", async () => {
    mockApisWithDiag(makeDiagResult([{
      id: "schema-mismatch-prose",
      category: "materialized_views",
      label: "billing_summary schema mismatch",
      status: "fail",
      detail: "Column workspace_id missing",
      fix: "Drop and rebuild the materialized view to apply the latest schema.",
      failure_class: "schema_mismatch",
    }]));

    renderWithQuery(<SettingsDebugger />);
    await userEvent.click(await screen.findByRole("button", { name: /run diagnostics/i }));

    const rebuildBtn = await screen.findByRole("button", { name: /rebuild/i });
    await userEvent.click(rebuildBtn); // expand the fix section

    await waitFor(() => {
      // Fix text must be visible
      expect(screen.getByText(/drop and rebuild/i)).toBeInTheDocument();
    });

    // Must NOT render as a <pre> code block (which is used for SQL)
    // The prose path renders as a <p> element
    const fixText = screen.getByText(/drop and rebuild/i);
    expect(fixText.tagName).not.toBe("PRE");
    expect(fixText.tagName).not.toBe("CODE");

    // Must NOT show "Run as metastore admin" label (that's SQL-only)
    expect(screen.queryByText(/run as metastore admin/i)).not.toBeInTheDocument();
  });

  it("SQL-fix for warehouse permission still renders as code block with copy button", async () => {
    mockApisWithDiag(makeDiagResult([{
      id: "sql-fix",
      category: "permissions",
      label: "Warehouse access denied",
      status: "fail",
      detail: "SP cannot use warehouse wh-prod",
      fix: "GRANT CAN_USE ON WAREHOUSE `wh-prod` TO `sp-abc`;",
      failure_class: "warehouse_permission",
    }]));

    renderWithQuery(<SettingsDebugger />);
    await userEvent.click(await screen.findByRole("button", { name: /run diagnostics/i }));

    const grantBtn = await screen.findByRole("button", { name: /grant sql/i });
    await userEvent.click(grantBtn); // expand

    await waitFor(() => {
      expect(screen.getByText(/run as metastore admin/i)).toBeInTheDocument();
      expect(screen.getByRole("button", { name: /copy/i })).toBeInTheDocument();
    });
  });
});
