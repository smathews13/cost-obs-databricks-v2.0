import { useCallback, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

export type ScopeWorkspace = { id: string; name: string; historical?: boolean };

const SCOPE_INDEPENDENT_QUERIES = new Set([
  "user", "user-permissions", "unified-settings", "app-config", "warehouses",
  "cloud-provider", "cloud-connections", "health", "debug-run", "mv-sources",
]);

export function isScopeIndependentQuery(head: string): boolean {
  return SCOPE_INDEPENDENT_QUERIES.has(head) || head.startsWith("settings-") || head.startsWith("setup-");
}

/** Load and save the app-wide workspace pool (admin only on the server). */
export function useWorkspaceScopeEditor() {
  const queryClient = useQueryClient();
  const [loading, setLoading] = useState(false);
  const [workspaces, setWorkspaces] = useState<ScopeWorkspace[]>([]);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [saved, setSaved] = useState(false);
  const [saving, setSaving] = useState(false);
  // The pool saved last time. [] = all workspaces.
  const [savedIds, setSavedIds] = useState<string[]>([]);
  // Set when the saved pool or the workspace list couldn't be loaded. Saving is
  // blocked then: the selection would be empty or "all", silently widening the pool.
  const [poolError, setPoolError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setSaved(false);
    setPoolError(null);
    setSaveError(null);
    try {
      const [filterRes, listRes] = await Promise.all([
        fetch("/api/setup/workspace-filter").catch(() => null),
        fetch("/api/setup/list-workspaces").catch(() => null),
      ]);
      if (!filterRes?.ok) {
        const body = await filterRes?.json().catch(() => ({}));
        setPoolError(body?.detail || "Could not read the saved workspace scope.");
        return;
      }
      const savedPool: string[] = (await filterRes.json()).workspace_ids ?? [];
      setSavedIds(savedPool);
      // Without the list nothing is selectable, and saving an empty selection means
      // "all workspaces", so this blocks saving the same way an unreadable pool does.
      if (!listRes?.ok) {
        const body = await listRes?.json().catch(() => ({}));
        setPoolError(body?.detail || "Could not load the workspace list.");
        return;
      }
      const data = await listRes.json();
      const list: ScopeWorkspace[] = data.workspaces ?? [];
      // Keep saved ids the list didn't return selectable, so saving again can't drop them silently.
      const missing = savedPool.filter((id) => !list.some((w) => w.id === id));
      setWorkspaces([...list, ...missing.map((id) => ({ id, name: id, historical: true }))]);
      // Start from the saved pool; with none saved, everything is selected.
      setSelectedIds(new Set(savedPool.length > 0 ? savedPool : list.map((w) => w.id)));
    } catch (e) {
      setPoolError(`Could not load the workspaces: ${e}`);
    } finally {
      setLoading(false);
    }
  }, []);

  // Returns false (and sets saveError) when the save failed.
  const save = async (): Promise<boolean> => {
    if (poolError) return false;
    // Every workspace selected means "no filter". Sending the explicit list would lock
    // the pool to today's workspaces and hide any created later.
    const ids = selectedIds.size === workspaces.length ? [] : Array.from(selectedIds);
    setSaveError(null);
    setSaving(true);
    try {
      const res = await fetch("/api/setup/save-workspace-filter", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workspace_ids: ids }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        setSaveError(body.detail || `Could not save the workspace filter (HTTP ${res.status})`);
        return false;
      }
      setSaved(true);
      setSavedIds(ids);
      // Every cached tab payload belongs to the previous scope. Reset rather than
      // invalidate so tabs show their loading state, not the old numbers, while
      // they refetch. Identity and settings queries don't depend on the scope;
      // resetting them would blank the dialog this editor runs in.
      await queryClient.resetQueries({
        predicate: (q) => !isScopeIndependentQuery(String(q.queryKey[0] ?? "")),
      });
      queryClient.invalidateQueries({ queryKey: ["setup-workspace-filter"] });
      return true;
    } catch (e) {
      setSaveError(`Could not save the workspace filter: ${e}`);
      return false;
    } finally {
      setSaving(false);
    }
  };

  const toggle = (id: string) => {
    setSaved(false);
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };
  const selectAll = () => { setSaved(false); setSelectedIds(new Set(workspaces.map((w) => w.id))); };
  const clearAll = () => { setSaved(false); setSelectedIds(new Set()); };

  return {
    loading, workspaces, selectedIds, saved, saving, savedIds, poolError, saveError,
    load, save, toggle, selectAll, clearAll,
  };
}
