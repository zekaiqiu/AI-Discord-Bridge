import { useCallback, useEffect, useRef, useState } from "react";
import { api, FileEntry, SearchHit, streamCompress } from "../api";
import { transferStore } from "../transfers";
import { ContextMenu, ContextMenuItem, ContextMenuState } from "./ContextMenu";
import { viewerKindFor } from "./Viewer";
import { dialog } from "../dialogs";

type Props = {
  onOpen: (path: string) => void;
  /**
   * Fired when the user expands or focuses a directory. Optional. App-level
   * code may use this to update breadcrumb state.
   */
  onDirChange?: (path: string) => void;
};

type Node = {
  entry: FileEntry;
  path: string;
  children?: Node[] | "loading" | "error";
  open?: boolean;
};

function joinPath(parent: string, name: string): string {
  return parent ? `${parent}/${name}` : name;
}

async function loadDir(path: string): Promise<Node[]> {
  const { entries } = await api.list(path);
  const sorted = [...entries].sort((a, b) => {
    if (a.kind !== b.kind) {
      if (a.kind === "dir") return -1;
      if (b.kind === "dir") return 1;
    }
    return a.name.localeCompare(b.name);
  });
  return sorted.map((entry) => ({ entry, path: joinPath(path, entry.name) }));
}

const SEARCH_DEBOUNCE_MS = 250;

// MIME used for the drag payload. Native HTML5 DnD requires a string
// MIME — we serialize {path, kind} so a drop target can refuse e.g. a
// directory dropped into itself without an extra round-trip.
const DND_MIME = "application/x-dev-wizerith-path";

function pathDirname(p: string): string {
  if (!p) return "";
  const i = p.lastIndexOf("/");
  return i >= 0 ? p.substring(0, i) : "";
}
function pathBasename(p: string): string {
  const i = p.lastIndexOf("/");
  return i >= 0 ? p.substring(i + 1) : p;
}
function isDescendantOrSelf(parentPath: string, candidate: string): boolean {
  if (parentPath === candidate) return true;
  return candidate.startsWith(parentPath + "/");
}

