import { loader } from "@monaco-editor/react";
import { useEffect, useState } from "react";
import { useWorkspace } from "../workspace";

// Flat list of all LSP diagnostics across all open Monaco models, owner=pyright.
// Subscribes via monaco.editor.onDidChangeMarkers, so the panel re-renders
// whenever pyright publishes new diagnostics — no polling.

type Row = {
  uri: string;
  path: string;          // workspace-relative path
  line: number;          // 1-based
  column: number;        // 1-based
  severity: number;      // monaco MarkerSeverity (1=hint, 2=info, 4=warning, 8=error)
  message: string;
  source?: string;
};

const SEV_LABEL: Record<number, string> = {
  8: "error",
  4: "warning",
  2: "info",
  1: "hint",
};

const WORKSPACE_URI_PREFIX = "file:///workspace/";

export function ProblemsPanel() {
  const ws = useWorkspace();
  const [rows, setRows] = useState<Row[]>([]);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    let disposed = false;
    let unsubscribe: { dispose: () => void } | null = null;
    (async () => {
      const monaco = await loader.init();
      if (disposed) return;
      setReady(true);
      const refresh = () => {
        const markers = monaco.editor.getModelMarkers({ owner: "pyright" });
        const next: Row[] = markers
          .filter((m) => m.resource.toString().startsWith(WORKSPACE_URI_PREFIX))
          .map((m) => {
            const uri = m.resource.toString();
            const path = uri.substring(WORKSPACE_URI_PREFIX.length);
            return {
              uri,
              path,
              line: m.startLineNumber,
              column: m.startColumn,
              severity: m.severity,
              message: m.message,
              source: m.source,
            };
          })
          .sort((a, b) => {
            if (a.severity !== b.severity) return b.severity - a.severity;
            if (a.path !== b.path) return a.path.localeCompare(b.path);
            return a.line - b.line;
          });
        setRows(next);
      };
      refresh();
      unsubscribe = monaco.editor.onDidChangeMarkers(refresh);
    })();
    return () => {
      disposed = true;
      unsubscribe?.dispose();
    };
  }, []);

  if (!ready) {
    return <div className="panel-empty">loading monaco…</div>;
  }
  if (rows.length === 0) {
    return (
      <div className="panel-empty">
        No problems detected
        {ws.lspStatus === "ready" ? "." : ` (LSP ${ws.lspStatus}).`}
      </div>
    );
  }
  return (
    <div className="problems">
      {rows.map((r, i) => (
        <div
          key={`${r.uri}:${r.line}:${r.column}:${i}`}
          className={`problem-row sev-${SEV_LABEL[r.severity] ?? "info"}`}
          onClick={() => {
            void ws.openFile(r.path);
            // After openFile resolves the panel exists; navigation is
            // best-effort via Monaco's reveal API in a microtask so the
            // newly-opened model has a chance to mount.
            setTimeout(async () => {
              const monaco = await loader.init();
              const model = monaco.editor.getModel(monaco.Uri.parse(r.uri));
              if (!model) return;
              for (const editor of monaco.editor.getEditors()) {
                if (editor.getModel() === model) {
                  editor.revealPositionInCenter({ lineNumber: r.line, column: r.column });
                  editor.setPosition({ lineNumber: r.line, column: r.column });
                  editor.focus();
                  break;
                }
              }
            }, 50);
          }}
          title={r.message}
        >
          <span className={`problem-severity sev-${SEV_LABEL[r.severity] ?? "info"}`}>
            {SEV_LABEL[r.severity] ?? "info"}
          </span>
          <span className="problem-message">{r.message}</span>
          <span className="problem-location">{r.path}:{r.line}</span>
        </div>
      ))}
    </div>
  );
}
