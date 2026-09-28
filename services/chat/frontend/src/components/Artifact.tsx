/**
 * Inline artifact previews.
 *
 * The chat backend appends a markdown "**Artifacts**" footer listing files
 * the model produced under /data/generated/<sid>/. Thread.tsx parses that
 * footer into ArtifactInfo[] and hands each item to <ArtifactPreview/>,
 * which dispatches to a typed renderer based on the file extension:
 *   - image → <img>
 *   - csv / tsv → parsed table
 *   - html / htm → sandboxed iframe (allow-scripts, no allow-same-origin)
 *   - mmd → mermaid SVG (lazy-loaded)
 *   - xlsx / xls / ods → SheetJS-parsed sheets with tab switcher (lazy)
 *   - json → pretty-printed code block
 *   - anything else → download chip
 *
 * Size cap: files over MAX_INLINE_BYTES render as a download chip. Avoids
 * loading a 100 MB CSV into the page on a session re-open.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneDark, oneLight } from "react-syntax-highlighter/dist/esm/styles/prism";
import { formatBytes } from "../utils";
import { ArtifactRunModal } from "./ArtifactRunModal";

export interface ArtifactInfo {
  filename: string;
  url: string;
  size?: number;
}

const MAX_INLINE_BYTES = 5 * 1024 * 1024;
const MAX_TABLE_ROWS = 5000;

const IMAGE_EXTS = new Set(["png", "jpg", "jpeg", "webp", "gif", "svg"]);
const VIDEO_EXTS = new Set(["mp4", "mov", "webm", "mkv", "avi"]);
const AUDIO_EXTS = new Set(["mp3", "wav", "flac", "ogg", "m4a"]);
const FONT_EXTS = new Set(["ttf", "otf", "woff", "woff2"]);
// Office docs + binary archives + heavy 3D — download chip with a hint.
const OFFICE_EXTS = new Set(["docx", "doc", "pptx", "ppt"]);
const ARCHIVE_EXTS = new Set(["zip", "tar", "gz"]);
const BINARY_3D_EXTS = new Set(["glb"]);

// Extension → Prism language id. Anything not listed falls through to the
// generic fallback in pickLanguage(); unknown code-like extensions are
// rendered as plain text but still get the framed code block styling.
const CODE_LANG_BY_EXT: Record<string, string> = {
  py: "python",
  js: "javascript",
  jsx: "jsx",
  ts: "typescript",
  tsx: "tsx",
  go: "go",
  rs: "rust",
  java: "java",
  kt: "kotlin",
  swift: "swift",
  c: "c",
  h: "c",
  cpp: "cpp",
  cc: "cpp",
  cxx: "cpp",
  hpp: "cpp",
  cs: "csharp",
  rb: "ruby",
  php: "php",
  pl: "perl",
  lua: "lua",
  sh: "bash",
  bash: "bash",
  zsh: "bash",
  fish: "bash",
  sql: "sql",
  r: "r",
  yaml: "yaml",
  yml: "yaml",
  toml: "toml",
  ini: "ini",
  cfg: "ini",
  dockerfile: "docker",
  md: "markdown",
  txt: "text",
  log: "text",
  // Additional languages — Prism's autoload covers most of these via the
  // language id; unknown ones still render as plain monospace text.
  scala: "scala",
  dart: "dart",
  ex: "elixir",
  exs: "elixir",
  clj: "clojure",
  cljs: "clojure",
  hs: "haskell",
  zig: "zig",
  nim: "nim",
  jl: "julia",
  // Diagram source formats — no graph rendering yet, just highlighted text.
  dot: "dot",
  puml: "text",
  // 3D ASCII formats are plain text.
  obj: "text",
  stl: "text",
};
const CODE_EXTS = new Set(Object.keys(CODE_LANG_BY_EXT));

function extOf(filename: string): string {
  const dot = filename.lastIndexOf(".");
  if (dot < 0) return "";
  return filename.slice(dot + 1).toLowerCase();
}

export function ArtifactPreview({ artifact }: { artifact: ArtifactInfo }): JSX.Element {
  const ext = extOf(artifact.filename);
  const tooLarge = typeof artifact.size === "number" && artifact.size > MAX_INLINE_BYTES;

  let body: JSX.Element;
  if (tooLarge) {
    body = <DownloadOnly artifact={artifact} reason="File over 5 MB — open in new tab to view." />;
  } else if (IMAGE_EXTS.has(ext)) {
    body = <ImageBody artifact={artifact} />;
  } else if (VIDEO_EXTS.has(ext)) {
    body = <VideoBody artifact={artifact} ext={ext} />;
  } else if (AUDIO_EXTS.has(ext)) {
    body = <AudioBody artifact={artifact} ext={ext} />;
  } else if (FONT_EXTS.has(ext)) {
    body = <FontBody artifact={artifact} />;
  } else if (ext === "csv" || ext === "tsv") {
    body = <TableBody artifact={artifact} delimiter={ext === "tsv" ? "\t" : ","} />;
  } else if (ext === "html" || ext === "htm") {
    body = <HtmlBody artifact={artifact} />;
  } else if (ext === "mmd") {
    body = <MermaidBody artifact={artifact} />;
  } else if (ext === "xlsx" || ext === "xls" || ext === "ods") {
    body = <XlsxBody artifact={artifact} />;
  } else if (ext === "pdf") {
    body = <PdfBody artifact={artifact} />;
  } else if (ext === "ipynb") {
    body = <IpynbBody artifact={artifact} />;
  } else if (ext === "parquet") {
    body = <ParquetBody artifact={artifact} />;
  } else if (ext === "feather" || ext === "arrow") {
    body = <ArrowBody artifact={artifact} />;
  } else if (ext === "json" || ext === "geojson" || ext === "excalidraw" || ext === "gltf") {
    body = <JsonBody artifact={artifact} />;
  } else if (ext === "xml" || ext === "xsl" || ext === "xslt" || ext === "kml" || ext === "drawio") {
    body = <XmlBody artifact={artifact} />;
  } else if (CODE_EXTS.has(ext)) {
    body = <CodeBody artifact={artifact} language={CODE_LANG_BY_EXT[ext]} />;
  } else if (OFFICE_EXTS.has(ext)) {
    body = <DownloadOnly artifact={artifact} reason="Office documents have no inline preview — download to open, or ask for a PDF/HTML export to preview here." />;
  } else if (ARCHIVE_EXTS.has(ext)) {
    body = <DownloadOnly artifact={artifact} reason="Archive — download to extract." />;
  } else if (BINARY_3D_EXTS.has(ext)) {
    body = <DownloadOnly artifact={artifact} reason="Binary 3D model — download to open in a 3D viewer." />;
  } else {
    body = <DownloadOnly artifact={artifact} />;
  }

  // Cross-product deep-link targets. Artifacts that don't carry a
  // workspace path get a best-effort filename hint — the receiving service
  // searches for it in /workspace and falls back to the root.
  const termHref = artifactTerminalHref(artifact);
  const ideHref = artifactIdeHref(artifact);

  // Runnable code artifacts get an inline Run button — opens the
  // ArtifactRunModal which streams stdout/stderr and surfaces matplotlib
  // charts / written files. Python runs directly; C/C++ sources are
  // compiled (g++/gcc) then executed, all server-side. Keep this set in
  // sync with artifact_runner.RUNNABLE_EXTENSIONS on the backend. Source +
  // session id are inferred from the artifact's URL (same regex shape as
  // termHref/ideHref).
  const runnable = ["py", "cpp", "cc", "cxx", "c++", "c"].includes(ext);
  const runHandle = useMemo(
    () => (runnable ? runHandleForArtifact(artifact) : null),
    [runnable, artifact],
  );
  const [runOpen, setRunOpen] = useState(false);

  return (
    <figure className="artifact" data-ext={ext}>
      <figcaption className="artifact-caption">
        <span className="artifact-name">{artifact.filename}</span>
        {typeof artifact.size === "number" && (
          <span className="artifact-size">{formatBytes(artifact.size)}</span>
        )}
        <span className="artifact-actions" role="toolbar" aria-label="Artifact actions">
          {runnable && runHandle && (
            <button
              type="button"
              className="artifact-action artifact-run"
              onClick={() => setRunOpen(true)}
              aria-label={`Run ${artifact.filename}`}
              title="Run in your container"
            >
              ▶
            </button>
          )}
          <a
            className="artifact-action"
            href={termHref}
            target="_blank"
            rel="noopener noreferrer"
            aria-label="Open in terminal"
            title="Open in terminal"
          >
            ▌_
          </a>
          <a
            className="artifact-action"
            href={ideHref}
            target="_blank"
            rel="noopener noreferrer"
            aria-label="Open in IDE"
            title="Open in IDE"
          >
            {"</>"}
          </a>
          <a
            className="artifact-action artifact-download"
            href={artifact.url}
            download={artifact.filename}
            aria-label={`Download ${artifact.filename}`}
            title="Download"
          >
            ⤓
          </a>
        </span>
      </figcaption>
      <div className="artifact-body">{body}</div>
      {runOpen && runHandle && (
        <ArtifactRunModal
          sessionId={runHandle.sessionId}
          filename={runHandle.filename}
          source={runHandle.source}
          onClose={() => setRunOpen(false)}
        />
      )}
    </figure>
  );
}


// Extract session_id + source from the artifact URL so the run endpoint
// knows where to find the bytes. Mirrors the regex used by
// workspacePathForArtifact above — both formats are exposed by the chat
// backend; "generated" is the LLM-written path, "attachment" is the
// user-uploaded path.
function runHandleForArtifact(artifact: ArtifactInfo): {
  sessionId: string;
  filename: string;
  source: "generated" | "attachment";
} | null {
  const gen = artifact.url.match(/\/api\/sessions\/([^/]+)\/generated\/(.+)$/);
  if (gen) return { sessionId: gen[1], filename: safeDecode(gen[2]), source: "generated" };
  const att = artifact.url.match(/\/api\/sessions\/([^/]+)\/attachments\/(.+)$/);
  if (att) return { sessionId: att[1], filename: safeDecode(att[2]), source: "attachment" };
  return null;
}

// Artifact URLs carry a percent-encoded file name (spaces, parentheses,
// Chinese titles). The Run handle and the IDE path need the real name.
function safeDecode(s: string): string {
  try {
    return decodeURIComponent(s);
  } catch {
    return s;
  }
}

function devHostFromCurrent(): string {
  const host = window.location.host;
  return host.startsWith("chat.") ? host.replace(/^chat\./, "dev.")
    : host.split(".").length === 2 ? "dev." + host
    : "dev." + host.replace(/^[^.]+\./, "");
}

function workspacePathForArtifact(artifact: ArtifactInfo): string {
  // Artifact URLs are `/api/sessions/<session_id>/generated/<filename>`
  // (see app._scan_new_artifacts). The same file lives at
  // /workspace/.artifacts/<session_id>/<filename> inside the per-user
  // container — and the dev IDE mounts the same workspace, so this path
  // is reachable from the IDE side.
  const m = artifact.url.match(/\/api\/sessions\/([^/]+)\/generated\/(.+)$/);
  if (m) return `/workspace/.artifacts/${m[1]}/${safeDecode(m[2])}`;
  // Fallback for unknown URL shapes: hand the basename to the IDE and
  // let its openFile error out cleanly rather than guessing a path.
  return artifact.filename;
}

function artifactTerminalHref(artifact: ArtifactInfo): string {
  // Term.* was retired; the Terminal panel lives inside dev.<host> now.
  // We pass `panel=terminal` so dev activates the Terminal panel on
  // arrival, plus `path` pointing at the artifact's directory so future
  // file-tree-focus support has a target. `hint=<filename>` carries the
  // basename for any future "echo path into the new terminal" wiring.
  const wsPath = workspacePathForArtifact(artifact);
  const wsDir = wsPath.includes("/") ? wsPath.slice(0, wsPath.lastIndexOf("/")) : "/workspace";
  return `${window.location.protocol}//${devHostFromCurrent()}/?panel=terminal&path=${encodeURIComponent(wsDir)}&hint=${encodeURIComponent(artifact.filename)}`;
}

function artifactIdeHref(artifact: ArtifactInfo): string {
  return `${window.location.protocol}//${devHostFromCurrent()}/?file=${encodeURIComponent(workspacePathForArtifact(artifact))}`;
}

function DownloadOnly({ artifact, reason }: { artifact: ArtifactInfo; reason?: string }): JSX.Element {
  return (
    <div className="artifact-fallback">
      <p>{reason ?? "No inline preview for this type."}</p>
      <a href={artifact.url} target="_blank" rel="noopener noreferrer" download={artifact.filename}>
        Download {artifact.filename}
      </a>
    </div>
  );
}

function ImageBody({ artifact }: { artifact: ArtifactInfo }): JSX.Element {
  return (
    <a href={artifact.url} target="_blank" rel="noopener noreferrer">
      <img className="artifact-image" src={artifact.url} alt={artifact.filename} />
    </a>
  );
}

function HtmlBody({ artifact }: { artifact: ArtifactInfo }): JSX.Element {
  // sandbox without allow-same-origin → unique opaque origin, so embedded
  // scripts cannot read the chat origin's cookies/localStorage even though
  // the file itself is served same-origin (CF auth cookie attaches on the
  // network fetch automatically).
  return (
    <iframe
      className="artifact-iframe"
      src={artifact.url}
      sandbox="allow-scripts allow-popups"
      title={artifact.filename}
      loading="lazy"
    />
  );
}

function PdfBody({ artifact }: { artifact: ArtifactInfo }): JSX.Element {
  // Browsers render application/pdf natively in <iframe>; the backend
  // returns the right Content-Type for .pdf so this just works. Unlike
  // HTML, we don't sandbox — PDFs are content, not scripts, and
  // sandboxing breaks the browser's built-in PDF viewer chrome.
  return (
    <iframe
      className="artifact-iframe artifact-iframe-pdf"
      src={artifact.url}
      title={artifact.filename}
      loading="lazy"
    />
  );
}

interface FetchState<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
}

function useFetched<T>(
  url: string,
  fetcher: () => Promise<T>,
): FetchState<T> {
  const [state, setState] = useState<FetchState<T>>({ data: null, error: null, loading: true });
  useEffect(() => {
    let cancelled = false;
    setState({ data: null, error: null, loading: true });
    fetcher()
      .then((data) => {
        if (!cancelled) setState({ data, error: null, loading: false });
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setState({
            data: null,
            error: err instanceof Error ? err.message : String(err),
            loading: false,
          });
        }
      });
    return () => {
      cancelled = true;
    };
    // fetcher identity changes per render; we key on `url` since that's the
    // stable input. The closure captures the current fetcher each time.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [url]);
  return state;
}

function TableBody({ artifact, delimiter }: { artifact: ArtifactInfo; delimiter: string }): JSX.Element {
  const fetcher = useCallback(async () => {
    const r = await fetch(artifact.url, { credentials: "include" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.text();
  }, [artifact.url]);
  const { data, error, loading } = useFetched(artifact.url, fetcher);
  const rows = useMemo(() => (data ? parseDelimited(data, delimiter, MAX_TABLE_ROWS) : []), [data, delimiter]);

  if (loading) return <div className="artifact-loading">Loading…</div>;
  if (error) return <div className="artifact-error">Failed to load: {error}</div>;
  if (rows.length === 0) return <div className="artifact-empty">(empty)</div>;

  const truncated = data ? rows.length >= MAX_TABLE_ROWS : false;
  return <DataTable rows={rows} truncated={truncated} />;
}

function DataTable({ rows, truncated }: { rows: string[][]; truncated: boolean }): JSX.Element {
  const header = rows[0];
  const body = rows.slice(1);
  return (
    <div className="artifact-table-wrap">
      <table className="artifact-table">
        <thead>
          <tr>
            {header.map((h, i) => (
              <th key={i}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {body.map((r, i) => (
            <tr key={i}>
              {r.map((c, j) => (
                <td key={j}>{c}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      <div className="artifact-table-meta">
        {body.length} {body.length === 1 ? "row" : "rows"} × {header.length}{" "}
        {header.length === 1 ? "col" : "cols"}
        {truncated && ` (truncated to ${MAX_TABLE_ROWS} rows)`}
      </div>
    </div>
  );
}

// RFC 4180-ish delimited parser. Handles quoted fields with embedded
// delimiters, newlines, and doubled-quote escapes. Caps at maxRows so a
// pathological CSV doesn't lock the page.
function parseDelimited(text: string, delim: string, maxRows: number): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let cur = "";
  let inQuotes = false;
  let i = 0;
  const n = text.length;
  while (i < n && rows.length < maxRows) {
    const c = text[i];
    if (inQuotes) {
      if (c === '"') {
        if (text[i + 1] === '"') {
          cur += '"';
          i += 2;
          continue;
        }
        inQuotes = false;
        i++;
      } else {
        cur += c;
        i++;
      }
    } else {
      if (c === '"') {
        inQuotes = true;
        i++;
      } else if (c === delim) {
        row.push(cur);
        cur = "";
        i++;
      } else if (c === "\r") {
        i++;
      } else if (c === "\n") {
        row.push(cur);
        rows.push(row);
        row = [];
        cur = "";
        i++;
      } else {
        cur += c;
        i++;
      }
    }
  }
  if (cur !== "" || row.length > 0) {
    row.push(cur);
    rows.push(row);
  }
  return rows;
}

function MermaidBody({ artifact }: { artifact: ArtifactInfo }): JSX.Element {
  const [svg, setSvg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const idRef = useRef<string>(`m-${Math.random().toString(36).slice(2)}`);

  useEffect(() => {
    let cancelled = false;
    setSvg(null);
    setError(null);
    (async () => {
      try {
        const resp = await fetch(artifact.url, { credentials: "include" });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const src = await resp.text();
        const mermaidMod = await import("mermaid");
        const mermaid = mermaidMod.default;
        const isDark =
          typeof window !== "undefined" &&
          window.matchMedia &&
          window.matchMedia("(prefers-color-scheme: dark)").matches;
        mermaid.initialize({
          startOnLoad: false,
          securityLevel: "strict",
          theme: isDark ? "dark" : "default",
        });
        const { svg } = await mermaid.render(idRef.current, src);
        if (!cancelled) setSvg(svg);
      } catch (err: unknown) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : String(err));
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [artifact.url]);

  if (error) return <div className="artifact-error">Mermaid render failed: {error}</div>;
  if (svg === null) return <div className="artifact-loading">Loading diagram…</div>;
  return <div className="artifact-mermaid" dangerouslySetInnerHTML={{ __html: svg }} />;
}

interface SheetData {
  name: string;
  rows: string[][];
}

function XlsxBody({ artifact }: { artifact: ArtifactInfo }): JSX.Element {
  const [sheets, setSheets] = useState<SheetData[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [active, setActive] = useState<number>(0);

  useEffect(() => {
    let cancelled = false;
    setSheets(null);
    setError(null);
    setActive(0);
    (async () => {
      try {
        const resp = await fetch(artifact.url, { credentials: "include" });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const buf = await resp.arrayBuffer();
        const XLSX = await import("xlsx");
        const wb = XLSX.read(buf, { type: "array" });
        const out: SheetData[] = wb.SheetNames.map((name) => {
          const ws = wb.Sheets[name];
          const rows = XLSX.utils.sheet_to_json(ws, {
            header: 1,
            defval: "",
            raw: false,
            blankrows: false,
          }) as unknown[][];
          const stringRows = rows
            .slice(0, MAX_TABLE_ROWS)
            .map((r) => r.map((c) => (c == null ? "" : String(c))));
          return { name, rows: stringRows };
        });
        if (!cancelled) setSheets(out);
      } catch (err: unknown) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : String(err));
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [artifact.url]);

  if (error) return <div className="artifact-error">Failed to parse spreadsheet: {error}</div>;
  if (sheets === null) return <div className="artifact-loading">Loading spreadsheet…</div>;
  if (sheets.length === 0) return <div className="artifact-empty">(no sheets)</div>;

  const current = sheets[active] ?? sheets[0];
  return (
    <div className="artifact-xlsx">
      {sheets.length > 1 && (
        <div className="artifact-xlsx-tabs" role="tablist">
          {sheets.map((s, i) => (
            <button
              type="button"
              key={s.name}
              role="tab"
              aria-selected={i === active}
              className={i === active ? "artifact-xlsx-tab active" : "artifact-xlsx-tab"}
              onClick={() => setActive(i)}
            >
              {s.name}
            </button>
          ))}
        </div>
      )}
      {current.rows.length > 0 ? (
        <DataTable rows={current.rows} truncated={current.rows.length >= MAX_TABLE_ROWS} />
      ) : (
        <div className="artifact-empty">(empty sheet)</div>
      )}
    </div>
  );
}

function XmlBody({ artifact }: { artifact: ArtifactInfo }): JSX.Element {
  const fetcher = useCallback(async () => {
    const r = await fetch(artifact.url, { credentials: "include" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.text();
  }, [artifact.url]);
  const { data, error, loading } = useFetched(artifact.url, fetcher);

  // Indent the XML for readability. Browsers' built-in XML viewer is
  // nicer when available, but it requires loading the doc as a top-level
  // navigation; in an iframe most browsers just show source. So we
  // pretty-print + render in a scrollable <pre>, mirroring JsonBody.
  const pretty = useMemo(() => (data ? prettifyXml(data) : ""), [data]);

  if (loading) return <div className="artifact-loading">Loading…</div>;
  if (error) return <div className="artifact-error">Failed to load: {error}</div>;
  return <pre className="artifact-json">{pretty}</pre>;
}

// Minimal XML pretty-printer — adds line breaks + 2-space indents based
// on tag boundaries. Doesn't validate, just reformats. Falls back to the
// raw source if the input doesn't look like XML.
function prettifyXml(src: string): string {
  if (!src.trim().startsWith("<")) return src;
  const withBreaks = src
    .replace(/>\s*</g, ">\n<")
    .replace(/(<\?[^?]+\?>)\s*/g, "$1\n");
  const lines = withBreaks.split("\n");
  let depth = 0;
  const out: string[] = [];
  for (const raw of lines) {
    const line = raw.trim();
    if (!line) continue;
    const isClose = /^<\/[^>]+>$/.test(line);
    const isSelfClose = /\/>$/.test(line) || /^<\?/.test(line) || /^<!--/.test(line);
    const isOpen = /^<[^/!?][^>]*>$/.test(line) && !isSelfClose;
    if (isClose) depth = Math.max(0, depth - 1);
    out.push("  ".repeat(depth) + line);
    if (isOpen) depth++;
  }
  return out.join("\n");
}

