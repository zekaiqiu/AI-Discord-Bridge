// Topbar interpreter + active-project picker.
//
// Shows the currently-resolved interpreter as a button; clicking opens a
// popover with two combos:
//   - Project (auto-resolved from the active file's nearest pyproject.toml
//     ancestor; the user can override here)
//   - Interpreter (picked from the discovered list; saves to
//     .wizerith/project.json of the active project)
//
// The resolution rule (read at runActive time):
//   1. If we have an active project AND its .wizerith/project.json has
//      a non-null `interpreter`, use that.
//   2. Otherwise fall back to the global `selected_interpreter` from
//      ide-state.json.
//   3. If both null, /usr/local/bin/python3.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, Interpreter, ProjectSummary } from "../api";

const FALLBACK_INTERPRETER = "/usr/local/bin/python3";

type Props = {
  // The current "active project" — workspace-relative path of a directory
  // containing a pyproject.toml. Auto-derived from active file path by
  // findEnclosingProject() in App.tsx. Empty string means workspace root.
  activeProject: string | null;
  // The resolved interpreter (project override or global fallback).
  resolvedInterpreter: string;
  // Called when the user picks a new interpreter. App.tsx persists it to
  // the active project's config (or the global state if no active project).
  onInterpreterChange: (path: string) => void;
  // Called when the user picks a different project explicitly.
  onActiveProjectChange: (proj: string | null) => void;
  // Trigger to "+ New venv here…": open the New Project flow with a
  // venv-create checkbox prefilled to the chosen project's path. For
  // now this is just a callback the parent can wire to the New Project
  // dialog (passed-through to FileTree's existing modal).
  onCreateVenv?: () => void;
};

function shortLabel(version: string, path: string): string {
  // Compact label for the topbar button itself. Full label lives in the
  // dropdown rows.
  const ver = version || "?";
  if (path.includes("/.venv/")) {
    // /workspace/foo/.venv/bin/python → foo (3.12.3)
    const m = path.match(/^\/workspace\/(.+)\/\.venv\/bin\/python$/);
    if (m) return `${m[1].split("/").pop()} venv · ${ver}`;
  }
  if (path.startsWith("/home/linuxbrew/")) {
    return `brew · ${ver}`;
  }
  return `sys · ${ver}`;
}

export function InterpreterPicker({
  activeProject,
  resolvedInterpreter,
  onInterpreterChange,
  onActiveProjectChange,
  onCreateVenv,
}: Props) {
  const [open, setOpen] = useState(false);
  const [interpreters, setInterpreters] = useState<Interpreter[]>([]);
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const popoverRef = useRef<HTMLDivElement>(null);

  // Lazy-load on first open — interpreter discovery shells `find` and
  // `python --version` for every hit, which is a few seconds in the
  // worst case. Cache the result for the session; user can refresh
  // explicitly via the ↻ button.
  const loaded = useRef(false);
  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [iResp, pResp] = await Promise.all([api.interpreters(), api.projects()]);
      setInterpreters(iResp.interpreters);
      setProjects(pResp.projects);
      loaded.current = true;
    } catch {
      // Best-effort — leave whatever we had.
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (open && !loaded.current) void refresh();
  }, [open, refresh]);

  // Close on outside click.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!popoverRef.current) return;
      if (!popoverRef.current.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener("mousedown", onDown);
    return () => window.removeEventListener("mousedown", onDown);
  }, [open]);

  // Pick a label for the currently-resolved interpreter. If we know the
  // full Interpreter entry use that; otherwise format heuristically.
  const buttonLabel = useMemo(() => {
    const hit = interpreters.find((i) => i.path === resolvedInterpreter);
    if (hit) return shortLabel(hit.version, hit.path);
    return shortLabel("", resolvedInterpreter || FALLBACK_INTERPRETER);
  }, [interpreters, resolvedInterpreter]);

  return (
    <div className="interp-picker">
      <button
        className="interp-button"
        onClick={() => setOpen((v) => !v)}
        title={`Active interpreter: ${resolvedInterpreter || FALLBACK_INTERPRETER}\nClick to change`}
      >
        <span className="interp-button-icon" aria-hidden="true">⌥</span>
        <span className="interp-button-label">{buttonLabel}</span>
        <span className="interp-button-chev" aria-hidden="true">▾</span>
      </button>
      {open && (
        <div className="interp-popover" ref={popoverRef} role="dialog" aria-label="Interpreter & project">
          <div className="interp-popover-header">
            <span>Project &amp; Interpreter</span>
            <button className="btn btn-icon-mini" onClick={refresh} title="Refresh discovery" disabled={loading}>
              {loading ? "…" : "↻"}
            </button>
          </div>

          <div className="interp-popover-row">
            <label className="interp-popover-label">Active project</label>
            <select
              className="interp-popover-select"
              value={activeProject ?? ""}
              onChange={(e) => onActiveProjectChange(e.target.value || null)}
            >
              <option value="">(no project · workspace root)</option>
              {projects.map((p) => (
                <option key={p.path || "(root)"} value={p.path}>
                  {p.name}
                  {p.path ? ` · ${p.path}` : ""}
                  {p.has_venv ? " · venv" : ""}
                </option>
              ))}
            </select>
          </div>

          <div className="interp-popover-row">
            <label className="interp-popover-label">Interpreter</label>
            <select
              className="interp-popover-select"
              value={resolvedInterpreter}
              onChange={(e) => onInterpreterChange(e.target.value)}
            >
              {/* Always include the resolved interpreter even if it's
                  not in the discovered list (e.g. it was set by hand). */}
              {interpreters.find((i) => i.path === resolvedInterpreter) ? null : (
                <option value={resolvedInterpreter}>{resolvedInterpreter}</option>
              )}
              {interpreters.map((i) => (
                <option key={i.path} value={i.path}>
                  {i.label}
                </option>
              ))}
              {interpreters.length === 0 && !loading && (
                <option value="" disabled>(no interpreters discovered)</option>
              )}
            </select>
          </div>

          {onCreateVenv && (
            <div className="interp-popover-row">
              <button
                className="btn btn-secondary btn-sm interp-create-venv"
                onClick={() => { setOpen(false); onCreateVenv(); }}
              >
                + New venv via New Project…
              </button>
            </div>
          )}

          <div className="interp-popover-hint">
            Project picks override the global default. Stored in
            <code>.wizerith/project.json</code>.
          </div>
        </div>
      )}
    </div>
  );
}
