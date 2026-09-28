// Jupyter-style overlay that runs a `.py` artifact inside the user's
// per-user container and streams output back via SSE. Closes on Esc /
// backdrop click; survives the user keeping it open across multiple runs
// of the same artifact via the "Run again" button.
//
// Shape:
//   ┌───────────────────────────────────────────────────────┐
//   │  ▶ script.py                       Status     × Close │ <- header
//   ├───────────────────────────────────────────────────────┤
//   │  console (stdout/stderr, monospace, auto-scroll)      │
//   │                                                       │
//   ├───────────────────────────────────────────────────────┤
//   │  Charts / outputs (gallery of images, downloads, …)   │
//   ├───────────────────────────────────────────────────────┤
//   │              [▪ Stop]  [↻ Run again]                  │ <- footer
//   └───────────────────────────────────────────────────────┘

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  ApiError,
  cancelArtifactRun,
  RunMediaItem,
  RunStreamEvent,
  startArtifactRun,
  streamArtifactRun,
} from "../api";

type Props = {
  sessionId: string;
  filename: string;
  source: "generated" | "attachment";
  onClose: () => void;
};

type ConsoleLine = { id: number; kind: "stdout" | "stderr" | "system"; text: string };

type RunState =
  | { phase: "starting" }
  | { phase: "running"; runId: string }
  | { phase: "done"; runId: string; exitCode: number }
  | { phase: "cancelled"; runId: string; exitCode: number }
  | { phase: "error"; message: string; runId?: string };

const MAX_CONSOLE_LINES = 5000;

