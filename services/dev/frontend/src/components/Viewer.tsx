import { useEffect, useMemo, useRef, useState } from "react";
// xlsx is dynamically imported in XlsxView only — keeps the SheetJS bundle
// (~300KB) out of the main chunk for users who never open a spreadsheet.
type XlsxMod = typeof import("xlsx");

export type ViewerKind =
  | "image"
  | "pdf"
  | "audio"
  | "video"
  | "csv"
  | "xlsx"
  | "binary"
  | "text";

const EXT_MAP: Record<string, ViewerKind> = {
  png: "image", jpg: "image", jpeg: "image", gif: "image", webp: "image",
  bmp: "image", ico: "image", avif: "image", svg: "image",
  heic: "image", heif: "image", tif: "image", tiff: "image",
  pdf: "pdf",
  mp3: "audio", ogg: "audio", oga: "audio", wav: "audio", flac: "audio",
  m4a: "audio", aac: "audio", opus: "audio", weba: "audio",
  mp4: "video", webm: "video", mov: "video", mkv: "video", m4v: "video", avi: "video",
  csv: "csv", tsv: "csv",
  xlsx: "xlsx", xls: "xlsx", xlsm: "xlsx", ods: "xlsx",
  // Office documents — no in-IDE viewer; route to BinaryView so the user
  // gets a download button instead of raw bytes pasted into Monaco.
  doc: "binary", docx: "binary", dot: "binary", dotx: "binary",
  ppt: "binary", pptx: "binary", pps: "binary", ppsx: "binary",
  odt: "binary", odp: "binary", odg: "binary", odf: "binary",
  pages: "binary", numbers: "binary", key: "binary",
  rtf: "binary", epub: "binary", mobi: "binary",
  // Archives
  zip: "binary", tar: "binary", gz: "binary", tgz: "binary", bz2: "binary",
  tbz2: "binary", xz: "binary", txz: "binary", "7z": "binary", rar: "binary",
  zst: "binary", lz: "binary", lzma: "binary", lzo: "binary", cpio: "binary",
  ar: "binary",
  // Executables / native libs
  exe: "binary", dll: "binary", so: "binary", dylib: "binary", bin: "binary",
  com: "binary", msi: "binary", app: "binary",
  // Object / build artifacts
  o: "binary", obj: "binary", a: "binary", lib: "binary", class: "binary",
  pyc: "binary", pyo: "binary", pyd: "binary", wasm: "binary",
  jar: "binary", war: "binary", ear: "binary",
  // Disk / VM images
  iso: "binary", img: "binary", dmg: "binary", vhd: "binary", vhdx: "binary",
  vmdk: "binary", qcow2: "binary", ova: "binary",
  // OS packages
  deb: "binary", rpm: "binary", pkg: "binary", apk: "binary", snap: "binary",
  flatpak: "binary", whl: "binary",
  // Fonts
  ttf: "binary", otf: "binary", woff: "binary", woff2: "binary", eot: "binary",
  // Designs / project files
  psd: "binary", ai: "binary", sketch: "binary", fig: "binary", blend: "binary",
  xcf: "binary",
  // Databases
  db: "binary", sqlite: "binary", sqlite3: "binary", mdb: "binary", accdb: "binary",
};

export function viewerKindFor(path: string): ViewerKind {
  const name = path.split("/").pop() ?? "";
  const ext = name.includes(".") ? name.split(".").pop()!.toLowerCase() : "";
  return EXT_MAP[ext] ?? "text";
}

