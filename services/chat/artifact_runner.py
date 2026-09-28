"""Run a code artifact (`.py`, or a C/C++ source compiled on the fly)
inside the user's per-user container, streaming stdout/stderr to SSE
subscribers and surfacing any media (matplotlib charts, written files)
the program produced.

Python runs via an inlined shim under the container's python3. C/C++
sources are compiled with g++/gcc (both present via build-essential in
the shared image) and the resulting binary is exec'd with cwd=$OUT, so
file output is collected identically across languages.

This module is the model:
  * `Run` — one execution. Holds the docker subprocess + a deque of
    streamed events, served to async subscribers via an asyncio.Event.
  * `start_run(email, session_id, filename, source) -> Run` — kicks
    off `docker exec`-ing the user's container; returns immediately
    with the Run object. The actual subprocess is driven by a background
    asyncio task that the Run owns.
  * `get_run(run_id) -> Run | None` — registry lookup.
  * `cancel_run(run)` — kills the subprocess and emits a `cancelled`
    terminal event.

Storage layout inside the user container:
  /tmp/wizerith-runs/<run_id>/
    script.py          (the user/LLM artifact bytes — written by us)
    runner.py          (our shim that patches matplotlib + execs script)
    out/               (output dir for figures and other media)

Lifetime / GC:
  * Process-wide registry. Runs persist for the dev backend's process
    lifetime, so re-opening the modal can re-attach to a live run.
  * After a run terminates, the registry keeps it for `_RUN_RETENTION_S`
    so the frontend can still fetch media; a background sweep prunes
    older entries (and best-effort deletes the in-container temp dir).

Concurrency cap: `_MAX_PER_USER` concurrent runs. The (rare) case of a
user spam-clicking Run gets a 429 from the start endpoint.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import time
from collections import deque
from pathlib import Path
from typing import Any, AsyncIterator, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config.
# ---------------------------------------------------------------------------

_MAX_PER_USER = 2
_RUN_TIMEOUT_S = 600.0
_COMPILE_TIMEOUT_S = 120.0  # bound the g++/gcc build step for compiled languages
_RUN_RETENTION_S = 30 * 60.0  # keep finished runs around for 30 min so media URLs work

# Source extensions we know how to run. `str.endswith` accepts this tuple
# directly, so app.py can gate uploads/artifacts against it.
RUNNABLE_EXTENSIONS = (".py", ".cpp", ".cc", ".cxx", ".c++", ".c")
_OUTPUT_DIR_IN_CONTAINER = "/tmp/wizerith-runs"
_MAX_OUTPUT_FILES = 50
_MAX_MEDIA_BYTES = 25 * 1024 * 1024  # cap per media file we'll surface

# Filename guard for media we ship back to the browser. Matches the chat
# /generated/ convention.
_MEDIA_FILENAME_RE = re.compile(r"^(?!\.)[a-zA-Z0-9._-]{1,120}$")

# Tagged media types — anything else served as octet-stream.
_MEDIA_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
    ".html": "text/html", ".htm": "text/html",
    ".txt": "text/plain", ".csv": "text/csv", ".tsv": "text/tab-separated-values",
    ".json": "application/json",
}


class RunError(Exception):
    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _detect_language(filename: str) -> str:
    """Map an artifact filename to an execution backend: 'python', 'cpp',
    or 'c'. Raises RunError for anything not in RUNNABLE_EXTENSIONS."""
    ext = os.path.splitext(filename)[1].lower()
    if ext == ".py":
        return "python"
    if ext in (".cpp", ".cc", ".cxx", ".c++"):
        return "cpp"
    if ext == ".c":
        return "c"
    runnable = ", ".join(RUNNABLE_EXTENSIONS)
    raise RunError(
        f"unsupported artifact type {ext or '(none)'!r}; runnable: {runnable}",
        status_code=400,
    )


# ---------------------------------------------------------------------------
# In-container runner shim. Inlined so we don't need to ship a file.
#
# What it does:
#   1. Set matplotlib's backend to Agg (no display server in the container).
#   2. Patch `matplotlib.figure.Figure.savefig` (the underlying class method)
#      — this is what BOTH `plt.savefig(...)` and `fig.savefig(...)` call.
#      Patching only `plt.savefig` misses scripts that grab the figure
#      object and call `.savefig` directly on it (the more common pattern
#      for multi-figure workflows).
#      • Relative path → redirect to $OUT_DIR (so "savefig('chart.png')"
#        works without modification regardless of cwd).
#      • Absolute path → leave path alone but mkdir -p the parent dir
#        so a missing intermediate dir (the common case for absolute
#        writes to /workspace/.artifacts/<sid>/ that the chat backend
#        otherwise creates only during model turns) doesn't crash.
#   3. Patch `pyplot.show()` to save the current figure into $OUT_DIR.
#   4. Run the user's script as __main__ via runpy.run_path. cwd is $OUT_DIR
#      so relative writes (`df.to_csv("data.csv")`) also land in /out.
# ---------------------------------------------------------------------------

_RUNNER_SHIM = r"""
import os, sys, runpy

