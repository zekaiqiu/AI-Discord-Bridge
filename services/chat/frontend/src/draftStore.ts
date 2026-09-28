/**
 * Per-window composer draft persistence, decoupled from React state.
 *
 * Drafts are keyed by `s:<sessionId>` (created sessions) or `pane:<key>`
 * (a new window that hasn't sent yet) and stored in one localStorage blob.
 * The Composer reads/writes here directly (debounced) so typing NEVER
 * triggers an App-level re-render — that was the cause of composer lag when
 * drafts briefly lived in App state and every keystroke re-rendered every
 * open Thread.
 */

const DRAFTS_KEY = "chat.drafts";

type Drafts = Record<string, string>;

function load(): Drafts {
  try {
    const raw = localStorage.getItem(DRAFTS_KEY);
    if (!raw) return {};
    const o = JSON.parse(raw);
    if (o && typeof o === "object" && !Array.isArray(o)) return o as Drafts;
  } catch {
    /* ignore */
  }
  return {};
}

function save(d: Drafts): void {
  try {
    localStorage.setItem(DRAFTS_KEY, JSON.stringify(d));
  } catch {
    /* ignore */
  }
}

export function getDraft(key: string): string {
  return load()[key] ?? "";
}

/** Write a draft (empty string removes the key so the blob stays small). */
export function setDraft(key: string, value: string): void {
  const d = load();
  if ((d[key] ?? "") === value) return; // no-op write
  if (value) d[key] = value;
  else delete d[key];
  save(d);
}

export function clearDraft(key: string): void {
  setDraft(key, "");
}
