/**
 * Typed wrappers around the chat backend API + a fetch-based SSE consumer.
 *
 * Why fetch + ReadableStream instead of EventSource?
 *
 *   1. EventSource cannot send custom request headers. It works with
 *      cookies, so in production (where Cloudflare Access stamps a cookie
 *      onto every request) it would in principle suffice — but...
 *   2. ...the message-send endpoint is POST, not GET. EventSource is
 *      GET-only. We need POST so the user's text isn't smeared across the
 *      query string and so a long prompt isn't bounced by URL-length
 *      limits.
 *
 * Auth model:
 *
 *   * Production: Cloudflare Access injects ``Cf-Access-Jwt-Assertion`` on
 *     every same-origin request and also sets a session cookie. Browsers
 *     send the cookie automatically with ``credentials: "same-origin"``,
 *     and Cloudflare's edge translates that into the JWT header before the
 *     request reaches our origin. The frontend therefore does NOT carry
 *     any JWT itself; it just uses ``credentials: "same-origin"``.
 *   * Local dev (``npm run dev`` + Vite proxy): there is no Cloudflare
 *     Access in front, so the backend will 401. A dev-mode auth bypass is
 *     out of scope for Phase 3 and is documented in PROGRESS.md as a
 *     follow-up.
 */

// ---------------------------------------------------------------------------
// Types: a 1:1 mirror of the JSON shapes returned by the backend.
// ---------------------------------------------------------------------------

// "admin" is admin-role-only — backend 403s non-admin requests for it.
// The Sidebar only renders the Admin option when me.role === "admin".
export type Workspace = "personal" | "shared" | "admin";

export interface SessionSummary {
  id: string;
  title: string | null;
  created_at: string;
  updated_at: string;
  starred?: boolean;
  archived?: boolean;
  folder?: string | null;
  // Locked at session creation: which container the session's turns
  // dispatch into. Absent on legacy pre-toggle sessions (treated as
  // "personal"). The Sidebar shows a "shared" badge when set.
  workspace?: Workspace | null;
}

export interface SearchResult extends SessionSummary {
  snippet: string;
}

// Lineup since 2026-09-28: the TokenHub-hosted glm/kimi lead the picker and
// the Anthropic/claude aliases were removed from chat.wizerith.ai. "default"
// stays in the union as a legacy sentinel (old localStorage values / a stale
// tab) — it is no longer offered as a picker option and the backend maps it
// to the default model.
export type ModelChoice =
  | "default"
  | "glm"
  | "kimi"
  | "mimo"
  | "mimo-flash"
  | "qwen"
  | "deepseek"
  | "minimax"
  | "gemma4-local";

export const MODEL_LABELS: Record<ModelChoice, string> = {
  default: "Auto",
  glm: "GLM-5.3",
  kimi: "Kimi K3",
  mimo: "MiMo V2.6 Pro",
  "mimo-flash": "MiMo V2.6 Flash",
  qwen: "Qwen3.5 397B",
  deepseek: "DeepSeek V4",
  minimax: "MiniMax M2.7",
  "gemma4-local": "Gemma4 (Local)",
};

// Reasoning-effort levels per model, mirroring the backend's verified
// EFFORT_LEVELS table. Models without an entry offer no effort control —
// the composer hides the effort selector for them. "none" is the
// OpenAI-style "disable reasoning" level some gateways accept.
export const EFFORT_LEVELS: Partial<Record<ModelChoice, string[]>> = {
  glm: ["low", "high", "max"],
  kimi: ["low", "high", "max"],
  mimo: ["low", "medium", "high"],
  "mimo-flash": ["low", "medium", "high"],
  qwen: ["none", "low", "medium", "high"],
  deepseek: ["none", "low", "medium", "high", "max"],
  minimax: ["none", "low", "medium", "high"],
};

export interface AttachmentMeta {
  filename: string;
  size: number;
  mime: string;
}

/**
 * Phase 4: assistant placeholder lifecycle.
 *   * ``streaming`` — the worker is mid-run; content is partial.
 *   * ``complete`` — the worker emitted ``done`` and the full text is
 *     persisted.
 *   * ``error`` — the run aborted (subprocess crash / malformed
 *     stream / persistence failure / "no terminal" fallthrough).
 *   * ``cancelled`` — the user pressed Stop (POST /cancel).
 *
 * ``status`` is set on assistant messages only.  Absent on user
 * messages and on legacy assistant messages from completed
 * pre-Phase-2 turns; the SPA treats absent-status as ``complete``
 * for non-empty assistant content and as ``error`` for empty
 * assistant content (Phase 2 schema-migration rule, preserved here).
 */
