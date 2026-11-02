import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { SKUBreakdownResponse, WorkspaceBreakdown } from "@/types/billing";
import { SKUBreakdown } from "../SKUBreakdown";

const workspaces: WorkspaceBreakdown[] = [
  {
    workspace_id: "ws-1",
    workspace_name: "Sports Prod",
    total_dbus: 1,
    total_spend: 1,
    percentage: 100,
    top_products: [],
    top_users: [],
  },
];

const response = (product: string | null): SKUBreakdownResponse => ({
  skus: product
    ? [{ product, workspaces_using: 1, total_dbus: 1, total_spend: 10, percentage: 100 }]
    : [],
  total_spend: product ? 10 : 0,
  start_date: "",
  end_date: "",
});

type Pending = { signal?: AbortSignal | null; resolve: (body: SKUBreakdownResponse) => void };

describe("SKUBreakdown workspace fetch", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("ignores a slower response from a superseded date range", async () => {
    const pending: Pending[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((_url: string, init?: RequestInit) =>
        new Promise((resolve) => {
          pending.push({
            signal: init?.signal,
            resolve: (body) => resolve({ ok: true, json: () => Promise.resolve(body) }),
          });
        }),
      ),
    );

    const view = (startDate: string) => (
      <SKUBreakdown
        data={undefined}
        isLoading={false}
        workspaces={workspaces}
        dateRange={{ startDate, endDate: "2026-10-31" }}
      />
    );
    const { rerender } = render(view("2026-10-01"));
    expect(pending).toHaveLength(1);

    rerender(view("2026-10-15"));
    expect(pending).toHaveLength(2);
    await act(async () => pending[1].resolve(response(null)));
    expect(screen.getByText("No SKU data available")).toBeInTheDocument();

    await act(async () => pending[0].resolve(response("PREMIUM_JOBS_COMPUTE")));
    expect(screen.getByText("No SKU data available")).toBeInTheDocument();
    expect(pending[0].signal?.aborted).toBe(true);
  });
});