export function ArtifactRunModal({ sessionId, filename, source, onClose }: Props): JSX.Element {
  const [state, setState] = useState<RunState>({ phase: "starting" });
  const [lines, setLines] = useState<ConsoleLine[]>([]);
  const [media, setMedia] = useState<RunMediaItem[]>([]);
  const [generation, setGeneration] = useState(0); // bumped on "Run again"
  const lineIdRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);
  const consoleEndRef = useRef<HTMLDivElement>(null);
  const autoScrollRef = useRef(true);

  const appendLine = useCallback((kind: ConsoleLine["kind"], text: string) => {
    setLines((prev) => {
      // Coalesce consecutive same-kind chunks into one DOM node where
      // possible; keep newlines so the user still sees structure. We split
      // on newline boundaries so each visual line is its own entry.
      const parts = text.split(/(?<=\n)/);
      const out = prev.length > 0 ? [...prev] : [];
      for (const part of parts) {
        if (!part) continue;
        const idx = ++lineIdRef.current;
        out.push({ id: idx, kind, text: part });
      }
      if (out.length > MAX_CONSOLE_LINES) {
        const drop = out.length - MAX_CONSOLE_LINES;
        return out.slice(drop);
      }
      return out;
    });
  }, []);

  // Auto-scroll only if the user is already near the bottom. If they've
  // scrolled up to inspect older output, don't yank them back down.
  useEffect(() => {
    if (autoScrollRef.current) {
      consoleEndRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
    }
  }, [lines.length]);

  const onConsoleScroll = useCallback((e: React.UIEvent<HTMLDivElement>) => {
    const el = e.currentTarget;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    autoScrollRef.current = atBottom;
  }, []);

  // Drive one run-from-start lifecycle. Re-runs by bumping `generation`.
  useEffect(() => {
    let cancelled = false;
    setLines([]);
    setMedia([]);
    setState({ phase: "starting" });
    lineIdRef.current = 0;
    autoScrollRef.current = true;

    const ctrl = new AbortController();
    abortRef.current = ctrl;

    (async () => {
      let startResult: { run_id: string };
      try {
        startResult = await startArtifactRun(sessionId, filename, source);
      } catch (err) {
        if (cancelled) return;
        const msg = err instanceof ApiError
          ? `${err.status}: ${typeof err.detail === "string" ? err.detail : err.message}`
          : err instanceof Error ? err.message : String(err);
        appendLine("system", `[error] failed to start run: ${msg}\n`);
        setState({ phase: "error", message: msg });
        return;
      }
      if (cancelled) {
        // User closed the modal between start and stream — best-effort cancel.
        void cancelArtifactRun(sessionId, startResult.run_id).catch(() => {});
        return;
      }
      setState({ phase: "running", runId: startResult.run_id });

      const onEvent = (evt: RunStreamEvent) => {
        if (cancelled) return;
        switch (evt.type) {
          case "stdout":
            appendLine("stdout", evt.text);
            break;
          case "stderr":
            appendLine("stderr", evt.text);
            break;
          case "media":
            // Merge new media items by filename (de-dup if backend
            // re-emits on reattach).
            setMedia((prev) => {
              const byName = new Map(prev.map((m) => [m.filename, m]));
              for (const m of evt.items) byName.set(m.filename, m);
              return [...byName.values()].sort((a, b) => a.filename.localeCompare(b.filename));
            });
            break;
          case "done": {
            setMedia((prev) => {
              const byName = new Map(prev.map((m) => [m.filename, m]));
              for (const m of evt.media) byName.set(m.filename, m);
              return [...byName.values()].sort((a, b) => a.filename.localeCompare(b.filename));
            });
            setState(
              evt.status === "cancelled"
                ? { phase: "cancelled", runId: startResult.run_id, exitCode: evt.exit_code }
                : evt.status === "done"
                  ? { phase: "done", runId: startResult.run_id, exitCode: evt.exit_code }
                  : { phase: "error", message: `exit code ${evt.exit_code}`, runId: startResult.run_id },
            );
            break;
          }
          case "status":
            // server announces "running"; we already set that above.
            break;
          case "ping":
            break;
          case "error":
            appendLine("system", `[error] ${evt.message}\n`);
            break;
        }
      };

      try {
        await streamArtifactRun(sessionId, startResult.run_id, onEvent, { signal: ctrl.signal });
      } catch (err) {
        if (cancelled) return;
        if (err instanceof DOMException && err.name === "AbortError") return;
        const msg = err instanceof Error ? err.message : String(err);
        appendLine("system", `[error] stream interrupted: ${msg}\n`);
      }
    })();

    return () => {
      cancelled = true;
      ctrl.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, filename, source, generation, appendLine]);

  // Esc closes; click on backdrop closes.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const onStop = useCallback(() => {
    if (state.phase !== "running") return;
    void cancelArtifactRun(sessionId, state.runId).catch(() => {});
  }, [sessionId, state]);

  const onRunAgain = useCallback(() => {
    setGeneration((g) => g + 1);
  }, []);

  const statusBadge = useMemo(() => {
    switch (state.phase) {
      case "starting":  return { label: "Starting…", cls: "run-status-starting" };
      case "running":   return { label: "● Running", cls: "run-status-running" };
      case "done":      return { label: `✓ Done (exit ${state.exitCode})`, cls: "run-status-done" };
      case "cancelled": return { label: "Stopped", cls: "run-status-cancelled" };
      case "error":     return { label: `✗ ${state.message}`, cls: "run-status-error" };
    }
  }, [state]);

  return createPortal(
    <div
      className="run-backdrop"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="run-modal" role="dialog" aria-modal="true" aria-label={`Run ${filename}`}>
        <div className="run-header">
          <span className="run-title">
            <span className="run-icon">▶</span>
            <span className="run-filename">{filename}</span>
          </span>
          <span className={`run-status ${statusBadge.cls}`}>{statusBadge.label}</span>
          <button className="run-close" onClick={onClose} aria-label="Close" title="Close (Esc)">×</button>
        </div>

        <div className="run-console" onScroll={onConsoleScroll}>
          {lines.length === 0 && state.phase === "starting" && (
            <div className="run-empty">Starting in your container…</div>
          )}
          {lines.map((ln) => (
            <span key={ln.id} className={`run-line run-line-${ln.kind}`}>{ln.text}</span>
          ))}
          <div ref={consoleEndRef} />
        </div>

        {media.length > 0 && (
          <div className="run-media">
            <div className="run-media-header">Outputs · {media.length}</div>
            <div className="run-media-grid">
              {media.map((m) => (
                <MediaTile key={m.filename} item={m} />
              ))}
            </div>
          </div>
        )}

        <div className="run-footer">
          {state.phase === "running" && (
            <button className="btn btn-danger" onClick={onStop}>■ Stop</button>
          )}
          {(state.phase === "done" || state.phase === "cancelled" || state.phase === "error") && (
            <button className="btn btn-primary" onClick={onRunAgain}>↻ Run again</button>
          )}
          <button className="btn btn-secondary" onClick={onClose}>Close</button>
        </div>
      </div>
    </div>,
    document.body,
  );
}


function MediaTile({ item }: { item: RunMediaItem }): JSX.Element {
  const isImage = item.mime.startsWith("image/");
  return (
    <figure className="run-media-tile">
      {isImage ? (
        <a href={item.url} target="_blank" rel="noopener noreferrer" title={`${item.filename} (${formatBytes(item.size)})`}>
          <img src={item.url} alt={item.filename} loading="lazy" />
        </a>
      ) : (
        <a
          className="run-media-download"
          href={item.url}
          download={item.filename}
          target="_blank"
          rel="noopener noreferrer"
        >
          <span className="run-media-download-icon">⤓</span>
          <span className="run-media-download-name">{item.filename}</span>
          <span className="run-media-download-size">{formatBytes(item.size)}</span>
        </a>
      )}
      <figcaption className="run-media-caption">{item.filename}</figcaption>
    </figure>
  );
}


function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}