export type MessageStatus = "streaming" | "complete" | "error" | "cancelled";

/**
 * Per-turn metadata emitted by BOTH runner paths (claude + haihub) on the
 * terminal ``done`` event and persisted on the assistant message. Every
 * field is optional / nullable: a path that can't source a value (e.g.
 * haihub when the endpoint returns no usage object) sends ``null`` rather
 * than omitting, and the footer renderer skips null fields.
 *
 *   * ``completed_at`` — ISO-8601 UTC timestamp of turn completion.
 *   * ``model``        — display name / id of the producing model.
 *   * ``tokens``       — {input, output, total}; any may be null.
 *   * ``tok_s``        — output tokens / generation wall-clock seconds.
 *   * ``tokens_estimated`` — true when token counts are a length-based
 *                            estimate (haihub fallback), absent otherwise.
 */
export interface MessageMeta {
  completed_at?: string | null;
  model?: string | null;
  tokens?: {
    input?: number | null;
    output?: number | null;
    total?: number | null;
  } | null;
  tok_s?: number | null;
  tokens_estimated?: boolean;
  /**
   * The model's hidden reasoning for this turn, when the provider streamed
   * any (GLM/Kimi ``reasoning_content``, MiniMax ``<think>`` spans, claude
   * ``thinking`` blocks). Persisted so the thinking block survives a reload.
   * How much of it is SHOWN is the client's thinking-display setting.
   */
  reasoning?: string | null;
}

export interface Message {
  role: "user" | "assistant";
  content: string;
  ts: string;
  /**
   * Monotonic per-session sequence number assigned at write time
   * (Phase 2). Optional in TS so client-side optimistic placeholders
   * (which haven't reached the server yet) can omit it; React keys
   * fall back to array index when ``seq`` is undefined.
   */
  seq?: number;
  /** Phase 4 lifecycle marker; assistant messages only. */
  status?: MessageStatus;
  attachments?: AttachmentMeta[];
  /** Per-turn metadata footer; assistant messages only, set on ``done``. */
  meta?: MessageMeta;
  /**
   * What triggered this turn, when it wasn't the user typing. Currently only
   * ``"wake"`` (a scheduled wake fired). A wake turn persists an assistant
   * message with NO preceding user message — that's deliberate server-side,
   * but it meant the reply surfaced in the thread as if the assistant had
   * spontaneously started talking, with nothing on screen explaining why.
   * The Thread renders a small marker when this is set.
   */
  via?: "wake" | null;
  /**
   * CLIENT-ONLY. True while this message exists ONLY in the browser — it
   * was appended optimistically when the user hit send and the server has
   * not acknowledged the POST yet. Never present on a server payload.
   *
   * It exists because a server snapshot must never be allowed to erase a
   * message the server has not seen. ``mergePreservingLiveTail`` used to
   * reconcile local-vs-server positionally, so whenever the server held
   * even ONE message the local buffer lacked (a scheduled wake that fired
   * between polls, a turn sent from another tab), the positional offset
   * consumed the user's own just-sent bubble — that is the "my message
   * disappeared while the AI was replying" bug. Pending messages are now
   * carried across every merge verbatim, and the flag is cleared only once
   * the server has confirmed the write.
   */
  pending?: boolean;
  /**
   * CLIENT-ONLY. Reasoning text accumulated from live ``reasoning`` stream
   * events for the message currently streaming. On ``done`` the persisted
   * copy arrives as ``meta.reasoning``; this field is only the live buffer.
   */
  reasoning?: string;
}

export interface Session extends SessionSummary {
  email: string;
  claude_session_id: string;
  messages: Message[];
}

