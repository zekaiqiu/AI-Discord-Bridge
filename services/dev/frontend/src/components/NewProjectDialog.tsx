// PyCharm-style "New Project" dialog. Replaces the previous window.prompt
// flow with a real form covering the knobs people actually want:
//
//   - Project name (required)
//   - Parent directory (defaults to /workspace root)
//   - Template — empty / Python script / Python module / Quant research /
//     FastAPI starter
//   - Environment — new venv (with optional base-interpreter pick) or
//     "use existing interpreter"
//   - Extra packages (comma-separated)
//   - Toggles: main file, README.md, .gitignore, git init
//
// On Create, POSTs the whole shape to /api/projects/new and bubbles the
// resulting `{path, slug, warnings, main_file}` back to the parent for
// the post-create actions (open the main file, surface warnings).

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  api,
  Interpreter,
  NewProjectBody,
  NewProjectResponse,
} from "../api";

type Props = {
  visible: boolean;
  onClose: () => void;
  onCreated: (resp: NewProjectResponse) => void;
};

const TEMPLATES: { key: NonNullable<NewProjectBody["template"]>; label: string; desc: string }[] = [
  { key: "script",  label: "Python script", desc: "Single main.py with a Hello World main()." },
  { key: "module",  label: "Python module", desc: "Package directory with __init__.py + __main__.py." },
  { key: "quant",   label: "Quant research", desc: "numpy + pandas + yfinance + matplotlib preinstalled." },
  { key: "fastapi", label: "FastAPI app",    desc: "FastAPI + uvicorn starter with a root endpoint." },
  { key: "empty",   label: "Empty",          desc: "Just the directory + pyproject.toml." },
];

