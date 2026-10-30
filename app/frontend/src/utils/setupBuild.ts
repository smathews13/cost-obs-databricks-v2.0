/** USE + MANAGE grants Create Tables gives the installing user on the app catalog. */
export interface SetupUserGrants {
  ok: boolean;
  grantee: string;
  applied: string[];
  failed: { sql: string; error: string }[];
  error?: string;
  manual_sql: string;
}

export interface SetupBuildTask {
  status: string;
  error: string | null;
  table_progress?: Record<string, string>;
  table_errors?: Record<string, string>;
  /** Build outcome per table: created / refreshed / skipped… / ensured / error… */
  table_results?: Record<string, string>;
  user_grants?: SetupUserGrants | null;
}

export type SetupBuildOutcome =
  | { kind: "done" }
  | { kind: "interrupted" }
  | { kind: "running" }
  /** `serverBusy`: the server is still finishing tables already in flight, so a retry would be refused. */
  | { kind: "failed"; message: string; serverBusy: boolean }
  | { kind: "lost"; message: string };

/** Consecutive status polls that may fail before the wizard stops waiting. */
export const MAX_FAILED_POLLS = 6;

/**
 * What one status poll means for the Create Tables step.
 *
 * A single failed table ends the wait: the server stops at the first failure,
 * but it still finishes the tables already running, and the wizard should not
 * spin through that.
 */
export function setupBuildOutcome(task: SetupBuildTask | null | undefined, failedPolls: number): SetupBuildOutcome {
  if (!task) {
    return failedPolls >= MAX_FAILED_POLLS
      ? {
          kind: "lost",
          message:
            "Lost contact with the app while the tables were being created. Check that the app is running, then retry.",
        }
      : { kind: "running" };
  }
  const progress = task.table_progress ?? {};
  const states = Object.values(progress);
  if (task.status === "done" || (states.length > 0 && states.every((s) => s === "done"))) return { kind: "done" };
  if (task.status === "interrupted") return { kind: "interrupted" };
  const failedTable = Object.keys(progress).find((name) => progress[name] === "error");
  if (task.status === "error" || failedTable) {
    const reason = failedTable ? task.table_errors?.[failedTable] : undefined;
    const message =
      task.status === "error" && task.error
        ? task.error
        : failedTable
          ? `Creating \`${failedTable}\` failed${reason ? `: ${reason}` : "."}`
          : "Table creation failed. Check the app logs for details.";
    return { kind: "failed", message, serverBusy: task.status === "running" };
  }
  return { kind: "running" };
}

/** A grant the wizard can write for the user: CREATE SCHEMA / USE CATALOG with the SQL attached. */
export function isGrantableCatalogError(message: string): boolean {
  return /needs.*CREATE SCHEMA|needs.*USE CATALOG|GRANT USE CATALOG|GRANT CREATE SCHEMA/i.test(message);
}

/** Any other access failure, which the Permissions step can check and explain. */
export function isAccessError(message: string): boolean {
  return /INSUFFICIENT_PERMISSIONS|PERMISSION_DENIED|does not have .*(privilege|permission|SELECT|USE)|not authori[sz]ed|forbidden|\b403\b/i.test(
    message,
  );
}