export type StreamEvent =
  | { type: "delta"; text: string }
  /** Hidden-reasoning delta. Display-only; never part of the reply text. */
  | { type: "reasoning"; text: string }
  | { type: "tool_start"; name: string; input_summary: string }
  | { type: "tool_end"; name: string }
  | { type: "done"; full_text: string; meta?: MessageMeta | null }
  | { type: "error"; message: string }
  // Phase 4: ``cancelled`` is emitted as a terminal frame by the
  // worker when the user pressed Stop. Same shape as ``done`` — the
  // assistant placeholder's ``status`` field on the persisted message
  // distinguishes the two for the load path; the SSE consumer treats
  // ``cancelled`` like ``done`` (terminal, ``full_text`` is the
  // partial content the agent produced before cancellation).
  | { type: "cancelled"; full_text: string; meta?: MessageMeta | null }
  // Side-channel notifications the worker emits just before the terminal
  // frame. They carry no payload — they only tell the client that some
  // out-of-band state changed this turn, so an open panel can refresh
  // instead of waiting for its poll. Both were being emitted by the
  // backend and silently dropped here because they weren't in this union
  // (and had no branch in handleStreamEvent): the composer's scheduled-wake
  // pill lagged its 20s poll behind a wake the model had just armed, and
  // the memory panel never refreshed at all.
  | { type: "schedules_updated" }
  | { type: "memory_updated" };

/**
 * Authenticated user's role. Resolved server-side from
 * ``sandbox_users.json`` (if present) or by matching against
 * ``FELIX_EMAIL``; the value is one of two literals so callers can
 * switch on it without widening to ``string``.
 */
export type Role = "admin" | "user";

export type ThemeChoice = "dark" | "light" | "system";
export type WindowLayout = "columns" | "grid";

// Supported languages, site-wide. English + Simplified/Traditional Chinese.
// Codes match storage.py:_VALID_LANGUAGES. Used for both the UI display
// language (now driven by the shared topbar, see App.tsx) and the model's
// response language (output_language, still a chat-local setting).
export type LanguageChoice = "en" | "zh-CN" | "zh-TW";

export const LANGUAGE_LABELS: Record<LanguageChoice, string> = {
  "en": "English",
  "zh-CN": "简体中文 (Simplified Chinese)",
  "zh-TW": "繁體中文 (Traditional Chinese)",
};

/**
 * Per-user settings, stored server-side at <user_dir>/_settings.json.
 * Schema mirrors storage.py:DEFAULT_SETTINGS — all fields always
 * present in /api/settings responses (server fills missing keys with
 * defaults).
 */
export interface UserSettings {
  default_model: ModelChoice;
  send_on_enter: boolean;
  persona: string;
  notify_on_complete: boolean;
  theme: ThemeChoice;
  show_token_costs: boolean;
  auto_archive_days: number;
  // Split as of the language-split commit: ui_language drives sidebar /
  // composer / settings labels via i18n.ts, output_language drives the
  // model's response language via the prompt block in claude_runner.
  ui_language: LanguageChoice;
  output_language: LanguageChoice;
  // Multi-window tiling: "columns" = every window side by side (default),
  // "grid" = grow into a 2×2 (3rd window on top, 4th completes the grid).
  window_layout: WindowLayout;
}

export const DEFAULT_USER_SETTINGS: UserSettings = {
  default_model: "kimi",
  send_on_enter: true,
  persona: "",
  notify_on_complete: false,
  theme: "dark",
  show_token_costs: false,
  auto_archive_days: 0,
  ui_language: "en",
  output_language: "en",
  window_layout: "columns",
};

export interface MeResponse {
  email: string;
  role: Role;
  settings?: UserSettings;
  // True iff this deployment has set WIZERITH_SHARED_CONTAINER_NAME.
  // The Sidebar uses this to conditionally render the workspace toggle
  // — tenants without a shared container never see the control.
  shared_workspace_enabled?: boolean;
  // Server-side cap on a single message's text (CHAT_MAX_MESSAGE_BYTES).
  // Fed into limits.setMaxMessageBytes at boot so the composer converts
  // oversized text to a .txt attachment instead of eating a 413.
  max_message_bytes?: number;
}

// ---------------------------------------------------------------------------
// Plumbing.
// ---------------------------------------------------------------------------

const baseFetchInit: RequestInit = {
  credentials: "same-origin",
  cache: "no-store",
};

async function jsonRequest<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const resp = await fetch(path, {
    ...baseFetchInit,
    ...init,
    headers: {
      "Accept": "application/json",
      ...(init.body && !(init.body instanceof FormData)
        ? { "Content-Type": "application/json" }
        : {}),
      ...(init.headers || {}),
    },
  });
  if (!resp.ok) {
    let detail: unknown = undefined;
    try {
      detail = await resp.json();
    } catch {
      // body wasn't JSON; ignore
    }
    throw new ApiError(resp.status, resp.statusText, detail);
  }
  if (resp.status === 204) {
    return undefined as unknown as T;
  }
  return (await resp.json()) as T;
}

export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, statusText: string, detail: unknown) {
    super(`HTTP ${status} ${statusText}`);
    this.status = status;
    this.detail = detail;
  }
}

