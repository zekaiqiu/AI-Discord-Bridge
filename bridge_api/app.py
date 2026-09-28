"""FastAPI app exposing the bridge bot's handlers as JSON endpoints.

Mounts at ``/api/`` (caddy strips ``/api/ops``). Routes match the contract
in the brief — see ``schemas.py`` for the wire types.

Implementation notes:

* Read paths read directly from ``tasks.json``, ``handles.json``, and the
  active-project pointer file. They do NOT import bot.py — bot.py needs
  ``DISCORD_BOT_TOKEN`` at module load and we want the API to come up
  cleanly even if someone is iterating on it without that env var.
* Write paths (POST endpoints) DO import bot.py lazily inside the route
  handler. That triggers Discord client construction; the systemd unit has
  the same env file the bot uses, so prod is fine. Tests that exercise
  these routes monkeypatch ``_load_bot()`` to return a stub.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, status

from . import auth
from .schemas import (
    Agent,
    AgentCreated,
    AgentDetail,
    AgentList,
    AgentMutated,
    ProjectCreatePayload,
    TaskCreatePayload,
    UsagePayload,
    VerbosePayload,
)


# ---------------------------------------------------------------------------
# Locations of bridge state on disk. Hard-coded to match tasks.py / bot.py.
# Tests that need to use a fixture state can monkeypatch these.
# ---------------------------------------------------------------------------

STATE_DIR = Path(
    os.environ.get(
        "CLAUDE_BRIDGE_STATE_DIR",
        str(Path.home() / ".local/state/claude-bridge"),
    )
)
TASKS_JSON = STATE_DIR / "tasks.json"
HANDLES_JSON = STATE_DIR / "handles.json"
LOG_DIR = STATE_DIR / "logs"
ACTIVE_PROJECT_POINTER = Path("/home/felix/multi-agent-pipeline/active_project.txt")
PROJECTS_ROOT = Path("/home/felix/multi-agent-pipeline/projects")
PROJECTS_ARCHIVED_DIR = Path("/home/felix/multi-agent-pipeline/projects/.archived")


# ---------------------------------------------------------------------------
# Bot import shim. Lazy so module load doesn't require Discord env vars.
# ---------------------------------------------------------------------------

def _load_bot():
    """Lazily import bot.py and return the module.

    Tests can monkeypatch this to return a stub before any write-path route
    runs. We keep this in one place so test fixtures only have to override
    a single seam.
    """
    import bot  # noqa: WPS433 — intentional lazy import
    return bot


# ---------------------------------------------------------------------------
# Read helpers (no bot.py import needed).
# ---------------------------------------------------------------------------

def _load_tasks_state() -> dict:
    if not TASKS_JSON.exists():
        return {"active": [], "archived": []}
    try:
        data = json.loads(TASKS_JSON.read_text(encoding="utf-8"))
        data.setdefault("active", [])
        data.setdefault("archived", [])
        return data
    except (OSError, json.JSONDecodeError):
        return {"active": [], "archived": []}


def _load_handles() -> dict:
    if not HANDLES_JSON.exists():
        return {"task_handles": {}, "proj_handles": {}}
    try:
        data = json.loads(HANDLES_JSON.read_text(encoding="utf-8"))
        data.setdefault("task_handles", {})
        data.setdefault("proj_handles", {})
        return data
    except (OSError, json.JSONDecodeError):
        return {"task_handles": {}, "proj_handles": {}}


def _read_active_project_id() -> str | None:
    try:
        pid = ACTIVE_PROJECT_POINTER.read_text(encoding="utf-8").strip()
        return pid or None
    except OSError:
        return None


def _iso(epoch: float | int | None) -> str | None:
    if not epoch:
        return None
    try:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat()
    except (OSError, ValueError):
        return None


def _read_log_tail(task_id: str, n_lines: int = 100) -> list[str]:
    path = LOG_DIR / f"{task_id}.log"
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = text.splitlines()
    return lines[-n_lines:]


def _project_phase_info(project_id: str) -> dict[str, Any]:
    """Best-effort phase / artifact summary by reading on-disk state.

    The pipeline CLI has a ``projectstatus`` command that does this with
    full fidelity, but it's a subprocess; for the read-only API we want
    fast, cheap inspection. Anything missing → None.
    """
    out: dict[str, Any] = {
        "phase": None, "total_phases": None,
        "pause_cause": None, "last_artifact": None,
        "working_dir": None, "status": "running",
    }
    pdir = PROJECTS_ROOT / project_id
    if not pdir.is_dir():
        return out
    out["working_dir"] = str(pdir)
    phases_dir = pdir / "phases"
    if phases_dir.is_dir():
        try:
            phase_dirs = sorted(
                p for p in phases_dir.iterdir()
                if p.is_dir() and p.name.startswith("phase")
            )
        except OSError:
            phase_dirs = []
        if phase_dirs:
            out["total_phases"] = len(phase_dirs)
            # The current phase is the latest one with state — use mtime.
            current = max(phase_dirs, key=lambda p: p.stat().st_mtime)
            try:
                out["phase"] = int(current.name.removeprefix("phase").lstrip("_-"))
            except ValueError:
                out["phase"] = len(phase_dirs)
            # Last artifact: the newest file under the latest phase dir.
            try:
                files = [p for p in current.rglob("*") if p.is_file()]
                if files:
                    out["last_artifact"] = str(max(files, key=lambda p: p.stat().st_mtime))
            except OSError:
                pass
    # Pause-cause markers used by the pipeline orchestrator.
    for marker, label in (
        (".user_paused", "user_paused"),
        (".rate_limited", "rate_limited"),
        (".usage_high", "usage_high"),
    ):
        if (pdir / marker).exists():
            out["pause_cause"] = label
            out["status"] = "paused"
            break
    if out["phase"] is not None and out["total_phases"]:
        out["status"] = f"phase {out['phase']} of {out['total_phases']}"
    return out


def _agent_for_task(task: dict, handles: dict) -> Agent:
    return Agent(
        id=task["id"],
        kind="task",
        handle=handles.get("task_handles", {}).get(task["id"]),
        status=str(task.get("status") or "?"),
        description=task.get("description"),
        created_at=_iso(task.get("created_at")),
        working_dir=task.get("working_dir"),
        last_artifact=None,
    )


def _agent_for_project(project_id: str, handles: dict, info: dict[str, Any]) -> Agent:
    return Agent(
        id=project_id,
        kind="project",
        handle=handles.get("proj_handles", {}).get(project_id),
        status=info.get("status") or "running",
        description=None,
        created_at=None,
        working_dir=info.get("working_dir"),
        last_artifact=info.get("last_artifact"),
    )


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Claude Bridge Ops API")


# /api/healthz: deliberately mounted with NO auth dependency so caddy can
# probe the upstream without owning a JWT. The brief explicitly carves it out.
@app.get("/api/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}


@app.get("/api/agents", response_model=AgentList)
async def list_agents(_: str = Depends(auth.require_admin)) -> AgentList:
    state = _load_tasks_state()
    handles = _load_handles()
    active: list[Agent] = [_agent_for_task(t, handles) for t in state["active"]]
    archived: list[Agent] = [_agent_for_task(t, handles) for t in state["archived"]]
    pid = _read_active_project_id()
    if pid:
        info = _project_phase_info(pid)
        active.append(_agent_for_project(pid, handles, info))
    return AgentList(active=active, archived=archived)


@app.get("/api/agents/{agent_id}", response_model=AgentDetail)
async def get_agent(agent_id: str, _: str = Depends(auth.require_admin)) -> AgentDetail:
    handles = _load_handles()

    # Resolve handle ("task-1") or UUID. We do this inline rather than
    # importing agent_handles to keep the read path import-free.
    resolved_id = agent_id
    resolved_kind: str | None = None
    if agent_id.startswith("task-"):
        for uuid_, h in handles.get("task_handles", {}).items():
            if h == agent_id:
                resolved_id = uuid_
                resolved_kind = "task"
                break
    elif agent_id.startswith("proj-") and not agent_id[len("proj-"):].lstrip("0123456789") == "":
        # "proj-N" is a handle (small int); "proj-XXXXXXXX" is a project uuid.
        for uuid_, h in handles.get("proj_handles", {}).items():
            if h == agent_id:
                resolved_id = uuid_
                resolved_kind = "project"
                break
    elif agent_id.startswith("t-"):
        resolved_kind = "task"
    elif agent_id.startswith("proj-"):
        resolved_kind = "project"

    state = _load_tasks_state()
    if resolved_kind in (None, "task"):
        for t in state["active"] + state["archived"]:
            if t["id"] == resolved_id:
                base = _agent_for_task(t, handles)
                return AgentDetail(
                    **base.model_dump(),
                    log_tail=_read_log_tail(t["id"]),
                )

    if resolved_kind in (None, "project"):
        active_pid = _read_active_project_id()
        if resolved_id == active_pid or (resolved_kind == "project" and resolved_id):
            info = _project_phase_info(resolved_id)
            base = _agent_for_project(resolved_id, handles, info)
            return AgentDetail(
                **base.model_dump(),
                phase=info.get("phase"),
                total_phases=info.get("total_phases"),
                pause_cause=info.get("pause_cause"),
            )

    # Archived-UUID fallback (overnight brief item 1): the active-project
    # path above only knows about live tasks and the single active project.
    # If neither matched, look in the archived task list and the archived
    # project directory before giving up with a 404.
    for t in state["archived"]:
        if t["id"] == agent_id or t["id"] == resolved_id:
            base = _agent_for_task(t, handles)
            return AgentDetail(
                **base.model_dump(),
                log_tail=_read_log_tail(t["id"]),
            )
    if PROJECTS_ARCHIVED_DIR.is_dir():
        try:
            candidates = sorted(
                d for d in PROJECTS_ARCHIVED_DIR.iterdir()
                if d.is_dir() and d.name.startswith(f"{agent_id}-")
            )
        except OSError:
            candidates = []
        if candidates:
            adir = candidates[-1]
            description = None
            brief = adir / "INITIAL_BRIEF.md"
            if brief.exists():
                try:
                    description = brief.read_text(encoding="utf-8", errors="replace").strip() or None
                except OSError:
                    description = None
            return AgentDetail(
                id=agent_id, kind="project",
                handle=handles.get("proj_handles", {}).get(agent_id),
                status="archived", description=description,
                working_dir=str(adir),
            )

    raise HTTPException(status_code=404, detail=f"unknown agent {agent_id}")


@app.post("/api/agents/task", response_model=AgentCreated, status_code=201)
async def create_task(
    payload: TaskCreatePayload,
    _: str = Depends(auth.require_admin),
) -> AgentCreated:
    bot = _load_bot()
    try:
        result = await bot.core_task_create(
            payload.prompt, verbose_level=payload.verbosity
        )
    except bot.TaskCapacityError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return AgentCreated(
        id=result["id"], handle=result.get("handle"), status=result["status"],
    )


@app.post("/api/agents/project", response_model=AgentCreated, status_code=201)
async def create_project(
    payload: ProjectCreatePayload,
    _: str = Depends(auth.require_admin),
) -> AgentCreated:
    """Spawn a multi-agent-pipeline project.

    bot.handle_project_create couples the dispatch flow to a Discord
    message (attachment fan-in, channel.send for status). For the API we
    re-implement the orchestrator/reporter dispatch here against the
    pipeline CLI directly. Attachments are not supported on this surface
    yet — the dashboard can post a longer brief inline.
    """
    import asyncio
    import secrets
    import subprocess

    bot = _load_bot()
    if ACTIVE_PROJECT_POINTER.exists():
        try:
            existing = ACTIVE_PROJECT_POINTER.read_text().strip()
            if existing:
                raise HTTPException(
                    status_code=409,
                    detail=f"already have an active project: {existing}",
                )
        except OSError:
            pass

    pipeline_bin = bot.PIPELINE_BIN
    pipeline_root = bot.PIPELINE_ROOT
    pipeline_reporter = bot.PIPELINE_REPORTER
    if not pipeline_bin.exists():
        raise HTTPException(
            status_code=503, detail=f"pipeline not installed at {pipeline_bin}",
        )
    project_id = f"proj-{secrets.token_hex(4)}"
    proc = await asyncio.create_subprocess_exec(
        str(pipeline_bin), "new-project", project_id, "--brief", payload.brief,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"pipeline new-project failed: {stderr.decode()[:500]}",
        )

    # Orchestrator + reporter as transient user units (mirrors bot logic).
    orchestrator_unit = f"pipeline-orchestrator-{project_id}"
    try:
        subprocess.run(
            [
                "systemd-run", "--user", "--unit", orchestrator_unit,
                "--working-directory", str(pipeline_root),
                "--description", f"multi-agent-pipeline orchestrator for {project_id}",
                str(pipeline_bin), "run", project_id,
            ],
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise HTTPException(status_code=500, detail=f"orchestrator launch failed: {exc}")

    handle = bot.agent_handles.assign(project_id, "proj")
    return AgentCreated(id=project_id, handle=handle, status="running")


def _resolve_agent(agent_id: str):
    """Resolve agent_id (handle or uuid) → (uuid, kind). Returns None on miss."""
    bot = _load_bot()
    return bot.agent_handles.resolve(agent_id)


@app.post("/api/agents/{agent_id}/kill", response_model=AgentMutated)
async def kill_agent(agent_id: str, _: str = Depends(auth.require_admin)) -> AgentMutated:
    bot = _load_bot()
    resolved = _resolve_agent(agent_id)
    if resolved is None:
        raise HTTPException(status_code=404, detail=f"unknown agent {agent_id}")
    uuid_, kind = resolved
    if kind == "task":
        try:
            result = await bot.core_task_stop(uuid_)
        except LookupError:
            raise HTTPException(status_code=404, detail=f"no active task {uuid_}")
        return AgentMutated(id=result["id"], status="killed" if result["killed"] else "stopped")
    # project kill — invoke pipeline CLI.
    rc, out, err = await bot._run_pipeline_cli("projectkill")
    if rc != 0:
        raise HTTPException(status_code=500, detail=(out or err).strip())
    bot._stop_project_reporter(uuid_)
    return AgentMutated(id=uuid_, status="killed")


@app.post("/api/agents/{agent_id}/stop", response_model=AgentMutated)
async def stop_agent(agent_id: str, _: str = Depends(auth.require_admin)) -> AgentMutated:
    bot = _load_bot()
    resolved = _resolve_agent(agent_id)
    if resolved is None:
        raise HTTPException(status_code=404, detail=f"unknown agent {agent_id}")
    uuid_, kind = resolved
    if kind == "task":
        try:
            result = await bot.core_task_stop(uuid_)
        except LookupError:
            raise HTTPException(status_code=404, detail=f"no active task {uuid_}")
        return AgentMutated(id=result["id"], status="stopped")
    rc, out, err = await bot._run_pipeline_cli("projectpause")
    if rc != 0:
        raise HTTPException(status_code=500, detail=(out or err).strip())
    return AgentMutated(id=uuid_, status="paused")


@app.post("/api/agents/{agent_id}/resume", response_model=AgentMutated)
async def resume_agent(agent_id: str, _: str = Depends(auth.require_admin)) -> AgentMutated:
    bot = _load_bot()
    resolved = _resolve_agent(agent_id)
    if resolved is None:
        raise HTTPException(status_code=404, detail=f"unknown agent {agent_id}")
    uuid_, kind = resolved
    if kind == "task":
        record, loc = bot.task_module.find_task(uuid_)
        if record is None or loc != "active":
            raise HTTPException(status_code=404, detail=f"no active task {uuid_}")
        if record["status"] not in ("stopped", "stalled", "idle"):
            raise HTTPException(
                status_code=409,
                detail=f"task {uuid_} status is {record['status']}; nothing to resume",
            )
        if uuid_ in bot.task_module.WORKERS:
            raise HTTPException(status_code=409, detail=f"task {uuid_} already has a running worker")
        cwd = Path(record["working_dir"])
        bot.task_module.update_task_fields(uuid_, status="running")
        await bot.task_module.spawn_worker(
            uuid_, record["session_id"], cwd,
            bot.task_module.RESUME_PROMPT, bot.dm_user, is_resume=True,
        )
        return AgentMutated(id=uuid_, status="running")
    rc, out, err = await bot._run_pipeline_cli("projectresume")
    if rc != 0:
        raise HTTPException(status_code=500, detail=(out or err).strip())
    return AgentMutated(id=uuid_, status="running")


@app.post("/api/agents/{agent_id}/end", response_model=AgentMutated)
async def end_agent(agent_id: str, _: str = Depends(auth.require_admin)) -> AgentMutated:
    bot = _load_bot()
    resolved = _resolve_agent(agent_id)
    if resolved is None:
        raise HTTPException(status_code=404, detail=f"unknown agent {agent_id}")
    uuid_, kind = resolved
    if kind == "task":
        if uuid_ in bot.task_module.WORKERS:
            raise HTTPException(
                status_code=409,
                detail=f"task {uuid_} still has a running worker",
            )
        moved = bot.task_module.archive(uuid_, status="complete")
        if moved is None:
            raise HTTPException(status_code=404, detail=f"no active task {uuid_}")
        bot.VERBOSE_OVERRIDES.pop(uuid_, None)
        return AgentMutated(id=uuid_, status="complete")
    rc, out, err = await bot._run_pipeline_cli("projectend")
    if rc != 0:
        raise HTTPException(status_code=500, detail=(out or err).strip())
    bot._stop_project_reporter(uuid_)
    return AgentMutated(id=uuid_, status="ended")


@app.post("/api/agents/{agent_id}/verbose", response_model=AgentMutated)
async def set_agent_verbose(
    agent_id: str,
    payload: VerbosePayload,
    _: str = Depends(auth.require_admin),
) -> AgentMutated:
    bot = _load_bot()
    if payload.level not in bot.LEVELS:
        raise HTTPException(
            status_code=422,
            detail=f"unknown level: {payload.level}; valid: {list(bot.LEVELS)}",
        )
    resolved = _resolve_agent(agent_id)
    if resolved is None:
        raise HTTPException(status_code=404, detail=f"unknown agent {agent_id}")
    uuid_, kind = resolved
    if kind != "task":
        raise HTTPException(
            status_code=400,
            detail="per-project verbosity isn't implemented; projects use a fixed reporter interval",
        )
    record, loc = bot.task_module.find_task(uuid_)
    if record is None or loc != "active":
        raise HTTPException(status_code=404, detail=f"no active task {uuid_}")
    bot.VERBOSE_OVERRIDES[uuid_] = payload.level
    bot.task_module.update_task_fields(
        uuid_, next_ping_at=time.time() + bot.ping_interval_sec(payload.level),
    )
    return AgentMutated(id=uuid_, status=record.get("status", "?"))


@app.get("/api/usage", response_model=UsagePayload)
async def get_usage(_: str = Depends(auth.require_admin)) -> UsagePayload:
    """Live Anthropic quota windows + ccusage-derived cost. Reuses the
    same data structures as the !usage Discord command so the two surfaces
    stay in lockstep."""
    import usage_report
    data = usage_report.collect_usage_data()
    return UsagePayload(**data)


__all__ = ["app"]
