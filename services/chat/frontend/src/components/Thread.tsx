/**
 * Conversation transcript with progressive rendering.
 *
 * User bubbles render as plain text (right-aligned). Assistant bubbles run
 * through ``react-markdown`` + ``remark-gfm`` so tables, links, and bullet
 * lists work; code fences are highlighted via ``react-syntax-highlighter``.
 * A local ``remarkSoftBreaks`` plugin renders single newlines as line breaks
 * (claude.ai / GitHub behaviour) so line-separated prose doesn't collapse into
 * a wall of text.
 *
 * Tool-output fence:
 *   The backend currently does NOT tag which spans of an assistant message
 *   are tool output, so the v1 untrusted-content fence (``⚠ BEGIN/END
 *   UNTRUSTED USER CONTENT ⚠``) is best-effort and only kicks in when the
 *   backend wraps a span with the literal markers ``<<TOOL_OUTPUT>>...
 *   <</TOOL_OUTPUT>>``. Phase 2 doesn't emit those markers; the gap is
 *   documented in PROGRESS.md as a Phase 2.5 follow-up. Until then this
 *   path is dormant.
 *
 * Scroll behaviour: we auto-scroll to the bottom on new content unless the
 * user has scrolled up. We track that with a ``stickToBottom`` ref that
 * flips off as soon as the user moves more than ~80px above the bottom.
 */

import { memo, useCallback, useContext, useEffect, useLayoutEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneDark, oneLight } from "react-syntax-highlighter/dist/esm/styles/prism";
import "katex/dist/katex.min.css";
import { AttachmentMeta, Message, MessageMeta } from "../api";
import { useT } from "../i18n";
import { BRIEF_TAIL_CHARS, ThinkingModeContext } from "../thinkingMode";
import { formatBytes } from "../utils";
import { ArtifactPreview, ArtifactInfo } from "./Artifact";
import { fileTypeIcon, fileTypeLabel } from "./Composer";

const FENCE_BEGIN = "⚠ BEGIN UNTRUSTED USER CONTENT ⚠";
const FENCE_END = "⚠ END UNTRUSTED USER CONTENT ⚠";
const TOOL_OUTPUT_OPEN = "<<TOOL_OUTPUT>>";
const TOOL_OUTPUT_CLOSE = "<</TOOL_OUTPUT>>";

// Matches the trailing block emitted by app._format_artifacts_markdown:
//   \n\n---\n**Artifacts**\n<one or more artifact lines>\n
// Either an image embed (![name](url)) or a sized link bullet
// (- [name](url) (1.0 KB)). The block is always at end-of-message — we
// anchor with $ to keep older messages whose body coincidentally
// contains a literal "**Artifacts**" string from being eaten.
const ARTIFACTS_BLOCK_RE = /\n*\n---\n\*\*Artifacts\*\*\n([\s\S]+?)\s*$/;
const ARTIFACT_IMAGE_RE = /^!\[([^\]]*)\]\(([^)]+)\)\s*$/;
const ARTIFACT_LINK_RE = /^- \[([^\]]+)\]\(([^)]+)\)(?:\s*\(([^)]+)\))?\s*$/;

function parseHumanSize(s: string | undefined): number | undefined {
  if (!s) return undefined;
  const m = s.match(/^([\d.]+)\s*(B|KB|MB|GB)$/i);
  if (!m) return undefined;
  const n = parseFloat(m[1]);
  if (!isFinite(n)) return undefined;
  const unit = m[2].toUpperCase();
  if (unit === "B") return Math.round(n);
  if (unit === "KB") return Math.round(n * 1024);
  if (unit === "MB") return Math.round(n * 1024 * 1024);
  if (unit === "GB") return Math.round(n * 1024 * 1024 * 1024);
  return undefined;
}

function splitArtifactsFooter(content: string): { text: string; artifacts: ArtifactInfo[] } {
  const m = content.match(ARTIFACTS_BLOCK_RE);
  if (!m || m.index === undefined) return { text: content, artifacts: [] };
  const inner = m[1];
  const artifacts: ArtifactInfo[] = [];
  for (const raw of inner.split("\n")) {
    const line = raw.trimEnd();
    if (!line) continue;
    const img = line.match(ARTIFACT_IMAGE_RE);
    if (img) {
      artifacts.push({ filename: img[1] || basenameFromUrl(img[2]), url: img[2] });
      continue;
    }
    const lnk = line.match(ARTIFACT_LINK_RE);
    if (lnk) {
      artifacts.push({
        filename: lnk[1],
        url: lnk[2],
        size: parseHumanSize(lnk[3]),
      });
      continue;
    }
    // Unrecognised line — bail out and render the whole message as plain
    // markdown so we don't silently swallow content.
    return { text: content, artifacts: [] };
  }
  if (artifacts.length === 0) return { text: content, artifacts: [] };
  return { text: content.slice(0, m.index).replace(/\s+$/, ""), artifacts };
}

