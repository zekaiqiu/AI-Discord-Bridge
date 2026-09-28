"""Persistent per-session claude process (A1: the claude.ai turn model).

The legacy path spawned a fresh ``claude -p`` per user turn, wrote the prompt,
read stdout to EOF, and let the process exit at the first ``result``. That
threw away anything the agent kicked off in the background: a turn that started
a long task and "waited" simply ended, and the task's completion had nowhere to
land — the agent never reported back.

This module keeps **one long-lived process per chat session** open in realtime
streaming-input mode (``claude -p --verbose --input-format stream-json
--output-format stream-json``). Verified live against CLI 2.1.172, a backgrounded
task auto-surfaces WITHOUT a new user message:

    result/success            # turn 1 ends
    system/task_notification  # background task finished — CLI pings itself
    system/init               # a NEW turn opens autonomously
    assistant/ "...done..."   # the agent reports back
    result/success            # turn 2 ends

So the session process produces a STREAM OF TURNS, not one:
  * the user turn (opened by ``send_user``), then
  * zero or more *auto-continuation* turns the CLI opens on its own when a
    background task completes.

``SessionProcess`` exposes both: ``send_user`` returns the user ``Turn``, and
async-iterating the instance yields every subsequent auto ``Turn`` as it begins.
A ``Turn`` is itself an async iterator of the raw stream-json event dicts that
belong to it (terminated by its ``result``). The caller (app worker) normalises
those into the existing delta/tool_start/tool_end/done events and fans each turn
into its own ``_TurnRun`` / SSE.

Lifecycle: a process stays alive until it is idle AND has no pending background
tasks for ``idle_grace`` seconds (then evicted), or the session is closed, or it
dies. Turn framing and background-task accounting are driven entirely by the
event types above, so the policy needs no guesswork.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from typing import Any, AsyncIterator, Optional


# Event types the CLI emits that drive framing / bg-task accounting. Kept here
# (not buried in branches) so the protocol this relies on is auditable.
_TASK_STARTED = "task_started"          # system/ subtype — a bg task began
_TASK_NOTIFICATION = "task_notification"  # system/ subtype — a bg task finished
_RESULT = "result"                       # top-level type — a turn ended


class Turn:
    """One agent turn: an async stream of raw stream-json event dicts, closed
    when its ``result`` event arrives. ``kind`` is ``"user"`` for the turn a
    ``send_user`` opened, or ``"auto"`` for a CLI-initiated continuation (a
    background task reported back)."""

    _SENTINEL = object()

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self._q: asyncio.Queue[Any] = asyncio.Queue()
        self.closed = False

    def _push(self, event: dict[str, Any]) -> None:
        self._q.put_nowait(event)

    def _close(self) -> None:
        if not self.closed:
            self.closed = True
            self._q.put_nowait(self._SENTINEL)

    def __aiter__(self) -> "Turn":
        return self

    async def __anext__(self) -> dict[str, Any]:
        item = await self._q.get()
        if item is self._SENTINEL:
            raise StopAsyncIteration
        return item


class SessionProcess:
    """A single persistent ``claude`` streaming-input process for one session."""

    def __init__(
        self,
        args: list[str],
        env: Optional[dict[str, str]] = None,
        *,
        readline_limit: int = 16 * 1024 * 1024,
    ) -> None:
        self._args = args
        self._env = env
        self._readline_limit = readline_limit
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._pump_task: Optional[asyncio.Task[None]] = None

        # Turn framing. ``_current`` is the open turn receiving events; the pump
        # opens a fresh auto-Turn when events arrive with no turn open.
        self._current: Optional[Turn] = None
        self._auto_turns: asyncio.Queue[Any] = asyncio.Queue()

        # Background-task accounting drives idle eviction: never evict while the
        # agent is waiting on something that will ping it back.
        self.pending_bg = 0
        self.last_activity = _now()
        self._closed = False

    # ---- lifecycle ----------------------------------------------------------
    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *self._args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=os.setsid,
            limit=self._readline_limit,
            env=self._env,
        )
        self._pump_task = asyncio.create_task(self._pump())

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None and not self._closed

    def is_evictable(self, idle_grace: float) -> bool:
        """Safe to terminate: not closed already, no open turn, no pending
        background work, and idle past the grace window."""
        return (
            self.pending_bg == 0
            and self._current is None
            and (_now() - self.last_activity) >= idle_grace
        )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        proc = self._proc
        if proc is not None and proc.returncode is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        if self._pump_task is not None:
            self._pump_task.cancel()
        # Unblock anyone awaiting a turn / auto-turn.
        if self._current is not None:
            self._current._close()
            self._current = None
        self._auto_turns.put_nowait(_CLOSED)

    # ---- input --------------------------------------------------------------
    async def send_user(self, text: str, *, wait_timeout: float = 180.0) -> Turn:
        """Open a user turn: write one stream-json user message and return the
        ``Turn`` that will stream this turn's events.

        The app worker serialises *user* turns per session (the legacy at-most-
        one-run invariant), but a CLI-initiated **auto-continuation** turn (a
        backgrounded task reporting back) can hold ``_current`` open at any
        moment — independent of the user-run accounting. A user message arriving
        during that window must not crash the worker; it waits for the in-flight
        turn to close (the pump clears ``_current`` on its ``result``), bounded
        by ``wait_timeout``. The poll is race-free: there is no ``await`` between
        the final ``_current is None`` check and the assignment below, so the
        pump cannot open an auto-turn in the gap (single-threaded asyncio)."""
        if not self.alive or self._proc is None or self._proc.stdin is None:
            raise RuntimeError("session process is not alive")
        deadline = _now() + wait_timeout
        while self._current is not None:
            if not self.alive:
                raise RuntimeError("session process is not alive")
            if _now() >= deadline:
                raise RuntimeError(
                    "timed out waiting for the prior turn to close before sending"
                )
            await asyncio.sleep(0.05)
        turn = Turn("user")
        self._current = turn
        self.last_activity = _now()
        line = json.dumps(
            {
                "type": "user",
                "message": {"role": "user", "content": [{"type": "text", "text": text}]},
            }
        )
        self._proc.stdin.write((line + "\n").encode("utf-8"))
        await self._proc.stdin.drain()
        return turn

    # ---- auto-continuation turns -------------------------------------------
    def __aiter__(self) -> "SessionProcess":
        return self

    async def __anext__(self) -> Turn:
        item = await self._auto_turns.get()
        if item is _CLOSED:
            raise StopAsyncIteration
        return item

    # ---- the stdout pump ----------------------------------------------------
    async def _pump(self) -> None:
        """Read stdout forever, frame events into turns, account bg tasks.

        An event arriving while no turn is open means the CLI started a turn on
        its own (a background task pinged it) → open an auto-Turn and surface it
        via __aiter__. A ``result`` closes the open turn."""
        assert self._proc is not None and self._proc.stdout is not None
        stdout = self._proc.stdout
        try:
            while True:
                raw = await stdout.readline()
                if not raw:
                    break  # process exited
                line = raw.rstrip(b"\r\n")
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                self.last_activity = _now()
                self._account_bg(evt)

                if self._current is None:
                    # CLI-initiated continuation turn (task reported back).
                    self._current = Turn("auto")
                    self._auto_turns.put_nowait(self._current)

                self._current._push(evt)
                if evt.get("type") == _RESULT:
                    self._current._close()
                    self._current = None
        except asyncio.CancelledError:
            raise
        finally:
            # Process ended: close any open turn and stop auto-turn iteration.
            if self._current is not None:
                self._current._close()
                self._current = None
            self._auto_turns.put_nowait(_CLOSED)

    def _account_bg(self, evt: dict[str, Any]) -> None:
        if evt.get("type") != "system":
            return
        sub = evt.get("subtype")
        if sub == _TASK_STARTED:
            self.pending_bg += 1
        elif sub == _TASK_NOTIFICATION:
            # A background task finished (and is about to drive a continuation
            # turn). Clamp at 0 — we never want a negative latch wedging
            # eviction open forever if start/notify accounting ever drifts.
            self.pending_bg = max(0, self.pending_bg - 1)


_CLOSED = object()


def _now() -> float:
    return time.monotonic()


class SessionProcessManager:
    """Owns the live ``SessionProcess`` per chat session: get-or-spawn, the
    per-session auto-continuation consumer, and idle eviction.

    Decoupled from the app worker: the caller supplies ``make_args_env`` (builds
    the argv/env for a session's process, e.g. via ``_build_run_args(...,
    streaming_input=True)``) and ``on_auto_turn`` (handles a CLI-initiated
    continuation Turn — in app.py: open a new ``_TurnRun``, persist the assistant
    message, stream it live). The manager runs the ``async for turn in proc``
    loop itself so the consumer's lifetime is tied to the process.
    """

    def __init__(self) -> None:
        self._procs: dict[Any, SessionProcess] = {}
        self._consumers: dict[Any, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()

    def get(self, key: Any) -> Optional[SessionProcess]:
        sp = self._procs.get(key)
        return sp if (sp is not None and sp.alive) else None

    async def get_or_create(self, key: Any, make_args_env, on_auto_turn) -> SessionProcess:
        """Return the session's live process, spawning one (and its auto-turn
        consumer) if absent or dead. A dead process is replaced; the new one
        ``--resume``s the same claude session so context is preserved."""
        async with self._lock:
            sp = self._procs.get(key)
            if sp is not None and sp.alive:
                return sp
            if sp is not None:
                await self._drop_locked(key)  # replace a dead/closed process
            args, env = make_args_env()
            sp = SessionProcess(args, env)
            await sp.start()
            self._procs[key] = sp
            self._consumers[key] = asyncio.create_task(
                self._consume_auto(key, sp, on_auto_turn)
            )
            return sp

    async def _consume_auto(self, key: Any, sp: SessionProcess, on_auto_turn) -> None:
        try:
            async for turn in sp:
                try:
                    await on_auto_turn(key, turn)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 — one bad auto-turn must not
                    pass            # kill the consumer / orphan the process
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            pass
        finally:
            # Process ended (EOF): drop it so the next user turn respawns +
            # --resumes. Only drop if we're still the registered process.
            if self._procs.get(key) is sp:
                self._procs.pop(key, None)
                self._consumers.pop(key, None)

    async def evict_idle(self, idle_grace: float) -> int:
        """Terminate processes idle past ``idle_grace`` with no open turn and no
        pending background tasks. Returns the count evicted."""
        evicted = 0
        async with self._lock:
            for key, sp in list(self._procs.items()):
                if sp.is_evictable(idle_grace):
                    await self._drop_locked(key)
                    evicted += 1
        return evicted

    async def run_eviction_loop(
        self, stop: asyncio.Event, *, interval: float = 60.0, idle_grace: float = 900.0
    ) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            if stop.is_set():
                break
            try:
                await self.evict_idle(idle_grace)
            except Exception:  # noqa: BLE001 — eviction errors are non-fatal
                pass

    async def _drop_locked(self, key: Any) -> None:
        sp = self._procs.pop(key, None)
        consumer = self._consumers.pop(key, None)
        if consumer is not None:
            consumer.cancel()
        if sp is not None:
            await sp.aclose()

    async def aclose_all(self) -> None:
        async with self._lock:
            for key in list(self._procs.keys()):
                await self._drop_locked(key)
