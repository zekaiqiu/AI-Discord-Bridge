/**
 * Tiny cross-component notification bus, built on DOM CustomEvents.
 *
 * Why not React context: the only consumers are leaf components that are
 * deliberately self-contained (ScheduleIndicator polls its own data and is
 * rendered deep inside Composer, which is rendered per-pane by ChatPane).
 * Threading a "schedules changed" callback down three prop layers — through
 * components that otherwise know nothing about wakes — would be more coupling
 * than the feature warrants. `window` is already the shared object, and the
 * listener lifecycle matches the component's own useEffect exactly.
 *
 * Events are per-session: a wake armed in session A must not make session B's
 * composer refetch, so the session id rides on `detail` and subscribers filter.
 */

export const SCHEDULES_UPDATED = "chat:schedules-updated";
export const MEMORY_UPDATED = "chat:memory-updated";
/**
 * A send failed before the server ever saw it, and App has written the
 * user's text back into the draft store. The Composer owns its draft as
 * local state (typing must not re-render the App tree), so it can't notice a
 * write it didn't make — without this it would keep showing an empty box
 * while the recovered text sat in localStorage until the next reload.
 */
export const DRAFT_RESTORED = "chat:draft-restored";

/** Announce that a session's scheduled wakes changed server-side. */
export function emitSessionEvent(name: string, sessionId: string): void {
  try {
    window.dispatchEvent(new CustomEvent(name, { detail: { sessionId } }));
  } catch {
    /* CustomEvent unsupported — subscribers just fall back to polling */
  }
}

/**
 * Subscribe to a session-scoped event. Fires `handler` only for `sessionId`.
 * Returns an unsubscribe function suitable for a useEffect cleanup.
 */
export function onSessionEvent(
  name: string,
  sessionId: string,
  handler: () => void,
): () => void {
  const listener = (e: Event) => {
    const detail = (e as CustomEvent<{ sessionId?: string }>).detail;
    if (detail?.sessionId === sessionId) handler();
  };
  window.addEventListener(name, listener);
  return () => window.removeEventListener(name, listener);
}