/** FastAPI errors carry a human-readable string on ``detail``. Pull it out
 *  for display, falling back to the bare status line. */
export function apiErrorMessage(err: ApiError): string {
  const d = err.detail;
  if (typeof d === "string" && d) return d;
  if (d && typeof d === "object") {
    const inner = (d as { detail?: unknown }).detail;
    if (typeof inner === "string" && inner) return inner;
  }
  return err.message;
}

// ---------------------------------------------------------------------------
// Endpoint wrappers.
// ---------------------------------------------------------------------------

export function getMe(): Promise<MeResponse> {
  return jsonRequest<MeResponse>("/api/me");
}

export function getSettings(): Promise<UserSettings> {
  return jsonRequest<UserSettings>("/api/settings");
}

export function putSettings(patch: Partial<UserSettings>): Promise<UserSettings> {
  return jsonRequest<UserSettings>("/api/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
}

// Cross-session memory: a freeform markdown blob the model reads in front
// of every turn and may rewrite via a sentinel block. The dashboard lets
// users inspect/hand-edit/clear it from the Settings UI. Returned as
// {text} (server uses the same shape for PUT).
// Scheduled wakes: durable timers the model sets for a session ("ping me in
// 30m"). The backend fires them by re-resuming the session and streaming the
// reply into the thread; the composer shows a clock indicator for any pending.
export interface ScheduledWake {
  id: string;
  prompt: string;
  note: string | null;
  next_fire: number;          // epoch seconds
  next_fire_iso: string | null;
  interval_seconds: number | null;  // set → recurring
  created_at: string;
  last_fired: string | null;
  /** The wake's turn is in flight right now. A firing one-shot is LEASED in
   *  the store rather than deleted, so ``next_fire`` is pushed two hours out
   *  while it runs — rendering that countdown told the user their wake was
   *  still pending when it was already working. Absent on older backends. */
  running?: boolean;
}

export function listSchedules(sessionId: string): Promise<ScheduledWake[]> {
  return jsonRequest<ScheduledWake[]>(
    `/api/sessions/${encodeURIComponent(sessionId)}/schedules`,
  );
}

export async function cancelSchedule(
  sessionId: string,
  schedId: string,
): Promise<void> {
  await jsonRequest<void>(
    `/api/sessions/${encodeURIComponent(sessionId)}/schedules/${encodeURIComponent(schedId)}`,
    { method: "DELETE" },
  );
}

export function getMemory(): Promise<{ text: string }> {
  return jsonRequest<{ text: string }>("/api/memory");
}

export function putMemory(text: string): Promise<{ text: string }> {
  return jsonRequest<{ text: string }>("/api/memory", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
}

// Filesystem picker for the @-autocomplete in the Composer. Returns the
// user's allowed root + matching children of `prefix`'s parent dir.
// Admin: /home/felix tree. User: /workspace inside their per-user container.
export interface FileEntry {
  path: string;
  name: string;
  is_dir: boolean;
  size: number;
}
export interface FileListing {
  root: string;
  items: FileEntry[];
}
export function listFiles(
  prefix: string,
  limit = 50,
  workspace?: Workspace | null,
): Promise<FileListing> {
  const params = new URLSearchParams({ prefix, limit: String(limit) });
  if (workspace) params.set("workspace", workspace);
  return jsonRequest<FileListing>(`/api/files?${params}`);
}

export function getSessions(
  workspace?: Workspace | null,
): Promise<SessionSummary[]> {
  // `workspace` filters the returned list to that bucket; omit to get
  // every session (legacy callers). Sessions with no `workspace` field
  // are treated as personal on the server.
  if (workspace) {
    return jsonRequest<SessionSummary[]>(`/api/sessions?workspace=${encodeURIComponent(workspace)}`);
  }
  return jsonRequest<SessionSummary[]>("/api/sessions");
}

export function createSession(
  title?: string | null,
  workspace?: Workspace | null,
): Promise<SessionSummary> {
  return jsonRequest<SessionSummary>("/api/sessions", {
    method: "POST",
    body: JSON.stringify({
      title: title ?? null,
      // Backend defaults to "personal" when absent; only emit the field
      // when the SPA wants to override (i.e. shared mode is active).
      ...(workspace ? { workspace } : {}),
    }),
  });
}

/** Cheap change-detector for the idle poll — see GET .../head in app.py. */
export interface SessionHead {
  id: string;
  updated_at: string | null;
  message_count: number;
  last_role: "user" | "assistant" | null;
  last_status: MessageStatus | null;
}

export function getSessionHead(id: string): Promise<SessionHead> {
  return jsonRequest<SessionHead>(
    `/api/sessions/${encodeURIComponent(id)}/head`,
  );
}

export function getSession(id: string): Promise<Session> {
  return jsonRequest<Session>(`/api/sessions/${encodeURIComponent(id)}`);
}

export function renameSession(id: string, title: string): Promise<Session> {
  return updateSession(id, { title });
}

export interface SessionPatch {
  title?: string;
  starred?: boolean;
  archived?: boolean;
  folder?: string | null;
}

export function updateSession(id: string, patch: SessionPatch): Promise<Session> {
  return jsonRequest<Session>(`/api/sessions/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}

export function searchSessions(query: string): Promise<SearchResult[]> {
  return jsonRequest<SearchResult[]>(
    `/api/search?q=${encodeURIComponent(query)}`,
  );
}

export function forkSession(
  id: string,
  fromSeq: number,
  text: string,
): Promise<SessionSummary> {
  return jsonRequest<SessionSummary>(
    `/api/sessions/${encodeURIComponent(id)}/fork`,
    {
      method: "POST",
      body: JSON.stringify({ from_seq: fromSeq, text }),
    },
  );
}

export function exportSessionUrl(id: string, format: "md" | "json"): string {
  return `/api/sessions/${encodeURIComponent(id)}/export?format=${format}`;
}

export async function deleteSession(id: string): Promise<void> {
  await jsonRequest<void>(`/api/sessions/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}


/**
 * Phase 4: signal the server to abort the in-flight agent run for this
 * session.
 *
 * Server responses:
 *   * ``202 Accepted`` with ``{cancelled: true}`` — a run was active
 *     and a cancel was issued. The server may take a moment to
 *     finalise (kill the subprocess, write the cancelled status);
 *     the original ``POST /messages`` SSE response will yield the
 *     ``cancelled`` terminal frame when finalisation completes.
 *   * ``409 Conflict`` with ``detail: "no active run..."`` — there
 *     was nothing to cancel (the run already finished, or the user
 *     pressed Stop twice and lost the race). Treated as success here
 *     because the post-condition ("no run is active for this
 *     session") is the same.
 *   * ``404 Not Found`` — session missing or owned by another email.
 *     Surfaced as ``ApiError`` so the caller can show a banner.
 *
 * Typical caller is the Stop button in the Composer (rendered only
 * while ``streaming === true``), but ``cancelTurn`` is also safe to
 * call programmatically when the streaming state is uncertain — the
 * 409-as-success branch makes it idempotent.
 */
export async function cancelTurn(id: string): Promise<void> {
  const resp = await fetch(
    `/api/sessions/${encodeURIComponent(id)}/cancel`,
    {
      ...baseFetchInit,
      method: "POST",
      headers: { "Accept": "application/json" },
    },
  );
  // 202 success, 409 "no active run" — both fine.  Anything else gets
  // surfaced as an ApiError so the caller can decide.
  if (resp.status === 202 || resp.status === 409) {
    return;
  }
  if (!resp.ok) {
    let detail: unknown = undefined;
    try { detail = await resp.json(); } catch { /* ignore */ }
    throw new ApiError(resp.status, resp.statusText, detail);
  }
}

export async function uploadAttachments(
  id: string,
  files: File[],
): Promise<AttachmentMeta[]> {
  const fd = new FormData();
  for (const f of files) {
    fd.append("files", f, f.name);
  }
  return jsonRequest<AttachmentMeta[]>(
    `/api/sessions/${encodeURIComponent(id)}/attachments`,
    { method: "POST", body: fd },
  );
}

// ---------------------------------------------------------------------------
// Bridge ops API (admin only). Caddy strips /api/ops on the way to the bridge.
// ---------------------------------------------------------------------------

const OPS_BASE = "/api/ops";

export interface Agent {
  id: string;
  kind: string;
  handle: string | null;
  status: string;
  description: string | null;
  created_at: string | null;
  working_dir: string | null;
  last_artifact: string | null;
}

export interface AgentDetail extends Agent {
  log_tail: string[] | null;
  phase: number | null;
  total_phases: number | null;
  pause_cause: string | null;
}

export interface AgentList {
  active: Agent[];
  archived: Agent[];
}

export interface AgentCreated {
  id: string;
  handle: string | null;
  status: string;
}

export interface AgentMutated {
  id: string;
  status: string;
}

export type Verbosity = "quiet" | "normal" | "verbose" | "firehose";

export interface UsageWindow {
  key: string;
  label: string;
  utilization: number;
  resets_at: string | null;
  resets_in_minutes: number | null;
}

export interface ExtraUsage {
  used_credits: number;
  monthly_limit: number;
  currency: string;
}

export interface AccountUsage {
  name: string;
  is_active: boolean;
  available: boolean;
  error: string | null;
  windows: UsageWindow[];
  extra_usage: ExtraUsage | null;
}

export interface TodayModelCost {
  model: string;
  is_known_pricing: boolean;
  cost_usd: number;
  input_cost_usd: number;
  output_cost_usd: number;
  cache_write_cost_usd: number;
  cache_read_cost_usd: number;
}

export interface TodayUsage {
  date: string | null;
  total_cost_usd: number;
  models: TodayModelCost[];
}

export interface ActiveBlockUsage {
  start_time: string;
  end_time: string;
  remaining_minutes: number;
  cost_usd_so_far: number;
  tokens_per_minute: number | null;
  cost_per_hour: number | null;
  projected_cost_usd: number | null;
  projected_tokens: number | null;
}

export interface RollingUsage {
  days: number;
  cost_usd: number;
  tokens: number;
}

export interface UsagePayload {
  accounts: AccountUsage[];
  today_utc: TodayUsage;
  active_block: ActiveBlockUsage | null;
  rolling_5d: RollingUsage;
}

function opsErrorMessage(detail: unknown, statusText: string): string {
  if (detail && typeof detail === "object" && "detail" in detail) {
    const d = (detail as { detail: unknown }).detail;
    if (typeof d === "string") return d;
  }
  return statusText;
}

async function opsRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  const resp = await fetch(`${OPS_BASE}${path}`, {
    ...baseFetchInit,
    ...init,
    headers: {
      "Accept": "application/json",
      ...(init.body && !(init.body instanceof FormData)
        ? { "Content-Type": "application/json" }
        : {}),
      ...(init.headers || {}),
    },
  });
  if (!resp.ok) {
    let detail: unknown = undefined;
    try {
      detail = await resp.json();
    } catch {
      /* ignore */
    }
    throw new Error(opsErrorMessage(detail, `HTTP ${resp.status} ${resp.statusText}`));
  }
  if (resp.status === 204) {
    return undefined as unknown as T;
  }
  return (await resp.json()) as T;
}

export function getAgents(): Promise<AgentList> {
  return opsRequest<AgentList>("/agents");
}

export function getAgentDetail(id: string): Promise<AgentDetail> {
  return opsRequest<AgentDetail>(`/agents/${encodeURIComponent(id)}`);
}

export function spawnTask(prompt: string, verbosity: Verbosity): Promise<AgentCreated> {
  return opsRequest<AgentCreated>("/agents/task", {
    method: "POST",
    body: JSON.stringify({ prompt, verbosity }),
  });
}

export function spawnProject(brief: string): Promise<AgentCreated> {
  return opsRequest<AgentCreated>("/agents/project", {
    method: "POST",
    body: JSON.stringify({ brief }),
  });
}

export function stopAgent(id: string): Promise<AgentMutated> {
  return opsRequest<AgentMutated>(`/agents/${encodeURIComponent(id)}/stop`, { method: "POST" });
}

export function killAgent(id: string): Promise<AgentMutated> {
  return opsRequest<AgentMutated>(`/agents/${encodeURIComponent(id)}/kill`, { method: "POST" });
}

export function resumeAgent(id: string): Promise<AgentMutated> {
  return opsRequest<AgentMutated>(`/agents/${encodeURIComponent(id)}/resume`, { method: "POST" });
}

export function endAgent(id: string): Promise<AgentMutated> {
  return opsRequest<AgentMutated>(`/agents/${encodeURIComponent(id)}/end`, { method: "POST" });
}

export function setAgentVerbose(id: string, level: Verbosity): Promise<AgentMutated> {
  return opsRequest<AgentMutated>(`/agents/${encodeURIComponent(id)}/verbose`, {
    method: "POST",
    body: JSON.stringify({ level }),
  });
}

export function getUsage(): Promise<UsagePayload> {
  return opsRequest<UsagePayload>("/usage");
}

// ---------------------------------------------------------------------------
// SSE consumer. Parses ``event: <type>\ndata: <json>\n\n`` frames as they
// arrive and invokes ``onEvent`` for each parsed event. Resolves when the
// stream closes; rejects on transport error. The caller is expected to
// handle the ``done`` and ``error`` event types via ``onEvent`` — the
// returned promise is purely a lifecycle signal.
// ---------------------------------------------------------------------------

export interface StreamMessageOptions {
  model?: ModelChoice;
  /** Per-turn reasoning effort (OpenAI-style ``reasoning_effort``).
   *  Validated backend-side against the model's supported levels; null /
   *  undefined means "provider default". */
  effort?: string | null;
  /** Per-turn worker mode. ``"image"`` routes to gemini image-gen
   *  instead of claude. Anything else (or absent) is treated as chat. */
  mode?: "chat" | "image";
  signal?: AbortSignal;
  /**
   * Invoked exactly once, the moment the POST is accepted (2xx headers
   * received). By that point the server has ALREADY persisted the user
   * message and the assistant placeholder — ``post_message`` appends both
   * under the session lock before it constructs the SSE response — so this
   * is the precise boundary between "this message may not exist anywhere"
   * and "this message is durable".
   *
   * Callers need that boundary to classify a mid-flight failure: a
   * transport error BEFORE it means nothing was sent and the user's text
   * has to be handed back, while after it the worker is running
   * server-side and the right response is to poll for its result.
   */
  onAccepted?: () => void;
}

export async function streamMessage(
  id: string,
  text: string,
  onEvent: (evt: StreamEvent) => void,
  optsOrSignal?: StreamMessageOptions | AbortSignal,
): Promise<void> {
  // Backwards-compat: callers used to pass an AbortSignal as the 4th arg.
  let opts: StreamMessageOptions;
  if (optsOrSignal instanceof AbortSignal) {
    opts = { signal: optsOrSignal };
  } else {
    opts = optsOrSignal ?? {};
  }
  const body: Record<string, unknown> = { text };
  if (opts.model && opts.model !== "default") {
    body.model = opts.model;
  }
  if (opts.effort) {
    body.effort = opts.effort;
  }
  if (opts.mode === "image") {
    body.mode = "image";
  }
  const resp = await fetch(
    `/api/sessions/${encodeURIComponent(id)}/messages`,
    {
      ...baseFetchInit,
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
      },
      body: JSON.stringify(body),
      signal: opts.signal,
    },
  );
  if (!resp.ok) {
    let detail: unknown = undefined;
    try {
      detail = await resp.json();
    } catch {
      /* ignore */
    }
    throw new ApiError(resp.status, resp.statusText, detail);
  }
  // Headers are in and the status is 2xx: the user message and the
  // assistant placeholder are durable server-side. Signal before pumping
  // the body, so the caller's "was this ever accepted?" flag is set even if
  // the body dies on its very first frame.
  try {
    opts.onAccepted?.();
  } catch {
    /* a caller-side throw must not take down the stream */
  }
  await pumpSseResponse(resp, onEvent);
}

/**
 * Re-attach to a session's in-flight worker via GET .../stream.
 *
 * The server replays every event the run has emitted so far (so a
 * client that connects mid-run sees the full delta log from event 0)
 * and then drains live events to terminal. If no run is active, the
 * server emits a single empty ``done`` frame.
 *
 * Used when the user switches back to a session whose previous SSE was
 * aborted (we abort on session-switch to keep events for the old
 * session from clobbering the new one). The worker itself survives the
 * abort and keeps generating; this endpoint catches the client back up.
 */
// ---------------------------------------------------------------------------
// Artifact runs (Python-in-chat).
// ---------------------------------------------------------------------------

export type RunMediaItem = {
  filename: string;
  size: number;
  mime: string;
  url: string;
};

export type RunStreamEvent =
  | { type: "status"; status: string; ts?: number }
  | { type: "stdout"; text: string; ts?: number }
  | { type: "stderr"; text: string; ts?: number }
  | { type: "media"; items: RunMediaItem[]; ts?: number }
  | { type: "done"; status: string; exit_code: number; media: RunMediaItem[]; ts?: number }
  | { type: "ping"; ts?: number }
  | { type: "error"; message: string };

export type StartRunResult = {
  run_id: string;
  status: string;
  started_at: number;
  stream_url: string;
};

export async function startArtifactRun(
  sessionId: string,
  filename: string,
  source: "generated" | "attachment" = "generated",
): Promise<StartRunResult> {
  const resp = await fetch(
    `/api/sessions/${encodeURIComponent(sessionId)}/runs`,
    {
      ...baseFetchInit,
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename, source }),
    },
  );
  if (!resp.ok) {
    let detail: unknown = undefined;
    try { detail = await resp.json(); } catch { /* ignore */ }
    throw new ApiError(resp.status, resp.statusText, detail);
  }
  return (await resp.json()) as StartRunResult;
}