function basenameFromUrl(url: string): string {
  const slash = url.lastIndexOf("/");
  return slash >= 0 ? url.slice(slash + 1) : url;
}

// Lightweight URL extractor for the citations footer. Matches plain
// http(s) URLs anywhere in the assistant's text — these are the
// candidates the model is most likely to surface from WebSearch /
// WebFetch tool calls. Inline markdown links of the form `[t](url)`
// are also matched (the URL is captured the same way).
const URL_REGEX = /https?:\/\/[^\s<>"')\]]+/g;

function extractCitations(content: string): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const raw of content.match(URL_REGEX) || []) {
    // Strip trailing punctuation that the regex tolerated.
    const cleaned = raw.replace(/[.,;:!?]+$/, "");
    if (seen.has(cleaned)) continue;
    seen.add(cleaned);
    out.push(cleaned);
  }
  return out;
}

export interface ThreadProps {
  /** Active session id — needed to build per-message attachment preview URLs. */
  sessionId?: string | null;
  messages: Message[];
  streaming: boolean;
  onForkAndResend?: (fromSeq: number, newText: string) => void;
}

// Number of (tail) messages rendered when a thread opens, and how many more
// each "Load earlier" click reveals. Rendering only the tail keeps opening a
// long conversation fast — every bubble runs react-markdown + KaTeX + Prism, so
// mounting 180+ at once froze the main thread for seconds. The newest messages
// (and the streaming one) are always within the tail window.
const INITIAL_WINDOW = 30;
const WINDOW_STEP = 50;

