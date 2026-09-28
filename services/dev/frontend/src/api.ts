// Thin fetch wrapper. CF Access cookies ride along automatically since we're
// same-origin; no Authorization header to add.

export type Me = { email: string; role: "admin" | "user" };

export type FileEntry = {
  name: string;
  kind: "file" | "dir" | "link" | "other";
  size: number;
  mtime: number;
};

export type DirListing = {
  path: string;
  entries: FileEntry[];
};

export type ReadResult = {
  path: string;
  content: string;
  size: number;
  truncated: boolean;
};

export type IDEState = {
  version: number;
  open_tabs: TabState[];
  selected_interpreter: string | null;
  theme: "dark" | "light";
  // Opaque dockview layout JSON (`api.toJSON()` shape from dockview-react).
  // Stored as-is and passed back via `api.fromJSON()` on next boot. Backend
  // doesn't care about its shape — state.py treats the whole IDEState as
  // an opaque dict so adding fields here is forward-compatible.
  layout?: unknown;
};

export type TabState = {
  path: string;
  active: boolean;
  cursor_line?: number;
  cursor_col?: number;
};

export type JobInfo = {
  job_id: string;
  status: "running" | "done" | "killed" | "error";
  exit_code: number | null;
  started_at: number;
  argv: string[];
};

export type StartJob = {
  job_id: string;
  started_at: number;
  argv: string[];
  cwd: string;
};

export type SearchHit = {
  path: string;     // absolute (e.g. /workspace/foo/bar.py)
  name: string;
  type: "f" | "d" | "l";
};

export type Interpreter = {
  path: string;     // absolute path inside the user container
  version: string;  // "3.12.3" or "" if probe failed
  label: string;    // human-readable for the dropdown
  kind: "venv" | "linuxbrew" | "system";
};

export type ProjectSummary = {
  path: string;     // workspace-relative; "" = workspace root
  name: string;
  has_venv: boolean;
};

export type ProjectConfig = {
  version: number;
  interpreter: string | null;
};

export type NewProjectBody = {
  name: string;
  parent?: string;
  template?: "empty" | "script" | "module" | "quant" | "fastapi";
  with_venv?: boolean;
  base_interpreter?: string | null;
  packages?: string[];
  init_git?: boolean;
  create_main?: boolean;
  create_readme?: boolean;
  create_gitignore?: boolean;
  description?: string;
};

export type NewProjectResponse = {
  path: string;
  slug: string;
  template: string;
  main_file: string | null;
  venv_python: string | null;
  warnings: string[];
};

export class HttpError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = "HttpError";
  }
}

