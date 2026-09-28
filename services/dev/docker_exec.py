"""Wrappers around `docker exec` for the dev-wizerith IDE backend.

All work against the user's existing per-user container — same container the
chat service provisions via services.chat.user_container.ensure_user_container.
We never spawn our own; we just exec into whatever's there.

Two flavors:
  * sync helpers (read/write/list files, mkdir, rename, delete) — small
    one-shot commands, captured stdout, raise on non-zero exit
  * async streaming runner — for `python file.py` and friends, writes lines
    to an asyncio.Queue as they arrive so SSE handlers can fan them out
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import shlex
import subprocess
import time
import uuid
from typing import AsyncIterator, Iterable, Optional


# uid 1000 is the unprivileged `app` user inside the per-user container.
# It owns /workspace and the user-facing tooling. uid 2000 (claude-runner)
# is reserved for OAuth-credential mediation and must NOT be used here —
# see services.chat.user_container module docstring for the full rationale.
EXEC_UID = "1000:1000"

# Hard cap on a single one-shot exec (file ops). Streaming exec uses its
# own kill flow.
DEFAULT_TIMEOUT_SECONDS = 30.0


class DockerExecError(RuntimeError):
    """Raised when a `docker exec` returns non-zero or otherwise fails.

    Carries the stderr + exit code so the FastAPI handler can surface a
    useful 4xx/5xx without leaking the raw subprocess detail.
    """

    def __init__(self, message: str, *, exit_code: int = -1, stderr: str = ""):
        super().__init__(message)
        self.exit_code = exit_code
        self.stderr = stderr


# ---------------------------------------------------------------------------
# Synchronous helpers (file ops).
# ---------------------------------------------------------------------------

def _argv(
    container: str,
    cmd: list[str],
    *,
    user: str = EXEC_UID,
    workdir: Optional[str] = None,
    stdin: bool = False,
) -> list[str]:
    argv = ["docker", "exec"]
    if stdin:
        argv.append("-i")
    argv.extend(["--user", user])
    if workdir is not None:
        argv.extend(["--workdir", workdir])
    argv.append(container)
    argv.extend(cmd)
    return argv


def run_exec(
    container: str,
    cmd: list[str],
    *,
    user: str = EXEC_UID,
    workdir: Optional[str] = None,
    stdin_bytes: Optional[bytes] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> bytes:
    """Run a one-shot `docker exec`, return stdout, raise on non-zero.

    For binary-safe writes (file uploads) pass stdin_bytes; the resulting
    `docker exec -i` will stream them in on the container's stdin.
    """
    argv = _argv(container, cmd, user=user, workdir=workdir, stdin=stdin_bytes is not None)
    try:
        proc = subprocess.run(
            argv,
            input=stdin_bytes,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise DockerExecError(
            f"docker exec timed out after {timeout}s",
            exit_code=-1,
            stderr=str(exc),
        ) from exc
    if proc.returncode != 0:
        raise DockerExecError(
            f"docker exec failed: {' '.join(shlex.quote(a) for a in argv)}",
            exit_code=proc.returncode,
            stderr=proc.stderr.decode("utf-8", errors="replace"),
        )
    return proc.stdout


def run_exec_streaming(
    container: str,
    cmd: list[str],
    *,
    user: str = EXEC_UID,
    workdir: Optional[str] = None,
    stdin_fileobj,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[int, bytes, bytes]:
    """Like run_exec, but reads stdin from a file object instead of a bytes buffer.

    Used by the upload path so a 100 MB file can be piped from the
    UploadFile's underlying SpooledTemporaryFile straight into `docker exec`
    without first being copied into a Python `bytes` object — the OS handles
    the fd-to-fd transfer.

    Unlike run_exec, this does NOT raise on non-zero exit; the caller
    inspects (returncode, stdout, stderr) so it can distinguish a 413
    (size-cap exit code from the shell) from a 500 (real failure).
    """
    argv = _argv(container, cmd, user=user, workdir=workdir, stdin=True)
    try:
        proc = subprocess.run(
            argv,
            stdin=stdin_fileobj,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise DockerExecError(
            f"docker exec timed out after {timeout}s",
            exit_code=-1,
            stderr=str(exc),
        ) from exc
    return proc.returncode, proc.stdout, proc.stderr


# ---------------------------------------------------------------------------
# Streaming runner. Persists running jobs across SSE reconnects.
#
# The user can close the browser tab; the asyncio.Task on the server keeps
# pulling stdout from the docker exec subprocess into the job's ring buffer.
# When the user reattaches via SSE, we replay the buffer (drop nothing) and
# then continue forwarding live lines from the queue.
#
# Killing is explicit: a DELETE /api/jobs/{job_id} sends SIGTERM, then SIGKILL
# after a short grace.
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class JobLine:
    """One chunk of streamed output. `kind` is one of stdout/stderr/system/exit."""
    kind: str
    text: str
    ts: float


@dataclasses.dataclass
class Job:
    job_id: str
    email: str
    container: str
    argv: list[str]
    started_at: float
    status: str = "running"           # running | done | killed | error
    exit_code: Optional[int] = None
    buffer: list[JobLine] = dataclasses.field(default_factory=list)
    # max buffered lines kept in memory for SSE replay. Beyond this, the
    # head is truncated (FIFO). 10k lines * ~200 bytes/line ~= 2 MB/job
    # which is the right order of magnitude for "scrollback long enough
    # to inspect a normal training run, short enough to never OOM."
    buffer_cap: int = 10000
    subscribers: list[asyncio.Queue] = dataclasses.field(default_factory=list)
    # Set after the asyncio task that supervises the subprocess starts.
    # Kept here so DELETE can cancel the supervisor cleanly even if the
    # subprocess has exited but the line-pump hasn't drained.
    _proc: Optional[asyncio.subprocess.Process] = None
    _task: Optional[asyncio.Task] = None

    def append(self, line: JobLine) -> None:
        self.buffer.append(line)
        if len(self.buffer) > self.buffer_cap:
            # Cheap O(n) truncation. 10k cap means this fires at most every
            # 10k lines and a slice realloc is cheaper than a deque rotate
            # for this access pattern (mostly append + replay).
            self.buffer = self.buffer[-self.buffer_cap:]
        # Fan out to live subscribers; drop on full queue rather than block
        # the line pump (a slow subscriber is the subscriber's problem).
        for q in list(self.subscribers):
            try:
                q.put_nowait(line)
            except asyncio.QueueFull:
                pass


class JobRegistry:
    """In-memory registry of running / recently-finished jobs.

    Keyed by job_id (uuid4). Finished jobs are kept around for a grace period
    so a late-arriving SSE reconnect can still get the full buffer; after
    grace they're swept by `gc()`.
    """

    DONE_GRACE_SECONDS = 15 * 60  # 15 minutes — long enough for a coffee

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = asyncio.Lock()

    async def create(self, *, email: str, container: str, argv: list[str]) -> Job:
        job = Job(
            job_id=uuid.uuid4().hex,
            email=email,
            container=container,
            argv=argv,
            started_at=time.time(),
        )
        async with self._lock:
            self._jobs[job.job_id] = job
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def list_for(self, email: str) -> list[Job]:
        return [j for j in self._jobs.values() if j.email == email]

    async def gc(self, *, now: Optional[float] = None) -> int:
        """Drop finished jobs past their grace window. Returns count removed."""
        now = now if now is not None else time.time()
        to_remove = [
            job_id
            for job_id, j in self._jobs.items()
            if j.status != "running" and (now - (j.started_at + 1)) > self.DONE_GRACE_SECONDS
        ]
        async with self._lock:
            for job_id in to_remove:
                self._jobs.pop(job_id, None)
        return len(to_remove)


registry = JobRegistry()


async def _pump_stream(stream: asyncio.StreamReader, job: Job, kind: str) -> None:
    """Read stdout/stderr until EOF; tag each line and store in the buffer."""
    while True:
        try:
            chunk = await stream.readline()
        except Exception as exc:
            job.append(JobLine(kind="system", text=f"read error: {exc}", ts=time.time()))
            return
        if not chunk:
            return
        try:
            text = chunk.decode("utf-8", errors="replace")
        except Exception:
            text = repr(chunk)
        job.append(JobLine(kind=kind, text=text, ts=time.time()))


async def _supervise(job: Job, *, env: Optional[dict] = None) -> None:
    """Spawn the docker exec, pump output, mark status on exit."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *job.argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            env=env,
        )
    except Exception as exc:
        job.status = "error"
        job.append(JobLine(kind="system", text=f"failed to spawn: {exc}", ts=time.time()))
        job.append(JobLine(kind="exit", text="error", ts=time.time()))
        # Wake any subscribers with a sentinel so their SSE loop ends.
        for q in list(job.subscribers):
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass
        return

    job._proc = proc
    try:
        await asyncio.gather(
            _pump_stream(proc.stdout, job, "stdout"),
            _pump_stream(proc.stderr, job, "stderr"),
        )
        rc = await proc.wait()
    except asyncio.CancelledError:
        # The task was cancelled (e.g. DELETE /api/jobs/<id>). Best-effort
        # tear down, then re-raise so the asyncio runtime sees the cancel.
        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                proc.kill()
        except Exception:
            pass
        job.status = "killed"
        job.append(JobLine(kind="system", text="killed by user", ts=time.time()))
        job.append(JobLine(kind="exit", text="killed", ts=time.time()))
        raise
    else:
        job.exit_code = rc
        job.status = "done" if rc == 0 else "error"
        job.append(JobLine(kind="exit", text=str(rc), ts=time.time()))
    finally:
        for q in list(job.subscribers):
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass


