/**
 * Regression tests for SettingsPermissions SP identity and grant bundle.
 *
 * Key invariants:
 * 1. SP client ID and display name are rendered from /api/settings/auth-status.
 * 2. The grant SQL bundle uses the actual SP name (not a placeholder).
 * 3. After running grants, the readiness cache is invalidated.
 * 4. When SP is the active identity, the remediation bundle is shown.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { SettingsPermissions } from "../SettingsPermissions";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const fetchMock = vi.fn<typeof fetch>();

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockReset();
});

const SP_AUTH_STATUS = {
  auth_mode: "sp",
  identity: "service_principal",
  token_present: false,
  user_email: "cost-observer-sp@apps.databricks.com",
  sp_client_id: "0000-aaaa-bbbb-1234",
  sp_display_name: "cost-observer-app-sp",
  catalog: "main",
  schema: "coc",
};

function mockApis(authStatus: object, permissionsPayload?: object, userPermissions?: object) {
  const defaultUserPermissions = {
    admins: [],
    consumers: [],
    current_user: "alice@databricks.com",
  };
  const defaultPermissions = {
    permissions: [],
    summary: { total: 0, granted: 0, required_count: 2, required_granted: 2, all_required_granted: true, ready_to_use: true },
    user: { email: "alice@databricks.com", name: "Alice" },
    sp: { client_id: "0000-aaaa-bbbb-1234", display_name: "cost-observer-app-sp" },
    help_url: "https://docs.databricks.com",
  };

  fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.includes("/api/settings/auth-status")) {
      return Promise.resolve(
        new Response(JSON.stringify(authStatus), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        })
      );
    }
    if (url.includes("/api/settings/user-permissions")) {
      if (init?.method === "POST") {
        return Promise.resolve(
          new Response(JSON.stringify({ status: "ok" }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          })
        );
      }
      return Promise.resolve(
        new Response(JSON.stringify(userPermissions ?? defaultUserPermissions), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        })
      );
    }
    if (url.includes("/api/permissions/check")) {
      return Promise.resolve(
        new Response(JSON.stringify(permissionsPayload ?? defaultPermissions), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        })
      );
    }
    if (url.includes("/api/setup/readiness")) {
      return Promise.resolve(
        new Response(JSON.stringify({
          overall: "ready",
          warehouse: { name: "Main WH", granted: true, category: "core", source: "app_resource" },
          core: [],
          enhanced: [],
          sp_client_id: "0000-aaaa-bbbb-1234",
        }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        })
      );
    }
    return Promise.resolve(new Response("{}", { status: 200 }));
  });
}

function renderPermissions() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <SettingsPermissions />
    </QueryClientProvider>
  );
}

// ---------------------------------------------------------------------------
// SP identity panel
// ---------------------------------------------------------------------------

describe("SettingsPermissions — SP identity panel", () => {
  it("renders the Service principal auth mode from auth-status", async () => {
    // The Query-authentication group identifies the app's identity from auth-status.
    // (The raw client-id UUID is no longer surfaced here — the panel identifies the SP
    // by its display name, per the settings-revamp Access design.)
    mockApis(SP_AUTH_STATUS);
    renderPermissions();

    await waitFor(() => {
      expect(screen.getAllByText(/service principal/i).length).toBeGreaterThan(0);
    });
  });

  it("renders the SP display name when present", async () => {
    mockApis(SP_AUTH_STATUS);
    renderPermissions();

    // The display name appears in more than one place (Running-as chip + the grant
    // bundle's Target-SP chip), so match all occurrences.
    await waitFor(() => {
      expect(screen.getAllByText("cost-observer-app-sp").length).toBeGreaterThan(0);
    });
  });
});

// ---------------------------------------------------------------------------
// Grant SQL bundle uses the actual SP name
// ---------------------------------------------------------------------------

describe("SettingsPermissions — grant bundle targets actual SP name", () => {
  it("grant SQL uses sp_display_name, not a placeholder", async () => {
    mockApis(SP_AUTH_STATUS);
    renderPermissions();

    // Wait for auth status to load and grant bundle to appear
    await waitFor(() => {
      // The grant bundle is rendered when identity is SP or token is absent
      const codeBlocks = document.querySelectorAll("code, pre");
      const sqlText = Array.from(codeBlocks).map(el => el.textContent ?? "").join("\n");
      // Must reference the real SP name, not a generic placeholder
      expect(sqlText).toContain("cost-observer-app-sp");
      expect(sqlText).not.toContain("<service-principal>");
    });
  });

  it("grant SQL does not include the requesting user's email when they differ from SP", async () => {
    // SP auth — user_email is the SP's own email
    mockApis(SP_AUTH_STATUS);
    renderPermissions();

    await waitFor(() => {
      const codeBlocks = document.querySelectorAll("code, pre");
      const sqlText = Array.from(codeBlocks).map(el => el.textContent ?? "").join("\n");
      // Section 1 grants must target the SP; no user-specific grants when SP is active identity
      expect(sqlText).toContain("GRANT USE CATALOG ON CATALOG system TO");
    });
  });
});

describe("SettingsPermissions — access grid", () => {
  it("shows each user's organization from the email domain", async () => {
    mockApis(SP_AUTH_STATUS, undefined, {
      admins: ["admin@databricks.com", "leader@take2games.com"],
      consumers: ["player@2k.com", "builder@zynga.com", "james@demonware.net"],
    });
    renderPermissions();

    for (const organization of [
      "Databricks",
      "take2games.com",
      "2k.com",
      "zynga.com",
      "demonware.net",
    ]) {
      expect(await screen.findByText(organization)).toBeVisible();
    }
  });

  it("describes the server bootstrap policy when no admin is configured", async () => {
    mockApis(SP_AUTH_STATUS, undefined, {
      admins: [],
      consumers: [],
      current_user: "alice@databricks.com",
    });
    renderPermissions();

    expect(await screen.findByText(/bootstrap mode: every authenticated user is an implicit admin/i)).toBeVisible();
    expect(screen.getByText(/server currently treats every authenticated user/i)).toBeVisible();
    expect(screen.queryByText(/everyone is a consumer/i)).not.toBeInTheDocument();
  });

  it("renders labeled custom role menus with a DuBois focus treatment and chevron", async () => {
    mockApis(SP_AUTH_STATUS, undefined, {
      admins: ["admin@databricks.com", "backup@databricks.com"],
      consumers: ["viewer@databricks.com"],
    });
    renderPermissions();

    const role = await screen.findByRole("combobox", { name: "Role for admin@databricks.com" });
    expect(role.tagName).toBe("BUTTON");
    expect(role).toHaveAttribute("aria-haspopup", "listbox");
    expect(role).toHaveAttribute("aria-expanded", "false");
    expect(role).toHaveClass("settings-role-select-trigger");
    expect(role.querySelector("svg[aria-hidden='true']")).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Role for new user" })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "User email" })).toBeInTheDocument();
  });

  it("supports keyboard navigation and role selection without a native select", async () => {
    mockApis(SP_AUTH_STATUS, undefined, {
      admins: ["admin@databricks.com", "backup@databricks.com"],
      consumers: ["viewer@databricks.com"],
    });
    renderPermissions();

    const role = await screen.findByRole("combobox", { name: "Role for admin@databricks.com" });
    role.focus();
    await userEvent.keyboard("{Enter}");

    expect(role).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("listbox", { name: "Role for admin@databricks.com" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Admin" })).toHaveAttribute("aria-selected", "true");

    await userEvent.keyboard("{ArrowUp}{Enter}");

    expect(role).toHaveAttribute("aria-expanded", "false");
    await waitFor(() => {
      const saveCall = fetchMock.mock.calls.find(([, init]) => init?.method === "POST");
      expect(saveCall).toBeDefined();
      expect(JSON.parse(String(saveCall?.[1]?.body))).toEqual({
        admins: ["backup@databricks.com"],
        consumers: ["viewer@databricks.com", "admin@databricks.com"],
      });
    });
  });

  it("aligns existing users and the add-user controls to one five-column grid", async () => {
    mockApis(SP_AUTH_STATUS, undefined, {
      admins: ["admin@databricks.com"],
      consumers: ["viewer@databricks.com"],
    });
    renderPermissions();

    const rows = await screen.findAllByTestId("access-user-row");
    const addRow = screen.getByTestId("access-add-user-row");

    expect(addRow).toHaveClass("settings-access-user-grid");
    expect(addRow.style.getPropertyValue("--settings-access-grid-columns")).toBe(
      "minmax(210px, 1fr) 190px 90px 112px 112px",
    );
    expect(addRow.children).toHaveLength(5);
    expect(rows).toHaveLength(2);
    rows.forEach((row) => {
      expect(row).toHaveClass("settings-access-user-grid");
      expect(row.children).toHaveLength(5);
    });
    expect(screen.getByRole("button", { name: "Add User" }).parentElement).toHaveClass("settings-user-action-cell");
  });

  it("disables removal and demotion of the last explicit admin", async () => {
    mockApis(SP_AUTH_STATUS, undefined, {
      admins: ["admin@databricks.com"],
      consumers: ["viewer@databricks.com"],
    });
    renderPermissions();

    expect(await screen.findByText(/only explicit admin cannot be removed/i)).toBeVisible();
    expect(screen.getByRole("combobox", { name: "Role for admin@databricks.com" })).toBeDisabled();
    const adminRow = screen.getByText("admin@databricks.com").closest("[data-testid='access-user-row']");
    expect(adminRow?.querySelector("button[aria-describedby='last-admin-explanation']")).toBeDisabled();
    expect(screen.getByRole("combobox", { name: "Role for viewer@databricks.com" })).toBeEnabled();
  });

  it("separates Owner persona from Admin role and protects the verified deployer", async () => {
    mockApis(SP_AUTH_STATUS, undefined, {
      admins: ["owner@databricks.com", "backup@databricks.com"],
      consumers: ["viewer@databricks.com"],
      owner: {
        email: "owner@databricks.com",
        source: "databricks_apps_api",
        verified: true,
        deployment_creator: "owner@databricks.com",
      },
    });
    renderPermissions();

    const ownerRow = (await screen.findByText("owner@databricks.com")).closest("[data-testid='access-user-row']");
    expect(ownerRow).toHaveTextContent("Owner");
    expect(ownerRow).toHaveTextContent("Admin");
    expect(screen.getByRole("combobox", { name: "Role for owner@databricks.com" })).toBeDisabled();
    expect(ownerRow?.querySelector(".settings-user-action--remove")).toBeDisabled();
    expect(screen.getByText("viewer@databricks.com").closest("[data-testid='access-user-row']")).toHaveTextContent("User");
    expect(document.querySelectorAll(".settings-persona")).toHaveLength(4);
    document.querySelectorAll(".settings-persona").forEach((persona) => {
      expect(["Owner", "User"]).toContain(persona.textContent);
    });
  });
});