_OUT_DIR = os.environ["WIZERITH_RUN_OUT"]
_SCRIPT  = os.environ["WIZERITH_RUN_SCRIPT"]
os.makedirs(_OUT_DIR, exist_ok=True)

# Patch matplotlib BEFORE the user script imports it.
try:
    import matplotlib
    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt
    from matplotlib.figure import Figure
    _orig_figure_savefig = Figure.savefig
    _counter = [0]

    def _patched_figure_savefig(self, fname, *args, **kwargs):
        if isinstance(fname, str):
            if not os.path.isabs(fname):
                # Bare filename / relative path: route to $OUT so the
                # chart is captured regardless of cwd.
                fname = os.path.join(_OUT_DIR, os.path.basename(fname))
            else:
                # Absolute path: respect intent, but pre-create the
                # parent dir so naive scripts don't FileNotFoundError
                # on a path that the user-container hasn't materialised
                # (common with /workspace/.artifacts/<sid>/...).
                parent = os.path.dirname(fname)
                if parent:
                    try:
                        os.makedirs(parent, exist_ok=True)
                    except OSError:
                        pass
        return _orig_figure_savefig(self, fname, *args, **kwargs)
    Figure.savefig = _patched_figure_savefig

    def _show(*args, **kwargs):
        try:
            fig = plt.gcf()
            if not fig.axes:
                return
            _counter[0] += 1
            path = os.path.join(_OUT_DIR, "figure_{:03d}.png".format(_counter[0]))
            # Bypass the patched savefig (which would no-op the redirect
            # since we're already passing an absolute path under $OUT_DIR).
            _orig_figure_savefig(fig, path, dpi=120, bbox_inches="tight")
            print("[runner] saved figure -> figure_{:03d}.png".format(_counter[0]), file=sys.stderr, flush=True)
            plt.close(fig)
        except Exception as exc:
            print("[runner] show() shim failed: " + str(exc), file=sys.stderr, flush=True)
    plt.show = _show
except ImportError:
    pass

