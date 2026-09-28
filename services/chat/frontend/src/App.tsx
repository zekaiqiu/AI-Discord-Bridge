/**
 * Top-level chat layout + state machine.
 *
 * UI state is a back-stack:
 *   - "minimized-sidebar" — sidebar collapsed (icons only)
 *   - "expanded-sidebar"  — sidebar fully visible
 *   - "dashboard:agents" / "dashboard:usage" — full-pane overlay covering the
 *     sidebar region; chat thread on the right stays visible.
 *
 * The chevron pops the stack (or, when at the bottom of the stack on
 * "expanded-sidebar", replaces with "minimized-sidebar"; from
 * "minimized-sidebar" it expands).
 */

import { Suspense, lazy, useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  AttachmentMeta,
  DEFAULT_USER_SETTINGS,
  EFFORT_LEVELS,
  LanguageChoice,
  MeResponse,
  Message,
  ModelChoice,
  SearchResult,
  Session,
  SessionSummary,
  StreamEvent,
  UserSettings,
  Workspace,
  apiErrorMessage,
  attachStream,
  cancelTurn,
  createSession,
  deleteSession,
  forkSession,
  getMe,
  getSession,
  getSessionHead,
  getSessions,
  renameSession,
  searchSessions,
  streamMessage,
  updateSession,
  uploadAttachments,
} from "./api";
import { mergePreservingLiveTail } from "./messageMerge";
import { Sidebar, SidebarFilter, UiState } from "./components/Sidebar";
import { ChatPane } from "./components/ChatPane";
import { LoginGate } from "./components/LoginGate";
import { clearDraft, getDraft, setDraft } from "./draftStore";
import {
  DRAFT_RESTORED,
  MEMORY_UPDATED,
  SCHEDULES_UPDATED,
  emitSessionEvent,
} from "./events";
import {
  MAX_FILES_PER_TURN,
  exceedsTextBudget,
  makeTextFile,
  maxMessageBytes,
  setMaxMessageBytes,
} from "./limits";
import { LanguageContext } from "./i18n";

// The shared wizerith top bar sets <html lang="…"> to the user's site-wide
// display language. Read it back, narrowed to the languages we ship.
function readDocLang(): LanguageChoice | null {
  const l =
    typeof document !== "undefined"
      ? document.documentElement.getAttribute("lang")
      : null;
  return l === "en" || l === "zh-CN" || l === "zh-TW" ? l : null;
}

// Bumped to ".v2" when the default moved 4.8 -> Opus 5 (the "opus5" key now runs
// Opus 5.5 since 2026-09-23; same key, so no further bump): the old key holds each
// browser's last-used model, which would otherwise pin returning users to the
// previous default. A new key makes every client fall through to loadModelPref().
const MODEL_PREF_KEY = "chat.model.v2";
// Per-model reasoning-effort preferences: { [modelAlias]: level }, e.g.
// { glm: "max", kimi: "low" }. Stored per model so switching models keeps
// each model's own level (mirrors the Discord bridge's effort map).
const EFFORT_PREF_KEY = "chat.effort.v1";
const LAST_SESSION_KEY = "chat.lastSession";
// Multi-window: persisted layout of open panes (session ids + stable
// pane keys) and the focused pane, so a refresh restores the same set of
// side-by-side chat windows the user had open.
const PANES_KEY = "chat.panes";
const FOCUSED_PANE_KEY = "chat.focusedPane";
const MAX_PANES = 4;
/** How often an idle, visible tab re-checks open sessions for turns it did
 *  not start — i.e. scheduled wakes. Deliberately coarse: the fetch is one
 *  small JSON per open pane, and it only has to beat "the user notices
 *  nothing happened", not stream latency (re-attach takes over from there). */
const IDLE_RESYNC_MS = 15000;
/** How long a send keeps retrying through 409 ("another turn is already in
 *  flight for this session") before giving the message back to the user. */
const SEND_CONFLICT_DEADLINE_MS = 45000;
/** How long a 409 is treated as the previous turn's teardown handoff before
 *  it's treated as a foreign turn holding the session (wake / other tab) and
 *  escalated to a cancel. */
const SEND_CONFLICT_ESCALATE_MS = 2000;
const SEND_CONFLICT_RETRY_MS = 250;
// Default workspace for the next "+ New Chat". Persisted in localStorage
// so a user who toggled to "shared" yesterday lands back in shared mode
// today. Old sessions remain pinned to whatever workspace they were
// created in (locked at create time on the backend).
const WORKSPACE_PREF_KEY = "chat.workspace";
const WORKSPACE_COOKIE_NAME = "chat_workspace";

// One-shot migration: clear keys/cookies from a previous tenant-prefixed
// naming scheme so they don't sit in DevTools forever. Safe to remove
// after a couple of months of all users having loaded the new bundle.
function migrateLegacyTenantNames(): void {
  try {
    localStorage.removeItem("ald3.chat.model");
    localStorage.removeItem("ald3.chat.lastSession");
    localStorage.removeItem("wizerith.chat.workspace");
  } catch {
    /* ignore */
  }
  try {
    const host = window.location.hostname;
    const apex = host.startsWith("chat.") ? host.substring(5) : host;
    document.cookie = `wizerith_workspace=; Path=/; Domain=.${apex}; Max-Age=0; SameSite=Lax`;
  } catch {
    /* ignore */
  }
}
migrateLegacyTenantNames();

function loadWorkspacePref(): Workspace {
  // On wizerith hosts the shared topbar (`/_theme/wizerith-theme.js`) owns the
  // workspace selection and persists it as `wizerith.workspace` (admins are
  // pinned to "admin"). It is the source of truth, so read it FIRST — the boot
  // effect's `getSessions(workspace)` + saved-pane validation must run against
  // the right bucket. Previously chat only adopted the topbar value in a
  // post-mount effect, which RACED the boot effect: an admin booted to
  // "personal", `getSessions("personal")` excluded their admin sessions, and
  // the restored panes were nulled to empty new-chat views — so a refresh
  // looked like it opened brand-new sessions. Fall back to chat's own key,
  // then "personal".
  try {
    const host = typeof window !== "undefined" ? window.location.hostname : "";
    const onWizerith = host === "wizerith.ai" || host.endsWith(".wizerith.ai");
    if (onWizerith) {
      const top = localStorage.getItem("wizerith.workspace");
      if (top === "shared" || top === "personal" || top === "admin") return top;
    }
  } catch {
    /* ignore */
  }
  try {
    const raw = localStorage.getItem(WORKSPACE_PREF_KEY);
    if (raw === "shared" || raw === "personal" || raw === "admin") return raw;
  } catch {
    /* ignore */
  }
  return "personal";
}

function saveWorkspacePref(value: Workspace): void {
  try {
    localStorage.setItem(WORKSPACE_PREF_KEY, value);
  } catch {
    /* ignore */
  }
  // Also write a cookie so cross-subdomain services (term-router, the
  // dev IDE) can read the workspace selection without their own UI
  // plumbing. Cookie domain = current apex so all subdomains of this
  // tenant see it; SameSite=Lax so it survives same-site navigations
  // from the chat deep-link buttons. The cookie does NOT cross between
  // tenants — each tenant's apex gets its own copy.
  try {
    const host = window.location.hostname;
    // Drop a leading "chat." so the cookie lands on the apex and is
    // visible to dev.<apex> / term.<apex>. Two-part hostnames stay as-is.
    const apex = host.startsWith("chat.") ? host.substring(5) : host;
    document.cookie = `${WORKSPACE_COOKIE_NAME}=${value}; Path=/; Domain=.${apex}; Max-Age=${60 * 60 * 24 * 365}; SameSite=Lax`;
  } catch {
    /* ignore — cookie-write failure is non-fatal, terminal will fall back to "personal" */
  }
}

// The served lineup (Anthropic models were removed 2026-09-28; glm/kimi
// lead). Any stored value outside this set — including the removed
// claude aliases and "default" — falls back to "glm" so a returning
// browser never pins itself to a model the picker no longer offers.
const SERVED_MODELS: ModelChoice[] = [
  "glm",
  "kimi",
  "qwen",
  "deepseek",
  "minimax",
  "gemma4-local",
];

function loadModelPref(): ModelChoice {
  try {
    const raw = localStorage.getItem(MODEL_PREF_KEY) as ModelChoice | null;
    if (raw && SERVED_MODELS.includes(raw)) {
      return raw;
    }
  } catch {
    /* localStorage unavailable; fall through */
  }
  return "glm";
}

function loadEffortMap(): Record<string, string> {
  try {
    const raw = localStorage.getItem(EFFORT_PREF_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        return parsed as Record<string, string>;
      }
    }
  } catch {
    /* corrupt / unavailable; fall through */
  }
  return {};
}


function loadLastSessionId(): string | null {
  try {
    return localStorage.getItem(LAST_SESSION_KEY);
  } catch {
    return null;
  }
}

function saveLastSessionId(id: string | null): void {
  try {
    if (id) localStorage.setItem(LAST_SESSION_KEY, id);
    else localStorage.removeItem(LAST_SESSION_KEY);
  } catch {
    /* ignore */
  }
}

// ---- multi-window panes -------------------------------------------------
// A "chat window" rendered side-by-side in the main area. `sessionId` is
// null for a freshly-opened window that hasn't sent its first message yet
// (the session is lazily created on first send, mirroring ?new=1).
interface Pane {
  key: string;
  sessionId: string | null;
}

let __paneSeq = 0;
function newPaneKey(): string {
  __paneSeq += 1;
  return `pane-${Date.now().toString(36)}-${__paneSeq.toString(36)}`;
}

// The localStorage key under which a pane's draft is stored. Session-bound
// once the session exists so the draft survives the window being closed
// and the session re-opened elsewhere; pane-bound before that.
function draftKeyForPane(p: Pane): string {
  return p.sessionId ? `s:${p.sessionId}` : `pane:${p.key}`;
}

/**
 * Hand a failed send's text back to the composer for the session it was
 * meant for. Only called when the server never accepted the message, so
 * there is nothing to be confused with a real turn.
 *
 * Appends rather than overwrites: the user may well have started typing the
 * next thing during the seconds the doomed send was in flight, and silently
 * replacing that would be a second lost message.
 */
function restoreDraft(sessionId: string, text: string): void {
  const trimmed = text.trim();
  if (!trimmed) return;
  const key = `s:${sessionId}`;
  const existing = getDraft(key);
  if (existing.includes(trimmed)) return; // already restored (double failure)
  setDraft(key, existing ? `${text}\n\n${existing}` : text);
  emitSessionEvent(DRAFT_RESTORED, sessionId);
}

function loadPanes(): { panes: Pane[]; focused: string | null } | null {
  try {
    const raw = localStorage.getItem(PANES_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed) || parsed.length === 0) return null;
    const panes: Pane[] = [];
    for (const p of parsed) {
      if (p && typeof p.key === "string") {
        panes.push({ key: p.key, sessionId: typeof p.sessionId === "string" ? p.sessionId : null });
      }
    }
    if (panes.length === 0) return null;
    const focused = localStorage.getItem(FOCUSED_PANE_KEY);
    return { panes: panes.slice(0, MAX_PANES), focused };
  } catch {
    return null;
  }
}

function savePanes(panes: Pane[], focused: string | null): void {
  try {
    localStorage.setItem(PANES_KEY, JSON.stringify(panes));
    if (focused) localStorage.setItem(FOCUSED_PANE_KEY, focused);
  } catch {
    /* ignore */
  }
}

// Per-window composer state that is NOT the draft text: model override and
// the web-search / image-gen toggles and the queued attachments. Kept per
// pane so toggling web-search (or attaching a file) in one window doesn't
// leak into another.
interface PaneEphem {
  model: ModelChoice;
  webSearchOn: boolean;
  imageGenOn: boolean;
  pendingAttachments: AttachmentMeta[];
  pendingFiles: File[];
}

