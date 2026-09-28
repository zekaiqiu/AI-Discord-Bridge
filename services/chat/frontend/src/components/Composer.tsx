/**
 * Auto-grow composer with attachment chips, drop-zone, paperclip button.
 *
 * Send semantics:
 *   - Enter sends; Shift+Enter inserts a newline. (Common modern-chat UX.)
 *   - Send disabled while ``streaming`` is true, OR text is empty/whitespace
 *     AND there are no staged attachments (an attachments-only send is allowed).
 *
 * Drop-zone:
 *   - The whole composer wrapper is a drop target. ``dragover`` toggles a
 *     ``is-dropping`` class for the highlight.
 *   - ``drop`` extracts ``DataTransfer.files`` and forwards to ``onAttach``.
 *
 * Paste:
 *   - Clipboard *files* (screenshots etc.) go straight to ``onAttach``.
 *   - Clipboard *text* that would push the composer past the server's
 *     message-size cap is converted to a ``pasted-text-N.txt`` attachment
 *     instead of being inserted. Without this the POST comes back 413 and
 *     the turn dies with no visible error — see limits.ts for the full
 *     story. Everything below the cap pastes normally.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AttachmentMeta,
  FileEntry,
  EFFORT_LEVELS,
  MODEL_LABELS,
  ModelChoice,
  ScheduledWake,
  Workspace,
  cancelSchedule,
  listFiles,
  listSchedules,
} from "../api";
import { useT } from "../i18n";
import { formatBytes } from "../utils";
import { getDraft, setDraft } from "../draftStore";
import {
  MAX_FILES_PER_TURN,
  exceedsTextBudget,
  makeTextFile,
} from "../limits";
import { DRAFT_RESTORED, SCHEDULES_UPDATED, onSessionEvent } from "../events";

const DRAFT_PERSIST_DEBOUNCE_MS = 350;
/** How long the "your paste became a file" hint stays on screen. */
const PASTE_NOTICE_MS = 6000;
/** Fallback poll for the scheduled-wake pill. The push notification
 *  (``schedules_updated``) covers the common case; this catches wakes armed
 *  by a turn this tab never saw, and wakes that just fired. */
const POLL_MS = 20000;

const MAX_TEXTAREA_HEIGHT_VH = 40;
const FILE_PICKER_DEBOUNCE_MS = 120;
const FILE_PICKER_LIMIT = 30;

export interface ComposerProps {
  /** Storage key identifying which window/session this composer's draft
   *  belongs to (`s:<sid>` or `pane:<key>`). The draft text itself lives in
   *  Composer-local state + localStorage (see draftStore), NOT in App state,
   *  so typing never re-renders the App tree. Changing this key re-seeds the
   *  textarea from that key's saved draft (and flushes the previous one). */
  draftKey: string;
  disabled: boolean;
  /** Phase 4: when true, the send button is replaced with a Stop button
   *  that calls ``onCancel``. Distinguished from ``disabled`` because
   *  Phase 4 wants to render a visibly different control while a turn
   *  is in flight (Stop ⏹ vs the disabled-Send ➤ from Phase 2). */
  streaming?: boolean;
  /** Phase 4: invoked by the Stop button. Wired to ``cancelTurn(sid)``
   *  in App.tsx. Optional so existing call sites that don't pass it
   *  fall through to the legacy disabled-Send behaviour. */
  onCancel?: () => void;
  pendingAttachments: AttachmentMeta[];
  /** Parallel array of original File objects for the pending attachments.
   *  Used to build local blob URLs so images preview before send (claude.ai
   *  shows real thumbnails for queued image uploads). May be shorter than
   *  ``pendingAttachments`` if a session was rehydrated without the source
   *  Files — in that case images fall back to the generic card. */
  pendingFiles?: File[];
  onRemoveAttachment: (idx: number) => void;
  onSend: (text: string) => void;
  onAttach: (files: File[]) => void;
  /** Active model alias. ``"default"`` = no override; the CLI picks. */
  model?: ModelChoice;
  onModelChange?: (model: ModelChoice) => void;
  /** Stored reasoning-effort level for the active model (null = unset /
   *  provider default). Only rendered when the model has effort levels. */
  effort?: string | null;
  onEffortChange?: (level: string | null) => void;
  /** When true the next sent message is prefixed with a "use WebSearch"
   *  hint. Toggles off automatically after a successful send. */
  webSearchOn?: boolean;
  onToggleWebSearch?: () => void;
  /** When true the next sent message is routed to gemini image-gen
   *  instead of claude. Toggles off automatically after a successful send. */
  imageGenOn?: boolean;
  onToggleImageGen?: () => void;
  /** When true (default), Enter sends and Shift+Enter inserts newline.
   *  When false, Enter inserts newline and Cmd/Ctrl+Enter sends. From the
   *  user's per-account settings; falls back to true so existing call sites
   *  retain the historical behaviour. */
  sendOnEnter?: boolean;
  /** Workspace of the *active session* — drives the file-picker scope so
   *  attaching from a shared session lists the shared container's files
   *  (not the user's personal workspace). */
  workspace?: Workspace;
  /** Active session id — drives the scheduled-wake clock indicator. When
   *  set, the composer polls for pending wakes and shows a clock pill the
   *  user can open to review/cancel them. Absent for a not-yet-created
   *  session (no schedules possible). */
  sessionId?: string | null;
}

