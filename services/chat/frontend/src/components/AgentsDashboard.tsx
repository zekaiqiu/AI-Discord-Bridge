import { useCallback, useEffect, useState } from "react";
import {
  Agent,
  AgentDetail,
  AgentList,
  Verbosity,
  endAgent,
  getAgentDetail,
  getAgents,
  killAgent,
  resumeAgent,
  setAgentVerbose,
  spawnProject,
  spawnTask,
  stopAgent,
} from "../api";

const POLL_MS = 5000;
const VERBOSITY_OPTIONS: Verbosity[] = ["quiet", "normal", "verbose", "firehose"];

export interface AgentsDashboardProps {
  onClose: () => void;
  visible: boolean;
}

export function AgentsDashboard({ onClose, visible }: AgentsDashboardProps): JSX.Element {
  const [list, setList] = useState<AgentList | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<AgentDetail | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [showTaskForm, setShowTaskForm] = useState<boolean>(false);
  const [showProjectForm, setShowProjectForm] = useState<boolean>(false);
  const [taskPrompt, setTaskPrompt] = useState<string>("");
  const [taskVerbosity, setTaskVerbosity] = useState<Verbosity>("normal");
  const [projectBrief, setProjectBrief] = useState<string>("");
  const [taskError, setTaskError] = useState<string | null>(null);
  const [projectError, setProjectError] = useState<string | null>(null);
  const [taskSubmitting, setTaskSubmitting] = useState<boolean>(false);
  const [projectSubmitting, setProjectSubmitting] = useState<boolean>(false);

  const refresh = useCallback(async () => {
    try {
      const data = await getAgents();
      setList(data);
      setListError(null);
    } catch (err) {
      setListError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    if (!visible) return;
    refresh();
    const t = window.setInterval(refresh, POLL_MS);
    return () => window.clearInterval(t);
  }, [visible, refresh]);

  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      setDetailError(null);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const d = await getAgentDetail(selectedId);
        if (!cancelled) {
          setDetail(d);
          setDetailError(null);
        }
      } catch (err) {
        if (!cancelled) {
          setDetailError(err instanceof Error ? err.message : String(err));
          setDetail(null);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [selectedId, list]);

  const onSubmitTask = useCallback(async () => {
    const trimmed = taskPrompt.trim();
    if (!trimmed) return;
    setTaskSubmitting(true);
    setTaskError(null);
    try {
      await spawnTask(trimmed, taskVerbosity);
      setTaskPrompt("");
      setShowTaskForm(false);
      await refresh();
    } catch (err) {
      setTaskError(err instanceof Error ? err.message : String(err));
    } finally {
      setTaskSubmitting(false);
    }
  }, [taskPrompt, taskVerbosity, refresh]);

  const onSubmitProject = useCallback(async () => {
    const trimmed = projectBrief.trim();
    if (!trimmed) return;
    setProjectSubmitting(true);
    setProjectError(null);
    try {
      await spawnProject(trimmed);
      setProjectBrief("");
      setShowProjectForm(false);
      await refresh();
    } catch (err) {
      setProjectError(err instanceof Error ? err.message : String(err));
    } finally {
      setProjectSubmitting(false);
    }
  }, [projectBrief, refresh]);

  const onAction = useCallback(
    async (agentId: string, action: "stop" | "kill" | "resume" | "end") => {
      try {
        if (action === "stop") await stopAgent(agentId);
        else if (action === "kill") await killAgent(agentId);
        else if (action === "resume") await resumeAgent(agentId);
        else if (action === "end") await endAgent(agentId);
        await refresh();
      } catch (err) {
        setListError(err instanceof Error ? err.message : String(err));
      }
    },
    [refresh],
  );

  const onChangeVerbose = useCallback(
    async (agentId: string, level: Verbosity) => {
      try {
        await setAgentVerbose(agentId, level);
      } catch (err) {
        setListError(err instanceof Error ? err.message : String(err));
      }
    },
    [],
  );

  return (
    <div className="dashboard">
      <header className="dashboard-header">
        <button
          type="button"
          className="dashboard-back"
          onClick={onClose}
          aria-label="Close dashboard"
          title="Close"
        >
          ‹
        </button>
        <h1 className="dashboard-title">Agents</h1>
      </header>

      <div className="dashboard-body">
        <section className="dashboard-section">
          <div className="spawn-buttons">
            <button
              type="button"
              className="spawn-toggle"
              onClick={() => setShowTaskForm((v) => !v)}
            >
              {showTaskForm ? "− Spawn Task" : "+ Spawn Task"}
            </button>
            <button
              type="button"
              className="spawn-toggle"
              onClick={() => setShowProjectForm((v) => !v)}
            >
              {showProjectForm ? "− Spawn Project" : "+ Spawn Project"}
            </button>
          </div>

          {showTaskForm && (
            <div className="spawn-form">
              <textarea
                className="spawn-textarea"
                placeholder="task prompt"
                value={taskPrompt}
                onChange={(e) => setTaskPrompt(e.target.value)}
                rows={4}
              />
              <div className="spawn-form-row">
                <label className="spawn-label">
                  Verbosity:
                  <select
                    value={taskVerbosity}
                    onChange={(e) => setTaskVerbosity(e.target.value as Verbosity)}
                  >
                    {VERBOSITY_OPTIONS.map((v) => (
                      <option key={v} value={v}>
                        {v}
                      </option>
                    ))}
                  </select>
                </label>
                <button
                  type="button"
                  className="spawn-submit"
                  onClick={onSubmitTask}
                  disabled={taskSubmitting || !taskPrompt.trim()}
                >
                  {taskSubmitting ? "Spawning…" : "Spawn"}
                </button>
              </div>
              {taskError && <div className="spawn-error">{taskError}</div>}
            </div>
          )}

          {showProjectForm && (
            <div className="spawn-form">
              <textarea
                className="spawn-textarea"
                placeholder="project brief"
                value={projectBrief}
                onChange={(e) => setProjectBrief(e.target.value)}
                rows={6}
              />
              <div className="spawn-form-row">
                <button
                  type="button"
                  className="spawn-submit"
                  onClick={onSubmitProject}
                  disabled={projectSubmitting || !projectBrief.trim()}
                >
                  {projectSubmitting ? "Spawning…" : "Spawn"}
                </button>
              </div>
              {projectError && <div className="spawn-error">{projectError}</div>}
            </div>
          )}
        </section>

        {listError && <div className="dashboard-error">{listError}</div>}

        <section className="dashboard-section">
          <h2 className="dashboard-section-title">Active</h2>
          <AgentTable
            agents={list?.active ?? []}
            isActive
            selectedId={selectedId}
            onSelect={setSelectedId}
            onAction={onAction}
            onChangeVerbose={onChangeVerbose}
          />
        </section>

        <section className="dashboard-section">
          <h2 className="dashboard-section-title">Archived</h2>
          <AgentTable
            agents={list?.archived ?? []}
            isActive={false}
            selectedId={selectedId}
            onSelect={setSelectedId}
            onAction={onAction}
            onChangeVerbose={onChangeVerbose}
          />
        </section>

        {selectedId && (
          <section className="dashboard-section agent-detail">
            <div className="agent-detail-header">
              <h2 className="dashboard-section-title">Detail</h2>
              <button
                type="button"
                className="dashboard-back"
                onClick={() => setSelectedId(null)}
                aria-label="Close detail"
                title="Close detail"
              >
                ✕
              </button>
            </div>
            {detailError && <div className="dashboard-error">{detailError}</div>}
            {detail && <AgentDetailPanel detail={detail} />}
          </section>
        )}
      </div>
    </div>
  );
}

interface AgentTableProps {
  agents: Agent[];
  isActive: boolean;
  selectedId: string | null;
  onSelect: (id: string) => void;
  onAction: (id: string, action: "stop" | "kill" | "resume" | "end") => void;
  onChangeVerbose: (id: string, level: Verbosity) => void;
}

function AgentTable(props: AgentTableProps): JSX.Element {
  const { agents, isActive, selectedId, onSelect, onAction, onChangeVerbose } = props;
  if (agents.length === 0) {
    return <div className="dashboard-empty">None.</div>;
  }
  return (
    <div className="agent-table-wrap">
      <table className="agent-table">
        <thead>
          <tr>
            <th>Handle</th>
            <th>ID</th>
            <th>Kind</th>
            <th>Status</th>
            <th>Last Artifact</th>
            <th>Created</th>
            {isActive && <th>Actions</th>}
          </tr>
        </thead>
        <tbody>
          {agents.map((a) => (
            <tr
              key={a.id}
              className={`agent-row ${selectedId === a.id ? "is-selected" : ""}`}
              onClick={() => onSelect(a.id)}
            >
              <td>{a.handle ?? "—"}</td>
              <td className="mono">{shortId(a.id)}</td>
              <td>{a.kind}</td>
              <td>{a.status}</td>
              <td className="truncate" title={a.last_artifact ?? ""}>
                {a.last_artifact ? truncatePath(a.last_artifact) : "—"}
              </td>
              <td>{a.created_at ? formatTimestamp(a.created_at) : "—"}</td>
              {isActive && (
                <td className="agent-actions" onClick={(e) => e.stopPropagation()}>
                  <button type="button" onClick={() => onAction(a.id, "stop")}>
                    Stop
                  </button>
                  <button type="button" onClick={() => onAction(a.id, "kill")}>
                    Kill
                  </button>
                  <button type="button" onClick={() => onAction(a.id, "resume")}>
                    Resume
                  </button>
                  <button type="button" onClick={() => onAction(a.id, "end")}>
                    End
                  </button>
                  {a.kind === "task" && (
                    <select
                      defaultValue=""
                      onChange={(e) => {
                        const v = e.target.value as Verbosity | "";
                        if (v) onChangeVerbose(a.id, v);
                      }}
                      title="Set verbosity"
                    >
                      <option value="">Verbose…</option>
                      {VERBOSITY_OPTIONS.map((v) => (
                        <option key={v} value={v}>
                          {v}
                        </option>
                      ))}
                    </select>
                  )}
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

interface AgentDetailPanelProps {
  detail: AgentDetail;
}

function AgentDetailPanel({ detail }: AgentDetailPanelProps): JSX.Element {
  return (
    <div className="agent-detail-panel">
      <dl className="agent-detail-fields">
        <dt>ID</dt>
        <dd className="mono">{detail.id}</dd>
        <dt>Kind</dt>
        <dd>{detail.kind}</dd>
        <dt>Handle</dt>
        <dd>{detail.handle ?? "—"}</dd>
        <dt>Status</dt>
        <dd>{detail.status}</dd>
        <dt>Working dir</dt>
        <dd className="mono truncate">{detail.working_dir ?? "—"}</dd>
        <dt>Last artifact</dt>
        <dd className="mono truncate">{detail.last_artifact ?? "—"}</dd>
        {detail.kind === "project" && (
          <>
            <dt>Phase</dt>
            <dd>
              {detail.phase != null && detail.total_phases != null
                ? `${detail.phase} / ${detail.total_phases}`
                : "—"}
            </dd>
            <dt>Pause cause</dt>
            <dd>{detail.pause_cause ?? "—"}</dd>
          </>
        )}
      </dl>
      {detail.log_tail && detail.log_tail.length > 0 && (
        <div className="agent-log">
          <div className="agent-log-label">Log tail</div>
          <pre className="agent-log-tail">{detail.log_tail.join("\n")}</pre>
        </div>
      )}
    </div>
  );
}

function shortId(id: string): string {
  if (id.length <= 12) return id;
  return id.slice(0, 8) + "…";
}

function truncatePath(p: string, max: number = 48): string {
  if (p.length <= max) return p;
  return "…" + p.slice(p.length - (max - 1));
}

function formatTimestamp(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}
