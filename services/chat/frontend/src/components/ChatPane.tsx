/**
 * A single chat "window": its own transcript (Thread) + input (Composer),
 * bound to one session id (or `null` for an un-created new chat).
 *
 * ChatPane is purely presentational — all state (messages, streaming flag,
 * draft text, pending attachments, composer toggles) lives in App.tsx and
 * is threaded through props, keyed by this pane's stable `paneKey` and/or
 * its `sessionId`. That keeps the streaming / session-recovery machinery
 * centralized and lets multiple panes coexist on one page, each reading
 * from the shared per-session buffers in App.
 *
 * The pane header only renders when more than one window is open
 * (`showHeader`); a lone window looks exactly like the pre-multi-window UI.
 */

import { AttachmentMeta, Message, ModelChoice, Workspace } from "../api";
import { useT } from "../i18n";
import { Composer } from "./Composer";
import { Thread } from "./Thread";

export interface ChatPaneProps {
  paneKey: string;
  sessionId: string | null;
  /** Display title for the header (session title, or a "new chat" label). */
  title: string;
  messages: Message[];
  streaming: boolean;
  /** Whether this pane is the focused one (sidebar clicks load into it). */
  focused: boolean;
  /** Render the pane header (title + close). True only when >1 pane open. */
  showHeader: boolean;
  /** Allow closing this pane. False for the last remaining pane. */
  canClose: boolean;
  onFocus: () => void;
  onClose: () => void;
  onForkAndResend?: (fromSeq: number, newText: string) => void;

  // ---- composer (all per-pane) ----
  /** Draft storage key for this window (see draftStore); Composer owns the
   *  text locally and persists under this key. */
  draftKey: string;
  onSend: (text: string) => void;
  onCancel: () => void;
  onAttach: (files: File[]) => void;
  pendingAttachments: AttachmentMeta[];
  pendingFiles: File[];
  onRemoveAttachment: (idx: number) => void;
  model: ModelChoice;
  onModelChange: (model: ModelChoice) => void;
  /** Stored reasoning-effort level for the pane's model (null = unset). */
  effort: string | null;
  onEffortChange: (level: string | null) => void;
  webSearchOn: boolean;
  onToggleWebSearch: () => void;
  imageGenOn: boolean;
  onToggleImageGen: () => void;
  sendOnEnter: boolean;
  workspace: Workspace;
}

export function ChatPane(props: ChatPaneProps): JSX.Element {
  const {
    paneKey,
    sessionId,
    title,
    messages,
    streaming,
    focused,
    showHeader,
    canClose,
    onFocus,
    onClose,
    onForkAndResend,
    draftKey,
    onSend,
    onCancel,
    onAttach,
    pendingAttachments,
    pendingFiles,
    onRemoveAttachment,
    model,
    onModelChange,
    effort,
    onEffortChange,
    webSearchOn,
    onToggleWebSearch,
    imageGenOn,
    onToggleImageGen,
    sendOnEnter,
    workspace,
  } = props;
  const t = useT();
  const displayTitle = sessionId
    ? title || t("session.untitled")
    : t("pane.new_window_title");

  return (
    <section
      className={`chat-pane ${focused ? "is-focused" : ""}`}
      data-pane-key={paneKey}
      // Focus-follows-interaction: clicking anywhere in the pane makes it
      // the target for sidebar session selection. Capture phase so it wins
      // even when an inner control stops propagation.
      onMouseDownCapture={() => {
        if (!focused) onFocus();
      }}
    >
      {showHeader && (
        <header className="chat-pane-header">
          <span className="chat-pane-title" title={displayTitle}>
            {displayTitle}
          </span>
          {canClose && (
            <button
              type="button"
              className="chat-pane-close"
              onClick={(e) => {
                e.stopPropagation();
                onClose();
              }}
              aria-label={t("pane.close_aria")}
              title={t("pane.close_title")}
            >
              ✕
            </button>
          )}
        </header>
      )}
      <Thread
        sessionId={sessionId}
        messages={messages}
        streaming={streaming}
        onForkAndResend={onForkAndResend}
      />
      <Composer
        draftKey={draftKey}
        // Never hard-disable while streaming: the user can type mid-stream to
        // interrupt and add context (claude.ai-style). `streaming` still drives
        // the Stop button; App's handleSend turns a mid-stream submit into an
        // interrupt-then-send.
        disabled={false}
        streaming={streaming}
        onCancel={onCancel}
        pendingAttachments={pendingAttachments}
        pendingFiles={pendingFiles}
        onRemoveAttachment={onRemoveAttachment}
        onSend={onSend}
        onAttach={onAttach}
        model={model}
        onModelChange={onModelChange}
        effort={effort}
        onEffortChange={onEffortChange}
        webSearchOn={webSearchOn}
        onToggleWebSearch={onToggleWebSearch}
        imageGenOn={imageGenOn}
        onToggleImageGen={onToggleImageGen}
        sendOnEnter={sendOnEnter}
        workspace={workspace}
        sessionId={sessionId}
      />
    </section>
  );
}