/**
 * Short "fires in …" label from an epoch-seconds target.
 *
 * Two fixes over the original:
 *   - A wake that is already due used to clamp to a permanent "in 0s". Due
 *     wakes really do sit in that state — the tick loop waits on the session
 *     lock, and a failed one is re-armed with backoff — so "in 0s" was on
 *     screen for minutes at a time reading like a stuck clock. It now says
 *     "due now".
 *   - Rounding was applied at each step, so a wake 90 minutes out rendered as
 *     "in 2h" and one 36 hours out as "in 2d". Truncating toward the smaller
 *     unit and carrying the remainder ("1h 30m") never overstates the wait.
 */
function fireInLabel(nextFireEpoch: number, t: (k: string, v?: Record<string, string | number>) => string): string {
  const secs = Math.round(nextFireEpoch - Date.now() / 1000);
  if (secs <= 0) return t("schedule.due_now");
  if (secs < 60) return t("schedule.in_s", { s: secs });
  const mins = Math.floor(secs / 60);
  if (mins < 60) return t("schedule.in_m", { m: mins });
  const hours = Math.floor(mins / 60);
  const remMins = mins % 60;
  if (hours < 48) {
    return remMins
      ? t("schedule.in_hm", { h: hours, m: remMins })
      : t("schedule.in_h", { h: hours });
  }
  const days = Math.floor(hours / 24);
  const remHours = hours % 24;
  return remHours
    ? t("schedule.in_dh", { d: days, h: remHours })
    : t("schedule.in_d", { d: days });
}

/**
 * Clock pill + popover for a session's pending scheduled wakes. Self-contained:
 * polls the backend (so a wake the model just set, or one that just fired,
 * shows up within the poll window) and lets the user cancel any pending wake.
 * Renders nothing when there are none, so it's invisible until the feature is
 * actually used.
 */