async def start_job(
    *,
    email: str,
    container: str,
    cmd: list[str],
    workdir: str = "/workspace",
    user: str = EXEC_UID,
    extra_env: Optional[dict[str, str]] = None,
) -> Job:
    """Spawn `docker exec` in the background, return the Job immediately.

    Caller subscribes to the job via `subscribe(job)` to receive an
    asyncio.Queue of JobLine entries. The supervisor task lives in the
    asyncio event loop; closing the SSE connection does NOT cancel it.
    """
    argv = _argv(container, cmd, user=user, workdir=workdir)
    if extra_env:
        # docker exec doesn't accept --env=K=V on RHEL-era binaries, but the
        # ones we ship do (compose mounts /usr/bin/docker:ro from the host).
        # Format: argv = ["docker","exec","--env","K=V",...,"--user",...,container,cmd...]
        # — splice them BEFORE the user/workdir/container chunk.
        env_args: list[str] = []
        for k, v in extra_env.items():
            env_args.extend(["--env", f"{k}={v}"])
        # Insert after "docker exec" (positions 0 and 1).
        argv = argv[:2] + env_args + argv[2:]
    job = await registry.create(email=email, container=container, argv=argv)
    job._task = asyncio.create_task(_supervise(job))
    return job


def subscribe(job: Job) -> asyncio.Queue:
    """Attach a fresh subscriber queue. Returns the queue.

    SSE handlers do:
        q = subscribe(job)
        for line in job.buffer: yield serialize(line)   # replay
        while True:
            line = await q.get()
            if line is None: break                       # sentinel = job done
            yield serialize(line)
    """
    q: asyncio.Queue = asyncio.Queue(maxsize=4096)
    job.subscribers.append(q)
    return q


def unsubscribe(job: Job, q: asyncio.Queue) -> None:
    try:
        job.subscribers.remove(q)
    except ValueError:
        pass


async def kill_job(job: Job, *, grace_seconds: float = 3.0) -> bool:
    """SIGTERM, then SIGKILL after grace. Returns True if a kill was issued."""
    if job.status != "running" or job._task is None:
        return False
    job._task.cancel()
    try:
        await asyncio.wait_for(asyncio.shield(job._task), timeout=grace_seconds)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass
    return True
