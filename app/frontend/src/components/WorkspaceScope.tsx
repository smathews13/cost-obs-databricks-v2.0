import { useEffect } from "react";
import { Spinner } from "./Spinner";
import { VirtualizedList } from "./VirtualizedList";
import { useWorkspaceScopeEditor } from "@/hooks/useWorkspaceScopeEditor";
import type { ScopeWorkspace } from "@/hooks/useWorkspaceScopeEditor";

export function WorkspaceScopePicker({
  loading,
  workspaces,
  selectedIds,
  onToggle,
  onSelectAll,
  onClearAll,
  saved,
  savedIds,
  poolError,
  onRetry,
  intro,
  emptyHint,
  poolErrorHint,
}: {
  loading: boolean;
  workspaces: ScopeWorkspace[];
  selectedIds: Set<string>;
  onToggle: (id: string) => void;
  onSelectAll: () => void;
  onClearAll: () => void;
  saved: boolean;
  savedIds: string[];
  poolError: string | null;
  onRetry: () => void;
  intro: string;
  emptyHint: React.ReactNode;
  poolErrorHint: string;
}) {
  if (loading) {
    return (
      <div className="flex flex-col items-center justify-center py-12">
        <Spinner size="md" />
        <p className="mt-3 text-sm text-gray-500">Loading workspaces...</p>
      </div>
    );
  }

  if (poolError) {
    return (
      <div className="space-y-3 rounded-lg border border-red-200 bg-red-50 px-4 py-3">
        <p className="text-sm font-medium text-red-800">{poolError}</p>
        <p className="text-xs text-red-700">{poolErrorHint}</p>
        <button onClick={onRetry} className="text-sm font-medium hover:underline" style={{ color: '#FF3621' }}>
          Retry
        </button>
      </div>
    );
  }

  const nameOf = (id: string) => workspaces.find((w) => w.id === id)?.name || id;
  const currentScope = savedIds.length === 0
    ? "All workspaces"
    : savedIds.length === 1
    ? nameOf(savedIds[0])
    : `${savedIds.length} workspaces`;

  return (
    <div className="space-y-4">
      <p className="text-sm text-gray-600">{intro}</p>
      <div className="rounded-lg border border-gray-200 bg-gray-50 px-4 py-2.5 text-xs text-gray-600">
        Currently scoped to: <span className="font-medium text-gray-800">{currentScope}</span>
      </div>

      {workspaces.length === 0 ? (
        <div className="rounded-lg bg-amber-50 p-3 text-sm text-amber-700">{emptyHint}</div>
      ) : (
        <>
          <div className="flex items-center justify-between">
            <span className="text-xs text-gray-500">
              {selectedIds.size === 0 || selectedIds.size === workspaces.length ? "All workspaces will be shown" : `${selectedIds.size} of ${workspaces.length} selected`}
            </span>
            <div className="flex gap-3">
              <button onClick={onSelectAll} className="text-xs font-medium hover:underline" style={{ color: '#FF3621' }}>Select All</button>
              <button onClick={onClearAll} className="text-xs font-medium hover:underline" style={{ color: '#FF3621' }}>Clear All</button>
            </div>
          </div>

          <div className="rounded-lg border border-gray-200 p-2">
            <VirtualizedList
              items={workspaces}
              itemHeight={48}
              maxHeight={208}
              getKey={(ws) => ws.id}
              renderItem={(ws) => {
                const checked = selectedIds.has(ws.id);
                return (
                  <label className={`flex cursor-pointer items-center gap-3 rounded-md px-3 py-2 transition-colors ${checked ? "bg-orange-50" : "hover:bg-gray-50"}`}>
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() => onToggle(ws.id)}
                      className="h-3.5 w-3.5 rounded border-gray-300 text-orange-500 focus:ring-orange-500"
                    />
                    <div className="min-w-0">
                      <div className="flex items-center gap-1.5">
                        <span className="text-sm font-medium text-gray-800 truncate">{ws.name}</span>
                        {ws.historical && (
                          <span className="shrink-0 rounded-full border border-amber-200 bg-amber-50 px-1.5 py-0.5 text-[10px] font-medium text-amber-700" title="This workspace no longer exists in the account — data is historical.">
                            historical
                          </span>
                        )}
                      </div>
                      <div className="text-xs font-mono text-gray-500">{ws.id}</div>
                    </div>
                  </label>
                );
              }}
            />
          </div>

          {selectedIds.size === 0 && (
            <p className="text-xs text-gray-500">
              No workspaces selected — all workspaces will be visible in the dashboard. Select specific workspaces to restrict the data shown.
            </p>
          )}
        </>
      )}

      {saved && (
        <div className="rounded-lg border border-green-200 bg-green-50 px-4 py-2.5">
          <p className="text-xs font-medium text-green-800">Workspace scope saved. It persists across redeploys.</p>
        </div>
      )}
    </div>
  );
}

/** Settings page: admins change which workspaces the whole app reports on. */
export function WorkspaceScopeSection() {
  const scope = useWorkspaceScopeEditor();
  const { load } = scope;
  useEffect(() => { load(); }, [load]);

  const { selectedIds, workspaces } = scope;
  const allSelected = selectedIds.size === 0 || selectedIds.size === workspaces.length;
  const unchanged = scope.savedIds.length === 0
    ? allSelected
    : !allSelected && selectedIds.size === scope.savedIds.length && scope.savedIds.every((id) => selectedIds.has(id));

  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-base font-semibold text-gray-900">Workspace scope</h3>
        <p className="mt-1 text-xs text-gray-500">Admins only. Applies to every user and every tab.</p>
      </div>
      <WorkspaceScopePicker
        loading={scope.loading}
        workspaces={workspaces}
        selectedIds={selectedIds}
        onToggle={scope.toggle}
        onSelectAll={scope.selectAll}
        onClearAll={scope.clearAll}
        saved={scope.saved}
        savedIds={scope.savedIds}
        poolError={scope.poolError}
        onRetry={load}
        intro="Choose which workspaces this app shows data for. Tabs reload with the new scope as soon as you save."
        emptyHint="No workspaces with billing activity were found. Check that the app can read system.billing.usage, then reload this page."
        poolErrorHint="Saving is disabled so the current scope isn't replaced by mistake. Retry once the problem is fixed."
      />
      {scope.saveError && (
        <div className="rounded-lg border border-red-200 bg-red-50 px-4 py-2.5 text-sm text-red-800">{scope.saveError}</div>
      )}
      {!scope.loading && !scope.poolError && workspaces.length > 0 && (
        <div className="flex justify-end">
          <button
            onClick={() => { scope.save(); }}
            disabled={scope.saving || unchanged}
            className="btn-brand rounded-lg px-5 py-2 text-sm font-bold text-white transition-colors disabled:opacity-50"
          >
            {scope.saving ? "Saving…" : allSelected ? "Save (all workspaces)" : `Save (${selectedIds.size} workspace${selectedIds.size !== 1 ? "s" : ""})`}
          </button>
        </div>
      )}
    </div>
  );
}
