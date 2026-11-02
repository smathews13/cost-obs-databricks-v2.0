// Module-level so the debugger's run state survives tab switches (useState resets on unmount).
export const debuggerRunState = { runKey: 0, hasRun: false };

/** Reset persisted run state. Used only in tests to prevent cross-test pollution. */
export function _resetDebuggerState() {
  debuggerRunState.runKey = 0;
  debuggerRunState.hasRun = false;
}