function CodeBody({
  artifact,
  language,
}: {
  artifact: ArtifactInfo;
  language: string;
}): JSX.Element {
  const fetcher = useCallback(async () => {
    const r = await fetch(artifact.url, { credentials: "include" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.text();
  }, [artifact.url]);
  const { data, error, loading } = useFetched(artifact.url, fetcher);

  const dark =
    typeof window !== "undefined" &&
    window.matchMedia &&
    window.matchMedia("(prefers-color-scheme: dark)").matches;
  const codeStyle = dark ? oneDark : oneLight;

  if (loading) return <div className="artifact-loading">Loading…</div>;
  if (error) return <div className="artifact-error">Failed to load: {error}</div>;
  return (
    <div className="artifact-code">
      <SyntaxHighlighter
        language={language}
        style={codeStyle as { [key: string]: React.CSSProperties }}
        showLineNumbers
        customStyle={{
          margin: 0,
          padding: "12px 14px",
          fontSize: "12px",
          background: "transparent",
          maxHeight: "560px",
          overflow: "auto",
        }}
      >
        {data ?? ""}
      </SyntaxHighlighter>
    </div>
  );
}

function JsonBody({ artifact }: { artifact: ArtifactInfo }): JSX.Element {
  const fetcher = useCallback(async () => {
    const r = await fetch(artifact.url, { credentials: "include" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.text();
  }, [artifact.url]);
  const { data, error, loading } = useFetched(artifact.url, fetcher);

  const pretty = useMemo(() => {
    if (!data) return "";
    try {
      return JSON.stringify(JSON.parse(data), null, 2);
    } catch {
      return data;
    }
  }, [data]);

  if (loading) return <div className="artifact-loading">Loading…</div>;
  if (error) return <div className="artifact-error">Failed to load: {error}</div>;
  return <pre className="artifact-json">{pretty}</pre>;
}

const VIDEO_MIME_BY_EXT: Record<string, string> = {
  mp4: "video/mp4",
  mov: "video/quicktime",
  webm: "video/webm",
  mkv: "video/x-matroska",
  avi: "video/x-msvideo",
};
const AUDIO_MIME_BY_EXT: Record<string, string> = {
  mp3: "audio/mpeg",
  wav: "audio/wav",
  flac: "audio/flac",
  ogg: "audio/ogg",
  m4a: "audio/mp4",
};

function VideoBody({ artifact, ext }: { artifact: ArtifactInfo; ext: string }): JSX.Element {
  // .mkv and .avi often won't decode in the browser even though we send a
  // sensible Content-Type; the <video> element falls back to its built-in
  // unsupported-format UI in that case, and the caption row's download
  // button is still available.
  return (
    <video
      className="artifact-video"
      controls
      preload="metadata"
      src={artifact.url}
      data-ext={ext}
    >
      <source src={artifact.url} type={VIDEO_MIME_BY_EXT[ext] ?? "video/mp4"} />
      Your browser doesn't support inline playback of this video format.
    </video>
  );
}

function AudioBody({ artifact, ext }: { artifact: ArtifactInfo; ext: string }): JSX.Element {
  return (
    <audio
      className="artifact-audio"
      controls
      preload="metadata"
      src={artifact.url}
      data-ext={ext}
    >
      <source src={artifact.url} type={AUDIO_MIME_BY_EXT[ext] ?? "audio/mpeg"} />
      Your browser doesn't support inline playback of this audio format.
    </audio>
  );
}

function FontBody({ artifact }: { artifact: ArtifactInfo }): JSX.Element {
  // Each artifact gets a unique @font-face family name so multiple font
  // previews on the same page don't collide. The font is loaded via the
  // FontFace API and added to document.fonts, then the sample text below
  // picks it up by family name.
  const familyRef = useRef<string>(`af-${Math.random().toString(36).slice(2)}`);
  const [loaded, setLoaded] = useState<"loading" | "ok" | "fail">("loading");

  useEffect(() => {
    let cancelled = false;
    setLoaded("loading");
    const family = familyRef.current;
    let face: FontFace | null = null;
    try {
      face = new FontFace(family, `url(${artifact.url})`);
    } catch {
      setLoaded("fail");
      return;
    }
    face.load()
      .then((f) => {
        if (cancelled) return;
        document.fonts.add(f);
        setLoaded("ok");
      })
      .catch(() => {
        if (!cancelled) setLoaded("fail");
      });
    return () => {
      cancelled = true;
      if (face) {
        try {
          document.fonts.delete(face);
        } catch {
          // best-effort cleanup
        }
      }
    };
  }, [artifact.url]);

  if (loaded === "fail") {
    return <div className="artifact-error">Failed to load font.</div>;
  }
  const fontStack = loaded === "ok" ? `'${familyRef.current}', sans-serif` : "sans-serif";
  return (
    <div className="artifact-font" style={{ fontFamily: fontStack }}>
      <div className="artifact-font-row" style={{ fontSize: "10px" }}>
        ABCDEFGHIJKLMNOPQRSTUVWXYZ abcdefghijklmnopqrstuvwxyz 0123456789
      </div>
      <div className="artifact-font-row" style={{ fontSize: "16px" }}>
        The quick brown fox jumps over the lazy dog.
      </div>
      <div className="artifact-font-row" style={{ fontSize: "24px" }}>
        The quick brown fox jumps over the lazy dog.
      </div>
      <div className="artifact-font-row" style={{ fontSize: "36px" }}>
        Aa Bb Cc 123
      </div>
      <div className="artifact-font-row" style={{ fontSize: "56px", lineHeight: 1.1 }}>
        Hamburgefonts
      </div>
      {loaded === "loading" && (
        <div className="artifact-font-loading">Loading font…</div>
      )}
    </div>
  );
}

interface IpynbCell {
  cell_type: string;
  source: string | string[];
  outputs?: IpynbOutput[];
}
interface IpynbOutput {
  output_type: string;
  text?: string | string[];
  data?: Record<string, string | string[]>;
  ename?: string;
  evalue?: string;
  traceback?: string[];
}
interface IpynbDoc {
  cells?: IpynbCell[];
  metadata?: {
    kernelspec?: { language?: string };
    language_info?: { name?: string };
  };
}

function joinSource(s: string | string[] | undefined): string {
  if (!s) return "";
  return Array.isArray(s) ? s.join("") : s;
}

function IpynbBody({ artifact }: { artifact: ArtifactInfo }): JSX.Element {
  const fetcher = useCallback(async () => {
    const r = await fetch(artifact.url, { credentials: "include" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.text();
  }, [artifact.url]);
  const { data, error, loading } = useFetched(artifact.url, fetcher);

  const dark =
    typeof window !== "undefined" &&
    window.matchMedia &&
    window.matchMedia("(prefers-color-scheme: dark)").matches;
  const codeStyle = dark ? oneDark : oneLight;

  const parsed = useMemo<IpynbDoc | null>(() => {
    if (!data) return null;
    try {
      return JSON.parse(data) as IpynbDoc;
    } catch {
      return null;
    }
  }, [data]);

  if (loading) return <div className="artifact-loading">Loading notebook…</div>;
  if (error) return <div className="artifact-error">Failed to load: {error}</div>;
  if (!parsed || !Array.isArray(parsed.cells)) {
    return <div className="artifact-error">Notebook JSON missing cells array.</div>;
  }

  const lang =
    parsed.metadata?.language_info?.name ??
    parsed.metadata?.kernelspec?.language ??
    "python";

  return (
    <div className="artifact-ipynb">
      {parsed.cells.map((cell, idx) => {
        const src = joinSource(cell.source);
        if (cell.cell_type === "markdown") {
          return (
            <div key={idx} className="artifact-ipynb-cell artifact-ipynb-md">
              <pre>{src}</pre>
            </div>
          );
        }
        if (cell.cell_type === "code") {
          return (
            <div key={idx} className="artifact-ipynb-cell artifact-ipynb-code">
              <SyntaxHighlighter
                language={lang}
                style={codeStyle as { [key: string]: React.CSSProperties }}
                showLineNumbers
                customStyle={{
                  margin: 0,
                  padding: "10px 12px",
                  fontSize: "12px",
                  background: "transparent",
                }}
              >
                {src}
              </SyntaxHighlighter>
              {Array.isArray(cell.outputs) && cell.outputs.length > 0 && (
                <div className="artifact-ipynb-outputs">
                  {cell.outputs.map((out, j) => (
                    <IpynbOutputView key={j} output={out} />
                  ))}
                </div>
              )}
            </div>
          );
        }
        return (
          <div key={idx} className="artifact-ipynb-cell artifact-ipynb-raw">
            <pre>{src}</pre>
          </div>
        );
      })}
    </div>
  );
}

function stringifyCell(v: unknown): string {
  if (v == null) return "";
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  if (typeof v === "bigint") return v.toString();
  if (v instanceof Date) return v.toISOString();
  // Arrow returns proxied row objects; their values can be arrays / nested
  // structs / Decimal. Best-effort JSON round-trip; fall back to String().
  try {
    return JSON.stringify(v);
  } catch {
    return String(v);
  }
}

function tabularizeRows(
  rows: Array<Record<string, unknown>>,
  preferredColumns?: string[],
): { rows: string[][]; truncated: boolean } {
  const limit = MAX_TABLE_ROWS;
  const truncated = rows.length > limit;
  const sliced = truncated ? rows.slice(0, limit) : rows;
  if (sliced.length === 0) return { rows: [], truncated };
  const cols =
    preferredColumns && preferredColumns.length > 0
      ? preferredColumns
      : Object.keys(sliced[0]);
  const out: string[][] = [cols];
  for (const r of sliced) {
    out.push(cols.map((c) => stringifyCell(r[c])));
  }
  return { rows: out, truncated };
}

function ParquetBody({ artifact }: { artifact: ArtifactInfo }): JSX.Element {
  const [state, setState] = useState<{
    rows: string[][];
    truncated: boolean;
    rowCount: number;
    error: string | null;
    loading: boolean;
  }>({ rows: [], truncated: false, rowCount: 0, error: null, loading: true });

  useEffect(() => {
    let cancelled = false;
    setState({ rows: [], truncated: false, rowCount: 0, error: null, loading: true });
    (async () => {
      try {
        const r = await fetch(artifact.url, { credentials: "include" });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const buf = await r.arrayBuffer();
        const hyparquet = await import("hyparquet");
        const collected: Array<Record<string, unknown>> = [];
        await hyparquet.parquetRead({
          file: buf,
          rowFormat: "object",
          onComplete: (data: unknown) => {
            if (Array.isArray(data)) {
              for (const row of data) {
                collected.push(row as Record<string, unknown>);
              }
            }
          },
        });
        if (cancelled) return;
        const { rows, truncated } = tabularizeRows(collected);
        setState({
          rows,
          truncated,
          rowCount: collected.length,
          error: null,
          loading: false,
        });
      } catch (err: unknown) {
        if (!cancelled) {
          setState({
            rows: [],
            truncated: false,
            rowCount: 0,
            error: err instanceof Error ? err.message : String(err),
            loading: false,
          });
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [artifact.url]);

  if (state.loading) return <div className="artifact-loading">Loading parquet…</div>;
  if (state.error) return <div className="artifact-error">Failed to read parquet: {state.error}</div>;
  if (state.rows.length === 0) return <div className="artifact-empty">(empty)</div>;
  return <DataTable rows={state.rows} truncated={state.truncated} />;
}

function ArrowBody({ artifact }: { artifact: ArtifactInfo }): JSX.Element {
  const [state, setState] = useState<{
    rows: string[][];
    truncated: boolean;
    error: string | null;
    loading: boolean;
  }>({ rows: [], truncated: false, error: null, loading: true });

  useEffect(() => {
    let cancelled = false;
    setState({ rows: [], truncated: false, error: null, loading: true });
    (async () => {
      try {
        const r = await fetch(artifact.url, { credentials: "include" });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const buf = await r.arrayBuffer();
        const arrow = await import("apache-arrow");
        const table = arrow.tableFromIPC(new Uint8Array(buf));
        if (cancelled) return;
        const cols = table.schema.fields.map((f) => f.name);
        // toArray() yields proxied row objects keyed by column name; we
        // pull values via the column list to preserve column order.
        const rowObjects = table.toArray() as Array<Record<string, unknown>>;
        const { rows, truncated } = tabularizeRows(rowObjects, cols);
        setState({ rows, truncated, error: null, loading: false });
      } catch (err: unknown) {
        if (!cancelled) {
          setState({
            rows: [],
            truncated: false,
            error: err instanceof Error ? err.message : String(err),
            loading: false,
          });
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [artifact.url]);

  if (state.loading) return <div className="artifact-loading">Loading arrow…</div>;
  if (state.error) return <div className="artifact-error">Failed to read arrow: {state.error}</div>;
  if (state.rows.length === 0) return <div className="artifact-empty">(empty)</div>;
  return <DataTable rows={state.rows} truncated={state.truncated} />;
}

function IpynbOutputView({ output }: { output: IpynbOutput }): JSX.Element {
  if (output.output_type === "stream") {
    return <pre className="artifact-ipynb-stream">{joinSource(output.text)}</pre>;
  }
  if (output.output_type === "error") {
    const msg = `${output.ename ?? "Error"}: ${output.evalue ?? ""}`;
    return (
      <pre className="artifact-ipynb-error">
        {msg}
        {Array.isArray(output.traceback) && output.traceback.length > 0 && (
          <>{"\n" + output.traceback.join("\n")}</>
        )}
      </pre>
    );
  }
  if (output.output_type === "display_data" || output.output_type === "execute_result") {
    const data = output.data ?? {};
    const png = data["image/png"];
    if (png) {
      const b64 = Array.isArray(png) ? png.join("") : png;
      return (
        <img
          className="artifact-ipynb-image"
          src={`data:image/png;base64,${b64}`}
          alt="output"
        />
      );
    }
    const svg = data["image/svg+xml"];
    if (svg) {
      const txt = Array.isArray(svg) ? svg.join("") : svg;
      return (
        <div
          className="artifact-ipynb-svg"
          dangerouslySetInnerHTML={{ __html: txt }}
        />
      );
    }
    const html = data["text/html"];
    if (html) {
      const txt = Array.isArray(html) ? html.join("") : html;
      return (
        <iframe
          className="artifact-ipynb-html"
          sandbox="allow-scripts allow-popups"
          srcDoc={txt}
          title="notebook output"
        />
      );
    }
    const text = data["text/plain"];
    if (text) {
      const txt = Array.isArray(text) ? text.join("") : text;
      return <pre className="artifact-ipynb-text">{txt}</pre>;
    }
  }
  return <pre className="artifact-ipynb-text">(unrenderable output)</pre>;
}
