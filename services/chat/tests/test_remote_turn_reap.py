"""A cancelled host/user turn must not leave claude running in the container.

2026-09-17: `docker exec` does not forward termination to the process it
started. Cancelling a turn killed only the local docker-exec client; the
per-session lock was released while `claude --resume <sid>` kept running inside
chat-host-shell, and the next message resumed the SAME CLI session in a second
process. Two copies of one conversation then edited the same files -- the
orphan wrote to a repo 12s after its turn was "cancelled".

These pin the fix: a remote turn records its in-container PID and is reaped in
the container on completion AND on cancellation, and a `--resume` turn first
reaps any process still running that session.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

import pytest

import claude_runner as cr

SID = "3692aa46-7605-4f53-a3d7-1d6818af1adf"
PIDFILE_RE = re.compile(r"^/tmp/chat-turn-[0-9a-f]{32}\.pid$")


class _Stream:
    def __init__(self, lines, block_after=False):
        self._lines = list(lines)
        self._block = block_after

    async def readline(self):
        if self._lines:
            return self._lines.pop(0)
        if self._block:
            await asyncio.Event().wait()     # a turn still producing: never returns
        return b""

    async def read(self):
        return b""


class _Proc:
    def __init__(self, lines, block_after=False):
        self.stdout = _Stream(lines, block_after)
        self.stderr = _Stream([])
        self.stdin = None
        self.pid = 2 ** 22 + 12345           # not a real pid; killpg raises and is caught
        self.returncode = None

    async def wait(self):
        self.returncode = 0
        return 0


@pytest.fixture
def rig(monkeypatch):
    """Records every spawn and every in-container reap, in order."""
    events = []

    def make(lines=(b'{"type":"x"}',), block_after=False):
        async def fake_exec(*argv, **kw):
            events.append(("spawn", list(argv)))
            return _Proc(lines, block_after)

        async def fake_reap(prefix, script, *args, timeout=15.0):
            kind = ("pidfile" if script is cr._REAP_PIDFILE_SH
                    else "resume" if script is cr._REAP_RESUME_SH else "other")
            events.append(("reap", kind, list(prefix), list(args)))
            return ""

        monkeypatch.setattr(cr.asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(cr, "_reap_in_container", fake_reap)
        monkeypatch.setattr(cr.user_container, "refresh_credentials_if_stale",
                            lambda *a, **k: None)
        return events
    return make


async def _drain(gen):
    return [line async for line in gen]


def _spawn_argv(events):
    return next(e[1] for e in events if e[0] == "spawn")


def _reaps(events, kind):
    return [e for e in events if e[0] == "reap" and e[1] == kind]


# ---- the wrapper ---------------------------------------------------------------
def test_host_turn_runs_behind_the_pidfile_wrapper(rig):
    ev = rig()
    asyncio.run(_drain(cr.spawn_claude(["claude", "-p", "hi"], dispatch="host")))
    argv = _spawn_argv(ev)
    i = argv.index(cr._HOST_SHELL_CONTAINER)
    assert argv[i + 1:i + 4] == ["sh", "-c", cr._PIDFILE_WRAPPER]
    assert PIDFILE_RE.match(argv[i + 4]), argv[i + 4]
    assert argv[i + 5:] == ["claude", "-p", "hi"]


def test_user_turn_runs_behind_the_pidfile_wrapper(rig):
    ev = rig()
    asyncio.run(_drain(cr.spawn_claude(["claude", "-p", "hi"], dispatch="user",
                                       container_name="portfolio-user-abc")))
    argv = _spawn_argv(ev)
    i = argv.index("portfolio-user-abc")
    assert argv[i + 1:i + 4] == ["sh", "-c", cr._PIDFILE_WRAPPER]
    assert argv[i + 5:] == ["claude", "-p", "hi"]
    (reap,) = _reaps(ev, "pidfile")
    assert reap[2] == ["docker", "exec", "--user", "1000:1000", "portfolio-user-abc"]


def test_local_turn_is_untouched(rig):
    ev = rig()
    asyncio.run(_drain(cr.spawn_claude(["claude", "-p", "hi"], dispatch="local")))
    assert _spawn_argv(ev) == ["claude", "-p", "hi"]
    assert [e for e in ev if e[0] == "reap"] == []


# ---- reaping ---------------------------------------------------------------------
def test_completed_host_turn_reaps_its_own_pidfile(rig):
    ev = rig()
    asyncio.run(_drain(cr.spawn_claude(["claude", "-p", "hi"], dispatch="host")))
    argv = _spawn_argv(ev)
    pidfile = argv[argv.index(cr._PIDFILE_WRAPPER) + 1]
    (reap,) = _reaps(ev, "pidfile")
    assert reap[2] == ["docker", "exec", cr._HOST_SHELL_CONTAINER]
    assert reap[3] == [pidfile, "claude"]


def test_CANCELLED_host_turn_is_reaped_inside_the_container(rig):
    """THE BUG. Cancellation used to kill only the local docker-exec client."""
    ev = rig(lines=[b'{"type":"x"}'], block_after=True)

    async def cancel_mid_turn():
        gen = cr.spawn_claude(["claude", "--resume", SID, "-p", "x"], dispatch="host")
        await gen.__anext__()                # the turn is running...
        await gen.aclose()                   # ...and the user cancels it

    asyncio.run(cancel_mid_turn())
    argv = _spawn_argv(ev)
    pidfile = argv[argv.index(cr._PIDFILE_WRAPPER) + 1]
    assert [r[3] for r in _reaps(ev, "pidfile")] == [[pidfile, "claude"]]


def test_resume_turn_reaps_orphans_BEFORE_it_spawns(rig):
    ev = rig()
    asyncio.run(_drain(cr.spawn_claude(["claude", "--resume", SID, "-p", "x"],
                                       dispatch="host")))
    kinds = [e[0] if e[0] == "spawn" else e[1] for e in ev]
    assert kinds.index("resume") < kinds.index("spawn"), kinds
    (reap,) = _reaps(ev, "resume")
    assert reap[3] == [cr._resume_pattern("claude", SID)]


def test_fresh_turn_does_not_reap_by_session(rig):
    ev = rig()
    asyncio.run(_drain(cr.spawn_claude(["claude", "--session-id", SID, "-p", "x"],
                                       dispatch="host")))
    assert _reaps(ev, "resume") == []


# ---- the session id never reaches a regex unchecked ----------------------------
def test_resume_session_id_is_validated():
    assert cr._resume_session_id(["claude", "--resume", SID]) == SID
    assert cr._resume_session_id(["claude", "-p", "x"]) is None
    assert cr._resume_session_id(["claude", "--resume"]) is None
    assert cr._resume_session_id(["claude", "--resume", ".*"]) is None
    assert cr._resume_session_id(["claude", "--resume", "x; kill -9 1"]) is None


def test_resume_pattern_matches_the_orphan_and_nothing_else():
    pat = re.compile(cr._resume_pattern("claude", SID))
    assert pat.search("claude --resume %s --output-format stream-json" % SID)
    assert pat.search("/home/linuxbrew/.linuxbrew/bin/claude --resume %s" % SID)
    assert pat.search("claude --resume %s" % SID)
    # the reaper's own sh carries the pattern in its argv and must not match
    assert not pat.search("sh -c <script> sh ^([^ ]*/)?claude --resume %s( |$)" % SID)
    assert not pat.search("claude --resume %sX" % SID)
    assert not pat.search("docker exec host-shell claude --resume %s" % SID)


def test_reap_never_raises(monkeypatch):
    async def boom(*a, **k):
        raise FileNotFoundError("docker")
    monkeypatch.setattr(cr.asyncio, "create_subprocess_exec", boom)
    assert asyncio.run(cr._reap_in_container(["docker", "exec", "x"], "true")) == ""


# ---- the real shell script, against real processes -----------------------------
needs_proc = pytest.mark.skipif(
    not (sys.platform.startswith("linux") and shutil.which("pgrep") and shutil.which("sleep")),
    reason="needs Linux /proc and pgrep")


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # a zombie child of this test still answers kill(0); read its state
    with open("/proc/%d/stat" % pid) as fh:
        return fh.read().split(")")[-1].split()[0] != "Z"


@needs_proc
def test_pidfile_script_kills_the_tree():
    child = subprocess.Popen(["sleep", "60"])
    with tempfile.NamedTemporaryFile("w", suffix=".pid", delete=False) as fh:
        fh.write("%d\n" % child.pid)
    out = subprocess.run(["sh", "-c", cr._REAP_PIDFILE_SH, "sh", fh.name, "sleep"],
                         capture_output=True, text=True, timeout=20)
    child.wait(timeout=5)
    assert not _alive(child.pid)
    assert "reaped %d" % child.pid in out.stdout
    assert not os.path.exists(fh.name)


@needs_proc
def test_pidfile_script_never_kills_a_REUSED_pid():
    """A stale pidfile now naming an unrelated process must leave it alone."""
    child = subprocess.Popen(["sleep", "60"])
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".pid", delete=False) as fh:
            fh.write("%d\n" % child.pid)
        subprocess.run(["sh", "-c", cr._REAP_PIDFILE_SH, "sh", fh.name, "claude"],
                       capture_output=True, text=True, timeout=20)
        time.sleep(0.2)
        assert _alive(child.pid), "reaped a process whose argv[0] is not claude"
        assert not os.path.exists(fh.name)
    finally:
        child.kill()
        child.wait()
