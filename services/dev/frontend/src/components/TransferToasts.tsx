import { useEffect, useState } from "react";
import { Transfer, transferStore } from "../transfers";

function verbForKind(kind: "upload" | "download" | "compress"): string {
  if (kind === "download") return "downloading";
  if (kind === "compress") return "compressing";
  return "uploading";
}

function iconForKind(kind: "upload" | "download" | "compress"): string {
  if (kind === "download") return "↓";
  if (kind === "compress") return "⊟";
  return "↑";
}

function formatBytes(b: number): string {
  if (b < 1024) return `${b} B`;
  if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)} KB`;
  if (b < 1024 * 1024 * 1024) return `${(b / 1024 / 1024).toFixed(1)} MB`;
  return `${(b / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

export function TransferToasts() {
  const [list, setList] = useState<Transfer[]>(transferStore.list());
  useEffect(() => {
    // Snapshot a *new* array on each notify so React sees a referentially
    // different list and re-renders — the store mutates its internal array
    // immutably already, but we duplicate here for safety against future
    // store changes.
    return transferStore.subscribe(() => setList([...transferStore.list()]));
  }, []);

  if (!list.length) return null;

  const active = list.filter((t) => t.status === "active");
  const done = list.filter((t) => t.status === "done").length;
  const errored = list.filter((t) => t.status === "error").length;

  // Pick the dominant verb to show in the header (mixed-batch UX). If
  // most actives are uploads, say "uploading", etc. — simpler than a
  // per-kind count.
  const verbCounts: Record<string, number> = {};
  for (const t of active) {
    const verb = verbForKind(t.kind);
    verbCounts[verb] = (verbCounts[verb] ?? 0) + 1;
  }
  const dominantVerb = Object.entries(verbCounts).sort((a, b) => b[1] - a[1])[0]?.[0] ?? "working";

  const summary = active.length > 0
    ? `${active.length} ${dominantVerb}${done ? ` · ${done} done` : ""}${errored ? ` · ${errored} failed` : ""}`
    : `${done} done${errored ? ` · ${errored} failed` : ""}`;

  return (
    <div className="transfer-toasts" role="status" aria-live="polite">
      <div className="transfer-toast-header">
        <span>{summary}</span>
        <button
          className="transfer-toast-clear"
          onClick={() => transferStore.clearCompleted()}
          title="Dismiss completed transfers"
          aria-label="Dismiss completed"
        >
          ×
        </button>
      </div>
      <ul className="transfer-toast-list">
        {list.map((t) => {
          const pct = t.total && t.total > 0
            ? Math.min(100, Math.round((t.bytes / t.total) * 100))
            : null;
          return (
            <li key={t.id} className={`transfer-item transfer-${t.status}`}>
              <div className="transfer-row">
                <span className="transfer-icon" aria-hidden="true">
                  {t.status === "active" && iconForKind(t.kind)}
                  {t.status === "done" && "✓"}
                  {t.status === "error" && "✗"}
                </span>
                <span className="transfer-name" title={t.destDir ? `${t.name} → /workspace/${t.destDir}` : t.name}>
                  {t.name}
                </span>
                <span className="transfer-meta">
                  {t.status === "active" && pct !== null && `${pct}%`}
                  {t.status === "done" && t.total != null && formatBytes(t.total)}
                </span>
                <button
                  className="transfer-dismiss"
                  onClick={() => transferStore.dismiss(t.id)}
                  title="Dismiss"
                  aria-label="Dismiss"
                >
                  ×
                </button>
              </div>
              {t.status === "active" && (
                <div className="transfer-progress">
                  <div
                    className={`transfer-progress-bar ${pct === null ? "transfer-progress-indeterminate" : ""}`}
                    style={pct === null ? undefined : { width: `${pct}%` }}
                  />
                </div>
              )}
              {t.status === "active" && t.total != null && (
                <div className="transfer-progress-text">
                  {formatBytes(t.bytes)} / {formatBytes(t.total)}
                </div>
              )}
              {t.status === "error" && t.error && (
                <div className="transfer-error" title={t.error}>{t.error}</div>
              )}
              {t.destDir && (
                <div className="transfer-dest" title={`destination: /workspace/${t.destDir}`}>
                  → /workspace/{t.destDir}
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
