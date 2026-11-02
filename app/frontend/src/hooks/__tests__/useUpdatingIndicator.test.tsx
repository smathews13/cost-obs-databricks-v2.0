import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { useUpdatingIndicator } from "../useUpdatingIndicator";

function setup(maxMs?: number) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  const hook = renderHook(() => useUpdatingIndicator(maxMs), { wrapper });
  return { queryClient, ...hook };
}

function deferred() {
  let resolve!: (v: string) => void;
  const promise = new Promise<string>((r) => { resolve = r; });
  return { promise, resolve };
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

describe("useUpdatingIndicator", () => {
  it("starts idle", () => {
    const { result } = setup();
    expect(result.current.updating).toBe(false);
  });

  it("clears after the grace period when no fetch starts", async () => {
    const { result } = setup();
    act(() => result.current.arm());
    expect(result.current.updating).toBe(true);
    await waitFor(() => expect(result.current.updating).toBe(false), { timeout: 1500 });
  });

  it("stays on while a fetch is in flight and clears once it settles", async () => {
    const { result, queryClient } = setup();
    const d = deferred();
    act(() => result.current.arm());
    act(() => { void queryClient.fetchQuery({ queryKey: ["k"], queryFn: () => d.promise }); });

    await act(() => sleep(700));
    expect(result.current.updating).toBe(true);

    await act(async () => { d.resolve("done"); await d.promise; });
    await waitFor(() => expect(result.current.updating).toBe(false));
  });

  it("treats a fetch already in flight at arm time as the cycle to wait for", async () => {
    const { result, queryClient } = setup();
    const d = deferred();
    act(() => { void queryClient.fetchQuery({ queryKey: ["k"], queryFn: () => d.promise }); });
    act(() => result.current.arm());

    await act(() => sleep(700));
    expect(result.current.updating).toBe(true);

    await act(async () => { d.resolve("done"); await d.promise; });
    await waitFor(() => expect(result.current.updating).toBe(false));
  });

  it("clears on the safety timeout if a fetch never settles", async () => {
    const { result, queryClient } = setup(300);
    act(() => result.current.arm());
    act(() => { void queryClient.fetchQuery({ queryKey: ["k"], queryFn: () => new Promise<string>(() => {}) }); });
    expect(result.current.updating).toBe(true);
    await waitFor(() => expect(result.current.updating).toBe(false), { timeout: 1500 });
  });
});
