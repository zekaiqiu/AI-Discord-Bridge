// Tiny pub-sub store for in-flight file transfers (uploads today; design
// supports `kind: "download"` if we ever add a file-export flow). Used by
// the TransferToasts component to show a bottom-right status panel so the
// user can see what's uploading and avoid re-dropping the same files.
//
// Plain module-scope state + Set of listeners — no React context needed, so
// any component (FileTree, future download buttons, etc.) can fire events
// without prop-drilling.

export type TransferStatus = "active" | "done" | "error";
export type TransferKind = "upload" | "download" | "compress";

export type Transfer = {
  id: string;
  name: string;            // basename shown in the toast
  kind: TransferKind;
  destDir: string;         // workspace-relative; "" = root
  bytes: number;           // bytes transferred so far
  total: number | null;    // null when size is unknown (some uploads omit it)
  status: TransferStatus;
  error?: string;
  startedAt: number;
  finishedAt?: number;
};

type Listener = () => void;

let transfers: Transfer[] = [];
const listeners = new Set<Listener>();

function notify() { listeners.forEach((l) => l()); }

let counter = 0;
function nextId(): string {
  counter += 1;
  return `t${Date.now().toString(36)}-${counter}`;
}

export const transferStore = {
  list(): Transfer[] {
    return transfers;
  },
  subscribe(listener: Listener): () => void {
    listeners.add(listener);
    return () => { listeners.delete(listener); };
  },
  start(opts: { name: string; total: number | null; kind?: TransferKind; destDir?: string }): string {
    const id = nextId();
    const t: Transfer = {
      id,
      name: opts.name,
      kind: opts.kind ?? "upload",
      destDir: opts.destDir ?? "",
      bytes: 0,
      total: opts.total,
      status: "active",
      startedAt: Date.now(),
    };
    transfers = [...transfers, t];
    notify();
    return id;
  },
  progress(id: string, bytes: number): void {
    let changed = false;
    transfers = transfers.map((t) => {
      if (t.id !== id || t.status !== "active") return t;
      changed = true;
      return { ...t, bytes };
    });
    if (changed) notify();
  },
  // Late-binding total: some flows (compress) only learn the total size
  // *after* the server has walked the source tree, after the toast is
  // already showing. Lets the indeterminate bar switch to a real % once
  // the scan completes.
  setTotal(id: string, total: number): void {
    let changed = false;
    transfers = transfers.map((t) => {
      if (t.id !== id || t.status !== "active") return t;
      changed = true;
      return { ...t, total };
    });
    if (changed) notify();
  },
  finish(id: string, error?: string): void {
    transfers = transfers.map((t) => (
      t.id === id
        ? { ...t, status: error ? "error" : "done", error, finishedAt: Date.now(), bytes: error ? t.bytes : (t.total ?? t.bytes) }
        : t
    ));
    notify();
    if (!error) {
      // Auto-dismiss successful transfers after a short delay — successful
      // uploads don't need to linger. Errors stay until the user dismisses
      // so they don't disappear before being read.
      window.setTimeout(() => { transferStore.dismiss(id); }, 4000);
    }
  },
  dismiss(id: string): void {
    const before = transfers.length;
    transfers = transfers.filter((t) => t.id !== id);
    if (transfers.length !== before) notify();
  },
  clearCompleted(): void {
    const before = transfers.length;
    transfers = transfers.filter((t) => t.status === "active");
    if (transfers.length !== before) notify();
  },
};