export function Thread({ sessionId, messages, streaming, onForkAndResend }: ThreadProps): JSX.Element {
  const scrollerRef = useRef<HTMLDivElement | null>(null);
  const stickToBottomRef = useRef<boolean>(true);
  const [visibleCount, setVisibleCount] = useState<number>(INITIAL_WINDOW);

  // Reset the window to the tail whenever the open session changes. React's
  // documented "adjust state during render" pattern — runs synchronously
  // before paint, so a freshly-opened long thread never renders its full
  // history, not even for one frame.
  const [renderedSession, setRenderedSession] = useState<string | null | undefined>(sessionId);
  if (sessionId !== renderedSession) {
    setRenderedSession(sessionId);
    setVisibleCount(INITIAL_WINDOW);
  }

  // Track user scroll: if they're near bottom, keep auto-scrolling; if not,
  // honour their position.
  useEffect(() => {
    const el = scrollerRef.current;
    if (!el) return;
    const onScroll = () => {
      const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
      stickToBottomRef.current = distanceFromBottom < 80;
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      el.removeEventListener("scroll", onScroll);
    };
  }, []);

  // Stick to bottom on every render if the user hasn't scrolled away.
  useLayoutEffect(() => {
    if (stickToBottomRef.current && scrollerRef.current) {
      scrollerRef.current.scrollTop = scrollerRef.current.scrollHeight;
    }
  });

  // After "Load earlier" prepends messages, keep the viewport anchored
  // (preserve distance-from-bottom) instead of jumping.
  const prependAnchorRef = useRef<number | null>(null);
  useLayoutEffect(() => {
    const el = scrollerRef.current;
    if (el && prependAnchorRef.current != null) {
      el.scrollTop = el.scrollHeight - prependAnchorRef.current;
      prependAnchorRef.current = null;
    }
  });

  const total = messages.length;
  const startIdx = Math.max(0, total - visibleCount);
  const hiddenCount = startIdx;

  // A scheduled wake's placeholder renders ITSELF rather than collapsing into
  // the pane-level typing indicator. The ⏰ tag on that bubble is the only
  // thing on screen that says the turn started on a timer instead of from
  // something the user typed, and collapsing it meant a wake in progress was
  // indistinguishable from an ordinary reply in progress — the bubble only
  // appeared, already labelled, once the first delta landed.
  const lastMsg = total > 0 ? messages[total - 1] : undefined;
  const trailingWakePlaceholder =
    !!lastMsg &&
    lastMsg.role === "assistant" &&
    lastMsg.content === "" &&
    lastMsg.via === "wake";

  const loadEarlier = (): void => {
    const el = scrollerRef.current;
    if (el) prependAnchorRef.current = el.scrollHeight - el.scrollTop;
    stickToBottomRef.current = false;
    setVisibleCount((c) => c + WINDOW_STEP);
  };

  return (
    <div className="thread-scroller" ref={scrollerRef}>
      <div className="thread-inner">
        {total === 0 ? (
          <EmptyState />
        ) : (
          <>
            {hiddenCount > 0 && (
              <button
                type="button"
                className="thread-load-earlier"
                onClick={loadEarlier}
                style={{
                  display: "block",
                  margin: "8px auto 14px",
                  padding: "6px 14px",
                  fontSize: "0.85em",
                  borderRadius: "999px",
                  border: "1px solid var(--wt-border, #3a3a3a)",
                  background: "transparent",
                  color: "inherit",
                  cursor: "pointer",
                  opacity: 0.75,
                }}
              >
                Load {Math.min(WINDOW_STEP, hiddenCount)} earlier ({hiddenCount} hidden)
              </button>
            )}
            {/* Render only the tail window. The REAL index `i` is preserved so
               the `seq` key and the streaming-placeholder check stay correct.

               Phase 4: key by ``seq``; fall back to the real array index for
               the optimistic placeholder inserted before the server assigns a
               seq. Hide the trailing empty assistant placeholder while
               streaming — App.tsx#onSend appends an empty assistant bubble, and
               rendering it next to the TypingIndicator produces a visible
               "double bubble"; collapse to just the indicator until the first
               delta lands. */}
            {messages.slice(startIdx).map((m, j) => {
              const i = startIdx + j;
              const isLast = i === total - 1;
              const isEmptyStreamingPlaceholder =
                streaming &&
                isLast &&
                m.role === "assistant" &&
                m.content === "" &&
                !(m.reasoning && m.reasoning.length > 0) &&
                !trailingWakePlaceholder;
              if (isEmptyStreamingPlaceholder) return null;
              return (
                <MessageBubble
                  key={m.seq ?? i}
                  sessionId={sessionId ?? null}
                  message={m}
                  onForkAndResend={onForkAndResend}
                />
              );
            })}
          </>
        )}
        {streaming && !trailingWakePlaceholder && <TypingIndicator />}
      </div>
    </div>
  );
}

function EmptyState(): JSX.Element {
  return (
    <div className="thread-empty">
      <p className="thread-empty-title">Start a conversation</p>
      <p className="thread-empty-sub">
        Type a message below, attach a file, or use a sidebar command.
      </p>
    </div>
  );
}

function TypingIndicator(): JSX.Element {
  return (
    <div className="thread-typing" aria-live="polite" aria-label="Assistant is typing">
      <span className="typing-dot" />
      <span className="typing-dot" />
      <span className="typing-dot" />
    </div>
  );
}

interface MessageBubbleProps {
  sessionId: string | null;
  message: Message;
  onForkAndResend?: (fromSeq: number, newText: string) => void;
}