export function FileTree({ onOpen, onDirChange }: Props) {
  const [nodes, setNodes] = useState<Node[]>([]);
  // Mirror of `nodes` in a ref so callbacks (toggle, drag handlers) can
  // read the current tree synchronously without depending on stale
  // closures. React 18's setState updater fires at commit time, so
  // side-effects in the updater (e.g. `let nextLoad; setNodes(prev => {
  // nextLoad = ...; ... })` then `if (!nextLoad) ...`) don't work — the
  // closure variable is read before the updater runs. The ref pattern
  // makes the read deterministic.
  const nodesRef = useRef<Node[]>([]);
  useEffect(() => { nodesRef.current = nodes; }, [nodes]);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [searching, setSearching] = useState(false);
  const [results, setResults] = useState<SearchHit[] | null>(null);
  const [resultsError, setResultsError] = useState<string | null>(null);
  // Drag-and-drop state. `draggedPath` is set during an active drag;
  // `dropTarget` is the path of the dir currently being hovered (or ""
  // for the workspace root). Both reset on dragend / drop.
  const [draggedPath, setDraggedPath] = useState<string | null>(null);
  const [draggedKind, setDraggedKind] = useState<"file" | "dir" | null>(null);
  const [dropTarget, setDropTarget] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setError(null);
    try {
      setNodes(await loadDir(""));
    } catch (err: any) {
      setError(err?.message ?? String(err));
    }
  }, []);

  // Re-fetch a directory listing and splice the result into the tree while
  // *preserving* the open/children state of every subtree that wasn't
  // touched. The hard `refresh()` above is reserved for the toolbar's
  // explicit Reset button — every mutating action (new file, mkdir, rename,
  // upload-to-root) should go through here, otherwise every operation
  // collapses the user's whole tree.
  //
  // `path === ""` reloads root; non-empty paths reload one subtree.
  // `expand: true` force-opens the reloaded dir (used after dropping a file
  // into a folder — the user expects to see it land).
  const reloadSubtree = useCallback(async (path: string, opts?: { expand?: boolean }) => {
    setError(null);
    let fresh: Node[];
    try {
      fresh = await loadDir(path);
    } catch (err: any) {
      setError(err?.message ?? String(err));
      return;
    }
    const mergeWithPrev = (
      newEntries: Node[],
      prevSiblings: Node[] | "loading" | "error" | undefined,
    ): Node[] => {
      const oldByPath = new Map<string, Node>();
      if (Array.isArray(prevSiblings)) {
        for (const c of prevSiblings) oldByPath.set(c.path, c);
      }
      return newEntries.map((newKid) => {
        const existing = oldByPath.get(newKid.path);
        // Only carry over expansion state for dirs whose kind is unchanged
        // — a path swap (rare) shouldn't smuggle stale children across.
        if (existing && existing.entry.kind === "dir" && newKid.entry.kind === "dir") {
          return { ...newKid, open: existing.open, children: existing.children };
        }
        return newKid;
      });
    };
    setNodes((prev) => {
      if (!path) {
        return mergeWithPrev(fresh, prev);
      }
      return mutate(prev, path, (n) => ({
        ...n,
        open: opts?.expand ? true : n.open,
        children: mergeWithPrev(fresh, Array.isArray(n.children) ? n.children : undefined),
      }));
    });
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  // Deep-link bridge: App.tsx fires `dev:new-project` when arriving with
  // `?action=new-project` so the toolbar's New Project prompt runs without
  // the user having to click the icon. Subscribe here rather than in App
  // so the FileTree owns the create/refresh flow end-to-end.
  useEffect(() => {
    const onAsk = () => { void onNewProjectLatest.current?.(); };
    window.addEventListener("dev:new-project", onAsk);
    return () => window.removeEventListener("dev:new-project", onAsk);
  }, []);
  const onNewProjectLatest = useRef<(() => Promise<void>) | null>(null);

  // -------- Tree expansion (lazy-load on open) ----------------------

  const toggle = useCallback(async (path: string) => {
    // Decide based on the current tree from the ref (synchronous read,
    // no dependence on updater timing). Three cases:
    //   1. Node is currently open       → close it
    //   2. Node is closed but loaded   → just re-open (no fetch)
    //   3. Node is closed and unloaded → mark loading + fetch
    const node = findNode(nodesRef.current, path);
    if (!node) return;
    if (node.open) {
      setNodes((prev) => mutate(prev, path, (n) => ({ ...n, open: false })));
      return;
    }
    if (Array.isArray(node.children) || node.children === "loading") {
      setNodes((prev) => mutate(prev, path, (n) => ({ ...n, open: true })));
      return;
    }
    setNodes((prev) => mutate(prev, path, (n) => (
      { ...n, open: true, children: "loading" as const }
    )));
    try {
      const kids = await loadDir(path);
      setNodes((prev) => mutate(prev, path, (n) => ({ ...n, children: kids })));
      onDirChange?.(path);
    } catch {
      setNodes((prev) => mutate(prev, path, (n) => ({ ...n, children: "error" as const })));
    }
  }, [onDirChange]);

  // -------- Action buttons ------------------------------------------

  const onNewFile = useCallback(async () => {
    const name = await dialog.prompt({
      title: "New file",
      message: "Create in /workspace.",
      placeholder: "example.py",
      validate: (v) => {
        if (!v.trim()) return "Name is required.";
        if (v.includes("/")) return "Name can't contain '/'.";
        return null;
      },
    });
    if (!name) return;
    try {
      await api.write(name, "");
      await reloadSubtree("");
      onOpen(name);
    } catch (err: any) {
      void dialog.alert({ title: "Create failed", message: err?.message ?? String(err), variant: "error" });
    }
  }, [reloadSubtree, onOpen]);

  const onNewFolder = useCallback(async () => {
    const name = await dialog.prompt({
      title: "New folder",
      message: "Create in /workspace.",
      placeholder: "src",
      validate: (v) => {
        if (!v.trim()) return "Name is required.";
        if (v.includes("/")) return "Name can't contain '/'.";
        return null;
      },
    });
    if (!name) return;
    try {
      await api.mkdir(name);
      await reloadSubtree("");
    } catch (err: any) {
      void dialog.alert({ title: "Create folder failed", message: err?.message ?? String(err), variant: "error" });
    }
  }, [reloadSubtree]);

  const onNewProject = useCallback(async () => {
    // Open the full New Project dialog rather than the legacy
    // prompt-then-confirm flow. App.tsx owns the dialog state; we just
    // ask it to open via a window event so we don't have to thread a
    // prop through here. After the dialog finishes it triggers the
    // tree refresh + main-file open by itself.
    window.dispatchEvent(new Event("dev:open-new-project-dialog"));
  }, []);
  // Keep the latest onNewProject in a ref so the window-event listener
  // (registered once on mount) always calls the freshest closure.
  useEffect(() => { onNewProjectLatest.current = onNewProject; }, [onNewProject]);

  const onOpenProject = useCallback(async () => {
    // VS Code-style: just refresh + scroll the tree to top; the user
    // expands the project folder they want. (Future: dedicated modal
    // listing detected projects via .wizerith/project.json markers.)
    await refresh();
  }, [refresh]);

  // Upload a list of local files into `destDir` (workspace-relative; "" = root).
  // Used by both the toolbar Upload button and OS drag-and-drop onto folder
  // rows / the root pane. Each file gets its own entry in the bottom-right
  // TransferToasts panel so the user can see what's in flight, its progress,
  // and (for failures) why it failed — replaces the old end-of-batch
  // `alert()` which made repeat-drop spam easy.
  const uploadFilesTo = useCallback(async (destDir: string, files: FileList | File[]) => {
    const arr = Array.from(files);
    if (!arr.length) return;
    for (const f of arr) {
      const tid = transferStore.start({
        name: f.name,
        total: f.size,
        kind: "upload",
        destDir,
      });
      try {
        await api.upload(destDir, f, {
          onProgress: (bytes) => transferStore.progress(tid, bytes),
        });
        transferStore.finish(tid);
      } catch (err: any) {
        transferStore.finish(tid, err?.message ?? String(err));
      }
    }
    // Refresh so the new files are visible without collapsing the rest of
    // the tree. Root drops re-load the top level; subdir drops re-load
    // that subtree and force-expand it (the user expects to see the
    // landed file).
    await reloadSubtree(destDir, { expand: destDir !== "" });
  }, [reloadSubtree]);

  const fileInputRef = useRef<HTMLInputElement>(null);
  const onUpload = useCallback(() => { fileInputRef.current?.click(); }, []);
  const onUploadChange = useCallback(async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (!files || !files.length) return;
    await uploadFilesTo("", files);
    e.target.value = "";
  }, [uploadFilesTo]);

  // -------- Context menu --------------------------------------------
  //
  // Right-click on any tree row (file or dir) or on the root pane opens
  // a PyCharm-style menu. Actions reuse the existing handlers above
  // where possible (open / new file / new folder / upload); the rest
  // (download, duplicate, rename, delete, run, copy path) are wired
  // below. Reveal-in-terminal is intentionally out of scope until the
  // Terminal panel exposes a `cd <path>` hook.

  const [menu, setMenu] = useState<ContextMenuState | null>(null);
  const closeMenu = useCallback(() => setMenu(null), []);

  // Unique-destination resolver for Duplicate. Tries "foo copy.py",
  // then "foo copy 2.py", etc., walking the same dir's listing.
  const uniqueDuplicatePath = useCallback(async (src: string): Promise<string> => {
    const parent = pathDirname(src);
    const base = pathBasename(src);
    const dot = base.lastIndexOf(".");
    const stem = dot > 0 ? base.substring(0, dot) : base;
    const ext = dot > 0 ? base.substring(dot) : "";
    const siblings = new Set<string>();
    try {
      const { entries } = await api.list(parent);
      for (const e of entries) siblings.add(e.name);
    } catch { /* fall through; backend will reject on collision */ }
    let candidate = `${stem} copy${ext}`;
    let i = 2;
    while (siblings.has(candidate)) {
      candidate = `${stem} copy ${i}${ext}`;
      i++;
    }
    return parent ? `${parent}/${candidate}` : candidate;
  }, []);

  const onDuplicate = useCallback(async (src: string) => {
    try {
      const dst = await uniqueDuplicatePath(src);
      await api.copy(src, dst);
      await reloadSubtree(pathDirname(src), { expand: true });
    } catch (err: any) {
      void dialog.alert({ title: "Duplicate failed", message: err?.message ?? String(err), variant: "error" });
    }
  }, [uniqueDuplicatePath, reloadSubtree]);

  const onRename = useCallback(async (src: string) => {
    const cur = pathBasename(src);
    const next = await dialog.prompt({
      title: "Rename",
      message: `Rename "${cur}" to:`,
      defaultValue: cur,
      confirmLabel: "Rename",
      validate: (v) => {
        if (!v.trim()) return "Name is required.";
        if (v.includes("/")) return "Name can't contain '/'.";
        return null;
      },
    });
    if (!next || next === cur) return;
    const parent = pathDirname(src);
    const dst = parent ? `${parent}/${next}` : next;
    try {
      await api.rename(src, dst);
      await reloadSubtree(parent);
    } catch (err: any) {
      void dialog.alert({ title: "Rename failed", message: err?.message ?? String(err), variant: "error" });
    }
  }, [reloadSubtree]);

  const onDelete = useCallback(async (path: string, kind: "file" | "dir") => {
    const base = pathBasename(path);
    const message = kind === "dir"
      ? `Delete folder "${base}" and everything inside it? This can't be undone.`
      : `Delete file "${base}"? This can't be undone.`;
    const ok = await dialog.confirm({
      title: `Delete ${kind === "dir" ? "folder" : "file"}?`,
      message,
      confirmLabel: "Delete",
      danger: true,
    });
    if (!ok) return;
    try {
      await api.remove(path);
      await reloadSubtree(pathDirname(path));
    } catch (err: any) {
      void dialog.alert({ title: "Delete failed", message: err?.message ?? String(err), variant: "error" });
    }
  }, [reloadSubtree]);

  const onDownload = useCallback(async (path: string) => {
    // XHR-based download so we can show a progress toast — `<a download>`
    // would push the file to the browser's native download manager which
    // is fine but invisible if the user has scrolled away from the toolbar.
    // Capped by the backend's MAX_UPLOAD_BYTES (100 MB) anyway, so loading
    // into a Blob is bounded.
    const name = pathBasename(path);
    const tid = transferStore.start({
      name,
      total: null,
      kind: "download",
      destDir: pathDirname(path),
    });
    try {
      const { blob, filename } = await api.downloadBlob(path, {
        onProgress: (bytes, total) => {
          if (total > 0) transferStore.setTotal(tid, total);
          transferStore.progress(tid, bytes);
        },
      });
      transferStore.finish(tid);
      // Browser-trigger the save now that we have the bytes.
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      // Revoke after a tick — the click has already queued the save.
      setTimeout(() => URL.revokeObjectURL(url), 5_000);
    } catch (err: any) {
      transferStore.finish(tid, err?.message ?? String(err));
    }
  }, []);

  const onCopyPath = useCallback(async (path: string) => {
    const abs = `/workspace/${path}`.replace(/\/+$/, "");
    try { await navigator.clipboard.writeText(abs); }
    catch {
      // Fallback for non-secure contexts: select-and-copy via a transient textarea.
      const ta = document.createElement("textarea");
      ta.value = abs;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); } catch { /* best-effort */ }
      ta.remove();
    }
  }, []);

  const onCompress = useCallback(async (path: string) => {
    const base = pathBasename(path);
    // Toast starts in indeterminate state (total: null). Once the
    // server's `scanned` event arrives we'll set the real total and the
    // progress bar switches from striped/animated to a filling bar.
    const tid = transferStore.start({
      name: `${base}.zip`,
      total: null,
      kind: "compress",
      destDir: pathDirname(path),
    });
    let id: string;
    try {
      const start = await api.compressStart(path);
      id = start.id;
    } catch (err: any) {
      transferStore.finish(tid, err?.message ?? String(err));
      void dialog.alert({
        title: "Compress failed",
        message: err?.message ?? String(err),
        variant: "error",
      });
      return;
    }
    // Subscribe to the SSE stream. The unsubscribe fn is fired implicitly
    // on `done` / `error` (streamCompress closes the EventSource then).
    streamCompress(id, {
      scanned: (totalBytes, _totalFiles) => {
        if (totalBytes > 0) transferStore.setTotal(tid, totalBytes);
      },
      progress: (bytesDone, _filesDone) => {
        transferStore.progress(tid, bytesDone);
      },
      done: (dst, _size) => {
        transferStore.finish(tid);
        const parent = pathDirname(dst);
        void reloadSubtree(parent, { expand: !!parent });
      },
      error: (_code, message) => {
        transferStore.finish(tid, message);
        void dialog.alert({
          title: "Compress failed",
          message,
          variant: "error",
        });
      },
      stream_error: () => {
        // Network blip / proxy hiccup. Mark errored only if the toast is
        // still active; otherwise the terminal event already finished.
        transferStore.finish(tid, "compress stream interrupted");
      },
    });
  }, [reloadSubtree]);

  const onRunFile = useCallback((path: string) => {
    // App.tsx owns the run pipeline (interpreter resolution, output
    // panel, kill button). Fire a window event with the path; App.tsx
    // opens the file as the active tab and triggers its run.
    window.dispatchEvent(new CustomEvent("dev:run-path", { detail: { path } }));
  }, []);

  // Variant of onNewFile / onNewFolder that targets a specific parent
  // directory (the dir the user right-clicked on). reloadSubtree
  // force-expands so the new entry is visible immediately.
  const newFileIn = useCallback(async (parentDir: string) => {
    const name = await dialog.prompt({
      title: "New file",
      message: `Create in /workspace${parentDir ? "/" + parentDir : ""}.`,
      placeholder: "example.py",
      validate: (v) => {
        if (!v.trim()) return "Name is required.";
        if (v.includes("/")) return "Name can't contain '/'.";
        return null;
      },
    });
    if (!name) return;
    const dest = parentDir ? `${parentDir}/${name}` : name;
    try {
      await api.write(dest, "");
      await reloadSubtree(parentDir, { expand: !!parentDir });
      onOpen(dest);
    } catch (err: any) {
      void dialog.alert({ title: "Create failed", message: err?.message ?? String(err), variant: "error" });
    }
  }, [reloadSubtree, onOpen]);
  const newFolderIn = useCallback(async (parentDir: string) => {
    const name = await dialog.prompt({
      title: "New folder",
      message: `Create in /workspace${parentDir ? "/" + parentDir : ""}.`,
      placeholder: "src",
      validate: (v) => {
        if (!v.trim()) return "Name is required.";
        if (v.includes("/")) return "Name can't contain '/'.";
        return null;
      },
    });
    if (!name) return;
    const dest = parentDir ? `${parentDir}/${name}` : name;
    try {
      await api.mkdir(dest);
      await reloadSubtree(parentDir, { expand: !!parentDir });
    } catch (err: any) {
      void dialog.alert({ title: "Create folder failed", message: err?.message ?? String(err), variant: "error" });
    }
  }, [reloadSubtree]);

  const openContextMenuForFile = useCallback((e: React.MouseEvent, path: string) => {
    e.preventDefault();
    e.stopPropagation();
    const isPy = path.toLowerCase().endsWith(".py");
    const isText = viewerKindFor(path) === "text";
    const items: ContextMenuItem[] = [
      { kind: "item", label: "Open", onClick: () => onOpen(path) },
      ...(isPy ? [{ kind: "item" as const, label: "Run", accelerator: "▶", onClick: () => onRunFile(path) }] : []),
      { kind: "item", label: "Download", onClick: () => onDownload(path), disabled: !isText && false /* always allow */ },
      { kind: "separator" },
      { kind: "item", label: "Duplicate", onClick: () => void onDuplicate(path) },
      { kind: "item", label: "Rename…", onClick: () => void onRename(path) },
      { kind: "item", label: "Copy Path", onClick: () => void onCopyPath(path) },
      { kind: "separator" },
      { kind: "item", label: "Delete", danger: true, onClick: () => void onDelete(path, "file") },
    ];
    setMenu({ x: e.clientX, y: e.clientY, items });
  }, [onOpen, onRunFile, onDownload, onDuplicate, onRename, onCopyPath, onDelete]);

  const openContextMenuForDir = useCallback((e: React.MouseEvent, path: string) => {
    e.preventDefault();
    e.stopPropagation();
    const items: ContextMenuItem[] = [
      { kind: "item", label: "New File…", onClick: () => void newFileIn(path) },
      { kind: "item", label: "New Folder…", onClick: () => void newFolderIn(path) },
      { kind: "separator" },
      { kind: "item", label: "Duplicate", onClick: () => void onDuplicate(path) },
      { kind: "item", label: "Compress to .zip", onClick: () => void onCompress(path) },
      { kind: "item", label: "Rename…", onClick: () => void onRename(path) },
      { kind: "item", label: "Copy Path", onClick: () => void onCopyPath(path) },
      { kind: "separator" },
      { kind: "item", label: "Delete", danger: true, onClick: () => void onDelete(path, "dir") },
    ];
    setMenu({ x: e.clientX, y: e.clientY, items });
  }, [newFileIn, newFolderIn, onDuplicate, onCompress, onRename, onCopyPath, onDelete]);

  const openContextMenuForRoot = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    const items: ContextMenuItem[] = [
      { kind: "item", label: "New File…", onClick: onNewFile },
      { kind: "item", label: "New Folder…", onClick: onNewFolder },
      { kind: "item", label: "Upload Files…", onClick: onUpload },
      { kind: "separator" },
      { kind: "item", label: "New Project…", onClick: onNewProject },
      { kind: "item", label: "Refresh", onClick: () => void refresh() },
    ];
    setMenu({ x: e.clientX, y: e.clientY, items });
  }, [onNewFile, onNewFolder, onUpload, onNewProject, refresh]);

  // -------- Search --------------------------------------------------

  const searchTimer = useRef<number | null>(null);

  useEffect(() => {
    if (searchTimer.current) window.clearTimeout(searchTimer.current);
    if (!query.trim()) {
      setResults(null);
      setSearching(false);
      setResultsError(null);
      return;
    }
    setSearching(true);
    setResultsError(null);
    searchTimer.current = window.setTimeout(async () => {
      try {
        const r = await api.search(query, "/");
        setResults(r.results);
      } catch (err: any) {
        setResultsError(err?.message ?? String(err));
        setResults([]);
      } finally {
        setSearching(false);
      }
    }, SEARCH_DEBOUNCE_MS);
  }, [query]);

  // -------- Drag & drop move -----------------------------------------
  //
  // On drop, the source path (workspace-relative) moves to <destDir>/<basename>.
  // destDir = "" means workspace root. We refuse no-op moves (src already
  // under destDir) and self-drops (dropping a dir into itself / a
  // descendant) — the backend would error anyway, but UX-wise we short
  // -circuit. After a successful move, the tree is refreshed.

  const onDropMove = useCallback(async (src: string, destDir: string) => {
    if (!src) return;
    if (isDescendantOrSelf(src, destDir)) {
      void dialog.alert({ title: "Move blocked", message: "Can't move a folder into itself.", variant: "warning" });
      return;
    }
    const srcParent = pathDirname(src);
    if (srcParent === destDir) return; // dropped on its own parent — no-op
    const base = pathBasename(src);
    const dst = destDir ? `${destDir}/${base}` : base;
    try {
      await api.rename(src, dst);
    } catch (err: any) {
      void dialog.alert({ title: "Move failed", message: err?.message ?? String(err), variant: "error" });
      return;
    }
    // Reload both endpoints of the move without collapsing the rest of the
    // tree. Source parent: the moved item is gone. Destination: it appeared,
    // force-expand so the user sees it land.
    await reloadSubtree(srcParent);
    await reloadSubtree(destDir, { expand: true });
  }, [reloadSubtree]);

  // Two kinds of drop are accepted:
  //   • Internal move      → dataTransfer carries DND_MIME (tree row drag)
  //   • OS file upload     → dataTransfer.types includes "Files"
  // The drop handler picks the path that matches the payload it actually
  // received. Both update `dropTarget` so the same visual highlight (dashed
  // outline on the root pane / accent ring on a folder row) covers both
  // cases — a user dragging from Finder/Explorer sees the same affordance
  // as a user dragging a tree row.
  const onRootDragOver = useCallback((e: React.DragEvent) => {
    const isInternal = e.dataTransfer.types.includes(DND_MIME);
    const isFiles = e.dataTransfer.types.includes("Files");
    if (!isInternal && !isFiles) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = isFiles ? "copy" : "move";
    // Set "" as drop target only if not already over a dir row — child
    // handlers stopPropagation, so this only fires for the empty area.
    setDropTarget("");
  }, []);
  const onRootDragLeave = useCallback((e: React.DragEvent) => {
    // Only clear if we actually left the container, not just a child.
    if (e.currentTarget === e.target) setDropTarget(null);
  }, []);
  const onRootDrop = useCallback((e: React.DragEvent) => {
    setDropTarget(null);
    setDraggedPath(null);
    setDraggedKind(null);
    // OS file upload — has File objects on the dataTransfer. Take this
    // branch first because a Finder drag never carries DND_MIME.
    const osFiles = e.dataTransfer.files;
    if (osFiles && osFiles.length > 0) {
      e.preventDefault();
      void uploadFilesTo("", osFiles);
      return;
    }
    const payload = e.dataTransfer.getData(DND_MIME);
    if (!payload) return;
    e.preventDefault();
    try {
      const { path } = JSON.parse(payload);
      void onDropMove(path, "");
    } catch { /* malformed payload — ignore */ }
  }, [onDropMove, uploadFilesTo]);

  const onResultClick = useCallback((hit: SearchHit) => {
    if (hit.type === "d") {
      // No "navigate to dir" in this UI — the tree is rooted at /workspace.
      // For now we just clear the search; tree expansion is per-user action.
      setQuery("");
      return;
    }
    // Only open files under /workspace via the relative path the editor
    // expects. For files outside /workspace (system search hits), bail.
    if (hit.path.startsWith("/workspace/")) {
      onOpen(hit.path.substring("/workspace/".length));
      setQuery("");
    } else {
      void dialog.alert({
        title: "Outside workspace",
        message: `${hit.path} is outside /workspace; open via terminal.`,
        variant: "warning",
      });
    }
  }, [onOpen]);

  // -------- Render --------------------------------------------------

  return (
    <div className="filetree">
      <div className="filetree-header">
        <span>/workspace</span>
        <button className="btn btn-icon-mini" onClick={refresh} title="Refresh" aria-label="Refresh">
          ↻
        </button>
      </div>

      <div className="filetree-actions" role="toolbar" aria-label="File actions">
        <button className="ft-icon-btn" onClick={onNewFile} title="New file" aria-label="New file">＋</button>
        <button className="ft-icon-btn" onClick={onNewFolder} title="New folder" aria-label="New folder">▢</button>
        <button className="ft-icon-btn" onClick={onUpload} title="Upload files (or drag from your computer onto any folder; ≤100MB each — use terminal for larger)" aria-label="Upload">⤒</button>
        <span className="ft-sep" aria-hidden="true"></span>
        <button className="ft-icon-btn" onClick={onNewProject} title="New project (creates folder + pyproject.toml; optional venv)" aria-label="New project">⊕</button>
        <button className="ft-icon-btn" onClick={onOpenProject} title="Open project (refresh tree)" aria-label="Open project">⤴</button>
        <input
          ref={fileInputRef}
          type="file"
          hidden
          multiple
          onChange={onUploadChange}
        />
      </div>

      <div className="filetree-search">
        <span className="ft-search-icon" aria-hidden="true">⌕</span>
        <input
          type="text"
          className="ft-search-input"
          placeholder="Search files by name…"
          aria-label="Search files"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Escape") setQuery(""); }}
        />
        {query && (
          <button className="ft-search-clear" onClick={() => setQuery("")} title="Clear" aria-label="Clear search">×</button>
        )}
      </div>

      {error && <div className="filetree-error">{error}</div>}

      {results !== null ? (
        <div className="filetree-list">
          {searching && <div className="tree-row tree-meta" style={{ paddingLeft: 12 }}>searching…</div>}
          {!searching && resultsError && (
            <div className="tree-row tree-meta tree-error" style={{ paddingLeft: 12 }}>{resultsError}</div>
          )}
          {!searching && !resultsError && results.length === 0 && (
            <div className="tree-row tree-meta" style={{ paddingLeft: 12 }}>no matches for &quot;{query}&quot;</div>
          )}
          {results.map((r) => (
            <div
              key={r.path}
              className={`tree-row ${r.type === "d" ? "tree-dir" : "tree-file"}`}
              style={{ paddingLeft: 12 }}
              onClick={() => onResultClick(r)}
            >
              <span className="tree-caret">{r.type === "d" ? "▸" : ""}</span>
              <span className="tree-name">{r.name}</span>
              <span className="tree-result-path">{r.path}</span>
            </div>
          ))}
        </div>
      ) : (
        <div
          className={`filetree-list ${dropTarget === "" ? "drop-root" : ""}`}
          onDragOver={onRootDragOver}
          onDragLeave={onRootDragLeave}
          onDrop={onRootDrop}
          onContextMenu={openContextMenuForRoot}
        >
          {nodes.map((n) => (
            <TreeNode
              key={n.path}
              node={n}
              depth={0}
              onOpen={onOpen}
              onToggle={toggle}
              draggedPath={draggedPath}
              draggedKind={draggedKind}
              dropTarget={dropTarget}
              onDragStartRow={(path, kind, e) => {
                setDraggedPath(path);
                setDraggedKind(kind);
                e.dataTransfer.effectAllowed = "move";
                e.dataTransfer.setData(DND_MIME, JSON.stringify({ path, kind }));
              }}
              onDragEndRow={() => {
                setDraggedPath(null);
                setDraggedKind(null);
                setDropTarget(null);
              }}
              setDropTarget={setDropTarget}
              onDropOnDir={(srcPath, destDir) => void onDropMove(srcPath, destDir)}
              onDropFilesOnDir={(destDir, files) => void uploadFilesTo(destDir, files)}
              onContextMenuFile={openContextMenuForFile}
              onContextMenuDir={openContextMenuForDir}
            />
          ))}
        </div>
      )}
      <ContextMenu state={menu} onClose={closeMenu} />
    </div>
  );
}

