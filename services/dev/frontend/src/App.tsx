import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { loader } from "@monaco-editor/react";
import {
  DockviewApi,
  DockviewReact,
  DockviewReadyEvent,
} from "dockview-react";
import "dockview-react/dist/styles/dockview.css";

import { api, HttpError, IDEState, Me, streamJob, TabState } from "./api";
import { LspClient, LspStatus } from "./lsp";
import { OutputLine } from "./components/OutputPane";
import { MobileGate } from "./components/MobileGate";
import { TopBar } from "./components/TopBar";
import { NewProjectDialog } from "./components/NewProjectDialog";
import { TransferToasts } from "./components/TransferToasts";
import { DialogRoot, dialog } from "./dialogs";
import { InterpreterPicker } from "./components/InterpreterPicker";
import { EditorPanel } from "./panels/EditorPanel";
import { viewerKindFor } from "./components/Viewer";
import { TerminalPanel } from "./panels/TerminalPanel";
import { FilesPanel } from "./panels/FilesPanel";
import { OutputPanel } from "./panels/OutputPanel";
import { ProblemsPanel } from "./panels/ProblemsPanel";
import { ProcessesPanel } from "./panels/ProcessesPanel";
import { Tab, WorkspaceCtx, WorkspaceProvider } from "./workspace";

// dockview's React panel components are registered by component-key. We
// register one factory per panel type. Editor and Terminal panels are
// *parametric* — the same component renders for many panels, distinguished
// by `params` (path for editor, shellId for terminal).
const PANEL_COMPONENTS = {
  editor: EditorPanel,
  terminal: TerminalPanel,
  files: FilesPanel,
  output: OutputPanel,
  problems: ProblemsPanel,
  processes: ProcessesPanel,
};

const DEFAULT_STATE: IDEState = {
  version: 1,
  open_tabs: [],
  selected_interpreter: "/usr/local/bin/python3",
  theme: "dark",
};

// Stable panel ids for non-editor panels. Editor panel ids are
// `editor:<path>`; terminal ids are `terminal:<n>`.
const PANEL_ID = {
  files: "view:files",
  output: "view:output",
  problems: "view:problems",
  processes: "view:processes",
} as const;

function editorPanelId(path: string): string {
  return `editor:${path}`;
}

function safeDockToJSON(api: DockviewApi): unknown {
  try {
    return api.toJSON();
  } catch {
    return undefined;
  }
}

// Workspace selector. Reads in priority order: `?workspace=` query param
// > `chat_workspace` cookie > "personal". The chat → dev deep-link uses
// the query form; the cookie is written by the topbar toggle (or by the
// chat sidebar at the apex domain) so subsequent tabs inherit the last
// choice. Container, file-tree, editor tabs, terminal panels all re-bind
// on change — simplest correct fix is a full reload.
type Workspace = "personal" | "shared";

const WORKSPACE_COOKIE_NAME = "chat_workspace";

function readWorkspaceFromUrlOrCookie(): Workspace {
  if (typeof window === "undefined") return "personal";
  try {
    const q = new URLSearchParams(window.location.search).get("workspace");
    if (q === "shared" || q === "personal") return q;
  } catch { /* ignore */ }
  try {
    const m = document.cookie.match(new RegExp("(?:^|;\\s*)" + WORKSPACE_COOKIE_NAME + "=([^;]+)"));
    if (m) {
      const v = decodeURIComponent(m[1]);
      if (v === "shared" || v === "personal") return v;
    }
  } catch { /* ignore */ }
  return "personal";
}

function writeWorkspaceCookie(value: Workspace): void {
  try {
    const host = window.location.hostname;
    // Strip the leading "dev." so the cookie lands on the apex and is
    // visible to chat.<apex> / term.<apex> too. Two-part hostnames stay
    // as-is.
    const apex = host.startsWith("dev.") ? host.substring(4) : host;
    document.cookie = `${WORKSPACE_COOKIE_NAME}=${value}; Path=/; Domain=.${apex}; Max-Age=${60 * 60 * 24 * 365}; SameSite=Lax`;
    // One-shot migration: clear the old tenant-prefixed cookie so it
    // doesn't sit in DevTools forever. Safe to remove after a couple
    // of months of all users having loaded the new bundle.
    document.cookie = `wizerith_workspace=; Path=/; Domain=.${apex}; Max-Age=0; SameSite=Lax`;
  } catch { /* cookie-write failure is non-fatal — backend falls back to "personal" */ }
}