function MessageBubbleImpl({ sessionId, message, onForkAndResend }: MessageBubbleProps): JSX.Element {
  const isUser = message.role === "user";
  const [editing, setEditing] = useState<boolean>(false);
  const [draft, setDraft] = useState<string>(message.content);

  const canEdit =
    isUser &&
    typeof message.seq === "number" &&
    typeof onForkAndResend === "function";

  const startEdit = useCallback(() => {
    setDraft(message.content);
    setEditing(true);
  }, [message.content]);

  const cancelEdit = useCallback(() => {
    setEditing(false);
    setDraft(message.content);
  }, [message.content]);

  const submitEdit = useCallback(() => {
    const trimmed = draft.trim();
    if (!trimmed || trimmed === message.content.trim()) {
      cancelEdit();
      return;
    }
    if (typeof message.seq === "number" && onForkAndResend) {
      onForkAndResend(message.seq, trimmed);
    }
    setEditing(false);
  }, [draft, message.content, message.seq, onForkAndResend, cancelEdit]);

  const hasAttachments = !!message.attachments && message.attachments.length > 0;
  return (
    <div className={`bubble bubble-${isUser ? "user" : "assistant"}`}>
      {message.via === "wake" && (
        // A scheduled wake writes an assistant message with no user turn in
        // front of it, so without this the bubble reads as the assistant
        // talking to itself. Label the trigger.
        <div className="bubble-wake-tag" title="This turn was started by a timer, not by a message">
          ⏰ Scheduled wake
        </div>
      )}
      {isUser && hasAttachments && !editing && (
        <BubbleAttachments
          sessionId={sessionId}
          seq={message.seq}
          items={message.attachments!}
        />
      )}
      {isUser ? (
        editing ? (
          <div className="bubble-edit">
            <textarea
              className="bubble-edit-textarea"
              value={draft}
              autoFocus
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                  e.preventDefault();
                  submitEdit();
                }
                if (e.key === "Escape") {
                  e.preventDefault();
                  cancelEdit();
                }
              }}
            />
            <div className="bubble-edit-actions">
              <button
                type="button"
                className="bubble-edit-cancel"
                onClick={cancelEdit}
              >
                Cancel
              </button>
              <button
                type="button"
                className="bubble-edit-submit"
                onClick={submitEdit}
                title="Fork conversation here and resend (⌘+Enter)"
              >
                Resend
              </button>
            </div>
          </div>
        ) : message.content || !hasAttachments ? (
          <div className="bubble-text user-text">
            {/* A user message only carries a status when the send failed
                outright — the server never took it, and App has handed the
                text back to the composer. Without the tag the bubble is
                indistinguishable from a delivered message sitting above a
                composer that mysteriously refilled itself. */}
            {message.status === "error" && (
              <div className="bubble-unsent" title="This message was never delivered">
                ⚠ Not sent
              </div>
            )}
            {message.content}
            {canEdit && (
              <button
                type="button"
                className="bubble-edit-btn"
                onClick={startEdit}
                aria-label="Edit and resend"
                title="Edit and resend (forks the conversation)"
              >
                ✎
              </button>
            )}
          </div>
        ) : null
      ) : (
        <AssistantContent message={message} hasAttachments={hasAttachments} />
      )}
      {!isUser && hasAttachments && (
        <BubbleAttachments
          sessionId={sessionId}
          seq={message.seq}
          items={message.attachments!}
        />
      )}
    </div>
  );
}

function attachmentPreviewUrl(
  sessionId: string | null,
  seq: number | undefined,
  filename: string,
): string | null {
  if (!sessionId || typeof seq !== "number") return null;
  return (
    `/api/sessions/${encodeURIComponent(sessionId)}` +
    `/attachment_previews/${seq}/${encodeURIComponent(filename)}`
  );
}

function isImageMime(mime: string | undefined): boolean {
  return typeof mime === "string" && mime.startsWith("image/");
}

interface BubbleAttachmentsProps {
  sessionId: string | null;
  seq: number | undefined;
  items: AttachmentMeta[];
}

function BubbleAttachments({ sessionId, seq, items }: BubbleAttachmentsProps): JSX.Element {
  return (
    <ul className="bubble-attachments" aria-label="Attached files">
      {items.map((a, i) => {
        const url = attachmentPreviewUrl(sessionId, seq, a.filename);
        if (isImageMime(a.mime) && url) {
          return (
            <li key={i} className="bubble-attachment bubble-attachment--image">
              <a
                href={url}
                target="_blank"
                rel="noopener noreferrer"
                title={`${a.filename} (${formatBytes(a.size)})`}
              >
                <img
                  className="attachment-thumb"
                  src={url}
                  alt={a.filename}
                  loading="lazy"
                />
              </a>
            </li>
          );
        }
        const icon = fileTypeIcon(a.filename, a.mime);
        const label = fileTypeLabel(a.filename, a.mime);
        return (
          <li key={i} className="bubble-attachment bubble-attachment--file">
            {url ? (
              <a
                className="attachment-link"
                href={url}
                target="_blank"
                rel="noopener noreferrer"
                download={a.filename}
                title={`${a.filename} (${formatBytes(a.size)})`}
              >
                <span className="attachment-icon" aria-hidden="true">{icon}</span>
                <span className="attachment-meta">
                  <span className="attachment-name">{a.filename}</span>
                  <span className="attachment-sub">{label} · {formatBytes(a.size)}</span>
                </span>
              </a>
            ) : (
              <div className="attachment-link" title={`${a.filename} (${formatBytes(a.size)})`}>
                <span className="attachment-icon" aria-hidden="true">{icon}</span>
                <span className="attachment-meta">
                  <span className="attachment-name">{a.filename}</span>
                  <span className="attachment-sub">{label} · {formatBytes(a.size)}</span>
                </span>
              </div>
            )}
          </li>
        );
      })}
    </ul>
  );
}