export function NewProjectDialog({ visible, onClose, onCreated }: Props) {
  const [name, setName] = useState("");
  const [parent, setParent] = useState("");
  const [template, setTemplate] = useState<NonNullable<NewProjectBody["template"]>>("script");
  const [withVenv, setWithVenv] = useState(true);
  const [baseInterp, setBaseInterp] = useState<string>("");   // empty = let uv pick
  const [packages, setPackages] = useState("");
  const [initGit, setInitGit] = useState(true);
  const [createMain, setCreateMain] = useState(true);
  const [createReadme, setCreateReadme] = useState(true);
  const [createGitignore, setCreateGitignore] = useState(true);
  const [description, setDescription] = useState("");

  const [interpreters, setInterpreters] = useState<Interpreter[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  // Reset form on open. Load interpreters once we're visible.
  useEffect(() => {
    if (!visible) return;
    setErrorMsg(null);
    // Don't reset all fields — preserve the user's last template/toggle
    // choices across opens. Only clear name+parent so they always
    // intend the new project.
    setName("");
    setParent("");
    (async () => {
      try {
        const r = await api.interpreters();
        setInterpreters(r.interpreters);
      } catch { /* keep whatever we had */ }
    })();
  }, [visible]);

  // Esc closes; Enter submits (when name is non-empty and not in a textarea).
  useEffect(() => {
    if (!visible) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.preventDefault(); onClose(); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [visible, onClose]);

  const parsedPackages = useMemo(
    () => packages.split(",").map((p) => p.trim()).filter(Boolean),
    [packages],
  );

  const submit = useCallback(async () => {
    setErrorMsg(null);
    const cleanName = name.trim();
    if (!cleanName || cleanName.includes("/") || cleanName.startsWith(".")) {
      setErrorMsg("Project name: non-empty, no '/', no leading '.'");
      return;
    }
    setSubmitting(true);
    try {
      const resp = await api.newProject({
        name: cleanName,
        parent: parent.trim().replace(/^\/+/, "") || undefined,
        template,
        with_venv: withVenv,
        base_interpreter: withVenv && baseInterp ? baseInterp : null,
        packages: parsedPackages,
        init_git: initGit,
        create_main: createMain,
        create_readme: createReadme,
        create_gitignore: createGitignore,
        description,
      });
      onCreated(resp);
      onClose();
    } catch (err: any) {
      setErrorMsg(err?.message ?? String(err));
    } finally {
      setSubmitting(false);
    }
  }, [
    name, parent, template, withVenv, baseInterp, parsedPackages,
    initGit, createMain, createReadme, createGitignore, description,
    onCreated, onClose,
  ]);

  if (!visible) return null;

  return (
    <div className="npd-backdrop" onMouseDown={(e) => {
      // Click outside the dialog itself closes it.
      if (e.target === e.currentTarget) onClose();
    }}>
      <div className="npd-dialog" role="dialog" aria-modal="true" aria-label="New project">
        <header className="npd-header">
          <h2>New project</h2>
          <button className="btn btn-icon-mini" onClick={onClose} title="Cancel" aria-label="Cancel">×</button>
        </header>

        <div className="npd-body">
          <label className="npd-field">
            <span className="npd-label">Name</span>
            <input
              className="npd-input"
              autoFocus
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="my-project"
              onKeyDown={(e) => { if (e.key === "Enter" && !submitting) void submit(); }}
            />
          </label>

          <label className="npd-field">
            <span className="npd-label">Location</span>
            <input
              className="npd-input"
              value={parent}
              onChange={(e) => setParent(e.target.value)}
              placeholder="(workspace root)"
            />
            <span className="npd-hint">Parent dir, workspace-relative. Empty = /workspace.</span>
          </label>

          <label className="npd-field">
            <span className="npd-label">Description</span>
            <input
              className="npd-input"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="(optional)"
            />
          </label>

          <fieldset className="npd-fieldset">
            <legend className="npd-legend">Template</legend>
            <div className="npd-templates">
              {TEMPLATES.map((t) => (
                <label key={t.key} className={`npd-template${template === t.key ? " selected" : ""}`}>
                  <input
                    type="radio"
                    name="template"
                    value={t.key}
                    checked={template === t.key}
                    onChange={() => setTemplate(t.key)}
                  />
                  <span className="npd-template-label">{t.label}</span>
                  <span className="npd-template-desc">{t.desc}</span>
                </label>
              ))}
            </div>
          </fieldset>

          <fieldset className="npd-fieldset">
            <legend className="npd-legend">Environment</legend>
            <label className="npd-checkbox">
              <input
                type="checkbox"
                checked={withVenv}
                onChange={(e) => setWithVenv(e.target.checked)}
              />
              <span>Create a new virtual environment (<code>.venv/</code> via <code>uv venv</code>)</span>
            </label>
            {withVenv && (
              <label className="npd-field npd-indent">
                <span className="npd-label">Base interpreter</span>
                <select
                  className="npd-input"
                  value={baseInterp}
                  onChange={(e) => setBaseInterp(e.target.value)}
                >
                  <option value="">(let uv pick a default)</option>
                  {interpreters.filter((i) => i.kind !== "venv").map((i) => (
                    <option key={i.path} value={i.path}>{i.label}</option>
                  ))}
                </select>
              </label>
            )}
          </fieldset>

          <label className="npd-field">
            <span className="npd-label">Packages</span>
            <input
              className="npd-input"
              value={packages}
              onChange={(e) => setPackages(e.target.value)}
              placeholder="comma-separated; e.g. requests, pydantic"
            />
            <span className="npd-hint">
              Added on top of the template's defaults. Installed into the venv via <code>uv pip install</code>.
            </span>
          </label>

          <fieldset className="npd-fieldset">
            <legend className="npd-legend">Files &amp; tooling</legend>
            <label className="npd-checkbox">
              <input type="checkbox" checked={createMain} onChange={(e) => setCreateMain(e.target.checked)} />
              <span>Create template files (main entry point)</span>
            </label>
            <label className="npd-checkbox">
              <input type="checkbox" checked={createReadme} onChange={(e) => setCreateReadme(e.target.checked)} />
              <span>Create <code>README.md</code></span>
            </label>
            <label className="npd-checkbox">
              <input type="checkbox" checked={createGitignore} onChange={(e) => setCreateGitignore(e.target.checked)} />
              <span>Create Python <code>.gitignore</code></span>
            </label>
            <label className="npd-checkbox">
              <input type="checkbox" checked={initGit} onChange={(e) => setInitGit(e.target.checked)} />
              <span>Initialize git repo + initial commit</span>
            </label>
          </fieldset>

          {errorMsg && <div className="npd-error">{errorMsg}</div>}
        </div>

        <footer className="npd-footer">
          <button className="btn btn-secondary" onClick={onClose} disabled={submitting}>Cancel</button>
          <button className="btn btn-primary" onClick={() => void submit()} disabled={submitting || !name.trim()}>
            {submitting ? "Creating…" : "Create"}
          </button>
        </footer>
      </div>
    </div>
  );
}