export default function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [bootError, setBootError] = useState<string | null>(null);
  // Reason for a missing /api/me identity: "anon" (401, no cookie) →
  // redirect to chat.ald3.com to log in; "forbidden" (403, email not on
  // allowlist) → show a permission-required screen with a Sign out action.
  // null means there's no auth-specific issue (general bootError flow).
  const [authStatus, setAuthStatus] = useState<"anon" | "forbidden" | null>(null);
  const [theme, setTheme] = useState<"dark" | "light">("dark");
  const [workspace, setWorkspaceState] = useState<Workspace>(() => readWorkspaceFromUrlOrCookie());
  // On mount: if we got the workspace from a `?workspace=...` query param,
  // promote it to the cookie and strip the query so reloads don't carry
  // it. The backend reads cookie or query, so this is just URL hygiene.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const q = new URLSearchParams(window.location.search).get("workspace");
    if (q === "shared" || q === "personal") {
      writeWorkspaceCookie(q);
      const url = new URL(window.location.href);
      url.searchParams.delete("workspace");
      window.history.replaceState({}, "", url.toString());
    }
  }, []);
  const onWorkspaceChange = useCallback((next: Workspace) => {
    if (next === workspace) return;
    writeWorkspaceCookie(next);
    // Hard reload — every panel (file tree, editor tabs, terminals, LSP)
    // is bound to the old container's state. Trying to swap them in place
    // would leak stale UI for marginal benefit; reload is reliable.
    setWorkspaceState(next);
    window.location.reload();
  }, [workspace]);
  const [tabs, setTabs] = useState<Tab[]>([]);
  const [activePath, setActivePath] = useState<string | null>(null);
  const [outputLines, setOutputLines] = useState<OutputLine[]>([]);
  const [currentJobId, setCurrentJobId] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [lspStatus, setLspStatus] = useState<LspStatus>("connecting");
  const [ideState, setIdeState] = useState<IDEState | null>(null);
  const [dockApi, setDockApi] = useState<DockviewApi | null>(null);
  // Bumped by dockview's onDidLayoutChange. The state-save effect picks
  // it up as a dep so resize/dock/tear-off events trigger a layout save.
  const [layoutTick, setLayoutTick] = useState(0);
  // Active project = workspace-relative directory containing a
  // pyproject.toml that's the nearest ancestor of activePath. Resolved
  // lazily via /api/projects + a longest-prefix match. User can override
  // explicitly via the topbar picker; that override sticks until they
  // change activePath to a file under a different project.
  const [activeProject, setActiveProject] = useState<string | null>(null);
  const [knownProjects, setKnownProjects] = useState<string[]>([]);
  // The interpreter to use when running. Either the active project's
  // .wizerith/project.json override, or the global selected_interpreter
  // from ide-state.json, or the system Python.
  const [projectInterpreter, setProjectInterpreter] = useState<string | null>(null);
  const [showNewProjectDialog, setShowNewProjectDialog] = useState(false);

  const lspRef = useRef<LspClient | null>(null);
  const lspOpenedPaths = useRef<Set<string>>(new Set());
  const lspChangeTimers = useRef<Map<string, number>>(new Map());
  const terminalCounter = useRef(0);
  // Tracks editor-panel ids currently in dockview so the reconcile effect
  // can diff tabs[] → dock panels.
  const editorPanelIds = useRef<Set<string>>(new Set());

  // -------------------------------------------------------------
  // Boot.
  // -------------------------------------------------------------
  useEffect(() => {
    let mounted = true;
    (async () => {
      try {
        const meResp = await api.me();
        if (!mounted) return;
        setMe(meResp);
        const { state } = await api.loadState();
        if (!mounted) return;
        const initialTheme = state.theme ?? "dark";
        setTheme(initialTheme);
        document.documentElement.dataset.theme = initialTheme;
        setIdeState(state);
        const reopened: Tab[] = [];
        for (const t of state.open_tabs ?? []) {
          if (viewerKindFor(t.path) !== "text") {
            // Binary viewer tab — no content prefetch; the viewer fetches the
            // raw bytes itself. Skip silently if /api/files/raw rejects later.
            reopened.push({ ...t, content: "", savedContent: "" });
            continue;
          }
          try {
            const r = await api.read(t.path);
            reopened.push({ ...t, content: r.content, savedContent: r.content });
          } catch {
            /* file gone since last session; skip */
          }
        }
        setTabs(reopened);
        const active = reopened.find((t) => t.active);
        setActivePath(active?.path ?? reopened[0]?.path ?? null);
      } catch (err: any) {
        if (err instanceof HttpError) {
          if (err.status === 401) {
            // No cookie at all — bounce to the tenant's dedicated sign-in
            // surface with a `next=` param so the user lands back here
            // after signing in. Both ald3 and wizerith now run the same
            // local-auth flow; pick the right `auth.` subdomain per host.
            const host = window.location.hostname;
            const onAld3 = host === "ald3.com" || host.endsWith(".ald3.com");
            const onWizerith = host === "wizerith.ai" || host.endsWith(".wizerith.ai");
            if (onAld3 || onWizerith) {
              const authHost = onAld3 ? "auth.ald3.com" : "auth.wizerith.ai";
              const next = encodeURIComponent(window.location.href);
              window.location.href = `${window.location.protocol}//${authHost}/?next=${next}`;
              return;
            }
            setAuthStatus("anon");
          } else if (err.status === 403) {
            setAuthStatus("forbidden");
          }
        }
        setBootError(err?.message ?? String(err));
      }
    })();
    return () => { mounted = false; };
  }, []);

  // -------------------------------------------------------------
  // LSP boot — unchanged from pre-dockview version.
  // -------------------------------------------------------------
  useEffect(() => {
    if (!me) return;
    let disposed = false;
    let client: LspClient | null = null;
    (async () => {
      const monaco = await loader.init();
      if (disposed) return;
      client = new LspClient(monaco);
      lspRef.current = client;
      client.onStatus((s) => { if (!disposed) setLspStatus(s); });
      try {
        await client.start();
      } catch (err) {
        // eslint-disable-next-line no-console
        console.error("LSP start failed:", err);
      }
    })();
    return () => {
      disposed = true;
      lspRef.current?.dispose();
      lspRef.current = null;
      lspChangeTimers.current.forEach((t) => window.clearTimeout(t));
      lspChangeTimers.current.clear();
    };
  }, [me]);

  // -------------------------------------------------------------
  // didOpen/didClose sync against pyright.
  // -------------------------------------------------------------
  useEffect(() => {
    if (lspStatus !== "ready" || !lspRef.current) return;
    const client = lspRef.current;
    const opened = lspOpenedPaths.current;
    const currentPaths = new Set(tabs.map((t) => t.path));
    for (const t of tabs) {
      if (viewerKindFor(t.path) !== "text") continue;  // pyright sees text only
      if (!opened.has(t.path)) {
        void client.didOpen(t.path, t.content);
        opened.add(t.path);
      }
    }
    for (const p of [...opened]) {
      if (!currentPaths.has(p)) {
        void client.didClose(p);
        opened.delete(p);
        const timer = lspChangeTimers.current.get(p);
        if (timer) {
          window.clearTimeout(timer);
          lspChangeTimers.current.delete(p);
        }
      }
    }
  }, [tabs, lspStatus]);

  // -------------------------------------------------------------
  // Persist IDE state (open_tabs, theme).
  // -------------------------------------------------------------
  const saveTimer = useRef<number | null>(null);
  useEffect(() => {
    if (!me) return;
    if (saveTimer.current) window.clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(() => {
      const open_tabs: TabState[] = tabs.map((t) => ({
        path: t.path,
        active: t.path === activePath,
        cursor_line: t.cursor_line,
        cursor_col: t.cursor_col,
      }));
      // Persist the layout only if dockview currently has panels.
      // An empty layout (user closed every panel) would otherwise be
      // saved and restored on next visit as a blank page — see the
      // self-heal check in the layout-apply effect below for the
      // matching guard. We refuse to write the bad state in the first
      // place so well-behaved clients (single tab open) don't need the
      // self-heal.
      let layoutPayload: unknown = undefined;
      if (dockApi && dockApi.panels.length > 0) {
        layoutPayload = safeDockToJSON(dockApi);
      }
      const state: IDEState = {
        version: 1,
        open_tabs,
        selected_interpreter: ideState?.selected_interpreter ?? "/usr/local/bin/python3",
        theme,
        layout: layoutPayload,
      };
      api.saveState(state).catch(() => { /* best-effort */ });
    }, 500);
    return () => {
      if (saveTimer.current) window.clearTimeout(saveTimer.current);
    };
    // layoutTick is intentionally a dep: dock reorganizations (resize,
    // dock, tear-off) bump it so the save effect re-fires and persists
    // the new dockview JSON.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tabs, activePath, theme, me, ideState?.selected_interpreter, dockApi, layoutTick]);

  // -------------------------------------------------------------
  // Theme toggle.
  // -------------------------------------------------------------
  const toggleTheme = useCallback(() => {
    setTheme((t) => {
      const next = t === "dark" ? "light" : "dark";
      document.documentElement.dataset.theme = next;
      return next;
    });
  }, []);

  // -------------------------------------------------------------
  // Tab ops.
  // -------------------------------------------------------------
  const openFile = useCallback(async (path: string) => {
    setTabs((prev) => {
      if (prev.find((t) => t.path === path)) return prev;
      return prev;  // changed below in the async branch
    });
    // Check current tabs synchronously via setter callback to avoid
    // a duplicate read race. If already open, just focus.
    let existing = false;
    setTabs((prev) => {
      existing = !!prev.find((t) => t.path === path);
      return prev;
    });
    if (existing) {
      setActivePath(path);
      return;
    }
    // Binary viewers (image/pdf/csv/xlsx/audio/video) don't go through Monaco.
    // Open the tab with empty content; the Viewer fetches /api/files/raw.
    if (viewerKindFor(path) !== "text") {
      setTabs((prev) => {
        if (prev.find((t) => t.path === path)) return prev;
        return [
          ...prev,
          { path, active: true, content: "", savedContent: "" },
        ];
      });
      setActivePath(path);
      return;
    }
    try {
      const r = await api.read(path);
      setTabs((prev) => {
        if (prev.find((t) => t.path === path)) return prev;
        return [
          ...prev,
          { path, active: true, content: r.content, savedContent: r.content },
        ];
      });
      setActivePath(path);
    } catch (err: any) {
      setOutputLines((prev) => [
        ...prev,
        { kind: "system", text: `failed to open ${path}: ${err?.message ?? err}`, ts: Date.now() / 1000 },
      ]);
    }
  }, []);

  const closeTab = useCallback((path: string) => {
    setTabs((prev) => {
      const filtered = prev.filter((t) => t.path !== path);
      if (path === activePath) {
        setActivePath(filtered[0]?.path ?? null);
      }
      return filtered;
    });
  }, [activePath]);

  const updateContent = useCallback((path: string, content: string) => {
    setTabs((prev) => prev.map((t) => (t.path === path ? { ...t, content } : t)));
    const existing = lspChangeTimers.current.get(path);
    if (existing) window.clearTimeout(existing);
    const timer = window.setTimeout(() => {
      lspChangeTimers.current.delete(path);
      if (lspRef.current && lspOpenedPaths.current.has(path)) {
        void lspRef.current.didChange(path, content);
      }
    }, 200);
    lspChangeTimers.current.set(path, timer);
  }, []);

  const saveActive = useCallback(async () => {
    const tab = tabs.find((t) => t.path === activePath);
    if (!tab) return;
    if (viewerKindFor(tab.path) !== "text") return;  // viewers are read-only
    try {
      await api.write(tab.path, tab.content);
      setTabs((prev) => prev.map((t) => (
        t.path === tab.path ? { ...t, savedContent: t.content } : t
      )));
    } catch (err: any) {
      setOutputLines((prev) => [
        ...prev,
        { kind: "system", text: `save failed: ${err?.message ?? err}`, ts: Date.now() / 1000 },
      ]);
    }
  }, [tabs, activePath]);

  // -------------------------------------------------------------
  // Run / kill.
  // -------------------------------------------------------------
  const runActive = useCallback(async () => {
    const tab = tabs.find((t) => t.path === activePath);
    if (!tab) return;
    if (tab.content !== tab.savedContent) {
      try {
        await api.write(tab.path, tab.content);
        setTabs((prev) => prev.map((t) => (
          t.path === tab.path ? { ...t, savedContent: t.content } : t
        )));
      } catch (err: any) {
        setOutputLines((prev) => [
          ...prev,
          { kind: "system", text: `pre-run save failed: ${err?.message ?? err}`, ts: Date.now() / 1000 },
        ]);
        return;
      }
    }
    setOutputLines([
      { kind: "system", text: `▶ running ${tab.path}…`, ts: Date.now() / 1000 },
    ]);
    setRunning(true);
    // Make sure the Output panel is visible so the user sees stream output.
    dockApi?.getPanel(PANEL_ID.output)?.api.setActive();
    try {
      // Resolve interpreter: project's .wizerith override → global
      // ide-state default → backend's own /usr/local/bin/python3.
      const interpreter = projectInterpreter
        || ideState?.selected_interpreter
        || undefined;
      const job = await api.run({ path: tab.path, interpreter });
      setCurrentJobId(job.job_id);
    } catch (err: any) {
      setOutputLines((prev) => [
        ...prev,
        { kind: "system", text: `run failed: ${err?.message ?? err}`, ts: Date.now() / 1000 },
      ]);
      setRunning(false);
    }
  }, [tabs, activePath, dockApi, projectInterpreter, ideState?.selected_interpreter]);

  // -------------------------------------------------------------
  // Active-project + interpreter resolution.
  //
  // We auto-derive the active project from the active file's path via a
  // longest-prefix match against the list of discovered projects. When
  // that changes, refresh the chosen interpreter from the project's
  // .wizerith/project.json (or fall back to the global ide-state default).
  // -------------------------------------------------------------
  useEffect(() => {
    if (!me) return;
    let cancelled = false;
    (async () => {
      try {
        const r = await api.projects();
        if (cancelled) return;
        setKnownProjects(r.projects.map((p) => p.path));
      } catch { /* best-effort */ }
    })();
    return () => { cancelled = true; };
  }, [me]);

  // Whenever activePath or the project list changes, recompute the
  // enclosing project (longest path prefix that matches a known
  // project). Empty-string project = workspace root with a
  // pyproject.toml at the root.
  useEffect(() => {
    if (!activePath) return;
    const candidates = knownProjects
      .filter((p) => p === "" || activePath.startsWith(p + "/") || activePath === p)
      .sort((a, b) => b.length - a.length);
    const next = candidates[0] ?? null;
    setActiveProject((prev) => (prev === next ? prev : next));
  }, [activePath, knownProjects]);

  // Load the active project's interpreter override whenever it changes.
  useEffect(() => {
    if (!me || activeProject === null) {
      setProjectInterpreter(null);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const r = await api.projectConfigGet(activeProject);
        if (cancelled) return;
        setProjectInterpreter(r.config.interpreter ?? null);
      } catch {
        if (!cancelled) setProjectInterpreter(null);
      }
    })();
    return () => { cancelled = true; };
  }, [me, activeProject]);

  // User picks a new interpreter via the topbar popover.
  const onInterpreterChange = useCallback(async (path: string) => {
    setProjectInterpreter(path);
    if (activeProject !== null) {
      try {
        await api.projectConfigPut(activeProject, { version: 1, interpreter: path });
      } catch (err) {
        // eslint-disable-next-line no-console
        console.warn("project config save failed", err);
      }
    } else {
      // No project — persist to the global ide-state instead.
      setIdeState((prev) => prev ? { ...prev, selected_interpreter: path } : prev);
    }
  }, [activeProject]);

  // -------------------------------------------------------------
  // New Project dialog wiring. FileTree's ⊕ button now dispatches
  // a window event instead of using window.prompt; we listen here.
  // -------------------------------------------------------------
  useEffect(() => {
    const onOpen = () => setShowNewProjectDialog(true);
    window.addEventListener("dev:open-new-project-dialog", onOpen);
    // The deep-link path (`?action=new-project`) fires `dev:new-project`
    // — also open the dialog so URL-driven and click-driven flows
    // agree.
    window.addEventListener("dev:new-project", onOpen);
    return () => {
      window.removeEventListener("dev:open-new-project-dialog", onOpen);
      window.removeEventListener("dev:new-project", onOpen);
    };
  }, []);

  // Context-menu "Run" in the file tree. Mirrors runActive's pipeline
  // (open the file as active tab, surface the output panel, dispatch
  // the job, set the running flag) but bypasses runActive's closure
  // over `tabs` / `activePath` — which would still hold the pre-open
  // snapshot because state updates are async and the freshly-added
  // tab hasn't been committed yet by the time the event handler runs.
  // We skip the pre-run save because the file was just opened from
  // disk, so there's no local edit to flush.
  useEffect(() => {
    const onRunPath = async (evt: Event) => {
      const path = (evt as CustomEvent<{ path?: string }>).detail?.path;
      if (!path) return;
      await openFile(path);
      setActivePath(path);
      setOutputLines([
        { kind: "system", text: `▶ running ${path}…`, ts: Date.now() / 1000 },
      ]);
      setRunning(true);
      dockApi?.getPanel(PANEL_ID.output)?.api.setActive();
      try {
        const interpreter = projectInterpreter
          || ideState?.selected_interpreter
          || undefined;
        const job = await api.run({ path, interpreter });
        setCurrentJobId(job.job_id);
      } catch (err: any) {
        setOutputLines((prev) => [
          ...prev,
          { kind: "system", text: `run failed: ${err?.message ?? err}`, ts: Date.now() / 1000 },
        ]);
        setRunning(false);
      }
    };
    window.addEventListener("dev:run-path", onRunPath as EventListener);
    return () => window.removeEventListener("dev:run-path", onRunPath as EventListener);
  }, [openFile, dockApi, projectInterpreter, ideState?.selected_interpreter]);

  const onProjectCreated = useCallback((resp: import("./api").NewProjectResponse) => {
    // Refresh the project list so the new project is auto-resolvable.
    void (async () => {
      try {
        const r = await api.projects();
        setKnownProjects(r.projects.map((p) => p.path));
      } catch { /* ignore */ }
    })();
    // If the template wrote a main file, open it.
    if (resp.main_file) {
      const target = resp.path
        ? `${resp.path}/${resp.main_file}`
        : resp.main_file;
      void openFile(target);
    }
    // Surface any warnings the backend reported.
    if (resp.warnings && resp.warnings.length) {
      setOutputLines((prev) => [
        ...prev,
        ...resp.warnings.map((w) => ({
          kind: "system",
          text: `new project: ${w}`,
          ts: Date.now() / 1000,
        })),
      ]);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openFile]);

  useEffect(() => {
    if (!currentJobId) return;
    const close = streamJob(currentJobId, {
      line: (kind, text, ts) => setOutputLines((prev) => [...prev, { kind, text, ts }]),
      status: (status, exitCode) => {
        setOutputLines((prev) => [
          ...prev,
          {
            kind: "system",
            text: status === "done" ? "✓ exit 0" : `${status} ${exitCode !== null ? `(exit ${exitCode})` : ""}`.trim(),
            ts: Date.now() / 1000,
          },
        ]);
        setRunning(false);
      },
      error: () => {
        setOutputLines((prev) => [
          ...prev,
          { kind: "system", text: "stream interrupted; retrying…", ts: Date.now() / 1000 },
        ]);
      },
    });
    return close;
  }, [currentJobId]);

  const killActive = useCallback(async () => {
    if (!currentJobId) return;
    try { await api.killJob(currentJobId); } catch { /* SSE will surface */ }
  }, [currentJobId]);

  // -------------------------------------------------------------
  // Keyboard shortcuts.
  // -------------------------------------------------------------
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const cmd = e.metaKey || e.ctrlKey;
      if (cmd && e.key === "s") {
        e.preventDefault();
        void saveActive();
      } else if (cmd && e.key === "Enter") {
        e.preventDefault();
        if (running) void killActive();
        else void runActive();
      } else if (cmd && (e.key === "`" || e.key === "j")) {
        // PyCharm uses Alt+F12 for terminal; we use Cmd/Ctrl+` and
        // Cmd/Ctrl+J (the VSCode binding) — both reach for new terminal.
        e.preventDefault();
        addTerminal();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [saveActive, runActive, killActive, running]);

  // -------------------------------------------------------------
  // Multi-terminal: add a new terminal panel to the bottom group.
  // -------------------------------------------------------------
  const addTerminal = useCallback(() => {
    if (!dockApi) return;
    const id = `terminal:${terminalCounter.current++}`;
    dockApi.addPanel({
      id,
      component: "terminal",
      title: `Terminal ${terminalCounter.current}`,
      params: { shellId: terminalCounter.current },
      position: { referencePanel: PANEL_ID.output, direction: "within" },
    });
    dockApi.getPanel(id)?.api.setActive();
  }, [dockApi]);

  // -------------------------------------------------------------
  // Dockview ready: build default layout.
  // -------------------------------------------------------------
  // Stable callback ref pattern so the dockview listener can call the
  // latest closeTab without re-subscribing each render.
  const closeTabRef = useRef(closeTab);
  useEffect(() => { closeTabRef.current = closeTab; }, [closeTab]);

  const onDockReady = useCallback((event: DockviewReadyEvent) => {
    const dock = event.api;
    setDockApi(dock);

    // Tab → tabs[] sync: when a panel is removed (user clicked the X
    // or the panel was disposed for any reason), if it's an editor:*
    // panel, drop the matching file from tabs[].
    dock.onDidRemovePanel((panel) => {
      const id = panel.id;
      if (id.startsWith("editor:")) {
        const path = id.substring("editor:".length);
        editorPanelIds.current.delete(id);
        closeTabRef.current(path);
      }
    });
    dock.onDidLayoutChange(() => setLayoutTick((t) => t + 1));
  }, []);

  // Default layout builder. Used on first-ever visit (no saved layout) or
  // as a fallback if a restored layout is malformed.
  //
  // Ordering matters: the FIRST panel goes into the root with no position
  // (dockview-react v4 requires an existing reference for any directional
  // placement). Everything else hangs off that anchor. Bug history: an
  // earlier draft put `terminal:0` first with `direction: "bottom"` and
  // no referencePanel — silently rendered an empty dock.
  const buildDefaultLayout = useCallback((dock: DockviewApi) => {
    // 1. Anchor: Files panel (no position → root).
    dock.addPanel({
      id: PANEL_ID.files,
      component: "files",
      title: "Files",
    });
    // 2. Bottom row anchored to the right of Files.
    const term0 = dock.addPanel({
      id: "terminal:0",
      component: "terminal",
      title: "Terminal 1",
      params: { shellId: 0 },
      position: { referencePanel: PANEL_ID.files, direction: "right" },
    });
    terminalCounter.current = 1;
    // 3. Stack Output / Problems / Processes within the Terminal group.
    dock.addPanel({
      id: PANEL_ID.output,
      component: "output",
      title: "Output",
      position: { referencePanel: term0.id, direction: "within" },
    });
    dock.addPanel({
      id: PANEL_ID.problems,
      component: "problems",
      title: "Problems",
      position: { referencePanel: term0.id, direction: "within" },
    });
    dock.addPanel({
      id: PANEL_ID.processes,
      component: "processes",
      title: "Processes",
      position: { referencePanel: term0.id, direction: "within" },
    });
    // Best-effort sizing: Files ~240 px wide.
    try {
      const filesGroup = dock.getPanel(PANEL_ID.files)?.group;
      if (filesGroup) filesGroup.api.setSize({ width: 240 });
    } catch { /* ignore */ }
  }, []);

  const resetLayout = useCallback(async () => {
    if (!dockApi) return;
    const ok = await dialog.confirm({
      title: "Reset panel layout?",
      message: "Your open files will stay open, but the panel docking arrangement will be reset to defaults.",
      confirmLabel: "Reset",
    });
    if (!ok) return;
    try { dockApi.clear(); } catch { /* ignore */ }
    editorPanelIds.current.clear();
    terminalCounter.current = 0;
    // Editor tabs reattach via the existing reconcile effect once
    // panels exist.
    buildDefaultLayout(dockApi);
  }, [dockApi, buildDefaultLayout]);

  // Apply layout once dockApi + ideState are both ready. fromJSON if a
  // saved layout exists; otherwise build the default. Guarded by a ref
  // so it runs exactly once per session.
  const layoutAppliedRef = useRef(false);
  useEffect(() => {
    if (layoutAppliedRef.current) return;
    if (!dockApi || ideState === null) return;
    layoutAppliedRef.current = true;
    const saved = ideState.layout;
    if (saved && typeof saved === "object") {
      try {
        dockApi.fromJSON(saved as never);
        // Self-heal: a previous session could have saved an empty layout
        // (user closed every panel via the X button → save effect ran →
        // empty `panels:{}` persisted) which would render the IDE as a
        // blank page on next visit. If the restored layout has no
        // panels at all, fall through to the default layout instead of
        // leaving the user stuck.
        if (dockApi.panels.length === 0) {
          try { dockApi.clear(); } catch { /* ignore */ }
          buildDefaultLayout(dockApi);
          return;
        }
        // Seed editorPanelIds.current from restored panels so the
        // tabs ↔ panel reconcile effect doesn't try to re-add them.
        for (const panel of dockApi.panels) {
          if (panel.id.startsWith("editor:")) {
            editorPanelIds.current.add(panel.id);
          }
          if (panel.id.startsWith("terminal:")) {
            // Bump counter past any restored terminal id so new
            // terminals get a fresh number.
            const n = parseInt(panel.id.substring("terminal:".length), 10);
            if (Number.isFinite(n) && n >= terminalCounter.current) {
              terminalCounter.current = n + 1;
            }
          }
        }
        return;
      } catch (err) {
        // eslint-disable-next-line no-console
        console.warn("layout restore failed, falling back to default", err);
        try { dockApi.clear(); } catch { /* ignore */ }
      }
    }
    buildDefaultLayout(dockApi);
  }, [dockApi, ideState, buildDefaultLayout]);

  // -------------------------------------------------------------
  // Deep-link from chat: ?file=<abspath> opens the file; ?panel=terminal
  // activates a Terminal panel on arrival. Runs once per session, after
  // the layout has been applied so the file tree, editor reconcile
  // effect, and Terminal panels are all in place to receive focus.
  //
  // The chat side ("Open in IDE" / "Open in Terminal" on artifact
  // previews) constructs these links with an absolute /workspace path —
  // see Artifact.tsx:workspacePathForArtifact. Anything else with no
  // leading slash is treated as relative to /workspace.
  // -------------------------------------------------------------
  const deepLinkAppliedRef = useRef(false);
  useEffect(() => {
    if (deepLinkAppliedRef.current) return;
    if (!dockApi || ideState === null) return;
    if (!layoutAppliedRef.current) return;
    deepLinkAppliedRef.current = true;
    const params = new URLSearchParams(window.location.search);
    const fileParam = params.get("file");
    const panelParam = params.get("panel");
    if (fileParam) {
      const target = fileParam.startsWith("/") ? fileParam : `/workspace/${fileParam}`;
      void openFile(target);
    }
    if (panelParam === "terminal") {
      const term = dockApi.panels.find((p) => p.id.startsWith("terminal:"));
      if (term) term.api.setActive();
      else addTerminal();
    }
    // Strip the query string so a refresh doesn't re-trigger and so the
    // address bar isn't cluttered. Keep the path + hash.
    if (fileParam || panelParam) {
      try {
        window.history.replaceState(null, "", window.location.pathname + window.location.hash);
      } catch { /* ignore */ }
    }
  }, [dockApi, ideState, openFile, addTerminal]);

  // -------------------------------------------------------------
  // Reconcile tabs[] → dockview editor panels.
  //
  // When tabs[] changes (open / close / order), we mirror the change into
  // the dockview panel registry. Editor panels read their content live
  // from the workspace context, so we don't need to push content here;
  // we just need to make sure each tab has a panel and panels for closed
  // tabs are removed.
  // -------------------------------------------------------------
  useEffect(() => {
    if (!dockApi) return;
    const wantedIds = new Set(tabs.map((t) => editorPanelId(t.path)));
    // Remove panels for closed tabs.
    for (const id of [...editorPanelIds.current]) {
      if (!wantedIds.has(id)) {
        try {
          dockApi.getPanel(id)?.api.close();
        } catch { /* may already be closed */ }
        editorPanelIds.current.delete(id);
      }
    }
    // Add panels for newly-opened tabs.
    for (const t of tabs) {
      const id = editorPanelId(t.path);
      if (editorPanelIds.current.has(id)) continue;
      // Place new editor panels in the center group. First-ever editor
      // panel: no reference → dockview creates a new group above the
      // bottom row. Subsequent ones: same group as the existing
      // editors.
      const existingEditor = [...editorPanelIds.current][0];
      const positionArgs = existingEditor
        ? { referencePanel: existingEditor, direction: "within" as const }
        : { referencePanel: PANEL_ID.files, direction: "right" as const };
      dockApi.addPanel({
        id,
        component: "editor",
        title: t.path.split("/").pop() ?? t.path,
        params: { path: t.path },
        position: positionArgs,
      });
      editorPanelIds.current.add(id);
    }
  }, [tabs, dockApi]);

  // -------------------------------------------------------------
  // Focus the active editor panel when activePath changes.
  // -------------------------------------------------------------
  useEffect(() => {
    if (!dockApi || !activePath) return;
    const id = editorPanelId(activePath);
    dockApi.getPanel(id)?.api.setActive();
  }, [activePath, dockApi]);

  // -------------------------------------------------------------
  // Workspace context value.
  // -------------------------------------------------------------
  const wsValue = useMemo<WorkspaceCtx | null>(() => {
    if (!me) return null;
    return {
      me,
      theme,
      tabs,
      activePath,
      openFile,
      closeTab,
      updateContent,
      saveActive,
      setActivePath,
      outputLines,
      running,
      currentJobId,
      runActive,
      killActive,
      clearOutput: () => setOutputLines([]),
      lspStatus,
      lspRef,
      ideState,
      setIdeState,
    };
  }, [
    me, theme, tabs, activePath, outputLines, running, currentJobId,
    lspStatus, ideState,
    openFile, closeTab, updateContent, saveActive, runActive, killActive,
  ]);

  // -------------------------------------------------------------
  // Render.
  // -------------------------------------------------------------
  const activeTab = useMemo(
    () => tabs.find((t) => t.path === activePath) ?? null,
    [tabs, activePath],
  );
  const dirty = activeTab ? activeTab.content !== activeTab.savedContent : false;

  if (bootError) {
    const isAuth =
      authStatus !== null ||
      /jwt|unauthor|401|403|not authenticated|email not authorized/i.test(bootError);
    const devHost = typeof window !== "undefined" ? window.location.host : "dev";
    // dev.ald3.com → auth.ald3.com; dev.wizerith.ai → auth.wizerith.ai.
    // Both tenants now have dedicated local-auth sign-in surfaces.
    const sibling = devHost.startsWith("dev.") ? devHost.slice(4) : devHost;
    const chatHost =
      sibling === "ald3.com" ? "auth.ald3.com"
      : sibling === "wizerith.ai" ? "auth.wizerith.ai"
      : sibling;
    const chatUrl = typeof window !== "undefined"
      ? `${window.location.protocol}//${chatHost}/`
      : "/";
    const isForbidden = authStatus === "forbidden";
    return (
      <div className="boot-error">
        <h1>{devHost}</h1>
        {isAuth && isForbidden ? (
          <>
            <p>Permission required.</p>
            <p className="hint">
              You&rsquo;re signed in, but this email isn&rsquo;t authorized to use {devHost}.
              Ask the admin to grant access, or sign out and try a different account.
            </p>
            <p className="boot-actions">
              <button
                className="btn btn-primary"
                onClick={async () => {
                  try {
                    await fetch("/api/auth/logout", { method: "POST", credentials: "same-origin" });
                  } catch { /* ignore */ }
                  window.location.href = chatUrl;
                }}
              >
                Sign out
              </button>
              <button className="btn btn-secondary" onClick={() => window.location.reload()}>Retry</button>
            </p>
          </>
        ) : isAuth ? (
          <>
            <p>You're not signed in.</p>
            <p className="hint">
              {devHost} requires a valid session. Sign in to {chatHost} and the
              auth cookie will carry over to this hostname automatically.
            </p>
            <p className="boot-actions">
              <a className="btn btn-primary" href={chatUrl}>Sign in via {chatHost}</a>
              <button className="btn btn-secondary" onClick={() => window.location.reload()}>Retry</button>
            </p>
            <p className="boot-detail">backend says: {bootError}</p>
          </>
        ) : (
          <>
            <p>{bootError}</p>
            <p className="hint">If this looks transient, retry. If it persists, ping the operator.</p>
            <p className="boot-actions">
              <button className="btn btn-secondary" onClick={() => window.location.reload()}>Retry</button>
            </p>
          </>
        )}
      </div>
    );
  }
  if (!me || !wsValue) {
    return <div className="boot-loading"><span>loading…</span></div>;
  }
  void DEFAULT_STATE;

  return (
    <>
      <MobileGate />
      <div className="app desktop-only">
        <TopBar
          email={me.email}
          theme={theme}
          onToggleTheme={toggleTheme}
          activePath={activePath}
          dirty={dirty}
          running={running}
          lspStatus={lspStatus}
          workspace={workspace}
          onWorkspaceChange={onWorkspaceChange}
          onSave={() => void saveActive()}
          onRun={() => void runActive()}
          onKill={() => void killActive()}
          onNewTerminal={addTerminal}
          onResetLayout={resetLayout}
        >
          <InterpreterPicker
            activeProject={activeProject}
            resolvedInterpreter={
              projectInterpreter
              || ideState?.selected_interpreter
              || "/usr/local/bin/python3"
            }
            onInterpreterChange={onInterpreterChange}
            onActiveProjectChange={setActiveProject}
            onCreateVenv={() => setShowNewProjectDialog(true)}
          />
        </TopBar>
        <WorkspaceProvider value={wsValue}>
          <div className="dock-host">
            <DockviewReact
              components={PANEL_COMPONENTS}
              onReady={onDockReady}
              className={theme === "dark" ? "dockview-theme-abyss" : "dockview-theme-light"}
            />
          </div>
        </WorkspaceProvider>
        <NewProjectDialog
          visible={showNewProjectDialog}
          onClose={() => setShowNewProjectDialog(false)}
          onCreated={onProjectCreated}
        />
        <TransferToasts />
        <DialogRoot />
      </div>
    </>
  );
}
