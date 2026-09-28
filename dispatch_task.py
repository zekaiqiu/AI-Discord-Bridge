#!/usr/bin/env python3
"""Dispatcher for delegated bridge-bot tasks: writes tasks.json entry, daemonizes,
spawns claude as a detached child, supervises, updates status, DMs alde on finish.

Lives in claude-bridge/ (not /tmp) so systemd-tmpfiles doesn't wipe it.

Usage: dispatch_task.py <task_id> <session_id> <cwd> <description> <prompt_file>
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bridge_account_router  # noqa: E402

BRIDGE_DIR = Path("/home/felix/projects/claude-bridge")
STATE_DIR = Path(
    os.environ.get(
        "CLAUDE_BRIDGE_STATE_DIR",
        str(Path.home() / ".local/state/claude-bridge"),
    )
)
TASKS_JSON = STATE_DIR / "tasks.json"
LOG_DIR = STATE_DIR / "logs"
ALLOWED_USER_ID = os.environ.get("ALLOWED_USER_ID", "")

DECISION_PREFIX = (
    "If you encounter a decision point that materially changes the approach, "
    "pick the most defensible option, document the alternatives in decisions.md "
    "in the working directory, and continue. Cite this file in your status "
    "updates so the user can review."
)


def daemonize() -> None:
    """Double-fork into the background. Skip when running under systemd-run
    (or any launcher that already isolates us in its own cgroup) — set
    DISPATCH_NO_DAEMONIZE=1. With systemd-run --user, daemonizing causes the
    service's main pid to exit, which makes systemd think the unit completed
    and the cgroup gets killed before the grandchild can do anything."""
    if os.environ.get("DISPATCH_NO_DAEMONIZE"):
        return
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    devnull = os.open("/dev/null", os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)


def get_token() -> str:
    for line in (BRIDGE_DIR / ".env").read_text().splitlines():
        line = line.strip()
        if line.startswith("DISCORD_BOT_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("no DISCORD_BOT_TOKEN in .env")


def load_tasks() -> dict:
    if not TASKS_JSON.exists():
        return {"active": [], "archived": []}
    try:
        d = json.loads(TASKS_JSON.read_text())
        d.setdefault("active", [])
        d.setdefault("archived", [])
        return d
    except Exception:
        return {"active": [], "archived": []}


def save_tasks(state: dict) -> None:
    tmp = TASKS_JSON.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n")
    os.replace(tmp, TASKS_JSON)


def update_status(task_id: str, **fields) -> None:
    state = load_tasks()
    for t in state["active"]:
        if t["id"] == task_id:
            t.update(fields)
            break
    save_tasks(state)


def dm_alde(text: str) -> bool:
    try:
        token = get_token()
    except Exception:
        return False
    h = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
        "User-Agent": "Dispatcher (1.0)",
    }
    try:
        req = urllib.request.Request(
            "https://discord.com/api/v10/users/@me/channels",
            data=json.dumps({"recipient_id": str(ALLOWED_USER_ID)}).encode(),
            headers=h, method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            ch = json.loads(r.read())
        body = text[:1900]
        req2 = urllib.request.Request(
            f"https://discord.com/api/v10/channels/{ch['id']}/messages",
            data=json.dumps({"content": body}).encode(),
            headers=h, method="POST",
        )
        with urllib.request.urlopen(req2, timeout=15):
            return True
    except Exception:
        return False


def main() -> None:
    if len(sys.argv) != 6:
        print("usage: dispatch_task.py <task_id> <session_id> <cwd> <description> <prompt_file>")
        sys.exit(2)
    task_id, session_id, cwd_str, description, prompt_file = sys.argv[1:6]
    cwd = Path(cwd_str)
    prompt_body = Path(prompt_file).read_text(encoding="utf-8")
    full_prompt = f"{DECISION_PREFIX}\n\n{prompt_body}"

    cwd.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    state = load_tasks()
    state["active"].append({
        "id": task_id,
        "session_id": session_id,
        "description": description[:500],
        "created_at": time.time(),
        "last_ping_at": 0.0,
        "last_ping_summary": "",
        "status": "running",
        "working_dir": str(cwd),
    })
    save_tasks(state)

    daemonize()

    log_path = LOG_DIR / f"{task_id}.log"
    log_fh = open(log_path, "ab", buffering=0)
    log_fh.write(
        f"\n=== dispatch_task spawn {task_id} at "
        f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} ===\n".encode()
    )

    args = [
        "claude", "--session-id", session_id, "-p", full_prompt,
        "--permission-mode", "bypassPermissions",
        # Background tasks run headless with no interactive UI — never let the
        # model reach for AskUserQuestion (it would hang the task).
        "--disallowedTools", "AskUserQuestion",
    ]
    try:
        route = bridge_account_router.resolve_account_for_run()
        proc = subprocess.Popen(
            args,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            env={**os.environ, "TERM": "dumb", "HOME": str(route.home_path)},
        )
        rc = proc.wait()
    except Exception as e:
        log_fh.write(f"\n=== dispatcher spawn error: {e} ===\n".encode())
        log_fh.close()
        update_status(task_id, status="stalled")
        dm_alde(f"[{task_id}] dispatcher failed to spawn worker: {e}")
        return

    log_fh.close()
    try:
        with log_path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 1500))
            tail = f.read().decode("utf-8", errors="replace")
    except Exception:
        tail = "(log read failed)"

    update_status(task_id, status="idle")
    dm_alde(
        f"[{task_id}] initial run finished. exit={rc}\n"
        f"tail:\n```\n{tail[-1300:]}\n```"
    )


if __name__ == "__main__":
    main()