/**
 * What to show inside an assistant bubble that carries no text at all.
 *
 * Blank bubbles are not hypothetical: a scheduled wake persists an EMPTY
 * placeholder the moment it fires and only fills it in when the turn ends, and
 * a wake whose turn dies (account cooldown, stale ``--resume``) is finalised
 * with ``status: "error"`` and no content, because the reason only ever
 * travelled on the SSE ``error`` event — which nobody was subscribed to, since
 * a wake runs with no client attached. The result on screen was a completely
 * blank box: no text, no spinner, no explanation, permanently.
 *
 * The bubble therefore has to be able to describe its own state from the
 * persisted message alone, without depending on the pane's local `streaming`
 * flag (which is only true for turns THIS tab is attached to).
 */
function AssistantBlankState({ status }: { status?: Message["status"] }): JSX.Element {
  if (status === "streaming") {
    return (
      <div className="bubble-pending" aria-live="polite">
        <span className="typing-dot" />
        <span className="typing-dot" />
        <span className="typing-dot" />
        <span className="bubble-pending-label">Working…</span>
      </div>
    );
  }
  if (status === "error") {
    return (
      <div className="bubble-blank bubble-blank-error">
        ⚠ This turn ended before any reply was produced.
      </div>
    );
  }
  if (status === "cancelled") {
    return <div className="bubble-blank">Stopped before any reply was produced.</div>;
  }
  return <div className="bubble-blank">No reply — this turn produced no text.</div>;
}

/**
 * The model's hidden reasoning for one assistant message, rendered according
 * to the thinking-display setting (ThinkingModeContext):
 *   off   → nothing;
 *   brief → one muted line showing the TAIL of the reasoning (the latest
 *           thought while streaming), click to expand this message to full;
 *   full  → a collapsible block, open by default, whole text.
 * Source of the text: the live client-side buffer while streaming
 * (``message.reasoning``), then the persisted ``meta.reasoning`` after a
 * reload. Purely presentational — nothing here changes what was requested.
 */
function ThinkingBlock({ message }: { message: Message }): JSX.Element | null {
  const mode = useContext(ThinkingModeContext);
  const t = useT();
  const [expanded, setExpanded] = useState(false);
  const text = message.reasoning || message.meta?.reasoning || "";
  if (mode === "off" || !text.trim()) return null;
  const live = message.status === "streaming";
  const label = live ? t("thread.thinking_live") : t("thread.thinking");
  if (mode === "brief" && !expanded) {
    const trimmed = text.trimEnd();
    const tail = trimmed.length > BRIEF_TAIL_CHARS
      ? "…" + trimmed.slice(-BRIEF_TAIL_CHARS)
      : trimmed;
    return (
      <button
        type="button"
        className={`msg-thinking msg-thinking--brief ${live ? "is-live" : ""}`}
        onClick={() => setExpanded(true)}
        title={t("thread.thinking_expand")}
        aria-label={`${label}: ${t("thread.thinking_expand")}`}
      >
        <span className="msg-thinking-label">💭 {label}</span>
        <span className="msg-thinking-tail">{tail.replace(/\s+/g, " ")}</span>
      </button>
    );
  }
  // full mode, or a brief-mode block the user expanded.
  return (
    <details className={`msg-thinking msg-thinking--full ${live ? "is-live" : ""}`} open>
      <summary className="msg-thinking-label">
        💭 {label}
        {mode === "brief" && (
          <button
            type="button"
            className="msg-thinking-collapse"
            onClick={(e) => {
              e.preventDefault();
              setExpanded(false);
            }}
          >
            {t("thread.thinking_collapse")}
          </button>
        )}
      </summary>
      <div className="msg-thinking-body">{text}</div>
    </details>
  );
}

/**
 * Splits an assistant message at <<TOOL_OUTPUT>>...<</TOOL_OUTPUT>> markers
 * (if any) and renders each tool-output span inside the
 * "untrusted content" fence. Non-tool spans render as plain markdown.
 */
