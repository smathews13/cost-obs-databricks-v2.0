import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { AWSCostsResponse } from "@/types/billing";
import { CloudCostsView } from "../CloudCostsView";

const loadedData: AWSCostsResponse = {
  clusters: [
    {
      cluster_id: "c-1",
      cluster_name: "etl-nightly",
      driver_instance_type: "m5.xlarge",
      worker_instance_type: "m5.xlarge",
      cluster_source: "JOB",
      workspace_id: "ws-1",
      workspace_name: "Prod",
      state: null,
      total_dbu_hours: 12,
      estimated_aws_cost: 34,
      days_active: 3,
      percentage: 100,
    },
  ],
  instance_families: [{ instance_family: "m5", total_dbu_hours: 12, days_active: 3 }],
  total_estimated_cost: 34,
  total_dbu_hours: 12,
  start_date: "2026-10-01",
  end_date: "2026-10-31",
};

describe("CloudCostsView hook order", () => {
  it("renders cluster data after an initial loading render", () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const view = (isLoading: boolean, data: AWSCostsResponse | undefined) => (
      <QueryClientProvider client={client}>
        <CloudCostsView
          data={data}
          isLoading={isLoading}
          timeseriesData={undefined}
          timeseriesLoading={false}
          host={null}
        />
      </QueryClientProvider>
    );

    const { rerender } = render(view(true, undefined));
    expect(screen.getByText("Loading cloud costs...")).toBeInTheDocument();

    rerender(view(false, loadedData));
    expect(screen.getAllByText("etl-nightly").length).toBeGreaterThan(0);
  });
});