export async function cancelArtifactRun(
  sessionId: string,
  runId: string,
): Promise<void> {
  const resp = await fetch(
    `/api/sessions/${encodeURIComponent(sessionId)}/runs/${encodeURIComponent(runId)}/cancel`,
    { ...baseFetchInit, method: "POST" },
  );
  if (!resp.ok) {
    let detail: unknown = undefined;
    try { detail = await resp.json(); } catch { /* ignore */ }
    throw new ApiError(resp.status, resp.statusText, detail);
  }
}

export async function streamArtifactRun(
  sessionId: string,
  runId: string,
  onEvent: (evt: RunStreamEvent) => void,
  opts: { signal?: AbortSignal } = {},
): Promise<void> {
  const resp = await fetch(
    `/api/sessions/${encodeURIComponent(sessionId)}/runs/${encodeURIComponent(runId)}/stream`,
    {
      ...baseFetchInit,
      method: "GET",
      headers: { "Accept": "text/event-stream" },
      signal: opts.signal,
    },
  );
  if (!resp.ok) {
    let detail: unknown = undefined;
    try { detail = await resp.json(); } catch { /* ignore */ }
    throw new ApiError(resp.status, resp.statusText, detail);
  }
  await pumpSseResponse(resp, onEvent as (e: unknown) => void);
}


