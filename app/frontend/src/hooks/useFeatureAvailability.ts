import { useQuery } from "@tanstack/react-query";
import { normalizeReadinessResult } from "@/components/settings/readiness";
import type { ReadinessCheck } from "@/components/settings/ReadinessChecks";

/** Shared query key — import this whenever you need to invalidate readiness. */
export const READINESS_QUERY_KEY = ["setup-readiness"] as const;

export interface FeatureAvailability {
  /** Whether the warehouse is granted. undefined = not yet loaded. */
  warehouseGranted: boolean | undefined;
  /**
   * Returns the grant state of a specific system table.
   * - true  = explicitly granted
   * - false = explicitly denied
   * - undefined = not yet checked / unknown
   */
  tableGranted: (table: string) => boolean | undefined;
  /** True once the readiness response has been received (even if it failed). */
  isLoaded: boolean;
}

/**
 * Shared hook for per-feature availability derived from /api/setup/readiness.
 *
 * Only marks a feature unavailable when a dependency is **explicitly denied**
 * (false). Unknown state (undefined) never blocks rendering — it just means
 * we haven't confirmed availability yet. Use isLoaded to distinguish the two.
 *
 * TanStack Query caches under READINESS_QUERY_KEY so this is safe to call
 * from multiple components without causing duplicate network requests.
 */
export function useFeatureAvailability(): FeatureAvailability {
  const { data: readiness, isFetched } = useQuery({
    queryKey: READINESS_QUERY_KEY,
    queryFn: () =>
      fetch("/api/setup/readiness")
        .then(r => r.ok ? r.json() : null)
        .then(normalizeReadinessResult)
        .catch(() => null),
    staleTime: 5 * 60 * 1000,
    retry: false,
    refetchOnWindowFocus: false,
  });

  const allChecks: ReadinessCheck[] = [
    ...(readiness?.core ?? []),
    ...(readiness?.enhanced ?? []),
  ];
  const tableGrantedMap = new Map<string, boolean>(
    allChecks.filter(c => c.table).map(c => [c.table!, c.granted])
  );

  return {
    warehouseGranted: readiness ? readiness.warehouse.granted : undefined,
    tableGranted: (table: string) => tableGrantedMap.get(table),
    isLoaded: isFetched,
  };
}
