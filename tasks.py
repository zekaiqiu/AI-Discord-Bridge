"""tasks.py — state model + worker management for claude-bridge delegated tasks.

No Discord-specific code lives here. bot.py wires this into the Discord client
(command handlers, ping loop registration via discord.ext.tasks, DM dispatch).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import time
import uuid
from pathlib import Path
from typing import Awaitable, Callable

import bridge_account_router

log = logging.getLogger("claude-bridge.tasks")

STATE_DIR = Path(
    os.environ.get(
        "CLAUDE_BRIDGE_STATE_DIR",
        str(Path.home() / ".local/state/claude-bridge"),
    )
)
TASKS_JSON = STATE_DIR / "tasks.json"
LOG_DIR = STATE_DIR / "logs"

MAX_ACTIVE_TASKS = 4
PING_INTERVAL_MIN = 30
PING_TIMEOUT_SEC = 90
STALL_TIMEOUT_SEC = 7200       # 2h
PENDING_TTL_SEC = 600          # 10m
LOG_TAIL_CHARS = 1500
SUMMARY_PERSIST_CHARS = 1000
SUMMARY_DM_CHARS = 1500

DECISION_PREFIX = (
    "If you encounter a decision point that materially changes the approach, "
    "pick the most defensible option, document the alternatives in decisions.md "
    "in the working directory, and continue. Cite this file in your status "
    "updates so the user can review."
)
STATUS_PROMPT = (
    "Status update: what have you done since the last summary, and what are "
    "you working on now? 2-3 sentences. If you're done, say so."
)
RESUME_PROMPT = (
    "Continue from where you left off. Pick up the in-progress work that "
    "was interrupted."
)

# In-process state. Lost on restart, by design.
WORKERS: dict[str, asyncio.Task] = {}
PROCS: dict[str, asyncio.subprocess.Process] = {}
PENDING: dict[int, dict] = {}


# -------------------------------------------------------------- state on disk

def init_state() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if not TASKS_JSON.exists():
        TASKS_JSON.write_text(json.dumps({"active": [], "archived": []}, indent=2))


def load_tasks() -> dict:
    if not TASKS_JSON.exists():
        return {"active": [], "archived": []}
    try:
        data = json.loads(TASKS_JSON.read_text(encoding="utf-8"))
        data.setdefault("active", [])
        data.setdefault("archived", [])
        return data
    except (json.JSONDecodeError, OSError):
        log.exception("tasks.json unreadable; starting fresh in memory")
        return {"active": [], "archived": []}


def save_tasks(state: dict) -> None:
    tmp = TASKS_JSON.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, TASKS_JSON)


def find_task(task_id: str) -> tuple[dict | None, str | None]:
    state = load_tasks()
    for t in state["active"]:
        if t["id"] == task_id:
            return t, "active"
    for t in state["archived"]:
        if t["id"] == task_id:
            return t, "archived"
    return None, None


def update_task_fields(task_id: str, **fields) -> None:
    state = load_tasks()
    for t in state["active"]:
        if t["id"] == task_id:
            t.update(fields)
            save_tasks(state)
            return
    log.warning("update_task_fields: %s not active; ignoring", task_id)


def append_active(task: dict) -> None:
    state = load_tasks()
    state["active"].append(task)
    save_tasks(state)


def archive(task_id: str, status: str) -> dict | None:
    state = load_tasks()
    moved = None
    keep: list[dict] = []
    for t in state["active"]:
        if t["id"] == task_id:
            t["status"] = status
            moved = t
        else:
            keep.append(t)
    if moved is None:
        return None
    state["active"] = keep
    state["archived"].append(moved)
    save_tasks(state)
    return moved


def make_task_id() -> str:
    return f"t-{secrets.token_hex(3)}"


def make_session_id() -> str:
    return str(uuid.uuid4())


# -------------------------------------------------------------- classifier

ACTION_VERBS = (
    "set up", "implement", "build", "create",
    "research", "investigate", "scaffold", "migrate",
    "refactor", "audit",
)
SEQUENCE_PATTERN = re.compile(
    r"\b(first|then|next|after that|finally)\b.*\b(then|next|after|finally)\b",
    re.IGNORECASE | re.DOTALL,
)


def is_complex(prompt: str) -> tuple[bool, str]:
    """Heuristic — returns (is_complex, summary)."""
    summary = _summarize(prompt)
    text = prompt.strip().lower()
    if len(prompt) > 200 and any(v in text for v in ACTION_VERBS):
        return True, summary
    if SEQUENCE_PATTERN.search(prompt):
        return True, summary
    return False, summary


def _summarize(prompt: str) -> str:
    s = re.split(r"(?<=[.!?])\s+", prompt.strip(), maxsplit=1)[0]
    if len(s) > 120:
        s = s[:117] + "..."
    return s


# -------------------------------------------------------------- pending

def set_pending(user_id: int, summary: str, original_prompt: str) -> None:
    PENDING[user_id] = {
        "summary": summary,
        "original_prompt": original_prompt,
        "expires_at": time.time() + PENDING_TTL_SEC,
    }


def get_pending(user_id: int) -> dict | None:
    p = PENDING.get(user_id)
    if not p:
        return None
    if time.time() > p["expires_at"]:
        PENDING.pop(user_id, None)
        return None
    return p


def clear_pending(user_id: int) -> None:
    PENDING.pop(user_id, None)


# -------------------------------------------------------------- workers

def read_log_tail(task_id: str, n_chars: int = LOG_TAIL_CHARS) -> str:
    path = LOG_DIR / f"{task_id}.log"
    if not path.exists():
        return "(no log yet)"
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - n_chars))
            data = f.read()
        return data.decode("utf-8", errors="replace")
    except OSError as e:
        return f"(log read failed: {e})"


DmCallable = Callable[[str], Awaitable[None]]


async def spawn_worker(
    task_id: str,
    session_id: str,
    cwd: Path,
    prompt: str,
    on_finish_dm: DmCallable,
    is_resume: bool = False,
) -> None:
    """Spawn the claude subprocess; the asyncio task tracks completion.

    last_ping_at deliberately does NOT update when this subprocess exits — only
    real ping responses bump it. That way a 4-hour initial run doesn't falsely
    trip the 2h stall detector. Don't "fix" this.
    """
    cwd.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{task_id}.log"
    log_fh = open(log_path, "ab", buffering=0)
    log_fh.write(
        f"\n=== spawn_worker {task_id} (resume={is_resume}) "
        f"at {time.strftime('%Y-%m-%dT%H:%M:%S%z')} ===\n".encode()
    )

    if is_resume:
        args = [
            "claude", "--resume", session_id, "-p", prompt,
            "--permission-mode", "bypassPermissions",
        ]
    else:
        args = [
            "claude", "--session-id", session_id, "-p", prompt,
            "--permission-mode", "bypassPermissions",
        ]

    route = await asyncio.to_thread(bridge_account_router.resolve_account_for_run)
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=log_fh,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "TERM": "dumb", "HOME": str(route.home_path)},
    )
    PROCS[task_id] = proc
    log.info(
        "spawned worker %s (pid=%s, resume=%s, cwd=%s)",
        task_id, proc.pid, is_resume, cwd,
    )

    async def watch() -> None:
        try:
            await proc.wait()
            # Don't overwrite a status that was set externally (e.g., !stop).
            cur, _ = find_task(task_id)
            if cur and cur.get("status") == "running":
                update_task_fields(task_id, status="idle")
                tail = read_log_tail(task_id)
                try:
                    await on_finish_dm(
                        f"[{task_id}] initial run finished. "
                        f"exit={proc.returncode}\n"
                        f"tail:\n```\n{tail[-1500:]}\n```"
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("on_finish_dm for %s failed", task_id)
        except asyncio.CancelledError:
            log.info("watch task for %s cancelled", task_id)
            raise
        except Exception:
            log.exception("watch task for %s crashed", task_id)
        finally:
            try:
                log_fh.close()
            except Exception:
                pass
            PROCS.pop(task_id, None)
            WORKERS.pop(task_id, None)

    WORKERS[task_id] = asyncio.create_task(watch())


async def stop_worker(task_id: str) -> bool:
    """Returns True if a running subprocess was actively killed."""
    killed = False
    proc = PROCS.get(task_id)
    if proc is not None and proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        except Exception:
            log.exception("proc.kill for %s failed", task_id)
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            log.warning("proc.wait timed out for %s after kill", task_id)
        killed = True
    task = WORKERS.get(task_id)
    if task is not None and not task.done():
        task.cancel()
    return killed


async def run_status_ping(session_id: str, prompt: str | None = None) -> str | None:
    """Run claude --resume <session> -p <prompt>, return summary or None.

    If `prompt` is None, uses the default STATUS_PROMPT (legacy "normal" cadence
    behavior). bot.py passes a level-specific prompt for verbose-toggle support.
    """
    effective_prompt = prompt if prompt is not None else STATUS_PROMPT
    args = [
        "claude", "--resume", session_id, "-p", effective_prompt,
        "--permission-mode", "bypassPermissions",
    ]
    route = await asyncio.to_thread(bridge_account_router.resolve_account_for_run)
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "TERM": "dumb", "HOME": str(route.home_path)},
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=PING_TIMEOUT_SEC
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        log.warning("ping timed out for session %s", session_id)
        return None
    if proc.returncode != 0:
        log.warning(
            "ping exit %s for %s: %s",
            proc.returncode, session_id, stderr.decode(errors="replace")[:300],
        )
        return None
    return stdout.decode("utf-8", errors="replace").strip() or None


def restart_cleanup() -> list[str]:
    """Reset stuck-as-running tasks to idle. Returns affected task IDs."""
    state = load_tasks()
    affected: list[str] = []
    for t in state["active"]:
        if t["status"] == "running":
            t["status"] = "idle"
            affected.append(t["id"])
    if affected:
        save_tasks(state)
    return affected