function AssistantContent(
  { message, hasAttachments = false }: { message: Message; hasAttachments?: boolean },
): JSX.Element {
  const content = message.content;
  const { text, artifacts } = splitArtifactsFooter(content);
  const parts = splitOnToolOutputs(text);
  const citations = extractCitations(text);
  const [copied, copy] = useCopy();
  const trimmed = text.trim();
  // Nothing renderable anywhere in the message — fall back to describing the
  // turn's state rather than emitting an empty <div>. Attachments render
  // outside this component, so a message that is nothing but a file is not
  // blank even though its text is.
  const isBlank = !trimmed && artifacts.length === 0 && !hasAttachments;
  // Reasoning arriving live means the turn is alive even with no visible
  // text yet: don't call it blank while the model is still thinking.
  const thinkingLive = !!message.reasoning && message.status === "streaming";
  return (
    <div className="bubble-text assistant-text">
      <ThinkingBlock message={message} />
      {isBlank && !thinkingLive && <AssistantBlankState status={message.status} />}
      {parts.map((part, i) =>
        part.kind === "tool_output" ? (
          <ToolOutputFence key={i} content={part.text} />
        ) : (
          <Markdown key={i} content={part.text} />
        ),
      )}
      {artifacts.length > 0 && (
        <div className="artifacts">
          <div className="artifacts-title">Artifacts</div>
          {artifacts.map((a, i) => (
            <ArtifactPreview key={`${a.url}-${i}`} artifact={a} />
          ))}
        </div>
      )}
      {citations.length > 0 && (
        <div className="citations" aria-label="Sources cited">
          <div className="citations-title">Sources</div>
          <ol className="citations-list">
            {citations.map((url, i) => (
              <li key={i}>
                <a href={url} target="_blank" rel="noopener noreferrer">
                  {url}
                </a>
              </li>
            ))}
          </ol>
        </div>
      )}
      <MessageMetaFooter message={message} />
      {trimmed && (
        <div className="msg-actions">
          <button
            type="button"
            className="msg-copy-btn"
            onClick={() => copy(trimmed)}
            aria-label={copied ? "Copied" : "Copy message"}
            title="Copy message"
          >
            {copied ? "✓ Copied" : "📋 Copy"}
          </button>
        </div>
      )}
    </div>
  );
}

/**
 * Subdued metadata footer beneath a completed assistant message: completion
 * time (localized), model, total tokens (with in/out split when present),
 * and output tok/s. Renders nothing unless the message has completed and
 * carries at least one displayable field — partial/streaming/errored turns
 * stay clean. All fields are individually optional (a runner that can't
 * source a value sends null), so each chip is conditionally rendered.
 */
function MessageMetaFooter({ message }: { message: Message }): JSX.Element | null {
  const meta: MessageMeta | undefined = message.meta;
  if (!meta) return null;
  // Only show on a settled turn — streaming placeholders shouldn't flash a
  // footer. Absent status on a legacy/loaded message is treated as complete.
  if (message.status && message.status !== "complete" && message.status !== "cancelled") {
    return null;
  }

  const chips: JSX.Element[] = [];

  if (meta.completed_at) {
    const d = new Date(meta.completed_at);
    if (!isNaN(d.getTime())) {
      chips.push(
        <span className="msg-meta-item" key="time" title={d.toISOString()}>
          <span className="msg-meta-label">Completed</span>
          <span className="msg-meta-value">{d.toLocaleString()}</span>
        </span>,
      );
    }
  }

  if (meta.model) {
    chips.push(
      <span className="msg-meta-item" key="model">
        <span className="msg-meta-label">Model</span>
        <span className="msg-meta-value">{meta.model}</span>
      </span>,
    );
  }

  const tokens = meta.tokens;
  if (tokens && (tokens.total != null || tokens.input != null || tokens.output != null)) {
    const est = meta.tokens_estimated ? "~" : "";
    const totalStr =
      tokens.total != null
        ? `${est}${tokens.total.toLocaleString()}`
        : "—";
    const splitStr =
      tokens.input != null || tokens.output != null
        ? ` (${est}${(tokens.input ?? 0).toLocaleString()} in / ${est}${(tokens.output ?? 0).toLocaleString()} out)`
        : "";
    chips.push(
      <span className="msg-meta-item" key="tokens">
        <span className="msg-meta-label">Tokens</span>
        <span className="msg-meta-value">
          {totalStr}
          {splitStr}
        </span>
      </span>,
    );
  }

  if (meta.tok_s != null && isFinite(meta.tok_s)) {
    chips.push(
      <span className="msg-meta-item" key="toks">
        <span className="msg-meta-label">Tok/s</span>
        <span className="msg-meta-value">{meta.tok_s.toFixed(1)}</span>
      </span>,
    );
  }

  if (chips.length === 0) return null;
  return <div className="msg-meta" aria-label="Response metadata">{chips}</div>;
}