function ScheduleIndicator({ sessionId }: { sessionId: string }): JSX.Element | null {
  const [wakes, setWakes] = useState<ScheduledWake[]>([]);
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLSpanElement | null>(null);
  const t = useT();

  const refresh = useCallback(async () => {
    try {
      setWakes(await listSchedules(sessionId));
    } catch {
      // transient — keep the last known list
    }
  }, [sessionId]);

  useEffect(() => {
    setWakes([]);
    setOpen(false);
    refresh();
    const iv = window.setInterval(refresh, POLL_MS);
    const onFocus = () => refresh();
    window.addEventListener("focus", onFocus);
    // Push refresh: the worker emits ``schedules_updated`` when the model
    // arms or cancels a wake mid-turn. Without this the pill sat wrong for
    // up to a full poll interval right at the moment the user was watching
    // the model say "I'll check back in 30 minutes".
    const unsub = onSessionEvent(SCHEDULES_UPDATED, sessionId, refresh);
    return () => {
      window.clearInterval(iv);
      window.removeEventListener("focus", onFocus);
      unsub();
    };
  }, [sessionId, refresh]);

  // Re-render on a timer while the popover is open so the "in 5m" labels
  // actually count down. Closed, the poll's own re-render is enough.
  const [, setTick] = useState(0);
  useEffect(() => {
    if (!open) return;
    const iv = window.setInterval(() => setTick((n) => n + 1), 1000);
    return () => window.clearInterval(iv);
  }, [open]);

  // Dismiss on outside click / Escape. The popover previously had no way to
  // close except re-clicking the pill, so it stayed open over the thread
  // while the user carried on typing.
  useEffect(() => {
    if (!open) return;
    const onDocDown = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDocDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDocDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  if (wakes.length === 0) return null;

  const onCancel = async (id: string) => {
    try {
      await cancelSchedule(sessionId, id);
    } catch {
      // fall through to refresh; if it 404'd it's already gone
    }
    refresh();
  };

  return (
    <span className="composer-schedule" ref={rootRef}>
      <button
        type="button"
        className={`composer-pill ${open ? "is-on" : ""}`}
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-haspopup="menu"
        title={
          wakes.length === 1
            ? t("schedule.pill_title.one", { count: wakes.length })
            : t("schedule.pill_title.other", { count: wakes.length })
        }
      >
        ⏰ {wakes.length}
      </button>
      {open && (
        <div className="composer-schedule-pop" role="menu">
          <div className="composer-schedule-head">{t("schedule.head")}</div>
          <ul>
            {wakes.map((w) => (
              <li key={w.id}>
                <div className="composer-schedule-when">
                  {w.running ? t("schedule.running") : fireInLabel(w.next_fire, t)}
                  {w.interval_seconds ? ` · ${t("schedule.repeats")}` : ""}
                </div>
                <div className="composer-schedule-what" title={w.prompt}>
                  {w.note || w.prompt}
                </div>
                <button
                  type="button"
                  className="composer-schedule-x"
                  onClick={() => onCancel(w.id)}
                  title={t("schedule.cancel")}
                  aria-label={t("schedule.cancel")}
                >
                  ✕
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </span>
  );
}

export function Composer(props: ComposerProps): JSX.Element {
  const {
    draftKey,
    disabled,
    streaming,
    onCancel,
    pendingAttachments,
    pendingFiles,
    onRemoveAttachment,
    onSend,
    onAttach,
    model = "default",
  effort = null,
  onEffortChange,
    onModelChange,
    webSearchOn = false,
    onToggleWebSearch,
    imageGenOn = false,
    onToggleImageGen,
    sendOnEnter = true,
    workspace = "personal",
    sessionId,
  } = props;

  const t = useT();
  // Draft text lives HERE (local state), seeded from the persisted store for
  // this window/session. Typing only re-renders the Composer — never the App
  // tree — which is what keeps it snappy with long conversations open.
  const [text, setText] = useState<string>(() => getDraft(draftKey));
  const [dropping, setDropping] = useState<boolean>(false);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  // Mirrors for the unmount / key-switch flush (read latest without deps).
  const textRef = useRef(text);
  textRef.current = text;
  const draftKeyRef = useRef(draftKey);

  // Re-seed when the window's bound session changes: flush the previous
  // key's text synchronously (so a fast switch doesn't lose it) then load
  // the new key's saved draft.
  useEffect(() => {
    if (draftKeyRef.current === draftKey) return;
    setDraft(draftKeyRef.current, textRef.current);
    setText(getDraft(draftKey));
    draftKeyRef.current = draftKey;
  }, [draftKey]);

  // Persist (debounced) on every text change. The timer captures the key it
  // was scheduled under; if the key changes the cleanup cancels the pending
  // write (the re-seed effect above already flushed the old key).
  useEffect(() => {
    const key = draftKey;
    const h = window.setTimeout(() => setDraft(key, text), DRAFT_PERSIST_DEBOUNCE_MS);
    return () => window.clearTimeout(h);
  }, [text, draftKey]);

  // Flush whatever's typed when the composer unmounts (e.g. window closed).
  useEffect(() => {
    return () => setDraft(draftKeyRef.current, textRef.current);
  }, []);

  // App writes the text of a send that the server never accepted back into
  // the draft store and fires DRAFT_RESTORED. `text` is local state, so the
  // write is invisible to us otherwise — this is what actually puts the
  // message back in the box instead of leaving it in localStorage until the
  // next reload.
  useEffect(() => {
    if (!sessionId) return;
    return onSessionEvent(DRAFT_RESTORED, sessionId, () => {
      const restored = getDraft(draftKey);
      if (restored) setText(restored);
    });
  }, [sessionId, draftKey]);

  // ---------- @-file picker state ----------
  // Opened by typing `@` at a word boundary; closed by Escape, blur,
  // selection, or by typing whitespace. Server lists files under
  // /workspace (user) or /home/felix (admin). The picker mutates `text`
  // on selection by replacing the `@<query>` span with the chosen path.
  const [pickerOpen, setPickerOpen] = useState<boolean>(false);
  const [pickerStart, setPickerStart] = useState<number>(0); // index of the `@` in text
  const [pickerQuery, setPickerQuery] = useState<string>(""); // chars typed after `@`
  const [pickerRoot, setPickerRoot] = useState<string>(""); // server-reported root (display only)
  const [pickerResults, setPickerResults] = useState<FileEntry[]>([]);
  const [pickerIndex, setPickerIndex] = useState<number>(0);
  const [pickerLoading, setPickerLoading] = useState<boolean>(false);

  // ---------- oversized-paste notice ----------
  // Transient one-liner under the textarea explaining that a paste was
  // turned into an attachment (or why it wasn't). Silently swapping the
  // user's paste for a file chip would be its own kind of mystery.
  const [pasteNotice, setPasteNotice] = useState<string | null>(null);
  const pasteNoticeTimerRef = useRef<number | null>(null);
  // Per-composer counter so two pastes in one draft don't both land on
  // ``pasted-text-1.txt`` (same name = the second overwrites the first
  // server-side, since attachments are stored by filename).
  const pasteSeqRef = useRef(0);
  const showPasteNotice = useCallback((msg: string) => {
    setPasteNotice(msg);
    if (pasteNoticeTimerRef.current !== null) {
      window.clearTimeout(pasteNoticeTimerRef.current);
    }
    pasteNoticeTimerRef.current = window.setTimeout(
      () => setPasteNotice(null),
      PASTE_NOTICE_MS,
    );
  }, []);
  useEffect(
    () => () => {
      if (pasteNoticeTimerRef.current !== null) {
        window.clearTimeout(pasteNoticeTimerRef.current);
      }
    },
    [],
  );

  // ---------- pending-attachment preview URLs ----------
  // Build blob: URLs for any pending image File so users see a real
  // thumbnail before send (claude.ai-style). We memoize by File identity:
  // when an entry is removed or sent, its URL is revoked on the next
  // render, and a final cleanup runs on unmount.
  const previewUrls = useMemo<(string | null)[]>(() => {
    if (!pendingFiles) return pendingAttachments.map(() => null);
    return pendingFiles.map((file) => {
      if (!file) return null;
      if (!file.type.startsWith("image/")) return null;
      return URL.createObjectURL(file);
    });
  }, [pendingFiles, pendingAttachments]);
  useEffect(() => {
    return () => {
      for (const url of previewUrls) {
        if (url) URL.revokeObjectURL(url);
      }
    };
  }, [previewUrls]);

  // Auto-grow: re-measure on every text change.
  //
  // PERF: reading `ta.scrollHeight` forces a SYNCHRONOUS, document-wide layout
  // flush on every keystroke. With a long conversation (or a second pane with
  // one) open in the tab, that flush re-validates the whole thread DOM, so
  // typing got laggy in proportion to total page content — even in an empty
  // new pane. Browsers that support CSS `field-sizing: content` (see
  // .composer-textarea) auto-size the textarea natively, with no forced layout
  // on the input path — so skip the JS measure entirely there. Older engines
  // fall back to the JS path (unchanged behaviour).
  useEffect(() => {
    const ta = textareaRef.current;
    if (!ta) return;
    if (
      typeof CSS !== "undefined" &&
      typeof CSS.supports === "function" &&
      CSS.supports("field-sizing", "content")
    ) {
      return; // native CSS auto-sizing; no synchronous reflow per keystroke
    }
    ta.style.height = "auto";
    const maxPx = (window.innerHeight * MAX_TEXTAREA_HEIGHT_VH) / 100;
    ta.style.height = `${Math.min(ta.scrollHeight, maxPx)}px`;
  }, [text]);

  // Re-evaluate picker state from the current text + caret position.
  // Detection rule: walk backwards from the caret, looking for `@`. If
  // found, the char before `@` must be start-of-text or whitespace
  // (i.e., we don't trigger on emails like a@b.com). Stop on whitespace.
  const evalPicker = useCallback((value: string, caret: number) => {
    let at = -1;
    for (let i = caret - 1; i >= 0; i--) {
      const c = value[i];
      if (c === "@") {
        const prev = i === 0 ? " " : value[i - 1];
        if (/\s/.test(prev)) at = i;
        break;
      }
      if (/\s/.test(c)) break;
    }
    if (at === -1) {
      setPickerOpen(false);
      return;
    }
    const query = value.slice(at + 1, caret);
    setPickerOpen(true);
    setPickerStart(at);
    setPickerQuery(query);
  }, []);

  // Fetch results when picker is open and query changes (debounced).
  useEffect(() => {
    if (!pickerOpen) return;
    let cancelled = false;
    const handle = window.setTimeout(async () => {
      // Build server-side prefix:
      //   - If query starts with `/`, use it directly as an absolute path.
      //   - Otherwise, default to the root + query, so `@foo` in /workspace
      //     becomes `/workspace/foo*` (matches "Composer.tsx", "MEMORY.md", etc.).
      // First fetch (no root yet): pass empty prefix; server returns the
      // user's allowed root, which we cache for subsequent prefixed calls.
      const serverPrefix = pickerQuery.startsWith("/")
        ? pickerQuery
        : pickerRoot
          ? `${pickerRoot}/${pickerQuery}`
          : "";
      setPickerLoading(true);
      try {
        const resp = await listFiles(serverPrefix, FILE_PICKER_LIMIT, workspace);
        if (cancelled) return;
        setPickerRoot(resp.root);
        setPickerResults(resp.items);
        setPickerIndex(0);
      } catch {
        if (!cancelled) setPickerResults([]);
      } finally {
        if (!cancelled) setPickerLoading(false);
      }
    }, FILE_PICKER_DEBOUNCE_MS);
    return () => {
      cancelled = true;
      window.clearTimeout(handle);
    };
  }, [pickerOpen, pickerQuery, pickerRoot, workspace]);

  const acceptPickerResult = useCallback(
    (entry: FileEntry) => {
      const ta = textareaRef.current;
      if (!ta) return;
      const caret = ta.selectionStart ?? text.length;
      const before = text.slice(0, pickerStart);
      const after = text.slice(caret);
      // Append "/" for dirs to support drilling further; files get a trailing space.
      const insertion = entry.is_dir ? `${entry.path}/` : `${entry.path} `;
      const next = `${before}${insertion}${after}`;
      setText(next);
      // Move caret to end of inserted span and KEEP the picker open if it's
      // a dir (so the user can keep navigating); close on file selection.
      const newCaret = (before + insertion).length;
      if (entry.is_dir) {
        // Defer one tick so React commits the new value before we move the caret.
        window.setTimeout(() => {
          if (!textareaRef.current) return;
          textareaRef.current.focus();
          textareaRef.current.setSelectionRange(newCaret, newCaret);
          // Re-evaluate picker against the new state — query is now the
          // full path with trailing slash, server returns dir contents.
          evalPicker(next, newCaret);
        }, 0);
      } else {
        setPickerOpen(false);
        window.setTimeout(() => {
          if (!textareaRef.current) return;
          textareaRef.current.focus();
          textareaRef.current.setSelectionRange(newCaret, newCaret);
        }, 0);
      }
    },
    [text, pickerStart, evalPicker],
  );

  const trySend = useCallback(() => {
    const trimmed = text.trim();
    // Allow an attachments-only send (no text) — claude.ai-style. onAttach
    // populates pendingAttachments, so its length reflects staged files.
    if (disabled || (!trimmed && pendingAttachments.length === 0)) return;
    onSend(text);
    setText("");
    setDraft(draftKey, "");
  }, [text, disabled, onSend, draftKey, pendingAttachments.length]);

  const onKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      // IME composition (pinyin, kana…): the Enter that commits a candidate
      // must not send the half-composed text. Chrome flags isComposing,
      // Safari delivers keyCode 229.
      if (e.nativeEvent.isComposing || e.keyCode === 229) return;
      // Picker handles its own keys when open: ↑↓ navigate, Enter accept,
      // Escape close, Tab also accepts (file-explorer style).
      if (pickerOpen && pickerResults.length > 0) {
        if (e.key === "ArrowDown") {
          e.preventDefault();
          setPickerIndex((i) => Math.min(i + 1, pickerResults.length - 1));
          return;
        }
        if (e.key === "ArrowUp") {
          e.preventDefault();
          setPickerIndex((i) => Math.max(i - 1, 0));
          return;
        }
        if (e.key === "Enter" || e.key === "Tab") {
          e.preventDefault();
          acceptPickerResult(pickerResults[pickerIndex]);
          return;
        }
      }
      if (pickerOpen && e.key === "Escape") {
        e.preventDefault();
        setPickerOpen(false);
        return;
      }
      if (e.key !== "Enter") return;
      if (sendOnEnter) {
        // Enter sends; Shift+Enter inserts newline (legacy behaviour).
        if (!e.shiftKey) {
          e.preventDefault();
          trySend();
        }
      } else {
        // Enter inserts newline; Cmd/Ctrl+Enter sends.
        if (e.metaKey || e.ctrlKey) {
          e.preventDefault();
          trySend();
        }
      }
    },
    [trySend, sendOnEnter, pickerOpen, pickerResults, pickerIndex, acceptPickerResult],
  );

  const onTextareaChange = useCallback(
    (e: React.ChangeEvent<HTMLTextAreaElement>) => {
      const value = e.target.value;
      setText(value);
      const caret = e.target.selectionStart ?? value.length;
      evalPicker(value, caret);
    },
    [evalPicker],
  );

  const reEvalFromTextarea = useCallback(() => {
    // Caret moved without text change (arrow keys, mouse click). Used by
    // onKeyUp/onClick to re-evaluate the picker state on caret motion.
    const ta = textareaRef.current;
    if (!ta) return;
    const caret = ta.selectionStart ?? ta.value.length;
    evalPicker(ta.value, caret);
  }, [evalPicker]);

  const onDrop = useCallback(
    (e: React.DragEvent<HTMLDivElement>) => {
      e.preventDefault();
      setDropping(false);
      const files = Array.from(e.dataTransfer.files || []);
      if (files.length) onAttach(files);
    },
    [onAttach],
  );

  const onPickFile = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const files = Array.from(e.target.files || []);
      if (files.length) onAttach(files);
      // Reset so picking the same file again still triggers ``onChange``.
      e.target.value = "";
    },
    [onAttach],
  );

  const onPaste = useCallback(
    (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
      const dt = e.clipboardData;
      const items = dt?.items;
      if (!dt || !items) return;
      const files: File[] = [];
      for (let i = 0; i < items.length; i++) {
        const item = items[i];
        if (item.kind === "file") {
          const file = item.getAsFile();
          if (file) files.push(file);
        }
      }
      if (files.length) {
        e.preventDefault();
        onAttach(files);
        return;
      }

      // Text paste. A paste replaces the current selection, so the text
      // that would survive it is everything outside the selection —
      // measuring against the full value would convert slightly too eagerly
      // when the user is pasting over a big selection.
      const pasted = dt.getData("text/plain");
      if (!pasted) return;
      const ta = e.currentTarget;
      const selStart = ta.selectionStart ?? ta.value.length;
      const selEnd = ta.selectionEnd ?? selStart;
      const before = ta.value.slice(0, selStart);
      const after = ta.value.slice(selEnd);
      if (!exceedsTextBudget(before + after, pasted)) return; // fits — paste normally

      // Over the cap. Spill the pasted text into a .txt attachment rather
      // than letting the send 413. If the attachment slots are full we
      // can't, so let the paste through and say why — the send-time guard
      // in App.handleSend surfaces a real error rather than hanging.
      if (pendingAttachments.length >= MAX_FILES_PER_TURN) {
        showPasteNotice(t("composer.paste.too_many_files", { max: MAX_FILES_PER_TURN }));
        return;
      }
      e.preventDefault();
      pasteSeqRef.current += 1;
      const filename = `pasted-text-${pasteSeqRef.current}.txt`;
      const file = makeTextFile(pasted, filename);
      onAttach([file]);
      showPasteNotice(
        t("composer.paste.converted", {
          filename,
          size: formatBytes(file.size),
        }),
      );
      // The surrounding text is preserved verbatim; only the pasted span
      // is replaced by the attachment, so a selection-paste still deletes
      // what it was pasted over.
      const next = before + after;
      setText(next);
      setDraft(draftKey, next);
      window.setTimeout(() => {
        const el = textareaRef.current;
        if (!el) return;
        el.focus();
        el.setSelectionRange(before.length, before.length);
      }, 0);
    },
    [onAttach, pendingAttachments.length, showPasteNotice, t, draftKey],
  );

  const canSend =
    !disabled && (text.trim().length > 0 || pendingAttachments.length > 0);

  return (
    <div
      className={`composer ${dropping ? "is-dropping" : ""}`}
      onDragOver={(e) => {
        e.preventDefault();
        setDropping(true);
      }}
      onDragLeave={() => setDropping(false)}
      onDrop={onDrop}
    >
      {pendingAttachments.length > 0 && (
        <ul className="composer-attachments" aria-label="Pending attachments">
          {pendingAttachments.map((a, idx) => {
            const previewUrl = previewUrls[idx];
            const isImage =
              previewUrl !== null || (a.mime && a.mime.startsWith("image/"));
            return (
              <li
                key={`${a.filename}-${idx}`}
                className={`composer-attachment ${isImage ? "composer-attachment--image" : "composer-attachment--file"}`}
                title={`${a.filename} (${formatBytes(a.size)})`}
              >
                {isImage && previewUrl ? (
                  <img
                    className="composer-attachment-thumb"
                    src={previewUrl}
                    alt={a.filename}
                  />
                ) : (
                  <>
                    <span className="composer-attachment-icon" aria-hidden="true">
                      {fileTypeIcon(a.filename, a.mime)}
                    </span>
                    <span className="composer-attachment-meta">
                      <span className="composer-attachment-name">{a.filename}</span>
                      <span className="composer-attachment-sub">
                        {fileTypeLabel(a.filename, a.mime)} · {formatBytes(a.size)}
                      </span>
                    </span>
                  </>
                )}
                <button
                  type="button"
                  className="composer-attachment-remove"
                  onClick={() => onRemoveAttachment(idx)}
                  aria-label={`Remove ${a.filename}`}
                >
                  ✕
                </button>
              </li>
            );
          })}
        </ul>
      )}
      {(onModelChange || onToggleWebSearch || onToggleImageGen || sessionId) && (
        <div className="composer-toolbar">
          {onModelChange && (
            <select
              className={`composer-pill composer-pill--select ${model !== "default" ? "is-on" : ""}`}
              value={model}
              disabled={disabled}
              onChange={(e) => onModelChange(e.target.value as ModelChoice)}
              aria-label={t("composer.pill.model_aria")}
              title={t("composer.pill.model_title")}
            >
              {(["glm", "kimi", "mimo", "qwen", "deepseek", "minimax", "gemma4-local"] as ModelChoice[]).map((m) => (
                <option key={m} value={m}>
                  {MODEL_LABELS[m]}
                </option>
              ))}
            </select>
          )}
          {onEffortChange && (() => {
            // Effort selector — only for models with effort levels.
            // Rendered next to the model selector; "auto" (empty value)
            // means "provider default" (no reasoning_effort sent).
            const levels = model ? EFFORT_LEVELS[model] : undefined;
            if (!levels) return null;
            const current =
              effort && levels.includes(effort) ? effort : null;
            return (
              <select
                className={`composer-pill composer-pill--select ${current ? "is-on" : ""}`}
                value={current ?? ""}
                disabled={disabled}
                onChange={(e) => onEffortChange(e.target.value || null)}
                aria-label={t("composer.pill.effort_aria")}
                title={t("composer.pill.effort_title")}
              >
                <option value="">{t("composer.pill.effort_auto")}</option>
                {levels.map((lvl) => (
                  <option key={lvl} value={lvl}>
                    {lvl}
                  </option>
                ))}
              </select>
            );
          })()}
          {onToggleWebSearch && (
            <button
              type="button"
              className={`composer-pill ${webSearchOn ? "is-on" : ""}`}
              onClick={onToggleWebSearch}
              disabled={disabled}
              aria-pressed={webSearchOn}
              title={webSearchOn ? t("composer.pill.web_search_on") : t("composer.pill.web_search_off")}
            >
              {t("composer.pill.web_search")}
            </button>
          )}
          {onToggleImageGen && (
            <button
              type="button"
              className={`composer-pill ${imageGenOn ? "is-on" : ""}`}
              onClick={onToggleImageGen}
              disabled={disabled}
              aria-pressed={imageGenOn}
              title={imageGenOn ? t("composer.pill.image_on") : t("composer.pill.image_off")}
            >
              {t("composer.pill.image")}
            </button>
          )}
          {sessionId && <ScheduleIndicator sessionId={sessionId} />}
        </div>
      )}
      <div className="composer-row">
        <button
          type="button"
          className="composer-paperclip"
          onClick={() => fileInputRef.current?.click()}
          disabled={disabled}
          aria-label={t("composer.attach_aria")}
          title={t("composer.attach_title")}
        >
          📎
        </button>
        <input
          ref={fileInputRef}
          type="file"
          multiple
          style={{ display: "none" }}
          onChange={onPickFile}
        />
        <textarea
          ref={textareaRef}
          className="composer-textarea"
          placeholder={
            disabled
              ? t("composer.placeholder.streaming")
              : imageGenOn
                ? t("composer.placeholder.image")
                : t("composer.placeholder.file_picker")
          }
          value={text}
          onChange={onTextareaChange}
          onKeyDown={onKeyDown}
          onKeyUp={reEvalFromTextarea}
          onClick={reEvalFromTextarea}
          onPaste={onPaste}
          onBlur={() => {
            // Defer close so a click on a picker item still fires.
            window.setTimeout(() => setPickerOpen(false), 120);
          }}
          disabled={disabled}
          rows={1}
        />
        {pickerOpen && (
          <div className="file-picker" role="listbox" aria-label={t("filepicker.aria")}>
            <div className="file-picker-header">
              {pickerLoading
                ? t("filepicker.searching")
                : pickerResults.length === 0
                  ? t("filepicker.no_matches", { root: pickerRoot || "/" })
                  : t("filepicker.header", { root: pickerRoot || "/" })}
            </div>
            {pickerResults.length > 0 && (
              <ul className="file-picker-list">
                {pickerResults.map((entry, i) => (
                  <li
                    key={entry.path}
                    className={`file-picker-item ${i === pickerIndex ? "is-selected" : ""}`}
                    role="option"
                    aria-selected={i === pickerIndex}
                    // mousedown fires before blur so the picker doesn't
                    // close before we can capture the click.
                    onMouseDown={(e) => {
                      e.preventDefault();
                      acceptPickerResult(entry);
                    }}
                    onMouseEnter={() => setPickerIndex(i)}
                  >
                    <span className="file-picker-icon" aria-hidden="true">
                      {entry.is_dir ? "📁" : "📄"}
                    </span>
                    <span className="file-picker-name">{entry.name}</span>
                    {!entry.is_dir && (
                      <span className="file-picker-size">{formatBytes(entry.size)}</span>
                    )}
                  </li>
                ))}
              </ul>
            )}
            <div className="file-picker-hint">
              {t("filepicker.hint")}
            </div>
          </div>
        )}
        {streaming && onCancel ? (
          /* Phase 4: Stop replaces Send while a turn is in flight.
             Class name is shared with the send button so existing
             CSS sizing rules apply unchanged; visually distinguished
             by the inner glyph (⏹) and the aria-label. */
          <button
            type="button"
            className="composer-send composer-stop"
            onClick={onCancel}
            aria-label={t("composer.stop_aria")}
            title={t("composer.stop_title")}
          >
            ⏹
          </button>
        ) : (
          <button
            type="button"
            className="composer-send"
            onClick={trySend}
            disabled={!canSend}
            title={t("composer.send_title")}
            aria-label={t("composer.send_aria")}
          >
            ➤
          </button>
        )}
      </div>
      {pasteNotice && (
        <div className="composer-paste-notice" role="status">
          {pasteNotice}
        </div>
      )}
      {dropping && (
        <div className="composer-drop-overlay" aria-hidden="true">
          {t("composer.drop_overlay")}
        </div>
      )}
    </div>
  );
}

// Maps filename/mime to a short type label shown beneath the filename on
// non-image attachment cards (claude.ai shows "PDF", "Document", etc.).
export function fileTypeLabel(filename: string, mime?: string): string {
  const ext = (filename.split(".").pop() || "").toLowerCase();
  if (mime?.startsWith("image/")) return "Image";
  if (mime?.startsWith("video/")) return "Video";
  if (mime?.startsWith("audio/")) return "Audio";
  if (ext === "pdf") return "PDF";
  if (["doc", "docx"].includes(ext)) return "Document";
  if (["xls", "xlsx", "csv"].includes(ext)) return "Spreadsheet";
  if (["ppt", "pptx"].includes(ext)) return "Presentation";
  if (["md", "markdown"].includes(ext)) return "Markdown";
  if (["txt", "log"].includes(ext)) return "Text";
  if (["json", "yaml", "yml", "toml", "xml"].includes(ext)) return "Data";
  if (["js", "ts", "tsx", "jsx", "py", "rs", "go", "rb", "java", "c", "cpp", "h", "hpp", "sh", "bash", "zsh", "html", "css", "scss"].includes(ext)) return "Code";
  if (["zip", "tar", "gz", "bz2", "7z", "rar"].includes(ext)) return "Archive";
  return ext ? ext.toUpperCase() : "File";
}

// Returns a single glyph that hints at the file kind. Kept emoji-based so we
// don't have to bundle an icon library; claude.ai uses colored SVGs but the
// glyph carries the same information at much lower cost.
export function fileTypeIcon(filename: string, mime?: string): string {
  const ext = (filename.split(".").pop() || "").toLowerCase();
  if (mime?.startsWith("image/")) return "🖼️";
  if (mime?.startsWith("video/")) return "🎬";
  if (mime?.startsWith("audio/")) return "🎵";
  if (ext === "pdf") return "📕";
  if (["doc", "docx"].includes(ext)) return "📘";
  if (["xls", "xlsx", "csv"].includes(ext)) return "📗";
  if (["ppt", "pptx"].includes(ext)) return "📙";
  if (["zip", "tar", "gz", "bz2", "7z", "rar"].includes(ext)) return "🗜️";
  if (["js", "ts", "tsx", "jsx", "py", "rs", "go", "rb", "java", "c", "cpp", "h", "hpp", "sh", "bash", "zsh", "html", "css", "scss", "json", "yaml", "yml", "toml", "xml"].includes(ext)) return "📜";
  return "📄";
}

