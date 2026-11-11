/**
 * Regression tests for SettingsConfig destructive action safety.
 *
 * Key invariant: "Drop Tables" must be disabled when the system is degraded
 * (any non-optional table has exists === false). Users must type "CONFIRM"
 * before the final drop button becomes active.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { SettingsConfig } from "../SettingsConfig";
import type { AppSettings } from "../../SettingsDialog";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const fetchMock = vi.fn<typeof fetch>();

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockReset();
});

const HEALTHY_TABLES = {
  tables: [
    { name: "billing_usage", exists: true, optional: false, row_count: 1000 },
    { name: "query_history", exists: true, optional: true,  row_count: 500  },
  ],
  refresh_status: { status: "ok", stale: false, hours_since_refresh: 0, last_refresh_utc: "2026-05-21T00:00:00Z" },
  auth_error: null,
};

const DEGRADED_TABLES = {
  tables: [
    { name: "billing_usage",  exists: false, optional: false, row_count: 0 },
    { name: "query_history",  exists: true,  optional: true,  row_count: 500 },
  ],
  refresh_status: { status: "error", stale: true, hours_since_refresh: 48, last_refresh_utc: null },
  auth_error: null,
};

const defaultLocalSettings: AppSettings = {
  genieSpaceId: "",
  warehouseId: "",
  warehouseSource: "auto",
  catalog: "main",
  schema: "coc",
};

function renderSettingsConfig(tablesPayload: object) {
  fetchMock.mockImplementation((input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/api/settings/tables")) {
      return Promise.resolve(
        new Response(JSON.stringify(tablesPayload), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        })
      );
    }
    // All other setup endpoints — return empty OK
    return Promise.resolve(new Response("{}", { status: 200 }));
  });

  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <SettingsConfig
        configLoading={false}
        appConfig={{
          warehouse: { id: "wh-1", name: "Main WH", size: "Small", state: "RUNNING" },
          identity: { display_name: "Cost Observer App", user_name: "coc-sp@apps.databricks.com" },
          storage_location: { catalog: "main", schema: "coc" },
        }}
        saveStatus={null}
        localSettings={defaultLocalSettings}
        updateSetting={() => undefined}
      />
    </QueryClientProvider>
  );
}

// ---------------------------------------------------------------------------
// Degraded state: Drop Tables button disabled
// ---------------------------------------------------------------------------

describe("SettingsConfig — destructive action disabled in degraded state", () => {
  it("'Drop Tables' button is disabled when a required table is missing", async () => {
    renderSettingsConfig(DEGRADED_TABLES);

    // Wait for the async table-status fetch to resolve; the degraded guard is applied
    // once tablesStatus loads (the button is always present, so findByRole alone would
    // capture the pre-fetch, not-yet-disabled state).
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /drop tables/i })).toBeDisabled()
    );
  });

  it("shows a degraded-state warning when tables are missing", async () => {
    renderSettingsConfig(DEGRADED_TABLES);

    expect(await screen.findByText(/table.*already missing|missing.*table/i)).toBeInTheDocument();
  });

  it("confirm UI never appears in degraded state — hard block, no break-glass path", async () => {
    renderSettingsConfig(DEGRADED_TABLES);

    await screen.findByRole("button", { name: /drop tables/i });

    // The button is disabled so wipePending can never become true.
    // CONFIRM input must not exist in the DOM at all.
    expect(screen.queryByPlaceholderText(/type confirm/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /confirm drop/i })).not.toBeInTheDocument();
  });

  it("'Drop Tables' button is enabled when all required tables exist", async () => {
    renderSettingsConfig(HEALTHY_TABLES);

    const dropBtn = await screen.findByRole("button", { name: /drop tables/i });
    expect(dropBtn).not.toBeDisabled();
  });
});

// ---------------------------------------------------------------------------
// CONFIRM gate: must type exact text before final drop button enables
// ---------------------------------------------------------------------------

describe("SettingsConfig — CONFIRM gate before irreversible drop", () => {
  it("shows the CONFIRM input after clicking 'Drop Tables'", async () => {
    renderSettingsConfig(HEALTHY_TABLES);

    const dropBtn = await screen.findByRole("button", { name: /drop tables/i });
    await userEvent.click(dropBtn);

    expect(screen.getByPlaceholderText(/type confirm/i)).toBeInTheDocument();
  });

  it("'Confirm Drop' button is disabled until 'CONFIRM' is typed exactly", async () => {
    renderSettingsConfig(HEALTHY_TABLES);

    const dropBtn = await screen.findByRole("button", { name: /drop tables/i });
    await userEvent.click(dropBtn);

    const confirmBtn = screen.getByRole("button", { name: /confirm drop/i });
    expect(confirmBtn).toBeDisabled();

    const input = screen.getByPlaceholderText(/type confirm/i);
    await userEvent.type(input, "confirm"); // lowercase — must not enable
    expect(confirmBtn).toBeDisabled();

    await userEvent.clear(input);
    await userEvent.type(input, "CONFIRM"); // exact match
    expect(confirmBtn).not.toBeDisabled();
  });

  it("partial text does not enable Confirm Drop", async () => {
    renderSettingsConfig(HEALTHY_TABLES);

    const dropBtn = await screen.findByRole("button", { name: /drop tables/i });
    await userEvent.click(dropBtn);

    const confirmBtn = screen.getByRole("button", { name: /confirm drop/i });
    const input = screen.getByPlaceholderText(/type confirm/i);
    await userEvent.type(input, "CON");
    expect(confirmBtn).toBeDisabled();
  });
});

// ---------------------------------------------------------------------------
// Rebuild status survives a page reload
// ---------------------------------------------------------------------------

describe("SettingsConfig — rebuild status", () => {
  it("shows a running rebuild after a reload and blocks a second one", async () => {
    renderSettingsConfig({
      ...HEALTHY_TABLES,
      refresh_status: {
        ...HEALTHY_TABLES.refresh_status,
        in_progress: { started_utc: "2026-11-10T09:00:00Z", trigger: "scheduled" },
      },
    });

    expect(await screen.findByText(/rebuild running since/i)).toBeInTheDocument();
    expect(screen.getByText(/nightly rebuild started/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /rebuilding/i })).toBeDisabled();
  });

  it("says never rebuilt when only history exists", async () => {
    renderSettingsConfig({
      ...HEALTHY_TABLES,
      refresh_status: {
        status: "never", stale: true, hours_since_refresh: null, last_refresh_utc: null,
        refresh_history: [{ timestamp: "2026-11-04T10:00:00Z", status: "config", duration_seconds: 0, lookback_days: null, trigger: "config" }],
      },
    });

    expect(await screen.findByText("Never rebuilt")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /rebuild now/i })).not.toBeDisabled();
  });
});

describe("SettingsConfig — manual rebuild result", () => {
  const manual = (timestamp: string, status = "success") => ({
    timestamp, status, duration_seconds: 60, lookback_days: 180, trigger: "manual",
  });
  const withHistory = (...history: object[]) => ({
    ...HEALTHY_TABLES,
    refresh_status: { ...HEALTHY_TABLES.refresh_status, refresh_history: history },
  });

  it("reports a refused rebuild instead of waiting", async () => {
    renderSettingsConfig(HEALTHY_TABLES);
    const tablesImpl = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) =>
      init?.method === "POST"
        ? Promise.resolve(new Response("{}", { status: 403 }))
        : tablesImpl(input, init),
    );

    await userEvent.click(await screen.findByRole("button", { name: /rebuild now/i }));

    expect(await screen.findByText(/rebuild failed/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /rebuild now/i })).not.toBeDisabled();
  });

  it("waits for its own run, not one that finished after the page loaded", async () => {
    let tables: object = withHistory(manual("2026-11-10T08:00:00Z"));
    renderSettingsConfig(tables);
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const body = String(input).includes("/api/settings/tables") ? tables : {};
      return Promise.resolve(new Response(JSON.stringify(body), { status: 200 }));
    });
    const button = await screen.findByRole("button", { name: /rebuild now/i });

    tables = withHistory(manual("2026-11-10T08:00:00Z"), manual("2026-11-10T09:00:00Z", "error"));
    await userEvent.click(button);
    await new Promise((r) => setTimeout(r, 3_500));
    expect(screen.getByRole("button", { name: /rebuilding/i })).toBeDisabled();
    expect(screen.queryByText(/rebuild failed/i)).not.toBeInTheDocument();
  }, 10_000);
});