interface ToolOutputFenceProps {
  content: string;
}

function ToolOutputFence({ content }: ToolOutputFenceProps): JSX.Element {
  return (
    <div className="untrusted-fence">
      <div className="untrusted-fence-marker">{FENCE_BEGIN}</div>
      <Markdown content={content} />
      <div className="untrusted-fence-marker">{FENCE_END}</div>
    </div>
  );
}

interface MarkdownProps {
  content: string;
}

function useCopy(): [boolean, (text: string) => void] {
  const [copied, setCopied] = useState(false);
  const timer = useRef<number | null>(null);
  const copy = useCallback((text: string) => {
    if (typeof navigator === "undefined" || !navigator.clipboard) return;
    navigator.clipboard
      .writeText(text)
      .then(() => {
        setCopied(true);
        if (timer.current) window.clearTimeout(timer.current);
        timer.current = window.setTimeout(() => setCopied(false), 1400);
      })
      .catch(() => {});
  }, []);
  useEffect(() => {
    return () => {
      if (timer.current) window.clearTimeout(timer.current);
    };
  }, []);
  return [copied, copy];
}

interface CodeBlockProps {
  language: string;
  code: string;
  codeStyle: { [key: string]: React.CSSProperties };
}

function CodeBlock({ language, code, codeStyle }: CodeBlockProps): JSX.Element {
  const [copied, copy] = useCopy();
  return (
    <div className="code-block">
      <button
        type="button"
        className="code-copy-btn"
        onClick={() => copy(code)}
        aria-label={copied ? "Copied" : "Copy code"}
        title="Copy code"
      >
        {copied ? "\u2713 Copied" : "Copy"}
      </button>
      <SyntaxHighlighter
        language={language}
        style={codeStyle}
        PreTag="div"
        customStyle={{
          borderRadius: "8px",
          fontSize: "0.85rem",
          margin: "0.5rem 0",
        }}
      >
        {code}
      </SyntaxHighlighter>
    </div>
  );
}

// Pull the raw source out of a hast ``<pre>`` node's ``<code>`` child.
// Returns null when the <pre> isn't a code fence (so it renders as-is).
//
// Reading the hast node rather than digging through the rendered React
// children is deliberate: ``properties.className`` and the raw text ``value``
// are stable across react-markdown versions, whereas the shape of the child
// element's props is not.
interface HastNode {
  tagName?: string;
  value?: string;
  properties?: { className?: unknown };
  children?: HastNode[];
}

function readCodeFence(node: HastNode | undefined): { language: string; code: string } | null {
  const codeNode = node?.children?.find((c) => c.tagName === "code");
  if (!codeNode) return null;
  const raw = codeNode.properties?.className;
  const classes: string[] = Array.isArray(raw)
    ? raw.map(String)
    : typeof raw === "string"
      ? raw.split(/\s+/)
      : [];
  const langClass = classes.find((c) => c.startsWith("language-"));
  // Info strings can carry more than the language (```python title=x) and can
  // use characters \w misses (c++, objective-c, shell-session). Take the first
  // token and keep it; Prism falls back to unhighlighted text for unknown
  // languages rather than throwing.
  const language = (langClass ? langClass.slice("language-".length) : "").split(/[\s,:]/)[0];
  const collect = (n: HastNode): string =>
    typeof n.value === "string" ? n.value : (n.children || []).map(collect).join("");
  return { language: language || "text", code: collect(codeNode).replace(/\n$/, "") };
}