function defaultPaneEphem(model: ModelChoice): PaneEphem {
  return {
    model,
    webSearchOn: false,
    imageGenOn: false,
    pendingAttachments: [],
    pendingFiles: [],
  };
}
// Lazy-loaded admin dashboards: each becomes its own chunk so a non-admin
// session never pays the JS cost. Named-export adapter pattern keeps the
// dashboard files' public API untouched.
const AgentsDashboard = lazy(() =>
  import("./components/AgentsDashboard").then((m) => ({ default: m.AgentsDashboard })),
);
const UsageDashboard = lazy(() =>
  import("./components/UsageDashboard").then((m) => ({ default: m.UsageDashboard })),
);
const SettingsDashboard = lazy(() =>
  import("./components/SettingsDashboard").then((m) => ({ default: m.SettingsDashboard })),
);
const AboutDashboard = lazy(() =>
  import("./components/AboutDashboard").then((m) => ({ default: m.AboutDashboard })),
);

function nowIso(): string {
  return new Date().toISOString();
}

const MOBILE_QUERY = "(max-width: 720px)";

function isMobileViewport(): boolean {
  return typeof window !== "undefined" && window.matchMedia(MOBILE_QUERY).matches;
}

export function App(): JSX.Element {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  // Multi-window: the open chat panes (side-by-side windows) and which one
  // is focused. Restored from localStorage so a refresh reopens the same
  // layout. `restored` tells the boot effect whether to hydrate the saved
  // panes' sessions or fall back to the legacy single-session auto-restore.
  const bootRef = useRef<{ panes: Pane[]; focused: string; restored: boolean } | null>(null);
  if (!bootRef.current) {
    const restored = loadPanes();
    if (restored && restored.panes.length) {
      const focused =
        restored.focused && restored.panes.some((p) => p.key === restored.focused)
          ? restored.focused
          : restored.panes[0].key;
      bootRef.current = { panes: restored.panes, focused, restored: true };
    } else {
      const k = newPaneKey();
      bootRef.current = { panes: [{ key: k, sessionId: null }], focused: k, restored: false };
    }
  }
  const [panes, setPanes] = useState<Pane[]>(bootRef.current.panes);
  const [focusedPaneKey, setFocusedPaneKey] = useState<string>(bootRef.current.focused);
  // Per-window composer ephemerals (model / web-search / image / queued
  // attachments), keyed by pane key. Absent entries fall back to defaults.
  const [paneEphem, setPaneEphem] = useState<Map<string, PaneEphem>>(() => new Map());
  // NOTE: composer draft text is intentionally NOT App state. It lives in
  // Composer-local state + localStorage (draftStore), so typing never
  // re-renders the App tree. App only references drafts to clear abandoned
  // pane drafts on window close (clearDraft).
  // Per-session message buffers + streaming set. Stream events for a
  // session land in that session's buffer regardless of which session
  // is currently visible, so a turn started in A keeps progressing
  // locally while the user is reading or sending in B. The visible
  // `messages` and `streaming` below are derived from these.
  const [messagesBySession, setMessagesBySession] = useState<Map<string, Message[]>>(
    () => new Map(),
  );
  const [streamingSessions, setStreamingSessions] = useState<Set<string>>(
    () => new Set(),
  );
  const [errorBanner, setErrorBanner] = useState<string | null>(null);
  const [uiStack, setUiStack] = useState<UiState[]>(() =>
    isMobileViewport() ? ["minimized-sidebar"] : ["expanded-sidebar"],
  );
  const [me, setMe] = useState<MeResponse | null>(null);
  // Mirror `me` into a ref so stable callbacks (updateWorkspace) can read the
  // current role without being re-created when identity resolves.
  const meRef = useRef<MeResponse | null>(null);
  meRef.current = me;
  // Set when /api/me returns 401 (no cookie) or 403 (cookie present but the
  // email is not on the allowlist). Both cases render LoginGate instead of
  // the broken half-rendered shell. `authReason` lets the gate show a
  // permission-required message for 403 vs the regular login form for 401.
  // Stays "ok" on the wizerith side because CF Access at the edge prevents
  // unauthenticated requests from ever reaching the SPA.
  const [authReason, setAuthReason] = useState<"ok" | "anon" | "forbidden">("ok");
  const needsAuth = authReason !== "ok";
  const [filter, setFilter] = useState<SidebarFilter>("all");
  const [searchQuery, setSearchQuery] = useState<string>("");
  const [searchResults, setSearchResults] = useState<SearchResult[]>([]);
  const [searching, setSearching] = useState<boolean>(false);
  // Global default model — seeds each new pane and stays in sync with the
  // user's Settings default. Per-message overrides live in paneEphem.
  const [model, setModel] = useState<ModelChoice>(() => loadModelPref());
  // Per-model reasoning-effort levels ({ glm: "max" }); read by every
  // pane's composer and sent with each turn. Persisted to localStorage on
  // change. Levels for a model not in EFFORT_LEVELS are never stored.
  const [effortMap, setEffortMap] = useState<Record<string, string>>(() =>
    loadEffortMap(),
  );
  const effortMapRef = useRef(effortMap);
  effortMapRef.current = effortMap;
  // Workspace selector. Two roles:
  //  1. Default workspace for new sessions (legacy use).
  //  2. Filter for the sidebar list — the toggle controls *which bucket
  //     of sessions* the user sees. Switching from personal to shared
  //     refetches sessions scoped to the new bucket and deselects the
  //     current session if it doesn't belong to the new bucket.
  const [workspace, setWorkspaceState] = useState<Workspace>(() => loadWorkspacePref());
  const updateWorkspace = useCallback(async (w: Workspace) => {
    // Personal is RETIRED for admins (personal === admin). Coerce any
    // non-admin target to "admin" so an admin account can NEVER be put into
    // the retired personal (or shared) bucket — whether from a stray topbar
    // `wizerith:workspace` event, a stale localStorage value, or the hidden
    // in-app toggle. This is the durable guarantee on top of the topbar's
    // admin lock: every workspace change flows through here, so personal is
    // unreachable for admins for good.
    if (meRef.current?.role === "admin" && w !== "admin") w = "admin";
    setWorkspaceState(w);
    saveWorkspacePref(w);
    // Re-scope the visible session list. Failure here is non-fatal —
    // the existing list stays put and the user can refresh.
    try {
      const ss = await getSessions(w);
      setSessions(ss);
      // Drop any open pane whose session isn't part of the new bucket so
      // no window shows a session hidden by the workspace filter. Panes
      // stay open but reset to an empty new-chat view.
      const visibleIds = new Set(ss.map((s) => s.id));
      setPanes((prev) =>
        prev.map((p) =>
          p.sessionId && !visibleIds.has(p.sessionId) ? { ...p, sessionId: null } : p,
        ),
      );
      if (currentSessionIdRef.current && !visibleIds.has(currentSessionIdRef.current)) {
        saveLastSessionId(null);
      }
    } catch {
      /* surface via the next refresh */
    }
  }, []);
  // Wizerith only: the shared topbar (`/_theme/wizerith-theme.js`) owns
  // the workspace toggle on its UI. It persists the choice as
  // `localStorage['wizerith.workspace']` and broadcasts changes via a
  // `wizerith:workspace` CustomEvent. Mirror that signal into chat's
  // own workspace state so the sidebar's session list re-filters.
  useEffect(() => {
    const host = typeof window !== "undefined" ? window.location.hostname : "";
    const onWizerith = host === "wizerith.ai" || host.endsWith(".wizerith.ai");
    if (!onWizerith) return;
    // Initial sync: if the topbar's stored choice differs from what chat
    // bootstrapped to, adopt the topbar's value (it's the source of truth).
    try {
      const stored = localStorage.getItem("wizerith.workspace");
      if (
        (stored === "personal" || stored === "shared" || stored === "admin") &&
        stored !== workspace
      ) {
        void updateWorkspace(stored);
      }
    } catch {
      /* ignore */
    }
    const onChange = (e: Event) => {
      const detail = (e as CustomEvent).detail as { workspace?: string } | undefined;
      const next = detail?.workspace;
      if (next === "personal" || next === "shared" || next === "admin") {
        void updateWorkspace(next);
      }
    };
    window.addEventListener("wizerith:workspace", onChange as EventListener);
    return () => {
      window.removeEventListener("wizerith:workspace", onChange as EventListener);
    };
    // Effect runs once on mount; updateWorkspace is a stable useCallback ref
    // and the initial-sync read should NOT re-fire if `workspace` later
    // changes via the listener (would cause a loop). We intentionally omit
    // `workspace` from deps.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [updateWorkspace]);
  // Per-user settings — bootstrapped from /api/me's `settings` payload,
  // refreshed when the SettingsDashboard saves. `applied` mirrors the
  // server's coerced state; consumed by Composer (default model + send-
  // on-Enter), turn-done effect (notify), theme effect (data-theme attr),
  // sidebar filter (auto-archive), and Thread (cost display).
  const [settings, setSettings] = useState<UserSettings>(DEFAULT_USER_SETTINGS);
  const settingsRef = useRef<UserSettings>(settings);
  settingsRef.current = settings;

  // UI display language. Source of truth is the shared wizerith top bar
  // (gear → Language), which writes the per-user `ui_language` server-side,
  // mirrors it onto <html lang>, and emits a `wizerith:lang` event. We read
  // that here so the chat chrome localizes site-wide. settings.ui_language is
  // the fallback for any surface that doesn't render the shared top bar.
  const [displayLang, setDisplayLang] = useState<LanguageChoice>(
    () => readDocLang() ?? "en",
  );

  // Per-session AbortController registry. A stream started in session A
  // is keyed by A's id, so creating / switching to session B leaves A's
  // controller untouched and A keeps streaming locally. Cancel-turn
  // looks up the entry by current session; the worker's finally clears
  // it. Replaces the previous single-ref design that forced cross-
  // session aborts on every switch.
  const streamAbortsRef = useRef<Map<string, AbortController>>(new Map());
  // Per-session "turn settled" promises. interrupt-then-send (typing while a
  // turn streams, claude.ai-style) cancels the in-flight turn and AWAITS its
  // settle here before starting the replacement turn — so the interrupted
  // bubble finalizes via its own SSE terminal and the server retires the run
  // from `_active_runs` before the new POST (no overlap, no dangling
  // "streaming" bubble). Resolved in runTurn's finally; awaited in interruptTurn.
  const turnSettleRef = useRef<
    Map<string, { promise: Promise<void>; resolve: () => void }>
  >(new Map());
  const modelRef = useRef<ModelChoice>(model);
  modelRef.current = model;

  // --- multi-window derivations ---------------------------------------
  // The focused pane is the target for sidebar session selection and the
  // legacy single-session call sites (`currentSessionId`). Falling back to
  // panes[0] keeps things sane if focusedPaneKey ever points at a closed
  // pane between renders.
  const focusedPane = panes.find((p) => p.key === focusedPaneKey) ?? panes[0] ?? null;
  const currentSessionId = focusedPane?.sessionId ?? null;
  // Set of every session id currently visible in *some* pane. Drives
  // "is this stream's session on screen" decisions (error banner,
  // tab-focus re-sync) which used to key off the single current session.
  const openSessionIds = new Set<string>();
  for (const p of panes) if (p.sessionId) openSessionIds.add(p.sessionId);
  const openSessionIdsRef = useRef<Set<string>>(openSessionIds);
  openSessionIdsRef.current = openSessionIds;
  const focusedPaneKeyRef = useRef<string>(focusedPaneKey);
  focusedPaneKeyRef.current = focusedPaneKey;
  // Refs mirroring the latest panes / pane-ephemeral state so async
  // callbacks (send, attach, new-window) read fresh values without
  // re-creating on every render.
  const panesRef = useRef<Pane[]>(panes);
  panesRef.current = panes;
  const paneEphemRef = useRef<Map<string, PaneEphem>>(paneEphem);
  paneEphemRef.current = paneEphem;

  // Point the focused pane at a session (or null). Replaces the old
  // setCurrentSessionId — every legacy "switch the visible session" call
  // routes through here and mutates only the focused pane.
  const setFocusedSession = useCallback((sid: string | null) => {
    setPanes((prev) => {
      const key = focusedPaneKeyRef.current;
      if (!prev.some((p) => p.key === key)) {
        // Focused pane vanished — retarget the first pane.
        return prev.map((p, i) => (i === 0 ? { ...p, sessionId: sid } : p));
      }
      return prev.map((p) => (p.key === key ? { ...p, sessionId: sid } : p));
    });
  }, []);

  // Tracked-by-ref so the recovery poller can bail out as soon as the
  // user navigates to a different session — otherwise its setMessages
  // call would clobber the new session's view with stale data.
  const currentSessionIdRef = useRef<string | null>(currentSessionId);
  currentSessionIdRef.current = currentSessionId;
  // Per-session recovery registry. Lets two sessions be re-attached
  // simultaneously (e.g. recovering A while sending in B) without
  // either one launching duplicate GET .../stream subscribers.
  const recoveryAttachedSidsRef = useRef<Set<string>>(new Set());
  // Panes with a send in flight (from Enter until the POST is issued):
  // a second Enter/click meanwhile used to create a second session and
  // upload+send the same attachments twice.
  const sendingPanesRef = useRef<Set<string>>(new Set());
  // Monotonic token bumped on every loadSession call. Stale getSession
  // responses (user clicked another session before this one returned)
  // are dropped instead of clobbering the latest view.
  const loadTokenRef = useRef<number>(0);
  // Forward-reference indirection: loadSession (declared before
  // tryAttachIfStreaming) calls it through this ref, which is wired
  // up by the useEffect below once the real callback has been created.
  const tryAttachIfStreamingRef = useRef<(sid: string, msgs: Message[]) => void>(
    () => { /* not yet wired */ },
  );

  // --- per-session buffer helpers -------------------------------------
  // Centralized so every setMessages-style update flows through a
  // sid-keyed write. The functional-updater pattern lets handleStream
  // events apply to the correct session even when React's render hasn't
  // re-resolved the latest state.
  // Replace this session's messages with a server-side snapshot, BUT
  // never reduce content that's already been streamed locally.
  //
  // Why this matters: every getSession() returns the session JSON the
  // server has persisted so far. While a stream is in flight, the
  // server worker accumulates text in memory and only flushes to the
  // session JSON periodically — so the snapshot's trailing assistant
  // message is usually BEHIND the deltas we've appended in-process.
  // The five callers that pass server snapshots in here (loadSession,
  // visibilitychange handler, recoverFromStreamError's poll loop,
  // tryAttachIfStreaming's post-recovery re-sync, and the boot path)
  // would all otherwise wipe a few seconds of partial text every
  // refresh — that's the "content disappears mid-stream" bug.
  //
  // The merge keeps server authoritative for everything EXCEPT the
  // trailing assistant message, where local wins if it has more
  // content (or if local has a trailing message server hasn't seen
  // yet — happens when we append an assistant placeholder before the
  // POST stream's first delta).
  const replaceMessages = useCallback((sid: string, msgs: Message[]) => {
    setMessagesBySession((prev) => {
      const local = prev.get(sid) ?? [];
      const merged = mergePreservingLiveTail(local, msgs);
      if (merged === local) return prev;
      const next = new Map(prev);
      next.set(sid, merged);
      return next;
    });
  }, []);
  const updateMessages = useCallback(
    (sid: string, updater: (prev: Message[]) => Message[]) => {
      setMessagesBySession((prev) => {
        const cur = prev.get(sid) ?? [];
        const out = updater(cur);
        if (out === cur) return prev;
        const next = new Map(prev);
        next.set(sid, out);
        return next;
      });
    },
    [],
  );
  const dropSessionState = useCallback((sid: string) => {
    setMessagesBySession((prev) => {
      if (!prev.has(sid)) return prev;
      const next = new Map(prev);
      next.delete(sid);
      return next;
    });
    setStreamingSessions((prev) => {
      if (!prev.has(sid)) return prev;
      const next = new Set(prev);
      next.delete(sid);
      return next;
    });
    const ctrl = streamAbortsRef.current.get(sid);
    if (ctrl) {
      streamAbortsRef.current.delete(sid);
      try { ctrl.abort(); } catch { /* ignore */ }
    }
    recoveryAttachedSidsRef.current.delete(sid);
  }, []);
  const setSessionStreaming = useCallback((sid: string, on: boolean) => {
    setStreamingSessions((prev) => {
      const has = prev.has(sid);
      if (has === on) return prev;
      const next = new Set(prev);
      if (on) next.add(sid);
      else next.delete(sid);
      return next;
    });
  }, []);

  // --- per-pane composer ephemerals -----------------------------------
  const getPaneEphem = useCallback(
    (key: string): PaneEphem => paneEphem.get(key) ?? defaultPaneEphem(model),
    [paneEphem, model],
  );
  const updatePaneEphem = useCallback((key: string, patch: Partial<PaneEphem>) => {
    setPaneEphem((prev) => {
      const cur = prev.get(key) ?? defaultPaneEphem(modelRef.current);
      const next = new Map(prev);
      next.set(key, { ...cur, ...patch });
      return next;
    });
  }, []);

  // Persist the open-pane layout + focused pane so a refresh restores it.
  useEffect(() => {
    savePanes(panes, focusedPaneKey);
  }, [panes, focusedPaneKey]);

  const uiTop: UiState = uiStack[uiStack.length - 1] ?? "expanded-sidebar";

  const pushUi = useCallback((s: UiState) => {
    setUiStack((prev) => [...prev, s]);
  }, []);

  const onChevron = useCallback(() => {
    setUiStack((prev) => {
      const top = prev[prev.length - 1];
      if (
        top === "dashboard:agents" ||
        top === "dashboard:usage" ||
        top === "dashboard:settings" ||
        top === "dashboard:about"
      ) {
        return prev.slice(0, -1);
      }
      if (top === "expanded-sidebar") {
        return ["minimized-sidebar"];
      }
      return ["expanded-sidebar"];
    });
  }, []);

  // True on auth.ald3.com / auth.wizerith.ai — sign-in-only surfaces.
  // When the user is already signed in we redirect straight to `?next=`
  // (or the landing page); when they aren't we keep LoginGate mounted
  // regardless of the chat boot path below. Both tenants run the same
  // SPA bundle; the tenant is fixed at deploy time by which backend
  // (chat / chat-wizerith) responds to /api/auth/*.
  const isAuthHost =
    typeof window !== "undefined" &&
    (window.location.hostname === "auth.ald3.com" ||
      window.location.hostname === "auth.wizerith.ai");

  useEffect(() => {
    // auth.ald3.com: sign-in-only surface. Don't bootstrap the chat shell.
    // Probe the OPEN /api/auth/me (no allowlist gate) — 200 = already
    // signed in, redirect away; 401 = anonymous, stay on the LoginGate.
    // Chat's /api/me would 403 non-allowlisted users (who can use
    // bet / market) and show them the "Permission required" card, which
    // is wrong on a public sign-in page.
    if (isAuthHost) {
      (async () => {
        try {
          const r = await fetch("/api/auth/me", { credentials: "same-origin" });
          if (r.status === 200) {
            const params = new URLSearchParams(window.location.search);
            const next = params.get("next");
            const fallback = "https://ald3.com/";
            let target = fallback;
            if (next) {
              try {
                const u = new URL(next, window.location.origin);
                const okProto = u.protocol === "https:" || u.protocol === "http:";
                const okHost = u.hostname === "ald3.com" || u.hostname.endsWith(".ald3.com");
                if (okProto && okHost) target = u.toString();
              } catch { /* malformed; fall through to landing */ }
            }
            window.location.href = target;
          }
          // Anything else (401 / network) keeps the LoginGate mounted.
        } catch { /* keep LoginGate */ }
      })();
      return;
    }
    (async () => {
      // The bucket the boot fetch uses; admins are pinned to 'admin' below
      // BEFORE sessions are fetched, or a non-admin closure value races it.
      let bootWorkspace: Workspace = workspace;
      try {
        const meResp = await getMe();
        // Cross-site sign-in honor: if bet/market/dev bounced the browser
        // here with `?next=https://*.ald3.com/...` and we already have a
        // valid session, send the user straight back without ever showing
        // the chat shell. Guarded by an ald3.com hostname allowlist (open-
        // redirect mitigation). Stays out of the way for normal chat
        // navigation (no `?next=` → no-op).
        try {
          const params = new URLSearchParams(window.location.search);
          const next = params.get("next");
          if (next) {
            const u = new URL(next, window.location.origin);
            const sameOrigin =
              u.origin === window.location.origin && (u.pathname === "/" || u.pathname === window.location.pathname);
            const okHost = u.hostname === "ald3.com" || u.hostname.endsWith(".ald3.com");
            if (okHost && !sameOrigin) {
              window.location.href = u.toString();
              return;
            }
            // Strip `next=` so a later refresh doesn't keep re-redirecting.
            const url = new URL(window.location.href);
            url.searchParams.delete("next");
            window.history.replaceState(null, "", url.toString());
          }
        } catch {
          /* malformed next — ignore and render chat normally */
        }
        setMe(meResp);
        // Adopt the deployment's real message-size cap so the composer's
        // paste-to-attachment threshold tracks CHAT_MAX_MESSAGE_BYTES
        // instead of the compiled-in default. See limits.ts.
        setMaxMessageBytes(meResp.max_message_bytes);
        // Personal is retired for admins. If identity resolves as admin and we
        // somehow booted into a non-admin bucket (stale pref, missing topbar
        // value), pin to "admin" now so personal never even flashes. No-op in
        // the normal case where loadWorkspacePref already resolved "admin".
        if (meResp.role === "admin" && workspace !== "admin") {
          bootWorkspace = "admin";
          await updateWorkspace("admin");
        }
        if (meResp.settings) {
          setSettings(meResp.settings);
          // Sync the per-message model picker default unless the user
          // already overrode it via the composer this session (the
          // composer's local model state would have been seeded from
          // localStorage in loadModelPref).
          if (meResp.settings.default_model && meResp.settings.default_model !== "default") {
            setModel(meResp.settings.default_model);
          }
        }
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) {
          setAuthReason("anon");
          return;
        }
        if (err instanceof ApiError && err.status === 403) {
          setAuthReason("forbidden");
          return;
        }
        setErrorBanner(formatError("Failed to load identity", err));
      }
      try {
        const ss = await getSessions(bootWorkspace);
        setSessions(ss);
        const url = new URL(window.location.href);
        const sParam = url.searchParams.get("s");
        const newParam = url.searchParams.get("new");
        const validIds = new Set(ss.map((s) => s.id));

        // Load a session's messages into its buffer and re-attach if it's
        // mid-stream. Returns false if the session no longer exists.
        const hydrate = async (id: string): Promise<boolean> => {
          try {
            const full = await getSession(id);
            replaceMessages(full.id, full.messages || []);
            tryAttachIfStreamingRef.current(full.id, full.messages || []);
            return true;
          } catch {
            return false;
          }
        };

        if (bootRef.current?.restored) {
          // Multi-window restore: hydrate every saved pane's session in
          // parallel; null-out any pane whose session has since been
          // deleted. ?s= still injects a session into the focused pane.
          const saved = bootRef.current.panes;
          const results = await Promise.all(
            saved.map((p) =>
              p.sessionId && validIds.has(p.sessionId)
                ? hydrate(p.sessionId)
                : Promise.resolve(false),
            ),
          );
          const failed = new Set<string>();
          saved.forEach((p, i) => {
            if (p.sessionId && !results[i]) failed.add(p.sessionId);
          });
          if (failed.size) {
            setPanes((prev) =>
              prev.map((p) =>
                p.sessionId && failed.has(p.sessionId) ? { ...p, sessionId: null } : p,
              ),
            );
          }
          if (sParam && validIds.has(sParam) && !saved.some((p) => p.sessionId === sParam)) {
            if (await hydrate(sParam)) setFocusedSession(sParam);
          }
          if (newParam) {
            url.searchParams.delete("new");
            window.history.replaceState(null, "", url.toString());
          }
        } else {
          // Legacy single-pane boot (first load before any saved layout).
          //   ?s=<id> → load that session; ?new=1 → stay on empty view.
          if (newParam) {
            url.searchParams.delete("new");
            window.history.replaceState(null, "", url.toString());
          } else if (sParam && currentSessionIdRef.current === null) {
            if (await hydrate(sParam)) {
              setFocusedSession(sParam);
              currentSessionIdRef.current = sParam;
            } else {
              url.searchParams.delete("s");
              window.history.replaceState(null, "", url.toString());
            }
          }
          // Auto-restore the last-active session so a refresh lands the
          // user back where they were.
          if (ss.length > 0 && currentSessionIdRef.current === null && !newParam) {
            const savedId = loadLastSessionId();
            const target = (savedId && ss.find((s) => s.id === savedId)?.id) || ss[0].id;
            if (await hydrate(target)) {
              setFocusedSession(target);
              currentSessionIdRef.current = target;
            }
          }
        }
      } catch (err) {
        setErrorBanner(formatError("Failed to load sessions", err));
      }
    })();
  }, []);

  // Mirror the active session into ?s=<id> so the URL is shareable and
  // right-click-friendly. replaceState (not pushState) keeps the back
  // button clean — clicking through 20 sessions shouldn't bury the
  // referring page under 20 history entries.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const url = new URL(window.location.href);
    const current = url.searchParams.get("s");
    if (currentSessionId) {
      if (current !== currentSessionId) {
        url.searchParams.set("s", currentSessionId);
        window.history.replaceState(null, "", url.toString());
      }
    } else if (current) {
      url.searchParams.delete("s");
      window.history.replaceState(null, "", url.toString());
    }
  }, [currentSessionId]);

  // Persist the active session id so a refresh restores it.
  useEffect(() => {
    saveLastSessionId(currentSessionId);
  }, [currentSessionId]);

  // Apply theme to <html data-theme=...>. CSS targets the attribute to
  // swap the color tokens. "system" defers to prefers-color-scheme.
  useEffect(() => {
    const root = document.documentElement;
    const apply = () => {
      let resolved: "dark" | "light" = "dark";
      if (settings.theme === "light") resolved = "light";
      else if (settings.theme === "system") {
        const mq = window.matchMedia("(prefers-color-scheme: light)");
        resolved = mq.matches ? "light" : "dark";
      }
      root.setAttribute("data-theme", resolved);
    };
    apply();
    if (settings.theme === "system") {
      const mq = window.matchMedia("(prefers-color-scheme: light)");
      const listener = () => apply();
      mq.addEventListener("change", listener);
      return () => mq.removeEventListener("change", listener);
    }
  }, [settings.theme]);

  // Track the site-wide display language from the shared top bar: listen for
  // its `wizerith:lang` event (live switches) and re-read <html lang> on mount
  // (the top bar may have set it before React mounted).
  useEffect(() => {
    const onLang = (e: Event) => {
      const code = (e as CustomEvent<{ language?: string }>).detail?.language;
      if (code === "en" || code === "zh-CN" || code === "zh-TW") {
        setDisplayLang(code);
      }
    };
    window.addEventListener("wizerith:lang", onLang);
    const docLang = readDocLang();
    if (docLang) setDisplayLang(docLang);
    return () => window.removeEventListener("wizerith:lang", onLang);
  }, []);

  // Fallback for surfaces without the shared top bar (no <html lang>): adopt
  // the per-user saved value once server settings load.
  useEffect(() => {
    if (!readDocLang()) setDisplayLang(settings.ui_language);
  }, [settings.ui_language]);

  useEffect(() => {
    const vv = typeof window !== "undefined" ? window.visualViewport : null;
    if (!vv) return;
    const root = document.documentElement;
    const KEYBOARD_THRESHOLD_PX = 80;
    let lastPx = -1;
    let kbOpen = false;
    const apply = () => {
      const innerH = window.innerHeight;
      const vvH = Math.round(vv.height);
      const hidden = innerH - vvH;
      const nowKbOpen = hidden >= KEYBOARD_THRESHOLD_PX;
      if (nowKbOpen) {
        if (vvH === lastPx && kbOpen) return;
        lastPx = vvH;
        kbOpen = true;
        root.style.setProperty("--app-height", `${vvH}px`);
        root.style.setProperty("--composer-safe-bottom", "0px");
      } else if (kbOpen) {
        kbOpen = false;
        lastPx = -1;
        root.style.removeProperty("--app-height");
        root.style.removeProperty("--composer-safe-bottom");
      }
    };
    apply();
    vv.addEventListener("resize", apply);
    return () => {
      vv.removeEventListener("resize", apply);
    };
  }, []);

  const loadSession = useCallback(async (id: string) => {
    // Token + ref so a stale getSession response (user clicked another
    // session mid-request) can't clobber the current view. Tap the new
    // session id into the ref synchronously so the recovery helper
    // and the SSE event filter see the freshest target immediately,
    // not after this async call resolves.
    const token = ++loadTokenRef.current;
    currentSessionIdRef.current = id;
    try {
      const full: Session = await getSession(id);
      if (loadTokenRef.current !== token) return;  // user moved on; drop
      // Only overwrite the buffer if the session is NOT currently
      // streaming locally. Otherwise the live in-memory partial would
      // be replaced by the server's persisted placeholder (empty
      // content), causing the typing-content to disappear briefly. The
      // tryAttachIfStreaming call below is a no-op when a local stream
      // is already in flight.
      const liveLocally = streamAbortsRef.current.has(full.id);
      if (!liveLocally) {
        replaceMessages(full.id, full.messages || []);
      }
      setFocusedSession(full.id);
      tryAttachIfStreamingRef.current(full.id, full.messages || []);
    } catch (err) {
      if (loadTokenRef.current !== token) return;  // stale failure; drop
      // Session was deleted server-side (or never existed) — quietly
      // drop the stale sidebar entry and reset any pane showing it to an
      // empty view. The user shouldn't see a scary banner for a session
      // that simply isn't there anymore.
      if (err instanceof ApiError && err.status === 404) {
        setSessions((prev) => prev.filter((s) => s.id !== id));
        if (currentSessionIdRef.current === id) currentSessionIdRef.current = null;
        setPanes((prev) =>
          prev.map((p) => (p.sessionId === id ? { ...p, sessionId: null } : p)),
        );
        dropSessionState(id);
        return;
      }
      setErrorBanner(formatError("Failed to load session", err));
    }
  }, [replaceMessages, dropSessionState, setFocusedSession]);

  // Recover from a transport-level stream interruption (tab-switch,
  // network drop, browser-throttled background tab, etc.) without
  // showing an error banner. The server-side worker keeps running
  // independently of the browser stream, so we poll the session until
  // the trailing assistant message is finalized.
  //
  // Caller is expected to AWAIT this so the `streaming` flag stays
  // true for the duration — that's what keeps the typing indicator
  // visible and the empty-assistant-placeholder hidden while we wait
  // for the server to finalize. Without the await, `streaming` flips
  // off immediately on the catch path and the user sees an empty
  // bubble even though the model is still generating.
  // Late-bound handle on handleStreamEvent (defined further down) so the
  // recovery loop can re-attach to a run the backend re-created after a
  // restart and stream its deltas live instead of only polling snapshots.
  const handleStreamEventRef = useRef<(sid: string, evt: StreamEvent) => void>(() => {});

  const recoverFromStreamError = useCallback(async (sid: string): Promise<void> => {
    const POLL_INTERVAL_MS = 2000;
    // A backend restart re-runs the interrupted turn on the same bubble; that
    // re-run can take minutes, so keep following it rather than giving up
    // after two minutes and leaving a stale spinner.
    const POLL_DEADLINE_MS = 900_000;
    const deadline = Date.now() + POLL_DEADLINE_MS;
    while (Date.now() < deadline) {
      try {
        const full = await getSession(sid);
        // Always write to the per-session buffer — the recovery owner
        // is the session id, not the user's currently visible session.
        // The user could be reading B while A is recovering; A's final
        // message still needs to land in A's buffer.
        replaceMessages(sid, full.messages || []);
        const last = full.messages?.[full.messages.length - 1];
        const stillStreaming =
          !!last && last.role === "assistant" && last.status === "streaming";
        if (!stillStreaming) return;
        // The server answered and the turn is still running (typically the
        // backend restarted and is re-running it): re-attach to the live
        // stream. GET .../stream replays the run's events FROM EVENT 0 and
        // then follows it, so clear the locally accumulated partial first —
        // otherwise the replay appends onto it and the bubble reads
        // "Hello wor…Hello wor…" until done. Abortable via the owning
        // controller so a pane close / session delete can stop it.
        const owner = streamAbortsRef.current.get(sid);
        if (owner?.signal.aborted) return;
        updateMessages(sid, (prev) => {
          const i = prev.length - 1;
          if (i < 0 || prev[i].role !== "assistant" || prev[i].status !== "streaming") return prev;
          const next = prev.slice();
          next[i] = { ...next[i], content: "" };
          return next;
        });
        try {
          await attachStream(sid, (evt) => handleStreamEventRef.current(sid, evt), {
            signal: owner?.signal,
          });
          const after = await getSession(sid);
          replaceMessages(sid, after.messages || []);
          const tail = after.messages?.[after.messages.length - 1];
          if (!(tail && tail.role === "assistant" && tail.status === "streaming")) return;
        } catch (err) {
          if ((err as { name?: string })?.name === "AbortError") return;
          /* attach failed — keep polling */
        }
      } catch (err) {
        // The session is gone (deleted while we were polling): stop.
        if (err instanceof ApiError && err.status === 404) return;
        /* single failed poll — try again */
      }
      await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
    }
  }, [replaceMessages, updateMessages]);

  // When the tab becomes visible again, re-sync the current session
  // messages from the server. Covers the case where the browser
  // silently closed the stream while the tab was backgrounded — by the
  // time the user looks at the page, the worker may have finished and
  // persisted the final result, but the local state hasn't seen it.
  //
  // The same routine also runs on a timer (see below), because
  // visibilitychange is not enough on its own: a SCHEDULED WAKE fires
  // server-side with no client stream attached, so a wake that lands while
  // the user is sitting on the session with the tab already visible
  // produced nothing at all on screen — no bubble, no spinner — until they
  // happened to switch tabs, click another session, or reload. The wake had
  // run and persisted; only the rendering was missing.
  const resyncOpenSessions = useCallback((sids: Iterable<string>) => {
    {
      if (document.visibilityState !== "visible") return;
      // Re-sync every session open in some pane, not just the focused one
      // — any background window could have finished while the tab slept.
      for (const sid of sids) {
        getSession(sid)
          .then((full) => {
            if (!openSessionIdsRef.current.has(sid)) return;
            // Same guard as loadSession: skip the overwrite when a local
            // stream is still in flight, so the live partial isn't wiped
            // by the persisted placeholder.
            if (!streamAbortsRef.current.has(sid)) {
              replaceMessages(sid, full.messages || []);
              // Clear the streaming flag when the persisted message
              // has moved off "streaming" — covers the case where the
              // tab was backgrounded long enough for the server to
              // crash + restart (startup sweep flipped status to
              // "error" but our local streamingSessions still has the
              // sid from before the crash, leaving the spinner stuck).
              const msgs = full.messages || [];
              const last = msgs[msgs.length - 1];
              const stillStreaming =
                !!last && last.role === "assistant" && last.status === "streaming";
              if (!stillStreaming) {
                setSessionStreaming(sid, false);
              }
            }
            tryAttachIfStreamingRef.current(sid, full.messages || []);
          })
          .catch(() => {
            /* ignore — non-fatal */
          });
      }
    }
  }, [replaceMessages, setSessionStreaming]);

  useEffect(() => {
    // Tab just became visible: we may have missed anything, so re-fetch every
    // open session unconditionally rather than trusting the cheap check.
    const handler = () => resyncOpenSessions(openSessionIdsRef.current);
    document.addEventListener("visibilitychange", handler);
    return () => document.removeEventListener("visibilitychange", handler);
  }, [resyncOpenSessions]);

  // Idle poll for server-initiated turns (scheduled wakes). Once the poll
  // pulls in the wake's assistant placeholder (status "streaming"),
  // tryAttachIfStreaming re-attaches to the live run and the rest of the reply
  // streams in normally — so the interval is only the delay before the bubble
  // APPEARS, not the granularity of the text.
  //
  // Cost matters here because this runs forever in every open tab, so it does
  // NOT fetch the session itself each tick: getSession returns the ENTIRE
  // message array, hundreds of KB on a long thread. It asks each open
  // session's /head endpoint instead (~100 bytes, one file read) and pays for
  // the full fetch only once ``updated_at`` has actually moved.
  //
  // GET /sessions would also work as a change-detector but is worse, not
  // better: storage.list_sessions parses EVERY session file the user owns to
  // build the summary list, so polling it is more server work than the thing
  // it was meant to avoid.
  const lastUpdatedRef = useRef<Map<string, string>>(new Map());
  useEffect(() => {
    const tick = async () => {
      if (document.visibilityState !== "visible") return;
      const open = Array.from(openSessionIdsRef.current);
      if (open.length === 0) return;
      const stale: string[] = [];
      await Promise.all(
        open.map(async (sid) => {
          // A local stream is bumping updated_at itself; leave the ref alone
          // so we re-compare (and re-sync once) after the turn settles.
          if (streamAbortsRef.current.has(sid)) return;
          let head;
          try {
            head = await getSessionHead(sid);
          } catch (err) {
            // A backend without /head (older image) 404s. Fall back to the
            // full fetch so the wake still renders — correctness first; the
            // endpoint is only an optimisation.
            if (err instanceof ApiError && err.status === 404) stale.push(sid);
            return;
          }
          const stamp = head.updated_at ?? String(head.message_count);
          const seen = lastUpdatedRef.current.get(sid);
          // No baseline yet → sync once to establish one. Never skip on a
          // missing entry: seeding without fetching would swallow a wake that
          // landed between page load and the first tick.
          if (seen === undefined || seen !== stamp) {
            lastUpdatedRef.current.set(sid, stamp);
            stale.push(sid);
          }
        }),
      );
      if (stale.length) {
        resyncOpenSessions(stale);
        // A server-initiated turn also reorders/retitles the sidebar.
        getSessions(workspace)
          .then(setSessions)
          .catch(() => { /* non-fatal */ });
      }
    };
    const iv = window.setInterval(tick, IDLE_RESYNC_MS);
    return () => window.clearInterval(iv);
  }, [resyncOpenSessions, workspace]);

  // Single source of truth for SSE event handling. Shared between the
  // original POST stream (onSend) and the re-attach GET stream
  // (recovery effect below) so behavior — delta accumulation, tool
  // markers, terminal full_text overwrite, error banner — is identical
  // whether the events arrive live or via the run's replayed event
  // log. All state writers used here are stable (setState + refs), so
  // the callback has empty deps and never re-creates.
  const handleStreamEvent = useCallback((sid: string, evt: StreamEvent) => {
    if (evt.type === "delta") {
      updateMessages(sid, (prev) => {
        const i = prev.length - 1;
        if (i < 0 || prev[i].role !== "assistant") return prev;
        const next = prev.slice();
        next[i] = { ...next[i], content: next[i].content + evt.text };
        return next;
      });
    } else if (evt.type === "tool_start") {
      updateMessages(sid, (prev) => {
        const i = prev.length - 1;
        if (i < 0 || prev[i].role !== "assistant") return prev;
        const next = prev.slice();
        const marker = `\n\n[Tool: ${evt.name}]\n\n`;
        next[i] = { ...next[i], content: next[i].content + marker };
        return next;
      });
    } else if (evt.type === "tool_end") {
      /* no-op */
    } else if (evt.type === "done" || evt.type === "cancelled") {
      updateMessages(sid, (prev) => {
        const i = prev.length - 1;
        if (i < 0 || prev[i].role !== "assistant") return prev;
        const next = prev.slice();
        next[i] = {
          ...next[i],
          // IMPORTANT: use a typeof check, not `||` — the server's
          // memory-update scrub can legitimately produce an empty
          // string when the entire response was a `<memory_update>`
          // block. `"" || streamed` falls back to the streamed text
          // (which still contains the memory_update text), so users
          // see the supposedly-stripped block. typeof "string" hits
          // both "" and "<actual text>" correctly.
          content: typeof evt.full_text === "string" ? evt.full_text : next[i].content,
          // The bubble is terminal now; without this it kept status
          // "streaming" (spinner / hidden footer) until the next resync.
          status: evt.type === "done" ? "complete" : "cancelled",
          // Attach the per-turn metadata footer payload (timestamp /
          // model / tokens / tok_s) when the backend sent one. Left
          // untouched if absent so a re-attach replay can't clear it.
          ...(evt.meta ? { meta: evt.meta } : {}),
        };
        return next;
      });
      if (
        evt.type === "done" &&
        settingsRef.current.notify_on_complete &&
        "Notification" in window &&
        Notification.permission === "granted" &&
        document.visibilityState !== "visible"
      ) {
        try {
          const body = (evt.full_text || "").slice(0, 140) || "Turn complete";
          new Notification("Claude finished a turn", { body, silent: false });
        } catch { /* ignore */ }
      }
    } else if (evt.type === "schedules_updated") {
      // The model armed or cancelled a wake during this turn. Tell that
      // session's composer to refetch now instead of leaving the ⏰ pill
      // wrong until its next 20s poll.
      emitSessionEvent(SCHEDULES_UPDATED, sid);
    } else if (evt.type === "memory_updated") {
      emitSessionEvent(MEMORY_UPDATED, sid);
    } else if (evt.type === "error") {
      // Only surface the banner if the failing session is visible in some
      // open pane. Otherwise the in-bubble error line below is the silent
      // signal and a noisy banner from a background session would be
      // distracting.
      if (openSessionIdsRef.current.has(sid)) {
        setErrorBanner(`Stream error: ${evt.message}`);
      }
      updateMessages(sid, (prev) => {
        const i = prev.length - 1;
        if (i < 0 || prev[i].role !== "assistant") return prev;
        const next = prev.slice();
        next[i] = {
          ...next[i],
          status: "error",
          content:
            (next[i].content || "") +
            `\n\n_⚠ generation failed: ${evt.message}_`,
        };
        return next;
      });
    }
  }, [updateMessages]);

  // Resume the typing indicator AND the live partial-content view when
  // the visible session's last assistant turn is still in
  // `status: "streaming"` but no SSE is attached locally. Called
  // imperatively (NOT via a useEffect on `messages`) by every callsite
  // that loads a new message list:
  //   • loadSession (user clicked a session in the sidebar)
  //   • the boot path (auto-restore last session on refresh)
  //   • the visibilitychange handler (tab-focus refresh)
  //
  // Why not an effect on [messages]? Because the SSE replay arrives via
  // handleStreamEvent → setMessages, which would re-trigger the effect
  // every delta, and the effect's cleanup would abort the in-flight
  // controller. The result is the jitter / duplicated-content loop:
  // each delta kills the attach, the next render re-attaches, replays
  // from event 0 again (appending content twice), and so on.
  // recoveryAttachedSidsRef + an explicit trigger eliminate the loop.
  //
  // We re-attach via GET .../stream which REPLAYS the run's full event
  // log (every delta since event 0) and then drains live events to
  // terminal. The server persists only the placeholder + terminal state
  // (not partials), so polling getSession would show an empty bubble
  // until done — the SSE replay is what lets the user see all the
  // progress that accumulated while they were on another session.
  useEffect(() => {
    handleStreamEventRef.current = handleStreamEvent;
  }, [handleStreamEvent]);

  const tryAttachIfStreaming = useCallback((sid: string, msgs: Message[]): void => {
    if (!sid) return;
    // A live POST stream (started in onSend) is its own subscriber to
    // the server's event log — re-attaching here would duplicate every
    // delta. Same for an in-flight recovery attach.
    if (streamAbortsRef.current.has(sid)) return;
    if (recoveryAttachedSidsRef.current.has(sid)) return;
    const last = msgs[msgs.length - 1];
    if (!last || last.role !== "assistant" || last.status !== "streaming") return;

    const ctrl = new AbortController();
    streamAbortsRef.current.set(sid, ctrl);
    recoveryAttachedSidsRef.current.add(sid);
    setSessionStreaming(sid, true);
    // Same settle handshake runTurn registers, so an interrupt-then-send
    // against a re-attached turn (a wake, or a turn started elsewhere)
    // waits for THIS attach to wind down instead of racing it.
    let settleResolve: () => void = () => {};
    const settleEntry = {
      promise: new Promise<void>((res) => {
        settleResolve = res;
      }),
      resolve: () => settleResolve(),
    };
    turnSettleRef.current.set(sid, settleEntry);
    (async () => {
      try {
        // Events apply to the per-session buffer regardless of which
        // session is currently visible; if the user switched away the
        // partial content keeps building in the background buffer.
        await attachStream(sid, (evt) => handleStreamEvent(sid, evt), {
          signal: ctrl.signal,
        });
        // After terminal: re-sync from persisted JSON so the final
        // message (status=complete + full text) is authoritative.
        try {
          const full = await getSession(sid);
          replaceMessages(sid, full.messages || []);
        } catch { /* non-fatal */ }
      } catch (err) {
        if ((err as { name?: string })?.name === "AbortError") return;
        try { await recoverFromStreamError(sid); } catch { /* ignore */ }
      } finally {
        recoveryAttachedSidsRef.current.delete(sid);
        // Only clear what we own: if a replacement turn already took the
        // slot, its streaming flag must survive our teardown.
        if (streamAbortsRef.current.get(sid) === ctrl) {
          streamAbortsRef.current.delete(sid);
          setSessionStreaming(sid, false);
        }
        settleEntry.resolve();
        if (turnSettleRef.current.get(sid) === settleEntry) {
          turnSettleRef.current.delete(sid);
        }
      }
    })();
  }, [handleStreamEvent, recoverFromStreamError, replaceMessages, setSessionStreaming]);
  // Wire the forward-reference ref so loadSession / boot / visibility
  // handlers can call the latest tryAttachIfStreaming without a TS
  // before-declaration error. Assigned in render so it's available
  // before any user interaction can call through to it.
  tryAttachIfStreamingRef.current = tryAttachIfStreaming;

  // --- pane lifecycle -------------------------------------------------
  const focusPane = useCallback((key: string) => {
    setFocusedPaneKey(key);
  }, []);

  const closePane = useCallback((key: string) => {
    const cur = panesRef.current;
    if (cur.length <= 1) return; // never collapse to zero windows
    const idx = cur.findIndex((p) => p.key === key);
    if (idx === -1) return;
    const next = cur.filter((p) => p.key !== key);
    setPanes(next);
    // If we closed the focused window, focus its left neighbour.
    if (focusedPaneKeyRef.current === key) {
      setFocusedPaneKey(next[Math.max(0, idx - 1)]?.key ?? next[0].key);
    }
    // Drop the closed window's ephemerals and its pane-scoped draft. A
    // session-scoped draft (`s:<id>`) is kept so reopening the session
    // elsewhere still shows the unsent text. The session itself keeps
    // streaming in the background — streams are keyed by sid, not pane.
    setPaneEphem((prev) => {
      if (!prev.has(key)) return prev;
      const next = new Map(prev);
      next.delete(key);
      return next;
    });
    // Drop the abandoned new-window draft. Deferred so it runs AFTER the
    // closing Composer's unmount flush (which fires during React's commit of
    // the setPanes above) — otherwise that flush would resurrect it.
    // Session-scoped drafts (`s:<id>`) are left intact so reopening the
    // session restores its unsent text.
    window.setTimeout(() => clearDraft(`pane:${key}`), 0);
  }, []);

  // "New Chat Window" — append an empty pane and focus it. The session is
  // created lazily on first send (mirrors ?new=1), so opening windows you
  // never type into doesn't burn session rows.
  const onNewChatWindow = useCallback(() => {
    if (panesRef.current.length >= MAX_PANES) {
      setErrorBanner(`You can open at most ${MAX_PANES} chat windows at once.`);
      return;
    }
    const key = newPaneKey();
    setPanes((prev) => (prev.length >= MAX_PANES ? prev : [...prev, { key, sessionId: null }]));
    setFocusedPaneKey(key);
    if (isMobileViewport()) setUiStack(["minimized-sidebar"]);
  }, []);

  const onSelectSession = useCallback(
    (id: string) => {
      // Deliberately NO abort here. Streams started in other sessions
      // keep going in the background. If the session is already open in a
      // window, just focus that window; otherwise load it into the
      // focused pane.
      const existing = panesRef.current.find((p) => p.sessionId === id);
      if (existing) {
        setFocusedPaneKey(existing.key);
      } else {
        loadSession(id);
      }
      if (isMobileViewport()) setUiStack(["minimized-sidebar"]);
    },
    [loadSession],
  );

  // Open an existing session in an ADDITIONAL window (vs onSelectSession
  // which loads into the focused window). If it's already open, focus it.
  const onOpenSessionInNewWindow = useCallback(
    (id: string) => {
      const existing = panesRef.current.find((p) => p.sessionId === id);
      if (existing) {
        setFocusedPaneKey(existing.key);
        if (isMobileViewport()) setUiStack(["minimized-sidebar"]);
        return;
      }
      if (panesRef.current.length >= MAX_PANES) {
        setErrorBanner(`You can open at most ${MAX_PANES} chat windows at once.`);
        return;
      }
      const key = newPaneKey();
      setPanes((prev) => (prev.length >= MAX_PANES ? prev : [...prev, { key, sessionId: id }]));
      setFocusedPaneKey(key);
      // Hydrate the new window's transcript (no-op if already buffered) and
      // re-attach if the session is mid-stream. We set the pane's session
      // directly above, so we don't route through loadSession (which targets
      // the focused pane and races with the setFocusedPaneKey above).
      (async () => {
        try {
          const full = await getSession(id);
          replaceMessages(full.id, full.messages || []);
          tryAttachIfStreamingRef.current(full.id, full.messages || []);
        } catch (err) {
          if (err instanceof ApiError && err.status === 404) {
            setSessions((prev) => prev.filter((s) => s.id !== id));
            setPanes((prev) => prev.map((p) => (p.key === key ? { ...p, sessionId: null } : p)));
          }
        }
      })();
      if (isMobileViewport()) setUiStack(["minimized-sidebar"]);
    },
    [replaceMessages],
  );

  // "+ New Chat" — reset the focused window to an empty new chat (lazy
  // session creation on first send). Other windows are untouched.
  const onNewChat = useCallback(() => {
    setFocusedSession(null);
    if (isMobileViewport()) setUiStack(["minimized-sidebar"]);
  }, [setFocusedSession]);

  const onRename = useCallback(async (id: string, title: string) => {
    try {
      const updated = await renameSession(id, title);
      setSessions((prev) =>
        prev.map((s) => (s.id === id ? { ...s, title: updated.title, updated_at: updated.updated_at } : s)),
      );
    } catch (err) {
      setErrorBanner(formatError("Failed to rename session", err));
    }
  }, []);

  const onDelete = useCallback(async (id: string) => {
    try {
      await deleteSession(id);
      setSessions((prev) => prev.filter((s) => s.id !== id));
      // Tear down every local trace of the deleted session — buffer,
      // streaming flag, and any in-flight AbortController. The server
      // has already cancelled the run on its side.
      dropSessionState(id);
      // Reset any window showing it to an empty new-chat view.
      setPanes((prev) =>
        prev.map((p) => (p.sessionId === id ? { ...p, sessionId: null } : p)),
      );
    } catch (err) {
      setErrorBanner(formatError("Failed to delete session", err));
    }
  }, [dropSessionState]);

  // Central auth-expiry policy. If `err` is a 401 (session/JWT expired) or a
  // 403 (access revoked), surface it and re-gate to LoginGate — the same path
  // the initial-load 401 uses. Every send-path entry point (create session /
  // upload attachments / stream message) funnels its 401s through here so an
  // expired cookie can never silently swallow a prompt ("send to the void")
  // or leave the user stuck in a chat UI where every action 401s. Returns
  // true when it handled the error, so callers can skip their generic banner.
  const handleAuthExpiry = useCallback((err: unknown): boolean => {
    if (!(err instanceof ApiError) || (err.status !== 401 && err.status !== 403)) {
      return false;
    }
    const expired = err.status === 401;
    setErrorBanner(
      expired
        ? "Your session expired — your last message wasn't sent. Please log in again."
        : "Your access was revoked. Please log in again.",
    );
    setAuthReason(expired ? "anon" : "forbidden");
    return true;
  }, []);

  // Lazily create a session for a specific pane (used by send + attach).
  // Migrates the pane-scoped draft to the new session key so unsent text
  // typed before the session existed isn't orphaned.
  const ensureSessionForPane = useCallback(
    async (paneKey: string): Promise<string | null> => {
      const pane = panesRef.current.find((p) => p.key === paneKey);
      if (pane?.sessionId) return pane.sessionId;
      try {
        const created = await createSession(null, workspace);
        setSessions((prev) => [
          {
            id: created.id,
            title: created.title,
            created_at: created.created_at,
            updated_at: created.updated_at,
            workspace: created.workspace,
          },
          ...prev,
        ]);
        setPanes((prev) =>
          prev.map((p) => (p.key === paneKey ? { ...p, sessionId: created.id } : p)),
        );
        replaceMessages(created.id, []);
        // No draft migration needed: the session is created from handleSend
        // only after trySend already cleared the pane's draft on send.
        return created.id;
      } catch (err) {
        if (!handleAuthExpiry(err)) {
          setErrorBanner(formatError("Failed to create session", err));
        }
        return null;
      }
    },
    [workspace, replaceMessages, handleAuthExpiry],
  );

  // Attaching only stages the files LOCALLY — no session is created and
  // nothing is uploaded until the message is actually sent (handleSend
  // uploads at send time). This keeps the "no session until a message is
  // sent" guarantee even when the user attaches then abandons the draft.
  // Synthesize display-only AttachmentMeta from each File so the Composer
  // can render chips; image previews come from the parallel File objects.
  const onAttach = useCallback((paneKey: string, files: File[]) => {
    if (!files.length) return;
    const metas: AttachmentMeta[] = files.map((f) => ({
      filename: f.name,
      size: f.size,
      mime: f.type,
    }));
    setPaneEphem((prev) => {
      const cur = prev.get(paneKey) ?? defaultPaneEphem(modelRef.current);
      const next = new Map(prev);
      next.set(paneKey, {
        ...cur,
        pendingAttachments: [...cur.pendingAttachments, ...metas],
        pendingFiles: [...cur.pendingFiles, ...files],
      });
      return next;
    });
  }, []);

  const onRemoveAttachment = useCallback((paneKey: string, idx: number) => {
    setPaneEphem((prev) => {
      const cur = prev.get(paneKey);
      if (!cur) return prev;
      const next = new Map(prev);
      next.set(paneKey, {
        ...cur,
        pendingAttachments: cur.pendingAttachments.filter((_, i) => i !== idx),
        pendingFiles: cur.pendingFiles.filter((_, i) => i !== idx),
      });
      return next;
    });
  }, []);

  // Core streaming turn — shared by the composer (handleSend) and the
  // fork/resend path. All per-message options come in via `cfg` so it
  // doesn't read any pane state directly.
  const runTurn = useCallback(
    async (
      sid: string,
      text: string,
      cfg: {
        model: ModelChoice;
        effort: string | null;
        webSearch: boolean;
        imageGen: boolean;
        attachments: AttachmentMeta[];
      },
    ) => {
      // Apply web-search preamble client-side so the prefix is visible in
      // the user's own bubble. Image-mode is a backend route (`mode`) so
      // the user's text is left unmodified.
      const isImageMode = cfg.imageGen;
      const userVisibleText =
        cfg.webSearch && !isImageMode
          ? `${text}\n\n[Use the WebSearch tool to research this question.]`
          : text;
      // `pending: true` on BOTH optimistic messages — see Message.pending.
      // Until the POST comes back 2xx these exist only in this tab, and the
      // merge is required to carry them through untouched. Without the flag
      // a server snapshot arriving mid-send (the recovery poll, a wake that
      // landed between idle polls) reconciled positionally and ate the
      // user's own bubble.
      const userMsg: Message = {
        role: "user",
        content: userVisibleText,
        ts: nowIso(),
        pending: true,
        ...(cfg.attachments.length ? { attachments: cfg.attachments } : {}),
      };
      // status: "streaming" so the bubble can describe itself if it ever gets
      // rendered before the first delta — Thread's blank-state fallback keys
      // off status, and an untagged placeholder would read "no reply" while
      // the turn was in fact still running.
      const placeholder: Message = {
        role: "assistant", content: "", ts: nowIso(), status: "streaming",
        pending: true,
      };
      // Drop any pending pair left behind by an earlier send that failed
      // outright. This keeps "pending messages are a single run at the tail"
      // true, which is the invariant mergePreservingLiveTail reconciles
      // against — a pending pair stranded in the MIDDLE of the buffer would
      // put the positional reasoning back exactly where this bug came from.
      // Nothing is lost: a failed send's text was handed back to the
      // composer, so this send either is it or supersedes it.
      updateMessages(sid, (prev) => {
        const kept = prev.some((m) => m.pending)
          ? prev.filter((m) => !m.pending)
          : prev;
        return [...kept, userMsg, placeholder];
      });
      setSessionStreaming(sid, true);
      setErrorBanner(null);

      const ctrl = new AbortController();
      streamAbortsRef.current.set(sid, ctrl);

      // Register a "settled" promise so a later interrupt-then-send can wait
      // for THIS turn to fully wind down before replacing it.
      let settleResolve: () => void = () => {};
      const settleEntry = {
        promise: new Promise<void>((res) => {
          settleResolve = res;
        }),
        resolve: () => settleResolve(),
      };
      turnSettleRef.current.set(sid, settleEntry);

      const titleRefreshTimer = window.setTimeout(() => {
        getSessions(workspace)
          .then((ss) => setSessions(ss))
          .catch(() => {
            /* ignore */
          });
      }, 6000);

      // Flipped by streamMessage the instant the POST returns 2xx, which is
      // also the instant the server has durably appended `userMsg` and
      // `placeholder`. Everything that goes wrong before this point means
      // the message was never sent; everything after means a worker is
      // running and only the browser's view of it was lost.
      let accepted = false;
      const markAccepted = (): void => {
        if (accepted) return;
        accepted = true;
        // The server owns these two messages now, so snapshots are
        // authoritative for them again. Cleared by object identity rather
        // than by scanning for `pending`, so an EARLIER send that failed
        // outright keeps its flag and stays on screen.
        updateMessages(sid, (prev) => {
          let changed = false;
          const next = prev.map((m) => {
            if (m !== userMsg && m !== placeholder) return m;
            changed = true;
            return { ...m, pending: false };
          });
          return changed ? next : prev;
        });
      };

      try {
        // The POST is answered 409 ("another turn is already in flight for
        // this session") in two quite different situations:
        //
        //   * The handoff race. The user's own previous turn was just
        //     cancelled and its worker has emitted the terminal event but
        //     hasn't yet retired from `_active_runs`. Waiting a beat is all
        //     this needs, and it resolves on its own.
        //   * A turn this tab never started — a scheduled wake, a send from
        //     another tab or another device, a CLI auto-continuation. Nothing
        //     retires on its own here; it can hold the session for minutes.
        //     Pressing send is an explicit interrupt (handleSend already
        //     cancels for turns it CAN see locally, via streamAbortsRef), so
        //     escalate to a cancel and take the session over rather than
        //     spinning until the retry budget runs out.
        //
        // The one thing that must never happen on 409 is falling through to
        // recoverFromStreamError. That polls somebody else's run, finds it
        // finished, and returns happy — while this user's message was never
        // written anywhere. Only the pre-stream POST can 409, so a retry
        // re-sends nothing partial.
        const sendStartedAt = Date.now();
        let cancelIssued = false;
        for (;;) {
          try {
            await streamMessage(sid, userVisibleText, (evt) => handleStreamEvent(sid, evt), {
              signal: ctrl.signal,
              model: cfg.model,
              effort: cfg.effort,
              mode: isImageMode ? "image" : "chat",
              onAccepted: markAccepted,
            });
            break;
          } catch (sendErr) {
            const conflict = sendErr instanceof ApiError && sendErr.status === 409;
            const elapsed = Date.now() - sendStartedAt;
            if (!conflict || ctrl.signal.aborted || elapsed >= SEND_CONFLICT_DEADLINE_MS) {
              throw sendErr;
            }
            if (!cancelIssued && elapsed >= SEND_CONFLICT_ESCALATE_MS) {
              cancelIssued = true;
              try {
                await cancelTurn(sid);
              } catch {
                /* 409 here just means the run retired on its own */
              }
            }
            await new Promise((r) => window.setTimeout(r, SEND_CONFLICT_RETRY_MS));
            continue;
          }
        }
      } catch (err) {
        if (handleAuthExpiry(err)) {
          // Auth lapsed mid-send (401 = session/JWT expired, 403 = revoked).
          // The POST was rejected outright, so NO worker exists — polling via
          // recoverFromStreamError would getSession()-401 for 120s (swallowed),
          // then give up, leaving the bubble hung in "streaming" with no signal
          // that the session died. That is the "prompt sent to the void" bug.
          // handleAuthExpiry has surfaced the banner + re-gated to LoginGate;
          // here we additionally finalize the hung assistant placeholder so it
          // doesn't sit in "streaming" forever behind the login screen.
          const expired = err instanceof ApiError && err.status === 401;
          const note = expired
            ? "your session expired — please log in again to keep chatting"
            : "your access to this chat was revoked";
          updateMessages(sid, (prev) => {
            const i = prev.length - 1;
            if (i < 0 || prev[i].role !== "assistant") return prev;
            const next = prev.slice();
            next[i] = {
              ...next[i],
              status: "error",
              content:
                (next[i].content || "") +
                `\n\n_⚠ ${note}. Your last message was not sent._`,
            };
            return next;
          });
        } else if (!accepted && (err as { name?: string })?.name !== "AbortError") {
          // The server never acknowledged the POST, so the user's message
          // exists NOWHERE but this tab: no worker was spawned and no reply
          // is ever coming.
          //
          // The old code gated this branch on `err instanceof ApiError &&
          // status !== 409`, which let two failure shapes slip past it into
          // the recovery poll below:
          //
          //   * 409 after the retry budget — somebody else's turn owns the
          //     session. We poll THEIR run, watch it finish, and report
          //     success for a message we never sent.
          //   * A transport-level failure of the POST itself, which is not
          //     an ApiError at all. This is the one that bit in practice:
          //     post_message used to block on the per-session lock for the
          //     entire duration of the in-flight turn, so while the model
          //     was mid-reply the request sat there with no response
          //     headers until the edge timed it out (Cloudflare, 100s) —
          //     routinely, because agent turns run for minutes. (The
          //     backend now answers 409 after SESSION_LOCK_WAIT_SEC instead
          //     of blocking, but the client must not depend on that.)
          //
          // Either way the poll finds a finished session, returns quietly,
          // and the next server snapshot overwrites the local buffer — which
          // is how a message vanished from the thread mid-response. So:
          // report it, mark the bubble, and hand the text back to the
          // composer. `pending` stays TRUE on both messages so no snapshot
          // can erase the evidence before the user has seen it.
          const reason = err instanceof ApiError
            ? apiErrorMessage(err)
            : (err instanceof Error && err.message) || "the connection dropped";
          setErrorBanner(`Message not sent: ${reason}`);
          updateMessages(sid, (prev) => {
            const ai = prev.indexOf(placeholder);
            if (ai < 0) return prev;
            const next = prev.slice();
            next[ai] = {
              ...next[ai],
              status: "error",
              content:
                (next[ai].content || "") +
                `\n\n_⚠ Your message was not sent — ${reason}. It has been put` +
                ` back in the message box._`,
            };
            // Tag the user's own bubble too. Its text is about to reappear in
            // the composer, and an untagged bubble would read as a message
            // that went through — leaving the user unsure whether resending
            // would say it twice.
            const ui = next.indexOf(userMsg);
            if (ui >= 0) next[ui] = { ...next[ui], status: "error" };
            return next;
          });
          // Put the text back where the user can resend it. Attachments were
          // already uploaded to the session and are still staged server-side,
          // so only the prose needs restoring.
          restoreDraft(sid, text);
        } else if ((err as { name?: string })?.name !== "AbortError") {
          // Transport-level interruption (tab-switch / network drop /
          // throttled background tab). The worker is still running and
          // will persist the final assistant message — silently poll and
          // AWAIT so `streaming` stays true until the server finalizes.
          try {
            await recoverFromStreamError(sid);
          } catch {
            /* ignore — poll-side errors are non-fatal */
          }
        }
      } finally {
        setSessionStreaming(sid, false);
        if (streamAbortsRef.current.get(sid) === ctrl) {
          streamAbortsRef.current.delete(sid);
        }
        // Unblock any interrupt-then-send waiting on this turn to wind down.
        settleEntry.resolve();
        if (turnSettleRef.current.get(sid) === settleEntry) {
          turnSettleRef.current.delete(sid);
        }
        window.clearTimeout(titleRefreshTimer);
        try {
          setSessions(await getSessions(workspace));
        } catch {
          /* ignore */
        }
      }
    },
    [workspace, updateMessages, setSessionStreaming, handleStreamEvent, recoverFromStreamError, handleAuthExpiry],
  );

  // Interrupt the in-flight turn for a session and resolve once it has fully
  // wound down. Used by interrupt-then-send (the user typed + submitted while
  // a turn was streaming, claude.ai-style): we cancel the server turn and
  // AWAIT the local stream's settle, so the interrupted bubble is finalized
  // and the run retired from `_active_runs` before the replacement starts.
  const interruptTurn = useCallback(async (sid: string) => {
    try {
      await cancelTurn(sid); // 202/409 both fine (409 = already finished)
    } catch (err) {
      // Non-fatal — proceed to wait; the replacement send retries on a 409 race.
      setErrorBanner(formatError("Interrupt failed", err));
    }
    const entry = turnSettleRef.current.get(sid);
    if (entry) {
      // Bounded: never hang the new send if the old turn somehow never settles.
      await Promise.race([
        entry.promise,
        new Promise<void>((r) => window.setTimeout(r, 5000)),
      ]);
    }
  }, []);

  // Composer send for a specific window. Resolves (lazily creates) the
  // pane's session, snapshots that pane's composer ephemerals, clears
  // them, then runs the turn.
  const handleSend = useCallback(
    async (paneKey: string, text: string) => {
      const trimmed = text.trim();
      const eph = paneEphemRef.current.get(paneKey) ?? defaultPaneEphem(modelRef.current);
      // Allow an attachments-only send (no text). pendingFiles are the
      // freshly-staged uploads; pendingAttachments covers rehydrated meta.
      const hasAttachments =
        eph.pendingFiles.length > 0 || eph.pendingAttachments.length > 0;
      if (!trimmed && !hasAttachments) return;
      if (sendingPanesRef.current.has(paneKey)) return;
      sendingPanesRef.current.add(paneKey);
      // The Composer clears its box synchronously on send; every early
      // return below must hand the text back or it is silently lost.
      const giveBack = (sid: string | null) => {
        if (!trimmed) return;
        if (sid) {
          restoreDraft(sid, text);
        } else {
          const key = `pane:${paneKey}`;
          const existing = getDraft(key);
          if (!existing.includes(trimmed)) setDraft(key, existing ? `${text}\n\n${existing}` : text);
        }
      };
      try {
      const sid = await ensureSessionForPane(paneKey);
      if (!sid) {
        giveBack(null);
        return;
      }
      // Mid-stream submit (claude.ai-style): a turn is already streaming for
      // this session. Interrupt it and wait for it to wind down before we
      // append the new user turn, so the interrupted and new bubbles don't
      // overlap and the server frees the session for the replacement turn.
      if (streamAbortsRef.current.has(sid)) {
        await interruptTurn(sid);
      }
      // Backstop for the Composer's paste-to-attachment rule: text can also
      // get oversized by typing, by several individually-small pastes, or by
      // a draft restored from before that rule existed. Spill the whole
      // message into a .txt attachment rather than POSTing something the
      // server will 413. See limits.ts for why 413 used to be invisible.
      let textToSend = text;
      const filesToUpload = eph.pendingFiles.slice();
      if (exceedsTextBudget(text)) {
        if (filesToUpload.length >= MAX_FILES_PER_TURN) {
          setErrorBanner(
            `Message is too long to send (over ${Math.floor(maxMessageBytes() / 1024)} KB) ` +
              `and all ${MAX_FILES_PER_TURN} attachment slots are full — ` +
              `remove an attachment or shorten the message.`,
          );
          giveBack(sid);
          return;
        }
        const overflowName = "long-message.txt";
        filesToUpload.push(makeTextFile(text, overflowName));
        textToSend =
          `[This message was too long for the chat box, so its full text is ` +
          `attached as ${overflowName}. Read that file — it is the actual message.]`;
      }
      // Upload any staged attachments now that we have a session. On failure
      // abort the send (keep the draft + files) so the user can retry.
      let attachments = eph.pendingAttachments;
      if (filesToUpload.length) {
        try {
          attachments = await uploadAttachments(sid, filesToUpload);
        } catch (err) {
          if (!handleAuthExpiry(err)) {
            setErrorBanner(formatError("Attachment upload failed", err));
          }
          giveBack(sid);
          return;
        }
      }
      // Reset per-send ephemerals (toggles + attachments) for this pane.
      setPaneEphem((prev) => {
        const cur = prev.get(paneKey) ?? defaultPaneEphem(modelRef.current);
        const next = new Map(prev);
        next.set(paneKey, {
          ...cur,
          webSearchOn: false,
          imageGenOn: false,
          pendingAttachments: [],
          pendingFiles: [],
        });
        return next;
      });
      // (Draft clearing is handled by the Composer itself on send.)
      // Effort for the pane's model, re-validated against its supported
      // levels (a stored level for a different model must not leak into
      // this turn's payload).
      const ephLevels = EFFORT_LEVELS[eph.model];
      const ephEffort =
        ephLevels && effortMapRef.current[eph.model] && ephLevels.includes(effortMapRef.current[eph.model])
          ? effortMapRef.current[eph.model]
          : null;
      await runTurn(sid, textToSend, {
        model: eph.model,
        effort: ephEffort,
        webSearch: eph.webSearchOn,
        imageGen: eph.imageGenOn,
        attachments,
      });
      } finally {
        sendingPanesRef.current.delete(paneKey);
      }
    },
    [ensureSessionForPane, runTurn, interruptTurn, handleAuthExpiry],
  );

  // Cancel the in-flight turn for a specific window's session.
  const onCancel = useCallback(async (sid: string | null) => {
    if (!sid) return;
    try {
      await cancelTurn(sid);
    } catch (err) {
      setErrorBanner(formatError("Cancel failed", err));
    }
  }, []);

  // ---- session metadata mutations (star / archive / folder) ------------
  const applyMetadataPatch = useCallback(
    (id: string, patch: Partial<Pick<SessionSummary, "starred" | "archived" | "folder">>) => {
      setSessions((prev) =>
        prev.map((s) => (s.id === id ? { ...s, ...patch } : s)),
      );
    },
    [],
  );

  const onToggleStar = useCallback(async (id: string, starred: boolean) => {
    applyMetadataPatch(id, { starred });
    try {
      await updateSession(id, { starred });
    } catch (err) {
      applyMetadataPatch(id, { starred: !starred });
      setErrorBanner(formatError("Toggle star failed", err));
    }
  }, [applyMetadataPatch]);

  const onToggleArchive = useCallback(async (id: string, archived: boolean) => {
    applyMetadataPatch(id, { archived });
    try {
      await updateSession(id, { archived });
    } catch (err) {
      applyMetadataPatch(id, { archived: !archived });
      setErrorBanner(formatError("Toggle archive failed", err));
    }
  }, [applyMetadataPatch]);

  const onSetFolder = useCallback(async (id: string, folder: string | null) => {
    const prevFolder = sessions.find((s) => s.id === id)?.folder ?? null;
    applyMetadataPatch(id, { folder });
    try {
      await updateSession(id, { folder });
    } catch (err) {
      applyMetadataPatch(id, { folder: prevFolder });
      setErrorBanner(formatError("Set folder failed", err));
    }
  }, [sessions, applyMetadataPatch]);

  // ---- search ----------------------------------------------------------
  useEffect(() => {
    const q = searchQuery.trim();
    if (!q) {
      setSearchResults([]);
      setSearching(false);
      return;
    }
    setSearching(true);
    const handle = window.setTimeout(() => {
      searchSessions(q)
        .then((rs) => setSearchResults(rs))
        .catch((err) => setErrorBanner(formatError("Search failed", err)))
        .finally(() => setSearching(false));
    }, 250);
    return () => window.clearTimeout(handle);
  }, [searchQuery]);

  // ---- branching: edit a past user message and resend in a new session.
  // The new session inherits messages with seq < fromSeq; the worker
  // assembles a context preamble in claude_runner.run_turn so the new
  // claude session continues coherently.
  // Fork happens from the focused window: branch its session, load the
  // fork into that same window, and resend with the focused pane's model.
  const onForkAndResend = useCallback(
    async (fromSeq: number, newText: string) => {
      const srcPaneKey = focusedPaneKeyRef.current;
      const srcPane = panesRef.current.find((p) => p.key === srcPaneKey);
      const srcSid = srcPane?.sessionId ?? null;
      if (!srcSid) return;
      try {
        const created = await forkSession(srcSid, fromSeq, newText);
        setSessions((prev) => [
          { ...created },
          ...prev.filter((s) => s.id !== created.id),
        ]);
        const full: Session = await getSession(created.id);
        replaceMessages(created.id, full.messages || []);
        setPanes((prev) =>
          prev.map((p) => (p.key === srcPaneKey ? { ...p, sessionId: created.id } : p)),
        );
        const eph = paneEphemRef.current.get(srcPaneKey) ?? defaultPaneEphem(modelRef.current);
        const forkLevels = EFFORT_LEVELS[eph.model];
        const forkEffort =
          forkLevels && effortMapRef.current[eph.model] && forkLevels.includes(effortMapRef.current[eph.model])
            ? effortMapRef.current[eph.model]
            : null;
        await runTurn(created.id, newText, {
          model: eph.model,
          effort: forkEffort,
          webSearch: false,
          imageGen: false,
          attachments: [],
        });
      } catch (err) {
        setErrorBanner(formatError("Fork failed", err));
      }
    },
    [runTurn, replaceMessages],
  );

  const collapsed = uiTop === "minimized-sidebar";
  const dashboardOpen =
    uiTop === "dashboard:agents" ||
    uiTop === "dashboard:usage" ||
    uiTop === "dashboard:settings" ||
    uiTop === "dashboard:about";

  // Apply per-user auto-archive: hide untagged, non-starred sessions
  // older than N days from the sidebar's All view. Non-destructive —
  // they're still under the Archived filter via the regular archived
  // flag (which we don't touch here). 0 = off.
  const sessionsForSidebar = (() => {
    const days = settings.auto_archive_days;
    if (!days || days <= 0) return sessions;
    const cutoff = Date.now() - days * 24 * 60 * 60 * 1000;
    return sessions.filter((s) => {
      if (s.starred || s.folder || s.archived) return true;
      const t = s.updated_at ? Date.parse(s.updated_at) : 0;
      return !t || t >= cutoff;
    });
  })();

  if (needsAuth) {
    return <LoginGate reason={authReason === "forbidden" ? "forbidden" : "anon"} />;
  }
  // auth.ald3.com is sign-in-only. If /api/me is still in flight we render
  // the login card immediately rather than flashing the chat shell; the
  // boot effect above already redirects away once we know the user is in.
  if (isAuthHost) {
    return <LoginGate reason="anon" />;
  }

  return (
    <LanguageContext.Provider value={displayLang}>
    <div className={`app-shell ${collapsed ? "is-collapsed" : ""} ${dashboardOpen ? "has-dashboard" : ""}`}>
      <Sidebar
        sessions={sessionsForSidebar}
        currentSessionId={currentSessionId}
        uiTop={uiTop}
        filter={filter}
        onFilterChange={setFilter}
        searchQuery={searchQuery}
        onSearchQueryChange={setSearchQuery}
        searchResults={searchResults}
        searching={searching}
        onChevron={onChevron}
        onNewChat={onNewChat}
        onNewChatWindow={onNewChatWindow}
        canOpenWindow={panes.length < MAX_PANES}
        onSelectSession={onSelectSession}
        onOpenSessionInNewWindow={onOpenSessionInNewWindow}
        onRename={onRename}
        onDelete={onDelete}
        onToggleStar={onToggleStar}
        onToggleArchive={onToggleArchive}
        onSetFolder={onSetFolder}
        onOpenAgents={() => pushUi("dashboard:agents")}
        onOpenUsage={() => pushUi("dashboard:usage")}
        onOpenSettings={() => pushUi("dashboard:settings")}
        onOpenAbout={() => pushUi("dashboard:about")}
        me={me}
        workspace={workspace}
        onWorkspaceChange={updateWorkspace}
      />
      {dashboardOpen && uiTop === "dashboard:agents" && (
        <Suspense fallback={<div className="dashboard-empty">Loading…</div>}>
          <AgentsDashboard onClose={onChevron} visible={true} />
        </Suspense>
      )}
      {dashboardOpen && uiTop === "dashboard:usage" && (
        <Suspense fallback={<div className="dashboard-empty">Loading…</div>}>
          <UsageDashboard onClose={onChevron} visible={true} />
        </Suspense>
      )}
      {dashboardOpen && uiTop === "dashboard:settings" && (
        <Suspense fallback={<div className="dashboard-empty">Loading…</div>}>
          <SettingsDashboard
            onClose={onChevron}
            visible={true}
            onChange={(next) => {
              setSettings(next);
              if (next.default_model && next.default_model !== "default") {
                setModel(next.default_model);
              }
            }}
          />
        </Suspense>
      )}
      {dashboardOpen && uiTop === "dashboard:about" && (
        <Suspense fallback={<div className="dashboard-empty">Loading…</div>}>
          <AboutDashboard onClose={onChevron} visible={true} />
        </Suspense>
      )}
      <main className="thread-main">
        {errorBanner && (
          <div className="error-banner" role="alert">
            <span>{errorBanner}</span>
            <button
              type="button"
              className="error-dismiss"
              onClick={() => setErrorBanner(null)}
              aria-label="Dismiss error"
            >
              ✕
            </button>
          </div>
        )}
        <div
          className={`thread-panes layout-${settings.window_layout} panes-${Math.min(panes.length, MAX_PANES)}`}
        >
          {panes.map((pane) => {
            const sid = pane.sessionId;
            const sess = sid ? sessions.find((s) => s.id === sid) : undefined;
            const ephem = getPaneEphem(pane.key);
            const draftKey = draftKeyForPane(pane);
            return (
              <ChatPane
                key={pane.key}
                paneKey={pane.key}
                sessionId={sid}
                title={sess?.title ?? ""}
                messages={sid ? messagesBySession.get(sid) ?? [] : []}
                streaming={sid ? streamingSessions.has(sid) : false}
                focused={pane.key === focusedPaneKey}
                showHeader={panes.length > 1}
                canClose={panes.length > 1}
                onFocus={() => focusPane(pane.key)}
                onClose={() => closePane(pane.key)}
                onForkAndResend={onForkAndResend}
                draftKey={draftKey}
                onSend={(t) => handleSend(pane.key, t)}
                onCancel={() => onCancel(sid)}
                onAttach={(files) => onAttach(pane.key, files)}
                pendingAttachments={ephem.pendingAttachments}
                pendingFiles={ephem.pendingFiles}
                onRemoveAttachment={(idx) => onRemoveAttachment(pane.key, idx)}
                model={ephem.model}
                onModelChange={(m) => {
                  updatePaneEphem(pane.key, { model: m });
                }}
                effort={effortMap[ephem.model] ?? null}
                onEffortChange={(lvl) => {
                  // Per-model effort preference, persisted immediately.
                  // The Composer only calls this with a level valid for the
                  // pane's current model (or null to clear).
                  setEffortMap((prev) => {
                    const next = { ...prev };
                    if (lvl) next[ephem.model] = lvl;
                    else delete next[ephem.model];
                    try {
                      localStorage.setItem(EFFORT_PREF_KEY, JSON.stringify(next));
                    } catch {
                      /* localStorage unavailable — memory-only is fine */
                    }
                    return next;
                  });
                }}
                webSearchOn={ephem.webSearchOn}
                onToggleWebSearch={() =>
                  updatePaneEphem(pane.key, { webSearchOn: !ephem.webSearchOn })
                }
                imageGenOn={ephem.imageGenOn}
                onToggleImageGen={() =>
                  updatePaneEphem(pane.key, { imageGenOn: !ephem.imageGenOn })
                }
                sendOnEnter={settings.send_on_enter}
                // File picker scopes to the pane's session workspace so
                // attaching from a shared session browses the shared
                // container; falls back to the default workspace for a
                // not-yet-created new chat.
                workspace={(sess?.workspace ?? workspace) as Workspace}
              />
            );
          })}
        </div>
      </main>
    </div>
    </LanguageContext.Provider>
  );
}

function formatError(prefix: string, err: unknown): string {
  if (err instanceof ApiError) {
    return `${prefix} (${err.status} ${err.message})`;
  }
  if (err instanceof Error) {
    return `${prefix}: ${err.message}`;
  }
  return prefix;
}