# Run the user's script as __main__ from the output dir so any relative
# paths it writes (open("data.csv", "w") etc.) also land in /out and get
# surfaced as media.
os.chdir(_OUT_DIR)
runpy.run_path(_SCRIPT, run_name="__main__")
"""


# ---------------------------------------------------------------------------
# Per-user docker exec helpers.
# ---------------------------------------------------------------------------

EXEC_UID = "1000:1000"


async def _docker_exec(
    container: str,
    argv: list[str],
    *,
    stdin_bytes: bytes | None = None,
    timeout: float = 30.0,
) -> tuple[int, bytes, bytes]:
    """Run `docker exec` and return (returncode, stdout, stderr)."""
    full = ["docker", "exec", "-i", "--user", EXEC_UID, container, *argv]
    proc = await asyncio.create_subprocess_exec(
        *full,
        stdin=asyncio.subprocess.PIPE if stdin_bytes is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin_bytes), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise
    return (proc.returncode or 0, out, err)


# ---------------------------------------------------------------------------
# Run state.
# ---------------------------------------------------------------------------

class _Event(dict):
    """One SSE event in the run's history. We keep them as plain dicts so
    `_sse_event(type, payload)` in app.py can format them without any
    further translation."""


class Run:
    """One Python-artifact execution."""

    def __init__(self, *, run_id: str, email: str, session_id: str, filename: str, container: str):
        self.id = run_id
        self.email = email
        self.session_id = session_id
        self.filename = filename
        self.container = container
        self.in_container_dir = f"{_OUTPUT_DIR_IN_CONTAINER}/{run_id}"
        self.out_dir_in_container = f"{self.in_container_dir}/out"
        # Mirror of the chat backend's per-session artifacts dir inside
        # the user container. Scripts (especially LLM-written ones) often
        # save to this path directly because it's the convention chat
        # docs to the model. We pre-create it before the run and scan
        # it for new files on completion, in addition to $OUT.
        self.session_artifacts_dir = f"/workspace/.artifacts/{session_id}"
        self.started_at = time.time()
        self.finished_at: Optional[float] = None
        self.exit_code: Optional[int] = None
        self.status: str = "starting"  # starting | running | done | error | cancelled
        # Bounded event history so the modal can re-attach and replay.
        # 10k lines covers very chatty scripts; anything beyond that is
        # truncated with a `truncated` flag in the next event.
        self.events: deque[dict] = deque(maxlen=10_000)
        self._new_event = asyncio.Event()
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._task: Optional[asyncio.Task] = None
        self._media: list[dict] = []   # populated on completion
        self._cancelled = False

    # ---------- public API used by the FastAPI layer ----------------

    def append_event(self, kind: str, payload: dict[str, Any]) -> None:
        evt = {"kind": kind, "ts": time.time(), **payload}
        self.events.append(evt)
        self._new_event.set()

    async def subscribe(self) -> AsyncIterator[dict]:
        """Replay every event so far, then yield new ones until terminal.

        Tracks position via the deque's length: each tick the subscriber
        yields events whose index is at-or-past `cursor`, then advances
        `cursor` to the new length. Safe because the deque only appends
        (and `maxlen` bounds memory — we'd lose oldest events on a
        runaway-output run, but new subscribers still see the latest).
        """
        cursor = 0
        while True:
            snapshot = list(self.events)
            if cursor < len(snapshot):
                # Yield everything we haven't sent yet.
                for evt in snapshot[cursor:]:
                    yield evt
                cursor = len(snapshot)
            if self._is_terminal():
                return
            self._new_event.clear()
            try:
                await asyncio.wait_for(self._new_event.wait(), timeout=15.0)
            except asyncio.TimeoutError:
                # Keepalive — keeps the SSE connection warm and lets
                # proxies (CF tunnel) avoid the idle timeout. Frontend
                # ignores `ping` events.
                yield {"kind": "ping", "ts": time.time()}
                continue

    def _is_terminal(self) -> bool:
        return self.status in ("done", "error", "cancelled")

    async def cancel(self) -> None:
        if self._is_terminal():
            return
        self._cancelled = True
        proc = self._proc
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        # `_run_loop` finally-block will set status + append terminal event.


# ---------------------------------------------------------------------------
# Registry.
# ---------------------------------------------------------------------------

_runs: dict[str, Run] = {}
_lock = asyncio.Lock()


def get_run(run_id: str) -> Optional[Run]:
    return _runs.get(run_id)


def list_runs_for_user(email: str) -> list[Run]:
    return [r for r in _runs.values() if r.email == email]


def _active_count(email: str) -> int:
    return sum(1 for r in _runs.values() if r.email == email and not r._is_terminal())


async def start_run(
    *,
    email: str,
    session_id: str,
    container: str,
    filename: str,
    script_bytes: bytes,
) -> Run:
    if _active_count(email) >= _MAX_PER_USER:
        raise RunError(
            f"too many concurrent runs ({_MAX_PER_USER} max); cancel an existing run first",
            status_code=429,
        )
    run_id = secrets.token_hex(8)
    run = Run(run_id=run_id, email=email, session_id=session_id, filename=filename, container=container)
    _runs[run_id] = run

    # Schedule the runner. start_run returns synchronously so the HTTP
    # response is fast; the long-running work happens in `_run_loop`.
    run._task = asyncio.create_task(_run_loop(run, script_bytes))
    return run


async def _run_loop(run: Run, script_bytes: bytes) -> None:
    """Driver: prepare files in the container, exec the runner shim,
    stream stdout/stderr into the run's event log, collect output media."""
    try:
        # 1) Prepare /tmp/wizerith-runs/<rid>/out  AND  /workspace/.artifacts/<sid>.
        # The latter is the chat backend's per-session artifacts convention
        # — LLM-written scripts often save to absolute paths under there.
        # We pre-create it so naive `fig.savefig("/workspace/.artifacts/<sid>/foo.png")`
        # calls don't FileNotFoundError, then scan it on completion to
        # surface any files the script produced there as media.
        rc, _, err = await _docker_exec(
            run.container,
            [
                "sh", "-c",
                f"mkdir -p {_shquote(run.out_dir_in_container)} && "
                f"mkdir -p {_shquote(run.session_artifacts_dir)}",
            ],
            timeout=15.0,
        )
        if rc != 0:
            run.append_event("stderr", {"text": f"[runner] mkdir failed: {err.decode('utf-8', 'replace')[:400]}\n"})
            _terminate(run, status="error", exit_code=rc)
            return

        # Write the source via `tee` + stdin. Keep the artifact's real
        # extension in the in-container name so the compiler (for C/C++)
        # and any __file__ lookups behave naturally.
        lang = _detect_language(run.filename)
        src_ext = os.path.splitext(run.filename)[1].lower() or ".py"
        src_name = "script.py" if lang == "python" else f"source{src_ext}"
        script_path = f"{run.in_container_dir}/{src_name}"
        rc, _, err = await _docker_exec(
            run.container,
            ["sh", "-c", f"cat > {_shquote(script_path)}"],
            stdin_bytes=script_bytes,
            timeout=30.0,
        )
        if rc != 0:
            run.append_event("stderr", {"text": f"[runner] write source failed: {err.decode('utf-8', 'replace')[:400]}\n"})
            _terminate(run, status="error", exit_code=rc)
            return

        # 2) Compiled languages: build the binary first, streaming any
        # compiler diagnostics. A failed compile is a terminal error.
        bin_path = f"{run.in_container_dir}/program"
        if lang in ("cpp", "c"):
            compiler = "g++" if lang == "cpp" else "gcc"
            std = "gnu++17" if lang == "cpp" else "gnu11"
            run.append_event("stdout", {"text": f"[runner] compiling with {compiler} -O2 -std={std} ...\n"})
            compile_cmd = (
                f"{compiler} -O2 -std={std} -pipe "
                f"-o {_shquote(bin_path)} {_shquote(script_path)} -lm"
            )
            try:
                crc, cout, cerr = await _docker_exec(
                    run.container, ["sh", "-c", compile_cmd], timeout=_COMPILE_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                run.append_event("stderr", {"text": f"[runner] compile timed out after {int(_COMPILE_TIMEOUT_S)}s — killed\n"})
                _terminate(run, status="error", exit_code=124)
                return
            diag = (cout.decode("utf-8", "replace") + cerr.decode("utf-8", "replace")).strip()
            if diag:
                run.append_event("stderr", {"text": diag + "\n"})
            if crc != 0:
                run.append_event("stderr", {"text": f"[runner] compilation failed (exit {crc})\n"})
                _terminate(run, status="error", exit_code=crc)
                return
            run.append_event("stdout", {"text": "[runner] compiled OK — running\n"})

        # 3) Spawn the program. Python runs via the inlined shim under
        # python3 -u (unbuffered); compiled languages exec their binary
        # with cwd=$OUT so relative file writes land there and get
        # surfaced as media, matching the Python behaviour.
        run.status = "running"
        run.append_event("status", {"status": "running"})
        if lang == "python":
            full_argv = [
                "docker", "exec", "-i", "--user", EXEC_UID,
                "-e", f"WIZERITH_RUN_OUT={run.out_dir_in_container}",
                "-e", f"WIZERITH_RUN_SCRIPT={script_path}",
                run.container,
                "/usr/local/bin/python3", "-u", "-c", _RUNNER_SHIM,
            ]
        else:
            full_argv = [
                "docker", "exec", "-i", "--user", EXEC_UID,
                "-e", f"WIZERITH_RUN_OUT={run.out_dir_in_container}",
                run.container,
                "sh", "-c",
                f"cd {_shquote(run.out_dir_in_container)} && exec {_shquote(bin_path)}",
            ]
        run._proc = await asyncio.create_subprocess_exec(
            *full_argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Pump stdout + stderr concurrently.
        async def _pump(stream, kind: str):
            assert stream is not None
            while True:
                chunk = await stream.readline()
                if not chunk:
                    return
                text = chunk.decode("utf-8", errors="replace")
                run.append_event(kind, {"text": text})

        # Bound the whole run with _RUN_TIMEOUT_S.
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    _pump(run._proc.stdout, "stdout"),
                    _pump(run._proc.stderr, "stderr"),
                    run._proc.wait(),
                ),
                timeout=_RUN_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            try:
                run._proc.kill()
            except ProcessLookupError:
                pass
            run.append_event("stderr", {"text": f"[runner] timed out after {int(_RUN_TIMEOUT_S)}s — killed\n"})
            _terminate(run, status="error", exit_code=124)
            return

        exit_code = run._proc.returncode if run._proc.returncode is not None else 0

        # 3) Collect media that landed in $OUT_DIR.
        try:
            media = await _collect_media(run)
        except Exception as exc:  # noqa: BLE001
            logger.warning("collect_media failed for run %s: %s", run.id, exc)
            media = []
        run._media = media
        if media:
            run.append_event("media", {"items": media})

        if run._cancelled:
            _terminate(run, status="cancelled", exit_code=exit_code)
        elif exit_code == 0:
            _terminate(run, status="done", exit_code=exit_code)
        else:
            _terminate(run, status="error", exit_code=exit_code)
    except Exception as exc:  # noqa: BLE001
        logger.exception("run %s crashed in _run_loop", run.id)
        run.append_event("stderr", {"text": f"[runner] internal error: {exc}\n"})
        _terminate(run, status="error", exit_code=-1)


def _terminate(run: Run, *, status: str, exit_code: int) -> None:
    run.status = status
    run.exit_code = exit_code
    run.finished_at = time.time()
    run.append_event("done", {"status": status, "exit_code": exit_code, "media": run._media})


async def _collect_media(run: Run) -> list[dict]:
    """Collect files the script produced as media tiles.

    Two locations are scanned:
      1. The run's $OUT (`/tmp/wizerith-runs/<rid>/out/`) — all files.
      2. The session artifacts dir (`/workspace/.artifacts/<sid>/`) —
         filtered to files modified during this run (mtime > run.started_at).
    Duplicates by filename are de-deduped, $OUT wins (since the matplotlib
    shim writes there, and the model's script may write the same name to
    both — we don't want two tiles for the same chart).
    """
    # One docker-exec for both scans — emit lines tagged with their source
    # so we can route media URLs correctly.
    out_dir = run.out_dir_in_container
    art_dir = run.session_artifacts_dir
    # `find ... -newermt @<unix-ts>` filters by mtime. Use the run's
    # started_at as the cutoff so prior turns' artifacts don't bleed in.
    started_ts = int(run.started_at)
    script = (
        f"find {_shquote(out_dir)} -maxdepth 1 -type f "
        f"-printf 'out\\t%f\\t%s\\n' 2>/dev/null | head -n {_MAX_OUTPUT_FILES}; "
        f"find {_shquote(art_dir)} -maxdepth 1 -type f "
        f"-newermt '@{started_ts}' "
        f"-printf 'art\\t%f\\t%s\\n' 2>/dev/null | head -n {_MAX_OUTPUT_FILES}"
    )
    rc, out, err = await _docker_exec(
        run.container, ["sh", "-c", script], timeout=10.0,
    )
    if rc != 0:
        return []
    items: list[dict] = []
    seen: set[str] = set()
    for line in out.decode("utf-8", "replace").splitlines():
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        source, name, size_s = parts
        if not name or not _MEDIA_FILENAME_RE.match(name):
            continue
        if name in seen:
            continue  # $OUT wins because find ordering above emits it first
        try:
            size = int(size_s)
        except ValueError:
            continue
        ext = os.path.splitext(name)[1].lower()
        mime = _MEDIA_TYPES.get(ext, "application/octet-stream")
        items.append({
            "filename": name,
            "size": size,
            "mime": mime,
            "url": f"/api/sessions/{run.session_id}/runs/{run.id}/media/{name}",
        })
        seen.add(name)
    items.sort(key=lambda d: d["filename"])
    return items


async def read_media_bytes(run: Run, filename: str) -> tuple[bytes, str]:
    """Stream a media file's bytes back from the user container.

    Tries the run's $OUT first, then falls back to the session artifacts
    dir. This matches `_collect_media`'s preference and means a media URL
    issued at run-completion stays valid even if the script wrote to one
    location and not the other.
    """
    if not _MEDIA_FILENAME_RE.match(filename):
        raise RunError("invalid media filename", status_code=400)
    last_err: Optional[RunError] = None
    for dir_in in (run.out_dir_in_container, run.session_artifacts_dir):
        path_in = f"{dir_in}/{filename}"
        rc, out, _err = await _docker_exec(
            run.container, ["stat", "-c", "%s", path_in], timeout=5.0,
        )
        if rc != 0:
            last_err = RunError("media not found", status_code=404)
            continue
        try:
            size = int(out.strip())
        except ValueError:
            last_err = RunError("media size unreadable", status_code=500)
            continue
        if size > _MAX_MEDIA_BYTES:
            raise RunError(f"media exceeds {_MAX_MEDIA_BYTES} bytes", status_code=413)
        rc, data, _err = await _docker_exec(
            run.container, ["cat", path_in], timeout=30.0,
        )
        if rc != 0:
            last_err = RunError("media read failed", status_code=500)
            continue
        ext = os.path.splitext(filename)[1].lower()
        mime = _MEDIA_TYPES.get(ext, "application/octet-stream")
        return data, mime
    raise last_err or RunError("media not found", status_code=404)


# ---------------------------------------------------------------------------
# GC sweeper.
# ---------------------------------------------------------------------------

async def _gc_loop() -> None:
    while True:
        try:
            now = time.time()
            stale: list[Run] = []
            for run in list(_runs.values()):
                if run.finished_at is not None and now - run.finished_at > _RUN_RETENTION_S:
                    stale.append(run)
            for run in stale:
                _runs.pop(run.id, None)
                # Best-effort container cleanup. Fire-and-forget; if the
                # container is gone (eviction etc.) we don't care.
                asyncio.create_task(_cleanup_run_dir(run))
        except Exception:  # noqa: BLE001
            logger.exception("artifact_runner GC loop crashed")
        await asyncio.sleep(60.0)


async def _cleanup_run_dir(run: Run) -> None:
    try:
        await _docker_exec(
            run.container,
            ["rm", "-rf", "--", run.in_container_dir],
            timeout=10.0,
        )
    except Exception:
        pass


def install_gc_task(loop: asyncio.AbstractEventLoop) -> None:
    """Mount the GC sweeper on the given event loop. Called from app
    startup so the runner cleans up after itself without growing
    unbounded."""
    loop.create_task(_gc_loop())


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------

_SHQUOTE_SAFE = re.compile(r"^[A-Za-z0-9_./@:+=-]+$")


def _shquote(s: str) -> str:
    if _SHQUOTE_SAFE.match(s):
        return s
    return "'" + s.replace("'", "'\"'\"'") + "'"