function formatBytes(n: number | null): string {
  if (n == null || !isFinite(n)) return "—";
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v >= 10 ? 0 : 1)} ${units[i]}`;
}

function rawUrl(path: string, bust: number): string {
  return `/api/files/raw?path=${encodeURIComponent(path)}&_=${bust}`;
}

type Props = { path: string; theme: "dark" | "light" };

export function Viewer({ path, theme }: Props) {
  const kind = useMemo(() => viewerKindFor(path), [path]);
  const [bust, setBust] = useState(() => Date.now());
  const url = rawUrl(path, bust);
  const reload = () => setBust(Date.now());

  return (
    <div className={`viewer viewer-${kind}`} data-theme={theme}>
      <div className="viewer-bar">
        <span className="viewer-bar-path">{path}</span>
        <span className="viewer-bar-kind">{kind}</span>
        <button className="viewer-bar-btn" onClick={reload} title="Reload">↻</button>
        <a className="viewer-bar-btn" href={url} download={path.split("/").pop()} title="Download">⤓</a>
      </div>
      <div className="viewer-body">
        {kind === "image" && <ImageView url={url} />}
        {kind === "pdf" && <PdfView url={url} />}
        {kind === "audio" && <AudioView url={url} />}
        {kind === "video" && <VideoView url={url} />}
        {kind === "csv" && <CsvView url={url} />}
        {kind === "xlsx" && <XlsxView url={url} />}
        {kind === "binary" && <BinaryView url={url} path={path} />}
      </div>
    </div>
  );
}

function BinaryView({ url, path }: { url: string; path: string }) {
  const [size, setSize] = useState<number | null>(null);
  const [mime, setMime] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const name = path.split("/").pop() ?? path;
  const ext = name.includes(".") ? name.split(".").pop()!.toLowerCase() : "";

  useEffect(() => {
    let cancelled = false;
    setSize(null); setMime(null); setError(null);
    (async () => {
      try {
        const r = await fetch(url, { method: "HEAD" });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        if (cancelled) return;
        const len = r.headers.get("content-length");
        setSize(len != null ? parseInt(len, 10) : null);
        setMime(r.headers.get("content-type"));
      } catch (err: any) {
        if (!cancelled) setError(err?.message ?? String(err));
      }
    })();
    return () => { cancelled = true; };
  }, [url]);

  return (
    <div className="viewer-binary">
      <div className="viewer-binary-icon">📦</div>
      <div className="viewer-binary-title">Preview unavailable</div>
      <div className="viewer-binary-sub">
        Binary file — content can't be shown as text.
      </div>
      <dl className="viewer-binary-meta">
        <dt>Name</dt><dd>{name}</dd>
        {ext && (<><dt>Type</dt><dd>{ext.toUpperCase()}{mime ? ` · ${mime}` : ""}</dd></>)}
        <dt>Size</dt><dd>{error ? `error: ${error}` : formatBytes(size)}</dd>
      </dl>
      <a className="viewer-binary-download" href={url} download={name}>
        ⤓ Download
      </a>
    </div>
  );
}

function ImageView({ url }: { url: string }) {
  return (
    <div className="viewer-image-wrap">
      <img className="viewer-image" src={url} alt="" />
    </div>
  );
}

function PdfView({ url }: { url: string }) {
  return <iframe className="viewer-pdf" src={url} title="pdf" />;
}

function AudioView({ url }: { url: string }) {
  return (
    <div className="viewer-media">
      <audio className="viewer-audio" controls src={url} />
    </div>
  );
}

function VideoView({ url }: { url: string }) {
  return (
    <div className="viewer-media">
      <video className="viewer-video" controls src={url} />
    </div>
  );
}

// Minimal RFC-4180-ish CSV/TSV parser. Handles quoted fields with embedded
// commas, escaped quotes (""), and CRLF. Caps at 10k rows in the DOM —
// anything larger is paginated.
function parseCsv(text: string, delimiter: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let inQuotes = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (inQuotes) {
      if (c === '"') {
        if (text[i + 1] === '"') { field += '"'; i++; }
        else { inQuotes = false; }
      } else {
        field += c;
      }
    } else {
      if (c === '"' && field === "") { inQuotes = true; }
      else if (c === delimiter) { row.push(field); field = ""; }
      else if (c === "\n") { row.push(field); rows.push(row); row = []; field = ""; }
      else if (c === "\r") { /* swallow; \n follows */ }
      else { field += c; }
    }
  }
  if (field.length > 0 || row.length > 0) {
    row.push(field);
    rows.push(row);
  }
  return rows;
}

function CsvView({ url }: { url: string }) {
  const [rows, setRows] = useState<string[][] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(0);
  const ROWS_PER_PAGE = 500;

  useEffect(() => {
    let cancelled = false;
    setRows(null); setError(null); setPage(0);
    (async () => {
      try {
        const r = await fetch(url);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const text = await r.text();
        const delimiter = url.toLowerCase().includes(".tsv") ? "\t" : ",";
        const parsed = parseCsv(text, delimiter);
        if (!cancelled) setRows(parsed);
      } catch (err: any) {
        if (!cancelled) setError(err?.message ?? String(err));
      }
    })();
    return () => { cancelled = true; };
  }, [url]);

  if (error) return <div className="viewer-error">failed to load: {error}</div>;
  if (!rows) return <div className="viewer-loading">loading…</div>;
  if (rows.length === 0) return <div className="viewer-empty">empty file</div>;

  const total = rows.length;
  const start = page * ROWS_PER_PAGE;
  const end = Math.min(start + ROWS_PER_PAGE, total);
  const header = rows[0];
  const bodyAll = rows.slice(1);
  const slice = bodyAll.slice(start, end);
  const pageCount = Math.max(1, Math.ceil(bodyAll.length / ROWS_PER_PAGE));

  return (
    <div className="viewer-table-wrap">
      <div className="viewer-table-bar">
        <span>{bodyAll.length.toLocaleString()} rows · {header.length} cols</span>
        {pageCount > 1 && (
          <span className="viewer-pager">
            <button disabled={page === 0} onClick={() => setPage((p) => p - 1)}>‹</button>
            <span>page {page + 1} / {pageCount}</span>
            <button disabled={page === pageCount - 1} onClick={() => setPage((p) => p + 1)}>›</button>
          </span>
        )}
      </div>
      <div className="viewer-table-scroll">
        <table className="viewer-table">
          <thead>
            <tr>
              <th className="viewer-table-rownum">#</th>
              {header.map((h, i) => <th key={i}>{h || `col ${i + 1}`}</th>)}
            </tr>
          </thead>
          <tbody>
            {slice.map((r, ri) => (
              <tr key={ri}>
                <td className="viewer-table-rownum">{start + ri + 2}</td>
                {header.map((_, ci) => <td key={ci}>{r[ci] ?? ""}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function XlsxView({ url }: { url: string }) {
  const [xlsx, setXlsx] = useState<XlsxMod | null>(null);
  const [book, setBook] = useState<ReturnType<XlsxMod["read"]> | null>(null);
  const [sheet, setSheet] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(0);
  const tableRef = useRef<HTMLDivElement>(null);
  const ROWS_PER_PAGE = 500;

  useEffect(() => {
    let cancelled = false;
    setBook(null); setSheet(null); setError(null); setPage(0);
    (async () => {
      try {
        const mod = xlsx ?? (await import("xlsx"));
        if (cancelled) return;
        if (!xlsx) setXlsx(mod);
        const r = await fetch(url);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const buf = await r.arrayBuffer();
        const wb = mod.read(buf, { type: "array" });
        if (cancelled) return;
        setBook(wb);
        setSheet(wb.SheetNames[0] ?? null);
      } catch (err: any) {
        if (!cancelled) setError(err?.message ?? String(err));
      }
    })();
    return () => { cancelled = true; };
  }, [url]);

  if (error) return <div className="viewer-error">failed to load: {error}</div>;
  if (!book || !sheet || !xlsx) return <div className="viewer-loading">loading…</div>;

  const ws = book.Sheets[sheet];
  const rows: any[][] = xlsx.utils.sheet_to_json(ws, { header: 1, defval: "", blankrows: false });
  const total = rows.length;
  const colCount = rows.reduce((m, r) => Math.max(m, r.length), 0);
  const start = page * ROWS_PER_PAGE;
  const end = Math.min(start + ROWS_PER_PAGE, total);
  const slice = rows.slice(start, end);
  const pageCount = Math.max(1, Math.ceil(total / ROWS_PER_PAGE));

  return (
    <div className="viewer-table-wrap">
      <div className="viewer-table-bar">
        <span className="viewer-sheet-tabs">
          {book.SheetNames.map((n) => (
            <button
              key={n}
              className={`viewer-sheet-tab${n === sheet ? " active" : ""}`}
              onClick={() => { setSheet(n); setPage(0); tableRef.current?.scrollTo(0, 0); }}
            >
              {n}
            </button>
          ))}
        </span>
        <span>{total.toLocaleString()} rows · {colCount} cols</span>
        {pageCount > 1 && (
          <span className="viewer-pager">
            <button disabled={page === 0} onClick={() => setPage((p) => p - 1)}>‹</button>
            <span>page {page + 1} / {pageCount}</span>
            <button disabled={page === pageCount - 1} onClick={() => setPage((p) => p + 1)}>›</button>
          </span>
        )}
      </div>
      <div className="viewer-table-scroll" ref={tableRef}>
        <table className="viewer-table">
          <tbody>
            {slice.map((r, ri) => (
              <tr key={ri}>
                <td className="viewer-table-rownum">{start + ri + 1}</td>
                {Array.from({ length: colCount }).map((_, ci) => (
                  <td key={ci}>{r[ci] !== undefined && r[ci] !== null ? String(r[ci]) : ""}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