function TreeNode({
  node, depth, onOpen, onToggle,
  draggedPath, draggedKind, dropTarget,
  onDragStartRow, onDragEndRow, setDropTarget, onDropOnDir, onDropFilesOnDir,
  onContextMenuFile, onContextMenuDir,
}: {
  node: Node;
  depth: number;
  onOpen: (path: string) => void;
  onToggle: (path: string) => void;
  draggedPath: string | null;
  draggedKind: "file" | "dir" | null;
  dropTarget: string | null;
  onDragStartRow: (path: string, kind: "file" | "dir", e: React.DragEvent) => void;
  onDragEndRow: () => void;
  setDropTarget: (path: string | null) => void;
  onDropOnDir: (srcPath: string, destDir: string) => void;
  onDropFilesOnDir: (destDir: string, files: FileList) => void;
  onContextMenuFile: (e: React.MouseEvent, path: string) => void;
  onContextMenuDir: (e: React.MouseEvent, path: string) => void;
}) {
  const isDir = node.entry.kind === "dir";
  const kind: "file" | "dir" = isDir ? "dir" : "file";
  const isDragging = draggedPath === node.path;
  // Drop-eligibility for an *internal* row drag (move). OS file drops are
  // accepted on any directory regardless of `draggedPath`.
  const dropEligibleInternal = (() => {
    if (!isDir || !draggedPath) return false;
    if (draggedKind === "dir" && isDescendantOrSelf(draggedPath, node.path)) return false;
    if (pathDirname(draggedPath) === node.path) return false; // already there
    return true;
  })();
  const dropEligible = isDir && (dropEligibleInternal || !draggedPath);
  const isDropHover = dropEligible && dropTarget === node.path;
  return (
    <>
      <div
        className={`tree-row ${isDir ? "tree-dir" : "tree-file"}${isDragging ? " dragging" : ""}${isDropHover ? " drop-target" : ""}`}
        style={{ paddingLeft: 8 + depth * 14 }}
        onClick={() => (isDir ? onToggle(node.path) : onOpen(node.path))}
        onContextMenu={(e) => (isDir ? onContextMenuDir(e, node.path) : onContextMenuFile(e, node.path))}
        draggable
        onDragStart={(e) => {
          e.stopPropagation();
          onDragStartRow(node.path, kind, e);
        }}
        onDragEnd={(e) => { e.stopPropagation(); onDragEndRow(); }}
        onDragOver={(e) => {
          if (!dropEligible) return;
          // Distinguish OS file drag (carries "Files") from internal row
          // drag (carries DND_MIME). Both are accepted on a directory;
          // the cursor effect differs (copy vs. move).
          const isFiles = e.dataTransfer.types.includes("Files");
          const isInternal = e.dataTransfer.types.includes(DND_MIME);
          if (!isFiles && !isInternal) return;
          e.preventDefault();
          e.stopPropagation();
          e.dataTransfer.dropEffect = isFiles ? "copy" : "move";
          setDropTarget(node.path);
        }}
        onDragLeave={(e) => {
          if (!isDir) return;
          e.stopPropagation();
          // Only clear if leaving this row (relatedTarget not inside)
          // Local `Node` type in this file shadows the DOM Node — use
          // `globalThis.Node` to disambiguate.
          if (!(e.currentTarget as HTMLElement).contains(e.relatedTarget as globalThis.Node | null)) {
            if (dropTarget === node.path) setDropTarget(null);
          }
        }}
        onDrop={(e) => {
          if (!dropEligible) return;
          e.preventDefault();
          e.stopPropagation();
          setDropTarget(null);
          // OS file upload — Finder/Explorer drop. Takes precedence; a
          // Finder drag never carries DND_MIME so this branch is safe.
          const osFiles = e.dataTransfer.files;
          if (osFiles && osFiles.length > 0) {
            onDropFilesOnDir(node.path, osFiles);
            return;
          }
          const payload = e.dataTransfer.getData(DND_MIME);
          if (!payload) return;
          try {
            const { path } = JSON.parse(payload);
            onDropOnDir(path, node.path);
          } catch { /* malformed — ignore */ }
        }}
      >
        <span className="tree-caret">{isDir ? (node.open ? "▾" : "▸") : ""}</span>
        <span className="tree-name">{node.entry.name}</span>
      </div>
      {isDir && node.open && (
        <>
          {node.children === "loading" && (
            <div className="tree-row tree-meta" style={{ paddingLeft: 8 + (depth + 1) * 14 }}>loading…</div>
          )}
          {node.children === "error" && (
            <div className="tree-row tree-meta tree-error" style={{ paddingLeft: 8 + (depth + 1) * 14 }}>error</div>
          )}
          {Array.isArray(node.children) &&
            node.children.map((c) => (
              <TreeNode
                key={c.path}
                node={c}
                depth={depth + 1}
                onOpen={onOpen}
                onToggle={onToggle}
                draggedPath={draggedPath}
                draggedKind={draggedKind}
                dropTarget={dropTarget}
                onDragStartRow={onDragStartRow}
                onDragEndRow={onDragEndRow}
                setDropTarget={setDropTarget}
                onDropOnDir={onDropOnDir}
                onDropFilesOnDir={onDropFilesOnDir}
                onContextMenuFile={onContextMenuFile}
                onContextMenuDir={onContextMenuDir}
              />
            ))}
        </>
      )}
    </>
  );
}

function findNode(nodes: Node[], path: string): Node | null {
  for (const n of nodes) {
    if (n.path === path) return n;
    if (Array.isArray(n.children)) {
      const hit = findNode(n.children, path);
      if (hit) return hit;
    }
  }
  return null;
}

function mutate(nodes: Node[], path: string, fn: (n: Node) => Node): Node[] {
  return nodes.map((n) => {
    if (n.path === path) return fn(n);
    if (Array.isArray(n.children)) {
      return { ...n, children: mutate(n.children, path, fn) };
    }
    return n;
  });
}