// react-markdown + remark-gfm follow CommonMark, where a SINGLE newline
// inside a paragraph is a "soft break" rendered as a space. The model writes
// step-by-step explanations as line-separated text (single \n, not blank-line
// paragraphs), so without this they collapse into a wall of text — the
// reported rendering bug. claude.ai and GitHub render soft breaks as real
// line breaks; this local plugin reproduces ``remark-breaks`` (without adding
// the dependency): it splits paragraph-level text nodes on "\n" into text +
// hard-break nodes. Fenced/inline code carry their text in a ``value`` string
// (not child ``text`` nodes), so code is left untouched.
function remarkSoftBreaks() {
  const walk = (node: { type?: string; children?: unknown[] }): void => {
    const children = node.children as
      | Array<{ type: string; value?: string; children?: unknown[] }>
      | undefined;
    if (!Array.isArray(children)) return;
    const next: Array<{ type: string; value?: string }> = [];
    for (const child of children) {
      if (
        child.type === "text" &&
        typeof child.value === "string" &&
        child.value.includes("\n")
      ) {
        const segs = child.value.split("\n");
        segs.forEach((seg, idx) => {
          if (seg) next.push({ type: "text", value: seg });
          if (idx < segs.length - 1) next.push({ type: "break" });
        });
      } else {
        walk(child);
        next.push(child);
      }
    }
    (node as { children: unknown[] }).children = next;
  };
  return (tree: { type?: string; children?: unknown[] }): void => walk(tree);
}

function Markdown({ content }: MarkdownProps): JSX.Element {
  const dark = typeof window !== "undefined"
    && window.matchMedia
    && window.matchMedia("(prefers-color-scheme: dark)").matches;
  const codeStyle = dark ? oneDark : oneLight;
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm, [remarkMath, { singleDollarTextMath: false }], remarkSoftBreaks]}
      rehypePlugins={[rehypeKatex]}
      components={{
        // Handle the fence at the <pre> level, not the <code> level. The old
        // code-level check required a ``language-\w+`` class, so a fence with
        // no info string (```) or an unmatched one (```c++) fell through to
        // the inline branch and rendered as a bare <pre><code> — no
        // background, no copy button, and no width clamp, which is why long
        // CLI lines overflowed the bubble. Every fenced/indented block now
        // routes through CodeBlock regardless of language.
        pre({ node, children, ...rest }) {
          const fence = readCodeFence(node as HastNode | undefined);
          if (fence) {
            return (
              <CodeBlock
                language={fence.language}
                code={fence.code}
                codeStyle={codeStyle as { [key: string]: React.CSSProperties }}
              />
            );
          }
          return <pre {...(rest as { [k: string]: unknown })}>{children}</pre>;
        },
        code({ className, children, ...rest }) {
          // Only inline code reaches here now — fenced blocks are consumed by
          // the <pre> handler above, which never renders this element.
          return (
            <code className={className} {...(rest as { [k: string]: unknown })}>
              {children}
            </code>
          );
        },
        a({ children, ...rest }) {
          return (
            <a {...rest} target="_blank" rel="noopener noreferrer">
              {children}
            </a>
          );
        },
      }}
    >
      {content || "\u200b" /* zero-width space so empty assistant bubbles still render */}
    </ReactMarkdown>
  );
}

interface SplitPart {
  kind: "text" | "tool_output";
  text: string;
}

function splitOnToolOutputs(content: string): SplitPart[] {
  // Phase 2 backend does not emit these markers, so the common path returns
  // a single text part. Phase 2.5 will emit them, at which point this
  // function activates without further client changes.
  if (!content.includes(TOOL_OUTPUT_OPEN)) {
    return [{ kind: "text", text: content }];
  }
  const parts: SplitPart[] = [];
  let i = 0;
  while (i < content.length) {
    const open = content.indexOf(TOOL_OUTPUT_OPEN, i);
    if (open === -1) {
      parts.push({ kind: "text", text: content.slice(i) });
      break;
    }
    if (open > i) {
      parts.push({ kind: "text", text: content.slice(i, open) });
    }
    const innerStart = open + TOOL_OUTPUT_OPEN.length;
    const close = content.indexOf(TOOL_OUTPUT_CLOSE, innerStart);
    if (close === -1) {
      // Unterminated tool-output; treat the rest as untrusted to be safe.
      parts.push({ kind: "tool_output", text: content.slice(innerStart) });
      break;
    }
    parts.push({ kind: "tool_output", text: content.slice(innerStart, close) });
    i = close + TOOL_OUTPUT_CLOSE.length;
  }
  return parts;
}


// Memoized message bubble. The streaming delta handler in App.tsx replaces
// only the last message's object (next[i] = {...}), leaving every other
// message's reference unchanged, and onForkAndResend is a stable useCallback —
// so a shallow-prop memo lets React skip re-rendering (re-parsing markdown,
// re-typesetting KaTeX, re-highlighting code for) every prior bubble on each
// token delta / pane-focus re-render. O(N)→O(1) per update.
const MessageBubble = memo(MessageBubbleImpl);
