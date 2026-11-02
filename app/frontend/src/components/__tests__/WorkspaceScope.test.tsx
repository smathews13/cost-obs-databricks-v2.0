import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { WorkspaceScopeSection } from "../WorkspaceScope";

const WORKSPACES = [
  { id: "111", name: "data-eng" },
  { id: "222", name: "sports-prod" },
];

function stubApi(saved: string[], saveStatus = 200) {
  const posts: unknown[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (url === "/api/setup/workspace-filter") return new Response(JSON.stringify({ workspace_ids: saved }));
    if (url === "/api/setup/list-workspaces") return new Response(JSON.stringify({ workspaces: WORKSPACES }));
    if (url === "/api/setup/save-workspace-filter") {
      posts.push(JSON.parse(String(init?.body)));
      return new Response(JSON.stringify(saveStatus === 200 ? { saved: [] } : { detail: "Admin access required" }), { status: saveStatus });
    }
    throw new Error(`unexpected fetch ${url}`);
  }));
  return posts;
}

function renderSection() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><WorkspaceScopeSection /></QueryClientProvider>);
}

afterEach(() => vi.unstubAllGlobals());

describe("WorkspaceScopeSection", () => {
  it("narrows an all-workspaces scope to the one the admin keeps", async () => {
    const posts = stubApi([]);
    renderSection();
    expect(await screen.findByText("All workspaces")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Save \(all workspaces\)/ })).toBeDisabled();

    await userEvent.click(screen.getByRole("checkbox", { name: /data-eng/ }));
    await userEvent.click(screen.getByRole("button", { name: /Save \(1 workspace\)/ }));

    await waitFor(() => expect(posts).toEqual([{ workspace_ids: ["222"] }]));
    expect(await screen.findByText(/Workspace scope saved/)).toBeInTheDocument();
  });

  it("widens a saved scope back to all workspaces by sending an empty pool", async () => {
    const posts = stubApi(["222"]);
    renderSection();
    await userEvent.click(await screen.findByRole("button", { name: "Select All" }));
    await userEvent.click(screen.getByRole("button", { name: /Save \(all workspaces\)/ }));
    await waitFor(() => expect(posts).toEqual([{ workspace_ids: [] }]));
  });

  it("shows the server's refusal instead of claiming the scope saved", async () => {
    stubApi([], 403);
    renderSection();
    await userEvent.click(await screen.findByRole("checkbox", { name: /data-eng/ }));
    await userEvent.click(screen.getByRole("button", { name: /Save \(1 workspace\)/ }));
    expect(await screen.findByText("Admin access required")).toBeInTheDocument();
    expect(screen.queryByText(/Workspace scope saved/)).not.toBeInTheDocument();
  });
});
