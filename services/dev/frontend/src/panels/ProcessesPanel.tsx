import { useCallback, useEffect, useState } from "react";
import { api, JobInfo } from "../api";

// Lists active + recently-finished jobs (the dev backend keeps each for
// 15 min after exit). Polls every 2 s while the panel is mounted — cheap
// compared to a /api/jobs SSE channel we don't have yet. Click a row to
// kill (running jobs) or no-op (done/killed/error). Future: click-to-
// attach to a job's SSE stream and pipe lines into the Output panel.

type Status = JobInfo["status"];

const STATUS_BADGE: Record<Status, string> = {
  running: "running",
  done: "done",
  killed: "killed",
  error: "error",
};

function fmtArgv(argv: string[]): string {
  // argv is the full `docker exec ... <interpreter> <path> ...` envelope.
  // Trim everything up to the first /workspace path so the row is readable.
  const i = argv.findIndex((a) => a === "/workspace" || a.startsWith("/workspace/"));
  if (i >= 0) return argv.slice(i).join(" ");
  // Fallback: show the trailing 3 args.
  return argv.slice(-3).join(" ");
}

function fmtAge(now: number, then: number): string {
  const s = Math.max(0, Math.floor(now - then));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h`;
}

export function ProcessesPanel() {
  const [jobs, setJobs] = useState<JobInfo[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const r = await api.jobs();
      setJobs(r.jobs);
      setErr(null);
    } catch (e: any) {
      setErr(e?.message ?? String(e));
    }
  }, []);

  useEffect(() => {
    void refresh();
    const t = window.setInterval(refresh, 2000);
    return () => window.clearInterval(t);
  }, [refresh]);

  const onKill = useCallback(async (j: JobInfo) => {
    if (j.status !== "running") return;
    setBusy(j.job_id);
    try {
      await api.killJob(j.job_id);
      void refresh();
    } finally {
      setBusy(null);
    }
  }, [refresh]);

  const now = Date.now() / 1000;
  return (
    <div className="processes">
      <div className="processes-header">
        <span>{jobs.length} job{jobs.length === 1 ? "" : "s"}</span>
        <button className="btn btn-icon-mini" onClick={refresh} title="Refresh">↻</button>
      </div>
      {err && <div className="processes-error">{err}</div>}
      {jobs.length === 0 && !err && (
        <div className="panel-empty">No jobs in the last 15 minutes. Press Cmd/Ctrl+Enter to run the active file.</div>
      )}
      <div className="processes-list">
        {jobs.map((j) => (
          <div key={j.job_id} className={`process-row status-${j.status}`}>
            <span className={`process-status status-${j.status}`}>{STATUS_BADGE[j.status]}</span>
            <span className="process-argv" title={j.argv.join(" ")}>{fmtArgv(j.argv)}</span>
            <span className="process-meta">
              {fmtAge(now, j.started_at)}
              {j.exit_code !== null && j.exit_code !== undefined ? ` · exit ${j.exit_code}` : ""}
            </span>
            {j.status === "running" ? (
              <button
                className="btn btn-danger btn-sm"
                onClick={() => onKill(j)}
                disabled={busy === j.job_id}
                title="Kill (SIGTERM, then SIGKILL after 3s)"
              >
                {busy === j.job_id ? "…" : "Kill"}
              </button>
            ) : (
              <span className="process-spacer" />
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