async function json<T>(resp: Response): Promise<T> {
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`;
    try {
      const body = await resp.json();
      if (body?.detail) detail = body.detail;
    } catch {
      // ignore
    }
    throw new HttpError(resp.status, detail);
  }
  return (await resp.json()) as T;
}

export const api = {
  me: () => fetch("/api/me").then((r) => json<Me>(r)),

  loadState: () => fetch("/api/state").then((r) => json<{ state: IDEState }>(r)),
  saveState: (state: IDEState) =>
    fetch("/api/state", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ state }),
    }).then((r) => json<{ ok: boolean }>(r)),

  list: (path: string) =>
    fetch(`/api/files?path=${encodeURIComponent(path)}`).then((r) =>
      json<DirListing>(r),
    ),
  read: (path: string) =>
    fetch(`/api/files/read?path=${encodeURIComponent(path)}`).then((r) =>
      json<ReadResult>(r),
    ),
  write: (path: string, content: string) =>
    fetch("/api/files/write", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, content }),
    }).then((r) => json<{ path: string; size: number }>(r)),
  mkdir: (path: string) =>
    fetch("/api/files/mkdir", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    }).then((r) => json<{ path: string }>(r)),
  rename: (src: string, dst: string) =>
    fetch("/api/files/rename", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ src, dst }),
    }).then((r) => json<{ src: string; dst: string }>(r)),
  copy: (src: string, dst: string) =>
    fetch("/api/files/copy", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ src, dst }),
    }).then((r) => json<{ src: string; dst: string }>(r)),
  remove: (path: string) =>
    fetch(`/api/files?path=${encodeURIComponent(path)}`, {
      method: "DELETE",
    }).then((r) => json<{ path: string }>(r)),

  // Compress is two steps: start (returns id), then subscribe to SSE for
  // progress + completion. Compress is the slow op so progress matters —
  // see `streamCompress` below.
  compressStart: (path: string) =>
    fetch("/api/files/compress", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    }).then((r) => json<{ id: string; src: string; started_at: number }>(r)),

  // Streaming download — uses XHR so we can report progress to the
  // TransferToasts panel. fetch+ReadableStream would also work but XHR's
  // `responseType: "blob"` gives us a saveable Blob directly and progress
  // events are simpler. Used by the file tree's "Download" context menu.
  downloadBlob: (
    path: string,
    opts?: { onProgress?: (bytes: number, total: number) => void; signal?: AbortSignal },
  ): Promise<{ blob: Blob; filename: string }> => {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("GET", `/api/files/raw?path=${encodeURIComponent(path)}`);
      xhr.responseType = "blob";
      xhr.onprogress = (e) => {
        if (opts?.onProgress) opts.onProgress(e.loaded, e.lengthComputable ? e.total : 0);
      };
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          // Pull a filename suggestion from Content-Disposition if the
          // server set one; otherwise fall back to the path basename.
          const cd = xhr.getResponseHeader("content-disposition") ?? "";
          const m = cd.match(/filename="([^"]+)"/i);
          const filename = m ? m[1] : (path.split("/").pop() ?? "download");
          resolve({ blob: xhr.response as Blob, filename });
        } else {
          let detail = `HTTP ${xhr.status}`;
          try {
            // Blob response can still contain JSON; read as text on error.
            const reader = new FileReader();
            reader.onload = () => {
              try {
                const body = JSON.parse(String(reader.result));
                if (body?.detail) detail = body.detail;
              } catch { /* fall through to status code */ }
              reject(new Error(detail));
            };
            reader.onerror = () => reject(new Error(detail));
            reader.readAsText(xhr.response as Blob);
            return;
          } catch {
            reject(new Error(detail));
          }
        }
      };
      xhr.onerror = () => reject(new Error("network error"));
      xhr.onabort = () => reject(new Error("aborted"));
      if (opts?.signal) {
        opts.signal.addEventListener("abort", () => { try { xhr.abort(); } catch { /* ignore */ } });
      }
      xhr.send();
    });
  },

  search: (q: string, root = "/") =>
    fetch(`/api/files/search?q=${encodeURIComponent(q)}&root=${encodeURIComponent(root)}`)
      .then((r) => json<{ query: string; root: string; results: SearchHit[] }>(r)),

  // Uses XHR (not fetch) so we can report upload progress to the
  // TransferToasts panel — `fetch` has no upload-side progress events as
  // of 2026. The opts.onProgress callback fires for every `loaded` update
  // the browser dispatches (one per ~64 KB chunk on Chromium/Firefox).
  upload: (
    path: string,
    file: File,
    opts?: { onProgress?: (bytes: number, total: number) => void; signal?: AbortSignal },
  ): Promise<{ path: string; size: number }> => {
    return new Promise((resolve, reject) => {
      // Raw octet-stream POST to /api/files/upload-raw. Cloudflare Access on
      // dev.wizerith.ai kills authenticated multipart POSTs at the tunnel
      // ("Incoming request ended abruptly: context canceled") before they
      // reach caddy. Raw-body POSTs traverse the same path fine. Filename
      // and destination dir go in X-Filename / X-Upload-Path headers.
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/files/upload-raw");
      xhr.setRequestHeader("Content-Type", "application/octet-stream");
      xhr.setRequestHeader("X-Filename", encodeURIComponent(file.name));
      xhr.setRequestHeader("X-Upload-Path", encodeURIComponent(path));
      xhr.responseType = "json";
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable && opts?.onProgress) opts.onProgress(e.loaded, e.total);
      };
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve(xhr.response);
        } else {
          const body = xhr.response;
          let detail: string = `HTTP ${xhr.status}`;
          if (body && typeof body === "object" && "detail" in body) {
            const d = (body as { detail: unknown }).detail;
            detail = typeof d === "string" ? d : JSON.stringify(d);
          }
          reject(new Error(detail));
        }
      };
      xhr.onerror = () => reject(new Error("network error"));
      xhr.onabort = () => reject(new Error("aborted"));
      if (opts?.signal) {
        opts.signal.addEventListener("abort", () => { try { xhr.abort(); } catch { /* ignore */ } });
      }
      xhr.send(file);
    });
  },

  newProject: (body: NewProjectBody) =>
    fetch("/api/projects/new", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then((r) => json<NewProjectResponse>(r)),

  interpreters: () =>
    fetch("/api/interpreters").then((r) => json<{ interpreters: Interpreter[] }>(r)),

  projects: () =>
    fetch("/api/projects").then((r) => json<{ projects: ProjectSummary[] }>(r)),

  projectConfigGet: (path: string) =>
    fetch(`/api/projects/config?path=${encodeURIComponent(path)}`)
      .then((r) => json<{ path: string; config: ProjectConfig }>(r)),

  projectConfigPut: (path: string, config: ProjectConfig) =>
    fetch("/api/projects/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, config }),
    }).then((r) => json<{ ok: boolean }>(r)),

  run: (body: { path: string; interpreter?: string; args?: string[]; cwd?: string }) =>
    fetch("/api/jobs/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then((r) => json<StartJob>(r)),
  jobs: () => fetch("/api/jobs").then((r) => json<{ jobs: JobInfo[] }>(r)),
  killJob: (jobId: string) =>
    fetch(`/api/jobs/${jobId}`, { method: "DELETE" }).then((r) =>
      json<{ killed: boolean; status: string }>(r),
    ),
};

// SSE wrapper around /api/jobs/{job_id}/stream. The backend sends a
// `status` event with a terminal value (`done` / `error` / `killed`)
// when the job is over, then closes the stream. EventSource's default
// behavior on a clean close is to *retry* — which would re-fetch the
// stream, replay the buffer, observe the same `status` again, and loop
// forever (visible to the user as "stream interrupted; retrying…"
// alternating with replayed output). We track the terminal flag and
// close the EventSource ourselves the first time `status` arrives. We
// also suppress `onerror` callbacks after the terminal status so the
// caller doesn't surface "stream interrupted" on what is actually the
// expected post-status disconnect.
export function streamJob(
  jobId: string,
  on: {
    line: (kind: string, text: string, ts: number) => void;
    status?: (status: string, exitCode: number | null) => void;
    error?: (err: Event) => void;
  },
): () => void {
  const es = new EventSource(`/api/jobs/${jobId}/stream`);
  let terminated = false;

  const TERMINAL_STATUSES = new Set(["done", "error", "killed"]);

  const handle = (kind: string) => (evt: MessageEvent) => {
    try {
      const data = JSON.parse(evt.data);
      if (kind === "status") {
        on.status?.(data.status, data.exit_code);
        if (TERMINAL_STATUSES.has(data.status)) {
          terminated = true;
          // Close on the next tick so any in-flight `exit` event the
          // server already sent ahead of `status` finishes dispatching
          // to its listener first.
          setTimeout(() => {
            try { es.close(); } catch { /* ignore */ }
          }, 0);
        }
      } else {
        on.line(kind, data.text ?? "", data.ts ?? 0);
      }
    } catch {
      // swallow malformed events; the job loop continues
    }
  };

  es.addEventListener("stdout", handle("stdout"));
  es.addEventListener("stderr", handle("stderr"));
  es.addEventListener("system", handle("system"));
  es.addEventListener("exit", handle("exit"));
  es.addEventListener("status", handle("status"));
  es.onerror = (e) => {
    if (terminated) {
      // Expected disconnect after the terminal status; eat it so the
      // UI doesn't show "stream interrupted; retrying…".
      try { es.close(); } catch { /* ignore */ }
      return;
    }
    on.error?.(e);
  };

  return () => es.close();
}

// Compress progress stream — backend emits NDJSON events under
// /api/files/compress/{id}/stream:
//   scanned   {total_bytes, total_files}
//   progress  {bytes_done, files_done}
//   done      {dst, size}              (terminal)
//   error     {code, message}          (terminal)
//   snapshot  {full final state}       (sent on close)
//
// Returns an unsubscribe fn. After a terminal event the underlying
// EventSource is closed; the caller doesn't have to do anything extra.
export function streamCompress(
  jobId: string,
  on: {
    scanned?: (totalBytes: number, totalFiles: number) => void;
    progress?: (bytesDone: number, filesDone: number) => void;
    done?: (dst: string, size: number) => void;
    error?: (code: string, message: string) => void;
    stream_error?: (err: Event) => void;
  },
): () => void {
  const es = new EventSource(`/api/files/compress/${jobId}/stream`);
  let terminated = false;

  const close = () => {
    terminated = true;
    setTimeout(() => { try { es.close(); } catch { /* ignore */ } }, 0);
  };

  es.addEventListener("scanned", (e) => {
    try {
      const d = JSON.parse((e as MessageEvent).data);
      on.scanned?.(d.total_bytes ?? 0, d.total_files ?? 0);
    } catch { /* ignore */ }
  });
  es.addEventListener("progress", (e) => {
    try {
      const d = JSON.parse((e as MessageEvent).data);
      on.progress?.(d.bytes_done ?? 0, d.files_done ?? 0);
    } catch { /* ignore */ }
  });
  es.addEventListener("done", (e) => {
    try {
      const d = JSON.parse((e as MessageEvent).data);
      on.done?.(String(d.dst ?? ""), d.size ?? 0);
    } catch { /* ignore */ }
    close();
  });
  es.addEventListener("error" as any, (e) => {
    // Event-named "error" can collide with the EventSource native
    // `onerror`. SSE-event-named errors arrive via addEventListener;
    // the native handler is set below.
    if ((e as any)?.data) {
      try {
        const d = JSON.parse((e as MessageEvent).data);
        on.error?.(String(d.code ?? "error"), String(d.message ?? "compress failed"));
      } catch { /* ignore */ }
      close();
    }
  });
  es.onerror = (e) => {
    if (terminated) {
      try { es.close(); } catch { /* ignore */ }
      return;
    }
    on.stream_error?.(e);
  };

  return () => { terminated = true; try { es.close(); } catch { /* ignore */ } };
}