export async function attachStream(
  id: string,
  onEvent: (evt: StreamEvent) => void,
  opts: { signal?: AbortSignal } = {},
): Promise<void> {
  const resp = await fetch(
    `/api/sessions/${encodeURIComponent(id)}/stream`,
    {
      ...baseFetchInit,
      method: "GET",
      headers: { "Accept": "text/event-stream" },
      signal: opts.signal,
    },
  );
  if (!resp.ok) {
    let detail: unknown = undefined;
    try { detail = await resp.json(); } catch { /* ignore */ }
    throw new ApiError(resp.status, resp.statusText, detail);
  }
  await pumpSseResponse(resp, onEvent);
}

async function pumpSseResponse(
  resp: Response,
  onEvent: (evt: StreamEvent) => void,
): Promise<void> {
  if (!resp.body) {
    throw new Error("response has no body — server did not stream");
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) {
      break;
    }
    buf += decoder.decode(value, { stream: true });
    // SSE frames are separated by a blank line (\n\n). We split, dispatch
    // every complete frame, and keep the trailing partial frame for the
    // next chunk.
    let sep: number;
    while ((sep = buf.indexOf("\n\n")) !== -1) {
      const frame = buf.slice(0, sep);
      buf = buf.slice(sep + 2);
      const evt = parseFrame(frame);
      if (evt) {
        onEvent(evt);
      }
    }
  }
  // Drain anything left in the buffer at EOF.
  const trailing = buf.trim();
  if (trailing) {
    const evt = parseFrame(trailing);
    if (evt) {
      onEvent(evt);
    }
  }
}

function parseFrame(frame: string): StreamEvent | null {
  // A frame looks like:
  //   event: delta
  //   data: {"text":"..."}
  // We tolerate keep-alive comments (lines starting with ":") and missing
  // event lines (which would indicate a malformed server response — we
  // surface them to the caller as ``error`` so the UI can recover).
  let evtType: string | null = null;
  const dataLines: string[] = [];
  for (const raw of frame.split("\n")) {
    if (!raw || raw.startsWith(":")) continue;
    if (raw.startsWith("event:")) {
      evtType = raw.slice("event:".length).trim();
    } else if (raw.startsWith("data:")) {
      dataLines.push(raw.slice("data:".length).replace(/^ /, ""));
    }
  }
  if (!evtType || dataLines.length === 0) {
    return null;
  }
  let payload: unknown;
  try {
    payload = JSON.parse(dataLines.join("\n"));
  } catch {
    return { type: "error", message: "malformed sse data" };
  }
  // Discriminated union: trust the server's ``event:`` line and overlay
  // the parsed JSON. Runtime shape correctness is the server's contract.
  return { type: evtType, ...(payload as object) } as StreamEvent;
}
