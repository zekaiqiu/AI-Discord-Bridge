"""Async wrapper around the ``claude`` CLI.

Design:
  * The ENTIRE service spawns ``claude`` through a single seam,
    ``spawn_claude``. This is the only function in the codebase that calls
    ``asyncio.create_subprocess_exec``. Tests monkeypatch this seam to
    feed canned stream-json bytes; CI never spawns a real binary.
  * ``run_turn`` and ``generate_title`` both go through ``spawn_claude``,
    so any test that fakes the seam covers both code paths.
  * Each downstream consumer (the SSE endpoint, the title task) consumes
    a normalised event stream of ``{type, ...}`` dicts. The shape is
    frozen by the brief: ``delta``, ``tool_start``, ``tool_end``, ``done``,
    ``error``. ``done`` and ``error`` are mutually exclusive and each is
    emitted at most once.

Cancellation contract:
  * If a consumer of ``spawn_claude`` aborts iteration (``aclose()`` on
    the async generator, or simply stops awaiting), the generator's
    finally-block kills the process and reaps it. This keeps zombies out
    of the container even on disconnect mid-stream.
  * ``run_turn`` enforces a hard timeout via ``asyncio.wait_for`` around
    each ``__anext__`` call. On timeout it triggers ``aclose()`` on the
    underlying generator, which kills the process, and yields a single
    ``error`` event. No ``done`` follows.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import mimetypes
import os
import re
import shlex
import signal
import tarfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import docker

import prompt_blocks
import token_ledger
import user_container


_logger = logging.getLogger(__name__)

# Where staged session attachments live inside per-user containers. The
# user shell runs as uid 1000 with HOME=/workspace, so this keeps
# attachments inside the user's persistent volume; --add-dir on the
# claude invocation grants tool access to that subtree.
USER_CONTAINER_ATTACHMENTS_ROOT = "/workspace/.attachments"


class ClaudeRunnerError(Exception):
    """Raised by ``spawn_claude`` for non-zero exit / IO failure.

    ``run_turn`` catches this and turns it into a single ``error`` event
    so downstream consumers never need a try/except around iteration.
    """


class _NeedsHeal(Exception):
    """Internal signal: a ``--resume`` turn hit a CLI session the backend
    no longer has (model switched mid-session, or the per-user container
    was recreated). ``run_turn`` catches this and retries once as a fresh
    ``--session-id`` session, replaying the transcript. Never escapes the
    module."""


def _sniff_api_error(line: bytes) -> str | None:
    """Extract a transient API error from a stream-json frame, if present.

    The CLI reports retryable upstream failures as
    ``{"type":"system","subtype":"api_retry","error_status":529,
    "error":"overloaded"}`` frames on stdout, and terminal ones on the
    ``result`` frame as ``api_error_status``. On retry-EXHAUSTION the CLI
    exits non-zero with an EMPTY stderr, so these frames are the only place
    the real cause is visible. We remember the last one and fold it into the
    ClaudeRunnerError message so ``_humanize_claude_error`` can map it to a
    friendly sentence (e.g. the "overloaded" pattern). Returns a short
    string like ``"API 529 overloaded"`` or None."""
    try:
        obj = json.loads(line)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    status = obj.get("error_status") or obj.get("api_error_status")
    err = obj.get("error")
    if isinstance(err, dict):
        err = err.get("message") or err.get("type")
    parts: list[str] = []
    if status:
        parts.append(f"API {status}")
    if isinstance(err, str) and err:
        parts.append(err)
    return " ".join(parts) if parts else None


def _is_missing_session_error(exc: Exception) -> bool:
    """True when a claude failure is a stale-resume miss — i.e. ``--resume``
    against a session UUID the CLI can't find. Matches the CLI's wording
    ("No conversation found with session ID: ...") plus a couple of close
    variants so a minor CLI message change doesn't silently disable heal."""
    msg = str(exc).lower()
    return (
        "no conversation found" in msg
        or ("session" in msg and "not found" in msg)
        or "no such session" in msg
    )


# Map known claude stderr signatures to a short, user-facing sentence.
# Ordered most-specific first; the first substring match wins.
_FRIENDLY_ERROR_PATTERNS: list[tuple[str, str]] = [
    (
        "no conversation found",
        "Couldn't resume the previous conversation (it may have been "
        "cleared or the workspace was rebuilt). Send your message again to "
        "continue in a fresh session.",
    ),
    (
        "already in use",
        "This conversation is busy finishing a previous reply. Wait a "
        "moment and try again.",
    ),
    (
        "credit balance is too low",
        "The model account is out of credit. Switch models or contact the "
        "operator.",
    ),
    (
        "overloaded",
        "Anthropic's API is temporarily overloaded — your message was "
        "retried several times but kept failing. Please wait a moment and "
        "send it again.",
    ),
    ("rate limit", "Rate limited by the model. Try again in a moment."),
    # The CLI's terminal ``result`` frame reports quota/rate failures with
    # the underscored API error *type* ("rate_limit_error") or a bare 429 —
    # neither matches the "rate limit" needle above, so list them explicitly.
    (
        "rate_limit",
        "The shared chat capacity is rate-limited right now. Please wait a "
        "few minutes and send your message again.",
    ),
    (
        "429",
        "The shared chat capacity is rate-limited right now. Please wait a "
        "few minutes and send your message again.",
    ),
    (
        "invalid api key",
        "The model account isn't authenticated. The operator needs to "
        "re-login that account.",
    ),
    (
        "401",
        "The model account isn't authenticated. The operator needs to "
        "re-login that account.",
    ),
    # claude exits 0 (not non-zero) on a local auth refusal, emitting a
    # ``result`` frame with ``error: "authentication_failed"`` / result text
    # "Not logged in · Please run /login". Without a pattern this surfaces as
    # a blank assistant bubble (see _extract_result_error). During pooled-
    # account saturation a self-wiped dummy credential produces this, so keep
    # the wording transient + actionable rather than alarming.
    (
        "not logged in",
        "The chat couldn't authenticate to the shared model account just "
        "now. Please resend your message in a moment; if it keeps failing, "
        "contact the operator.",
    ),
    (
        "authentication_failed",
        "The chat couldn't authenticate to the shared model account just "
        "now. Please resend your message in a moment; if it keeps failing, "
        "contact the operator.",
    ),
]


def _humanize_claude_error(exc: Exception) -> str:
    """Turn a raw ``ClaudeRunnerError`` into a short message fit for the
    chat UI. spawn_claude already strips the giant argv from the text;
    here we map common failure signatures to plain English and otherwise
    fall back to the cleaned ``code N: <stderr tail>`` string."""
    raw = str(exc)
    low = raw.lower()
    for needle, friendly in _FRIENDLY_ERROR_PATTERNS:
        if needle in low:
            return friendly
    # Unknown failure: keep it short. spawn_claude caps the stderr tail,
    # so this is already a single short line, not the full command.
    return raw


def _account_home_path(name: str | None) -> Path | None:
    """Filesystem HOME for an account name, or None if it can't be resolved.

    "main" (and the no-account case) map to MAIN_HOME rather than None here:
    callers of THIS helper want the real directory so they can look inside it,
    not the "inherit the parent HOME" signal ``_resolve_home_for_account``
    returns."""
    try:
        from account_router import home_for_account, MAIN_HOME
    except ImportError:
        return None
    if not name or name == "main":
        try:
            return Path(MAIN_HOME)
        except Exception:
            return None
    try:
        return Path(home_for_account(name))
    except Exception:
        return None


def _transcript_exists(home: Path | None, claude_session_id: str) -> bool:
    """True when ``home`` holds a claude transcript for this session UUID.

    The CLI stores transcripts at
    ``$HOME/.claude/projects/<cwd-slug>/<session-uuid>.jsonl``. We glob the
    slug rather than recompute the CLI's cwd-mangling, so a change in that
    scheme can't silently turn this check into a false negative."""
    if home is None or not claude_session_id:
        return False
    try:
        return any(
            (home / ".claude" / "projects").glob(f"*/{claude_session_id}.jsonl")
        )
    except OSError:
        return False


def _may_rehome(
    preferred: str,
    candidate: str,
    *,
    claude_session_id: str | None,
    is_first_turn: bool,
) -> bool:
    """Whether the router may move THIS turn off its preferred account.

    ``resolve_usable_account`` is safe for container dispatch by design — its
    docstring notes "session history lives in the container; the streamed
    bearer only decides which plan gets billed". That is NOT true on the host
    path, where the account name selects HOME and HOME *is* the transcript
    root. Re-homing a ``--resume`` turn there points claude at a home that has
    never seen the conversation, and it dies with "No conversation found with
    session ID" — the whole turn is lost. (Observed 2026-08-20: two scheduled
    wakes on an admin session were silently dropped this way when account-3
    hit a 300s rate-limit cooldown.)

    So: free to re-home when there is no transcript at stake (first turn, or
    the preferred home doesn't have one either), and when the candidate home
    can serve the resume just as well. Otherwise keep the preferred account
    and let it fail loudly on saturation — a rate-limit error is recoverable
    (the caller can retry later), a wrong-home resume is not."""
    if is_first_turn or not claude_session_id:
        return True
    preferred_home = _account_home_path(preferred)
    if not _transcript_exists(preferred_home, claude_session_id):
        # Nothing to orphan: either we can't see the homes at all, or the
        # transcript is already gone (a stale-resume the error path handles).
        return True
    if _transcript_exists(_account_home_path(candidate), claude_session_id):
        return True  # both homes can serve it (shared/bind-mounted roots)
    _logger.warning(
        "keeping session on account %s despite router pick %s: resuming "
        "session %s whose transcript exists only under the preferred home",
        preferred, candidate, claude_session_id,
    )
    return False


def _resolve_home_for_account(
    name: str | None,
    *,
    claude_session_id: str | None = None,
    is_first_turn: bool = True,
) -> str | None:
    """Map a stored session ``account`` name to the HOME path the claude
    subprocess should use, or None to keep the parent's HOME (default
    single-account behavior).

    Returns None for the legacy "no account stored" case AND for "main"
    so single-account installs and pre-pool sessions both behave exactly
    as before. Returns the wizerith home path only for explicitly-named
    non-main accounts.

    The stored name is a PREFERENCE, not a pin: it is passed through
    ``user_container.resolve_usable_account`` first, so a host-dispatched
    (admin-workspace) session whose account is saturated or dead runs the
    turn on the router's best pick and returns to its preferred account
    once it recovers — the same per-turn semantics the container dispatch
    paths get via ``refresh_credentials_if_stale``. Without this, an admin
    session created while one account was the only legal pick stays pinned
    to it through saturation and burns paid overage.

    That swap is vetoed by ``_may_rehome`` when this is a ``--resume`` turn
    whose transcript lives only under the preferred account's HOME; see there
    for why. Callers that omit ``claude_session_id``/``is_first_turn`` get the
    original unconditional behavior.
    """
    if not name:
        return None
    try:
        routed = user_container.resolve_usable_account(name)
    except Exception:
        # Fail static: any resolution error keeps the stored account,
        # which is exactly the pre-resolution behavior.
        _logger.exception("account resolution failed; keeping %s", name)
        routed = name
    if routed and routed != name and _may_rehome(
        name, routed,
        claude_session_id=claude_session_id, is_first_turn=is_first_turn,
    ):
        name = routed
    if name == "main":
        return None
    try:
        from account_router import home_for_account
    except ImportError:
        return None
    home = home_for_account(name)
    # ``home_for_account`` returns MAIN_HOME on fallback; in that case we
    # want None (inherit) rather than overriding HOME explicitly to the
    # same value. Compare against the routed-to-main case.
    try:
        from account_router import MAIN_HOME
        if home == MAIN_HOME:
            return None
    except ImportError:
        pass
    return str(home)


# ---------------------------------------------------------------------------
# THE SEAM. Tests replace this attribute via monkeypatch; do not call
# ``asyncio.create_subprocess_exec`` from anywhere else in the codebase.
# ---------------------------------------------------------------------------

_HOST_SHELL_CONTAINER = os.environ.get(
    "CHAT_HOST_SHELL_CONTAINER", "portfolio-chat-host-shell"
)


def host_shell_container() -> str:
    """Name of the tenant's host-shell container (the "host" dispatch
    target). Public accessor so app.py can point the OpenAI-compatible
    runners' run_bash tool at the same container admin claude turns use."""
    return _HOST_SHELL_CONTAINER


# ---------------------------------------------------------------------------
# In-container process lifetime.
#
# `docker exec` does NOT forward termination to the process it started. Kill
# the client and the exec'd process keeps running inside the container,
# reparented to containerd-shim. Measured 2026-09-17 on
# portfolio-chat-wizerith-host-shell: `docker exec -i ... sleep 600`, client
# killed, `sleep` still alive.
#
# So every cancel or timeout of a host/user-dispatch turn killed only the local
# client. The per-session lock was released, `claude` kept running, and the next
# message for that chat resumed the SAME CLI session in a second process. That
# day two copies of one conversation edited the same uncommitted files: the
# orphan wrote to a repo 12 seconds after its turn had been "cancelled".
#
# Two parts to the fix:
#   1. Every remote turn records its in-container PID. The wrapper `exec`s
#      claude, so the recorded PID IS claude's. _kill() reaps that process tree
#      inside the container, not just the local client.
#   2. Before a `--resume <sid>` turn starts, any claude still running that CLI
#      session in the target container is reaped. The backend already allows one
#      in-flight turn per chat session, so a live one here can only be an orphan
#      (a backend restart mid-turn, or a reap that failed).
# ---------------------------------------------------------------------------

_PIDFILE_WRAPPER = 'echo $$ > "$0"; exec "$@"'

# $1 = pidfile, $2 = expected argv[0] basename. The cmdline check guards against
# PID reuse: a pidfile left by a turn that already exited must never kill an
# unrelated process that happens to have been given the same PID. TERM, ~3s
# grace, then KILL, over the whole tree (claude's tool subprocesses included).
_REAP_PIDFILE_SH = r"""
p=$(cat "$1" 2>/dev/null) || exit 0
case "$p" in ''|*[!0-9]*) rm -f "$1"; exit 0;; esac
cmd=$(tr '\0' ' ' < /proc/"$p"/cmdline 2>/dev/null) || cmd=""
case "$cmd" in "$2 "*|*"/$2 "*) ;; *) rm -f "$1"; exit 0;; esac
tree() { for c in $(pgrep -P "$1"); do tree "$c"; done; echo "$1"; }
all=$(tree "$p")
kill -TERM $all 2>/dev/null
i=0; while [ $i -lt 30 ] && kill -0 "$p" 2>/dev/null; do sleep 0.1; i=$((i+1)); done
kill -0 "$p" 2>/dev/null && kill -KILL $(tree "$p") 2>/dev/null
rm -f "$1"
echo "reaped $all"
"""

# $1 = an ERE anchored on `claude --resume <sid>`. Anchoring matters: the sh
# running THIS script carries the pattern in its own argv, and must not match.
_REAP_RESUME_SH = r"""
tree() { for c in $(pgrep -P "$1"); do tree "$c"; done; echo "$1"; }
found=""
for p in $(pgrep -f -- "$1"); do found="$found $(tree "$p")"; done
[ -z "$found" ] && exit 0
kill -TERM $found 2>/dev/null
sleep 3
kill -KILL $found 2>/dev/null
echo "reaped$found"
"""

_SESSION_ID_RE = re.compile(r"^[0-9A-Za-z-]{8,64}$")


def _resume_session_id(args: list[str]) -> str | None:
    """The CLI session a ``--resume`` turn resumes, or None.

    Validated to a UUID-ish token before it is ever placed in a regex: an id
    that is not one returns None, and the orphan reaper simply does not run."""
    for i, a in enumerate(args[:-1]):
        if a == "--resume" and _SESSION_ID_RE.match(args[i + 1] or ""):
            return args[i + 1]
    return None


def _resume_pattern(argv0: str, sid: str) -> str:
    return "^([^ ]*/)?%s --resume %s( |$)" % (re.escape(argv0), sid)


async def _reap_in_container(
    exec_prefix: list[str], script: str, *script_args: str,
    timeout: float = 15.0,
) -> str:
    """Run a reap script inside the target container. Never raises.

    A reap that cannot run is logged, not propagated: a turn must not fail
    because the clean-up of a previous one did."""
    argv = [*exec_prefix, "sh", "-c", script, "sh", *script_args]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        text = (out or b"").decode("utf-8", "replace").strip()
        if proc.returncode not in (0, None):
            _logger.warning(
                "in-container reap exited %s: %s",
                proc.returncode, (err or b"").decode("utf-8", "replace")[-300:],
            )
        return text
    except Exception as exc:                          # noqa: BLE001
        _logger.warning("in-container reap could not run (%s): %s",
                        type(exc).__name__, exc)
        return ""


def _wrap_for_host_shell(
    args: list[str],
    *,
    home: str | None,
    pidfile: str | None = None,
) -> list[str]:
    """Wrap ``claude <args>`` so it runs inside the chat-host-shell container.

    Admin chat sessions need claude to see host PIDs / network / systemd —
    things the chat container's own namespaces hide. The chat-host-shell
    sidecar is a long-lived container with pid:host / network_mode:host /
    ipc:host (see docker-compose.yml). We dispatch into it via
    ``docker exec -i`` per turn; the container is uid 1000:1000 with
    /home/felix bind-mounted, so claude inside reads felix's real HOME
    and writes session logs to felix's real ~/.claude/projects/-home-felix.

    `-w /home/felix` matches the bridge's working directory so admin web
    sessions land in the same cwd a `ssh felix@host` shell would. ``-e
    HOME=...`` propagates the account-router's HOME swap (account-2 etc.).
    """
    docker_args = [
        "docker", "exec", "-i",
        "-w", "/home/felix",
    ]
    if home:
        docker_args += ["-e", f"HOME={home}"]
    # XDG_RUNTIME_DIR keeps systemctl --user happy if claude shells out to
    # journalctl/systemctl. The host-shell container already sets it via
    # its `environment:` block; pass-through here covers the case where
    # docker exec doesn't inherit the container's env (it doesn't, by
    # default — `-e` is the explicit knob).
    docker_args += ["-e", "XDG_RUNTIME_DIR=/run/user/1000"]
    docker_args.append(_HOST_SHELL_CONTAINER)
    if pidfile:
        docker_args += ["sh", "-c", _PIDFILE_WRAPPER, pidfile]
    return docker_args + args


async def spawn_claude(
    args: list[str],
    stdin: str | None = None,
    *,
    home: str | None = None,
    dispatch: str = "local",
    container_name: str | None = None,
    account: str | None = None,
) -> AsyncIterator[bytes]:
    """Spawn ``claude`` (or any binary, really) and yield stdout lines.

    Each yielded value is one line as ``bytes`` with the trailing newline
    stripped. On non-zero exit the generator raises ``ClaudeRunnerError``
    after reading whatever stdout was buffered.

    ``home``: optional override for the ``HOME`` env var of the spawned
    subprocess. claude reads ``$HOME/.claude/.credentials.json``, so this
    is how we route a session's invocation to a non-default account.
    None / unset / "" → inherit the parent process's HOME (default
    behavior, matches single-account mode exactly).

    ``dispatch``:
      * ``"local"`` (default) runs claude in the chat container's own
        namespaces, same as before.
      * ``"host"`` wraps the invocation in ``docker exec -i
        portfolio-chat-host-shell ...`` so claude runs in a sidecar with
        pid:host / network_mode:host / ipc:host — matching the Discord
        bridge's host visibility for admin web sessions.
      * ``"user"`` wraps the invocation in ``docker exec -i -w /workspace
        -e HOME=/workspace <container_name> ...`` so claude runs inside
        the per-user container provisioned by
        ``user_container.ensure_user_container``. ``container_name`` is
        REQUIRED for this dispatch. ``home`` is ignored (the per-user
        container has its own HOME=/workspace baked in).

    The implementation uses ``preexec_fn=os.setsid`` so the child is in its
    own process group; on cancellation we send ``SIGTERM`` to the whole
    group and ``SIGKILL`` if it doesn't exit promptly. For the docker exec
    wrappers that is NOT enough: killing the local docker-exec client does not
    terminate the process inside the container (measured 2026-09-17), so a
    remote turn also records its in-container PID and ``_kill`` reaps that
    process tree in the container. See "In-container process lifetime" above.
    ``shlex.join`` is used only for the error message — args are passed
    positionally to ``create_subprocess_exec`` (no shell).
    """
    # Locate the binary via PATH; pass through ``claude`` literally if no
    # absolute path was provided. This matches the krak/epx pattern where
    # the binary is on PATH inside the container image.
    if not args:
        raise ClaudeRunnerError("spawn_claude: empty args")

    # Remote turns: where to reap, which pidfile, and the argv[0] to guard on.
    reap_prefix: list[str] | None = None
    pidfile: str | None = None
    argv0 = os.path.basename(args[0])
    resume_sid = _resume_session_id(args)
    if dispatch in ("host", "user"):
        pidfile = "/tmp/chat-turn-%s.pid" % uuid.uuid4().hex

    if dispatch == "host":
        reap_prefix = ["docker", "exec", _HOST_SHELL_CONTAINER]
        args = _wrap_for_host_shell(args, home=home, pidfile=pidfile)
    elif dispatch == "user":
        if not container_name:
            raise ClaudeRunnerError(
                "spawn_claude: dispatch='user' requires a non-empty container_name"
            )
        # Item 1: refresh in-container credentials before exec. The cap
        # is rate-limited (60s in-process) so a turn burst pays at most
        # one stat call total. Account defaults to the Phase 1 stub when
        # the caller did not supply one (admin path never reaches here).
        _account = account or "account-1"
        try:
            user_container.refresh_credentials_if_stale(container_name, _account)
        except Exception as exc:
            # A credential refresh failure must NOT crash the turn — the
            # in-container file may still be valid from a prior call.
            # Log and continue; downstream claude exit will surface any
            # actually-broken state.
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "refresh_credentials_if_stale failed for %s: %s",
                container_name, exc,
            )
        # Build the docker exec prefix. We reuse DOCKER_EXEC_PREFIX (which
        # carries -w /workspace -e HOME=/workspace) and append:
        #   --user 1000:1000      claude (and the bash subprocesses it
        #                         spawns for tool calls) runs as uid 1000.
        #                         The real OAuth credential at
        #                         /var/claude-runner/.claude/.credentials
        #                         .json is mode 0400 owner uid 2000, so
        #                         uid 1000 cannot read it — closes the
        #                         intra-container leak that the prior
        #                         --user 2000:2000 design exposed.
        #   -e ANTHROPIC_BASE_URL claude points at the loopback auth proxy
        #                         spawned by user_container.start_auth_proxy.
        #                         The proxy (uid 2000) reads the real
        #                         credential, strips claude's dummy
        #                         Authorization, attaches the real bearer,
        #                         forwards to api.anthropic.com.
        # claude takes its bearer from CLAUDE_CODE_OAUTH_TOKEN (env), not the
        # on-disk dummy. Why env, not the file: when claude reads the bearer
        # from $HOME/.claude/.credentials.json it ALSO owns that file's
        # lifecycle — after a turn it runs an OAuth profile/refresh check
        # (which bypasses ANTHROPIC_BASE_URL), the placeholder bearer fails
        # that check, and claude "logs out" by zeroing the file. The next
        # turn then reads an empty credential and dies "Not logged in" without
        # ever reaching the proxy — a sticky self-inflicted logout (the recurring
        # sophia outage). A token supplied via env is read-only: claude can't
        # zero it, so every turn stays authenticated. The bearer is a
        # non-secret placeholder; the proxy strips it and injects the real
        # pooled token server-side regardless.
        prefix = list(DOCKER_EXEC_PREFIX)
        prefix += [
            "--user", "1000:1000",
            "-e", f"ANTHROPIC_BASE_URL=http://127.0.0.1:{user_container.AUTH_PROXY_PORT}",
            "-e", f"CLAUDE_CODE_OAUTH_TOKEN={user_container.DUMMY_ACCESS_TOKEN}",
        ]
        reap_prefix = ["docker", "exec", "--user", "1000:1000", container_name]
        args = [*prefix, container_name, "sh", "-c", _PIDFILE_WRAPPER, pidfile, *args]
    elif dispatch != "local":
        raise ClaudeRunnerError(f"spawn_claude: unknown dispatch={dispatch!r}")

    # When routing to a specific account, override HOME for the subprocess
    # only (not for our own process). Inherit everything else from os.environ
    # so the subprocess still sees PATH, NODE_PATH, etc. that the parent has.
    # When dispatch=="host", HOME in the local env doesn't matter — claude
    # runs inside chat-host-shell where HOME is set by `-e`. We still leave
    # the local env alone (claude isn't running here, docker exec is).
    env: dict[str, str] | None = None
    if home and dispatch == "local":
        env = dict(os.environ)
        env["HOME"] = home

    # 16 MiB readline limit. asyncio's default StreamReader limit is 64 KiB,
    # which trips for any single stream-json frame bigger than that — e.g.
    # a Read tool_result that inlines a large file (krak's recruit JSONs
    # bundle, the bridge's task logs, anything north of 64K) or an
    # assistant turn that emits a long tool_use input. The CLI emits one
    # JSON object per line, so we just need the line buffer to fit the
    # largest frame the model can produce in one go. 16M is well over the
    # claude API's per-turn output cap and cheap to allocate (only on
    # demand). Symptom this fixes:
    #     ValueError: Separator is found, but chunk is longer than limit
    if reap_prefix is not None and resume_sid:
        reaped = await _reap_in_container(
            reap_prefix, _REAP_RESUME_SH, _resume_pattern(argv0, resume_sid),
        )
        if reaped:
            _logger.warning(
                "orphaned turn still running CLI session %s was terminated before "
                "starting a new one: %s", resume_sid, reaped,
            )

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        preexec_fn=os.setsid,
        limit=16 * 1024 * 1024,
        env=env,
    )

    async def _kill() -> None:
        # The in-container process first: once the local client is gone nothing
        # else ever will. Runs on normal completion too -- the process is gone
        # by then and the script only removes the pidfile.
        if reap_prefix is not None and pidfile:
            await _reap_in_container(reap_prefix, _REAP_PIDFILE_SH, pidfile, argv0)
        if proc.returncode is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass

    try:
        if stdin is not None and proc.stdin is not None:
            try:
                proc.stdin.write(stdin.encode("utf-8"))
                await proc.stdin.drain()
            finally:
                proc.stdin.close()

        assert proc.stdout is not None
        # Remember the most recent transient API error seen on the stream.
        # Used only to enrich the error message when the CLI exits non-zero
        # with empty stderr (the retry-exhaustion case).
        last_api_error: str | None = None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            # readline() preserves the trailing \n on full lines and returns
            # whatever's buffered (without \n) on EOF. Strip uniformly.
            stripped = line.rstrip(b"\r\n")
            # Cheap pre-filter before JSON-parsing: only api_retry / result
            # frames carry an error status, so skip the parse for the rest.
            if b'"api_retry"' in stripped or b'"api_error_status"' in stripped:
                sniffed = _sniff_api_error(stripped)
                if sniffed:
                    last_api_error = sniffed
            yield stripped

        rc = await proc.wait()
        if rc != 0:
            stderr = b""
            if proc.stderr is not None:
                try:
                    stderr = await proc.stderr.read()
                except Exception:
                    stderr = b""
            # Surface a SHORT error: the exit code plus the meaningful tail
            # of stderr. The full argv (which includes the multi-KB prompt
            # preamble) is logged for debugging but kept OUT of the message
            # so the user never sees the giant command dump.
            stderr_txt = stderr.decode("utf-8", "replace").strip()
            tail = stderr_txt.splitlines()[-1].strip() if stderr_txt else ""
            # Retry-exhaustion (e.g. 529 overloaded) exits non-zero with an
            # EMPTY stderr. Fall back to the last API error sniffed from the
            # stream so the user sees "overloaded" rather than a bare code 1.
            if not tail and last_api_error:
                tail = last_api_error
            if len(tail) > 300:
                tail = tail[:300] + "…"
            _logger.warning(
                "claude exited %s; cmd=%s; stderr=%s; last_api_error=%s",
                rc, shlex.join(args), stderr_txt[-1000:], last_api_error,
            )
            raise ClaudeRunnerError(
                f"claude exited with code {rc}: {tail}" if tail
                else f"claude exited with code {rc}"
            )
    finally:
        await _kill()


# ---------------------------------------------------------------------------
# Stream-json normalisation. Best-effort: the upstream shape is documented
# but evolves; we extract what we recognise and ignore the rest. Anything
# unrecognised is silently dropped (no spurious deltas).
# ---------------------------------------------------------------------------

def _extract_delta_text(obj: dict[str, Any]) -> str | None:
    """Pull a text delta out of a stream-json line, if it carries one.

    Recognised shapes (in order of likelihood):
      * ``{"type": "content_block_delta", "delta": {"type": "text_delta", "text": "..."}}``
      * ``{"type": "assistant", "message": {"content": [{"type": "text", "text": "..."}, ...]}}``
        — partial assistant message frames; we collect text parts.
      * ``{"type": "text", "text": "..."}`` — flat shape some test fakes use.
      * ``{"delta": "..."}`` — minimal shape.
    """
    if not isinstance(obj, dict):
        return None
    t = obj.get("type")
    if t == "content_block_delta":
        d = obj.get("delta") or {}
        if isinstance(d, dict) and d.get("type") in ("text_delta", "text"):
            text = d.get("text")
            if isinstance(text, str):
                return text
    if t == "text" and isinstance(obj.get("text"), str):
        return obj["text"]
    if t == "assistant":
        msg = obj.get("message") or {}
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list):
            parts = [
                p.get("text") for p in content
                if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str)
            ]
            if parts:
                return "".join(parts)
    if isinstance(obj.get("delta"), str):
        return obj["delta"]
    return None


def _extract_reasoning_text(obj: dict[str, Any]) -> str | None:
    """Pull a hidden-reasoning (extended thinking) delta out of a stream-json
    line, if it carries one. Mirrors ``_extract_delta_text`` for the thinking
    shapes:
      * ``{"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "..."}}``
      * ``{"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "..."}, ...]}}``
    Display-only downstream (SSE ``reasoning`` events + ``meta.reasoning``);
    never joins the reply text. ``signature_delta`` frames carry no text and
    are ignored."""
    if not isinstance(obj, dict):
        return None
    t = obj.get("type")
    if t == "content_block_delta":
        d = obj.get("delta") or {}
        if isinstance(d, dict) and d.get("type") == "thinking_delta":
            text = d.get("thinking")
            if isinstance(text, str) and text:
                return text
        return None
    if t == "assistant":
        msg = obj.get("message") or {}
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list):
            parts = [
                p.get("thinking") for p in content
                if isinstance(p, dict) and p.get("type") == "thinking"
                and isinstance(p.get("thinking"), str) and p.get("thinking")
            ]
            if parts:
                return "".join(parts)
    return None


def _extract_tool_start(obj: dict[str, Any]) -> dict[str, Any] | None:
    """Detect a tool_use start frame and return ``{name, input_summary}``."""
    if not isinstance(obj, dict):
        return None
    t = obj.get("type")
    # content_block_start with a tool_use block.
    if t == "content_block_start":
        block = obj.get("content_block") or {}
        if isinstance(block, dict) and block.get("type") == "tool_use":
            name = str(block.get("name") or "tool")
            inp = block.get("input")
            return {"name": name, "input_summary": _summarise(inp)}
    # Flat shape: {"type": "tool_use", "name": ..., "input": ...}
    if t == "tool_use":
        return {
            "name": str(obj.get("name") or "tool"),
            "input_summary": _summarise(obj.get("input")),
        }
    return None


def _extract_tool_end(obj: dict[str, Any]) -> dict[str, Any] | None:
    """Detect a tool result frame and return ``{name}``."""
    if not isinstance(obj, dict):
        return None
    t = obj.get("type")
    if t == "tool_result":
        return {"name": str(obj.get("name") or obj.get("tool_use_id") or "tool")}
    if t == "content_block_stop":
        # We only emit tool_end on stops that follow a tool_use; the SSE
        # consumer doesn't need perfect accuracy here, but we try not to
        # emit tool_end for every content block stop. Heuristic: only emit
        # if the frame names a tool.
        name = obj.get("name") or obj.get("tool_name")
        if name:
            return {"name": str(name)}
    return None


def _extract_usage(obj: dict[str, Any]) -> dict[str, int | None] | None:
    """Pull token usage out of a stream-json frame, if it carries one.

    The claude CLI's terminal ``result`` frame (and, on some versions, the
    ``assistant`` message frame) embeds a ``usage`` object shaped like the
    Anthropic Messages API: ``{"input_tokens", "output_tokens",
    "cache_creation_input_tokens", "cache_read_input_tokens"}``. We map that
    to our normalized ``{input, output, total}`` contract. ``input`` rolls
    up the cache token buckets so the displayed total reflects everything the
    turn billed against the context window. Returns None if no usage is
    present so the caller can keep whatever it last saw (the final ``result``
    frame is authoritative and overwrites earlier partials).
    """
    if not isinstance(obj, dict):
        return None
    usage = obj.get("usage")
    if not isinstance(usage, dict):
        msg = obj.get("message")
        if isinstance(msg, dict):
            usage = msg.get("usage")
    if not isinstance(usage, dict):
        return None
    inp = usage.get("input_tokens")
    out = usage.get("output_tokens")
    cache_w = usage.get("cache_creation_input_tokens") or 0
    cache_r = usage.get("cache_read_input_tokens") or 0
    input_total: int | None
    if isinstance(inp, int):
        input_total = inp + (cache_w if isinstance(cache_w, int) else 0) \
            + (cache_r if isinstance(cache_r, int) else 0)
    else:
        input_total = None
    output_total = out if isinstance(out, int) else None
    total: int | None
    if input_total is not None and output_total is not None:
        total = input_total + output_total
    else:
        total = None
    return {"input": input_total, "output": output_total, "total": total}


def _extract_result_error(obj: dict[str, Any]) -> str | None:
    """If a terminal ``result`` frame signals a failure, return a short,
    user-facing message; otherwise None.

    The CLI emits ``type=="result"`` with ``is_error: true`` for failures
    that do NOT exit the process non-zero — notably a local auth refusal
    ("Not logged in · Please run /login", ``error: authentication_failed``)
    and some upstream rate-limit/quota responses. The streaming loop only
    recognises delta/tool/usage frames, so without this an is-error result
    is dropped and the turn ends as a blank assistant bubble. We fold the
    frame's ``error`` / ``result`` text through the same friendly-pattern
    map as the non-zero-exit path so both routes phrase identically."""
    if not isinstance(obj, dict) or obj.get("type") != "result":
        return None
    if not obj.get("is_error"):
        return None
    parts: list[str] = []
    err = obj.get("error")
    if isinstance(err, dict):
        err = err.get("message") or err.get("type")
    if isinstance(err, str) and err:
        parts.append(err)
    res = obj.get("result")
    if isinstance(res, str) and res:
        parts.append(res)
    status = obj.get("api_error_status")
    if status:
        parts.append(f"API {status}")
    raw = " ".join(parts).strip() or "the model returned an error"
    low = raw.lower()
    for needle, friendly in _FRIENDLY_ERROR_PATTERNS:
        if needle in low:
            return friendly
    # Unknown is-error result: surface the cleaned text rather than a blank
    # bubble. Cap length so a stray multi-line payload can't flood the UI.
    return raw if len(raw) <= 300 else raw[:300] + "…"


def _classify_rate_limit(obj: dict[str, Any]) -> tuple[bool, float | None]:
    """Inspect a stream frame for a rate-limit signal.

    Returns ``(blocked, resets_at_epoch)``:
      * ``blocked`` — the account actually hit its limit on this turn (a
        ``result`` is-error frame whose error/result mentions rate-limit/429,
        or a ``rate_limit_event`` whose status is anything other than
        allowed/allowed_warning). Feeds the router's live-429 cooldown.
      * ``resets_at_epoch`` — the wall-clock reset (seconds) carried by a
        ``rate_limit_event``, when present, so the cooldown parks the account
        until its real recovery time rather than a default guess.

    A bare ``allowed_warning`` is NOT treated as blocked (the request still
    served) — only its reset time is captured, in case a later frame on the
    same turn does block."""
    if not isinstance(obj, dict):
        return (False, None)
    t = obj.get("type")
    if t == "rate_limit_event":
        info = obj.get("rate_limit_info")
        if not isinstance(info, dict):
            info = {}
        resets = info.get("resetsAt")
        resets = float(resets) if isinstance(resets, (int, float)) else None
        status = str(info.get("status") or "").lower()
        blocked = status not in ("", "allowed", "allowed_warning")
        return (blocked, resets)
    if t == "result" and obj.get("is_error"):
        err = obj.get("error")
        if isinstance(err, dict):
            err = err.get("message") or err.get("type")
        blob = " ".join(
            str(x) for x in (err, obj.get("result"), obj.get("api_error_status")) if x
        ).lower()
        if ("rate" in blob and "limit" in blob) or "429" in blob:
            return (True, None)
    return (False, None)


def _summarise(value: Any, limit: int = 80) -> str:
    """One-line human-readable summary of a tool input. Never raises."""
    try:
        s = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    except (TypeError, ValueError):
        s = repr(value)
    s = s.replace("\n", " ").strip()
    if len(s) > limit:
        s = s[: limit - 1] + "\u2026"
    return s


# ---------------------------------------------------------------------------
# Public consumer-facing generators.
# ---------------------------------------------------------------------------

_ALLOWED_MODEL_ALIASES = frozenset({"opus", "sonnet", "haiku"})

# Aliases the claude CLI doesn't know natively, mapped to the full model ID
# passed verbatim to --model. "fable" -> Claude Fable 5.1 (released 2026-08-28;
# needs claude CLI >= 2.1.251 — older CLIs reject the ID with a 400);
# "opus5" -> Claude Opus 5.5 (released 2026-09-22; needs claude CLI >= 2.1.280,
# older CLIs reject the ID as unrecognized before any API call). The CLI's bare
# "opus" alias still resolves to Opus 4.8, so the newest Opus needs an explicit
# full-ID expansion; the alias key stays "opus5" so saved user settings and the
# browser model preference keep working. The CLI's
# built-in alias table doesn't cover these, so the bare alias is rejected
# ("It may not exist...") and we must expand it here.
_ALIAS_TO_MODEL_ID = {"fable": "claude-fable-5-1", "opus5": "claude-opus-5-5"}


def _validated_model(model: str | None) -> str | None:
    """Return the CLI ``--model`` value for an allowlisted alias, else None.

    The chat layer accepts only short aliases (opus / sonnet / haiku /
    fable) so a typo or attacker-supplied string can never reach the
    claude CLI's ``--model`` flag. Arbitrary full model IDs are
    intentionally not accepted — only the fixed expansions in
    ``_ALIAS_TO_MODEL_ID`` for families the CLI has no alias for.
    """
    if not model:
        return None
    if model in _ALLOWED_MODEL_ALIASES:
        return model
    if model in _ALIAS_TO_MODEL_ID:
        return _ALIAS_TO_MODEL_ID[model]
    return None


def _stage_attachments_into_user_container(
    container_name: str,
    session_id: str,
    attachments_dir: Path,
    *,
    docker_client=None,
) -> str | None:
    """Copy the session's attachments into the per-user container at
    ``/workspace/.attachments/<session_id>/`` using docker put_archive,
    and return that container-internal path.

    Returns None on any failure (caller should drop ``--add-dir``
    rather than pointing claude at a path it can't open). All copied
    files are owned uid:gid 1000:1000 inside the container so the user
    shell (uid 1000) can read them.

    The /workspace volume is per-user and persistent across container
    recreate; staging accumulates session attachments under
    ``.attachments/<sid>/``. We don't garbage-collect older sessions'
    files here — that's bounded by /workspace volume lifetime, not turn
    count, and on session-delete the chat backend already calls
    ``attachments.delete_attachments_dir`` (which only touches the
    chat-side dir; staged copies linger but cost negligible disk).
    """
    if not attachments_dir.exists():
        return None
    files = [p for p in attachments_dir.iterdir() if p.is_file()]
    if not files:
        return None

    client = docker_client or docker.from_env()
    try:
        container = client.containers.get(container_name)
    except Exception as exc:
        _logger.warning(
            "stage_attachments: get(%r) failed: %s",
            container_name, exc,
        )
        return None

    # The put_archive target dir must exist; create it owned by the
    # user (1000:1000) so the user shell can read+traverse later.
    try:
        container.exec_run(
            ["sh", "-c",
             f"mkdir -p {USER_CONTAINER_ATTACHMENTS_ROOT}/{session_id} && "
             f"chown -R 1000:1000 {USER_CONTAINER_ATTACHMENTS_ROOT}"],
            user="root",
        )
    except Exception as exc:
        _logger.warning(
            "stage_attachments: mkdir for %s failed: %s",
            container_name, exc,
        )
        return None

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for src in files:
            info = tar.gettarinfo(str(src), arcname=f"{session_id}/{src.name}")
            info.uid = 1000
            info.gid = 1000
            info.uname = ""
            info.gname = ""
            with open(src, "rb") as f:
                tar.addfile(info, f)

    try:
        ok = container.put_archive(USER_CONTAINER_ATTACHMENTS_ROOT, buf.getvalue())
    except Exception as exc:
        _logger.warning(
            "stage_attachments: put_archive to %s failed: %s",
            container_name, exc,
        )
        return None
    if not ok:
        _logger.warning(
            "stage_attachments: put_archive returned False for %s",
            container_name,
        )
        return None

    return f"{USER_CONTAINER_ATTACHMENTS_ROOT}/{session_id}"


def _claude_attachments_path(
    attachments_dir: Path | None,
    *,
    dispatch: str,
    container_name: str | None = None,
) -> str | None:
    """Translate the chat-side attachments path to one claude can actually
    open under the chosen dispatch.

    The chat container bind-mounts the host attachments dir at
    ``/data/attachments`` (override via ``CHAT_ATTACHMENTS_DIR``). That
    mount only exists in the chat container itself; host-shell and
    per-user containers don't have it.

    * ``local``: claude runs in the chat container — pass through.
    * ``host``: claude runs in chat-host-shell, which has /home/felix
      bind-mounted, so the attachment files are reachable as
      ``/home/felix/projects/chat/attachments/<sid>/...`` (or whatever
      ``CHAT_ATTACHMENTS_HOST_DIR`` overrides to).
    * ``user``: per-user containers have neither mount; we stage the
      files into ``/workspace/.attachments/<session_id>/`` via docker
      put_archive and return that container-internal path. Staging is
      a side-effect of this call (the only spot that knows the right
      container_name + session_id pair). Returns None if staging fails
      or if the required (container_name, session_id) aren't supplied.
    """
    if attachments_dir is None:
        return None
    s = str(attachments_dir)
    if dispatch == "host":
        chat_root = os.environ.get("CHAT_ATTACHMENTS_DIR", "/data/attachments")
        host_root = os.environ.get(
            "CHAT_ATTACHMENTS_HOST_DIR",
            "/home/felix/projects/chat/attachments",
        )
        if s == chat_root:
            return host_root
        if s.startswith(chat_root + "/"):
            return host_root + s[len(chat_root):]
        return s
    if dispatch == "user":
        if not container_name:
            # Caller didn't plumb the per-user container — fall back
            # to legacy passthrough rather than silently dropping.
            # Tests rely on the legacy chat-container path being
            # passed when no container is set up.
            return s
        # The session_id is the last path component of the chat-side
        # attachments dir (set by attachments.session_attachments_dir)
        # so we don't need to plumb it through separately.
        session_id = attachments_dir.name
        return _stage_attachments_into_user_container(
            container_name, session_id, attachments_dir,
        )
    return s


def _attachment_preamble(
    attachments_dir: Path,
    claude_attach_dir: str,
) -> str:
    """Produce a small preamble that names the user's attached files so
    claude knows they exist and where to read them from.

    Without this preamble, claude has to discover attachments by
    listing ``--add-dir`` first. For text-y tasks the model often does;
    for image/PDF questions it usually skips the discovery step and
    just answers from the prompt text alone — leaving the user with
    "you didn't open my image" frustration.

    The preamble lists each file's container-side path, mime guess,
    and size. It's prepended to the user message but does NOT replace
    it — chat storage still records the user's original text.
    """
    try:
        files = sorted(p for p in attachments_dir.iterdir() if p.is_file())
    except OSError:
        return ""
    if not files:
        return ""
    lines = []
    for p in files:
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        lines.append(
            f"  - {claude_attach_dir}/{p.name}  ({mime}, {size:,} bytes)"
        )
    return (
        "[The user attached the following file(s). Use the Read tool on each "
        "path to actually see the contents — do not answer without reading "
        "them. Read handles images and PDFs natively.]\n"
        + "\n".join(lines)
        + "\n\n"
    )


def build_identity_system_prompt(user_email: str | None) -> str | None:
    """Authoritative identity directive injected via ``--append-system-prompt``.

    Cross-user identity leak fix: the chat runs on a POOL of real Anthropic
    accounts (one per Wizerith person, used for quota load-balancing). Claude
    Code reads ``oauthAccount.displayName`` / ``emailAddress`` from the pooled
    account's ``.claude.json`` and surfaces it as the user's name — so a turn
    that the account-router lands on, say, Andre's pooled account greets *any*
    chat user as "Andre" (and leaks his email) regardless of who is actually
    signed in. This applies to EVERY dispatch mode (admin host-shell and per-
    user containers both authenticate via a pooled account), which is why the
    directive is emitted from ``_build_run_args`` rather than a single path.

    The wording is deliberately generic (it never names the pooled account, so
    it's correct whichever account the router picks) and was verified live:
    against Andre's pooled account, a bare prompt answers "your name is Andre
    Wagner"; with this directive it answers with the real signed-in user."""
    if not user_email:
        return None
    return (
        "IDENTITY CONTEXT (authoritative — this overrides any other identity "
        "signal you see): You are authenticated to the Anthropic API through "
        "a SHARED, POOLED billing account. Claude Code WILL surface that "
        "pooled account's profile name and email address — typically an "
        "@wizerith.com address — in your account info, in `claude` "
        "config/whoami output, and possibly in a system-reminder describing "
        "\"the user\". Treat ALL of those as infrastructure billing metadata, "
        "NOT the person you are talking to. This is EXPECTED and is NOT a "
        "conflict: whenever any account profile, config, or system-reminder "
        "names a different user than the one below, that other identity is "
        "the billing account and is WRONG — ignore it completely. The actual "
        f"person you are assisting is signed in as: {user_email}. That is the "
        "ONLY correct identity — use it for \"who am I\", for marking which "
        "session or container is \"you\", and anywhere you must identify the "
        "current user.\n"
        "HARD RULE — never disclose the billing account: Do NOT print, name, "
        "quote, link, echo, or otherwise reveal the pooled billing account's "
        "email address or display name under ANY circumstances — even if the "
        "user directly asks \"what account am I on\", \"what email are you "
        "running under\", or tells you to dump your config/account info. Never "
        "add a note or caveat comparing the two identities. Simply identify "
        f"the user as {user_email}; if pressed about the underlying account, "
        "say only that it is a shared internal billing-pool account and do not "
        "name it."
    )


def _build_run_args(
    claude_session_id: str,
    prompt: str,
    attachments_dir: Path | None,
    *,
    is_first_turn: bool,
    model: str | None = None,
    dispatch: str = "local",
    container_name: str | None = None,
    claude_attach_dir: str | None = None,
    streaming_input: bool = False,
    identity_prompt: str | None = None,
) -> list[str]:
    """Construct the argv vector for a streaming turn.

    On the first turn of a session we use ``--session-id <uuid>`` to CREATE
    the session. On every subsequent turn we use ``--resume <uuid>`` to
    continue it; calling ``--session-id`` again for an existing session id
    fails with "Session ID <uuid> is already in use." (claude code 2.1.x).

    ``--add-dir`` is only added when the attachments dir exists AND is
    non-empty: passing the flag with an empty dir would still cost claude
    a directory walk, and tests assert the flag isn't present otherwise.
    """
    session_arg = "--session-id" if is_first_turn else "--resume"
    args = [
        "claude",
        session_arg, claude_session_id,
        "--output-format", "stream-json",
    ]
    valid_model = _validated_model(model)
    if valid_model:
        args.extend(["--model", valid_model])
    # Identity directives, combined into ONE --append-system-prompt (the CLI
    # may only honour the last occurrence of the flag, so never pass it twice):
    #   - user identity (cross-user leak fix): assert the real signed-in user
    #     so claude doesn't surface the pooled API account's profile name/email.
    #   - model identity: tell this model which model it actually is, so the
    #     picker option self-identifies correctly instead of guessing.
    # Applies to every dispatch — see build_identity_system_prompt /
    # prompt_blocks.model_identity_directive.
    _directives = [
        d for d in (identity_prompt, prompt_blocks.model_identity_directive(model)) if d
    ]
    if _directives:
        args.extend(["--append-system-prompt", "\n\n".join(_directives)])
    args.extend([
        # --include-partial-messages is required for the stream-json delta
        # frames the normalisation layer (_extract_delta_text et al.)
        # depends on; without it claude only emits whole-message frames.
        "--include-partial-messages",
        # --verbose is required by the claude CLI when -p (--print) is paired
        # with --output-format=stream-json; without it claude exits with
        # code 1 and "Error: When using --print, --output-format=stream-json
        # requires --verbose". This is a CLI surface change, not optional.
        "--verbose",
        # bypassPermissions: matches the claude-bridge invocation so the
        # web agent doesn't stall waiting for an out-of-band approval the
        # SSE stream has no UI for. The chat session has no human-in-the-
        # loop tool-permission UI, so any non-bypassed Read/Write/Edit
        # request would surface as "permission required" in the LLM's
        # output and block the conversation. Cloudflare Access on
        # ald3.com is the gate we trust instead.
        "--permission-mode", "bypassPermissions",
        # AskUserQuestion is removed from the tool set: the chat surface has
        # no interactive question UI, so an attempt would stall the turn with
        # no way for the user to answer. Disallowing it makes the model never
        # offer it — it asks inline in its text instead.
        "--disallowedTools", "AskUserQuestion",
        # Same filesystem reach as the bridge: the chat container has
        # /home/felix bind-mounted rw, and --add-dir extends claude's
        # tool-access scope to include it. Without this, Read/Edit/Write
        # only resolve under cwd (=/app, the FastAPI working dir), so
        # the web agent can't touch the actual project source on the host.
        "--add-dir", "/home/felix",
    ])
    if streaming_input:
        # Persistent streaming-input session (A1): the process is held open and
        # user messages arrive as stream-json on stdin, so -p takes no inline
        # prompt and --input-format selects realtime streaming input. This is
        # what lets a backgrounded task auto-surface and the agent report back
        # without a fresh HTTP turn (see session_process.py). The prompt is
        # delivered via SessionProcess.send_user, not argv.
        args.extend(["-p", "--input-format", "stream-json"])
    else:
        args.extend(["-p", prompt])
    if attachments_dir is not None:
        try:
            has_any = attachments_dir.exists() and any(attachments_dir.iterdir())
        except OSError:
            has_any = False
        if has_any:
            # Use a precomputed path if the caller resolved it (avoids
            # re-staging on user dispatch). Fall back to inline resolution
            # for callers that don't pass it (legacy + tests).
            claude_path = claude_attach_dir
            if claude_path is None:
                claude_path = _claude_attachments_path(
                    attachments_dir,
                    dispatch=dispatch,
                    container_name=container_name,
                )
            if claude_path is not None:
                args.extend(["--add-dir", claude_path])
    return args


async def _tracked_frames(
    frames: AsyncIterator[dict[str, Any]],
    tracker: "token_ledger.ClaudeStreamTracker",
) -> AsyncIterator[dict[str, Any]]:
    """Pass frames through, feeding each to the token-ledger tracker; flush
    it (idempotent) however the stream ends."""
    status = "cancelled"
    try:
        async for obj in frames:
            tracker.feed(obj)
            yield obj
        status = "ok"
    finally:
        tracker.flush(status)


async def normalize_session_turn(
    turn: AsyncIterator[dict[str, Any]],
    *,
    model: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Normalise ONE persistent-session ``Turn`` (raw CLI stream-json event
    dicts, framed by session_process and terminated by its ``result``) into the
    SAME ``delta`` / ``tool_start`` / ``tool_end`` / ``done`` events ``run_turn``
    emits — so the app worker fans both the user turn and CLI-initiated
    auto-continuation turns through one consumer.

    Contract mirrors ``run_turn``: exactly one terminal event. ``done`` when the
    turn's ``result`` arrives; ``error`` if the stream ends first (the process
    died mid-turn). Reuses the same ``_extract_*`` helpers as the one-shot path,
    so normalisation can't drift between the two."""
    full_text_parts: list[str] = []
    usage: dict[str, int | None] | None = None
    turn_start = time.monotonic()
    saw_result = False
    tracker = token_ledger.ClaudeStreamTracker()
    async for obj in _tracked_frames(turn, tracker):
        reasoning = _extract_reasoning_text(obj)
        if reasoning:
            yield {"type": "reasoning", "text": reasoning}
            continue
        delta = _extract_delta_text(obj)
        if delta:
            full_text_parts.append(delta)
            yield {"type": "delta", "text": delta}
            continue
        ts = _extract_tool_start(obj)
        if ts is not None:
            yield {"type": "tool_start", **ts}
            continue
        te = _extract_tool_end(obj)
        if te is not None:
            yield {"type": "tool_end", **te}
            continue
        u = _extract_usage(obj)
        if u is not None:
            # Last usage wins — the terminal ``result`` supersedes partials.
            usage = u
        if obj.get("type") == "result":
            saw_result = True
            # ``result`` is the turn's final event; the Turn closes right after.
    tracker.flush("ok" if saw_result else "error", model_hint=model)

    if not saw_result:
        yield {
            "type": "error",
            "message": "session process ended before the turn completed",
        }
        return

    elapsed = max(time.monotonic() - turn_start, 0.0)
    tokens: dict[str, int | None] = (
        usage if usage is not None else {"input": None, "output": None, "total": None}
    )
    out_tok = tokens.get("output")
    tok_s = (out_tok / elapsed) if (isinstance(out_tok, int) and elapsed > 0) else None
    meta = {
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "tokens": tokens,
        "tok_s": tok_s,
    }
    yield {"type": "done", "full_text": "".join(full_text_parts), "meta": meta}


_LANGUAGE_NAMES = {
    "en": "English",
    "zh-CN": "Simplified Chinese (简体中文)",
    "zh-TW": "Traditional Chinese (繁體中文)",
    "de": "German (Deutsch)",
    "tl": "Tagalog (Filipino)",
}


def build_effective_prompt(
    prompt: str,
    *,
    prior_history: str | None = None,
    persona: str | None = None,
    output_language: str | None = None,
    memory: str | None = None,
) -> str:
    """Assemble the per-turn prompt prefixes (history -> persona -> language ->
    memory) that ``run_turn`` builds inline.

    Extracted so the persistent streaming-input path (A1) can send the SAME
    prefixed text via stdin that the one-shot path passes via argv. The
    artifacts-instructions prefix is intentionally NOT here — it depends on the
    dispatch resolution and stays in the caller, applied outermost.

    MUST stay in lockstep with ``run_turn``'s inline assembly; a unit test pins
    this to ``run_turn``'s exact block text. (``run_turn`` itself is left
    untouched on purpose — this path can't be exercised against the live chat
    from CI, so the working one-shot path stays byte-identical.)"""
    effective_prompt = prompt
    if prior_history:
        effective_prompt = (
            "[Conversation history — please continue as if you'd been part of "
            "this conversation. The user's NEW message follows after the "
            "history block.]\n\n"
            f"{prior_history}\n\n"
            "[End of history. The user's new message:]\n\n"
            f"{prompt}"
        )
    if persona and persona.strip():
        effective_prompt = (
            "[Personal system prompt from this user — apply to every turn:]\n"
            f"{persona.strip()}\n"
            "[End of personal system prompt.]\n\n"
            + effective_prompt
        )
    if output_language and output_language != "en" and output_language in _LANGUAGE_NAMES:
        lang_name = _LANGUAGE_NAMES[output_language]
        effective_prompt = (
            f"[Response language: respond in {lang_name}, regardless of "
            "what language the user writes in. Use natural, idiomatic "
            "phrasing — not a machine-translated style. Code, file paths, "
            "and English technical terms (function names, library names, "
            "command-line flags) stay in their original form.]\n\n"
            + effective_prompt
        )
    if memory is not None:
        memory_block = (memory.strip() if memory and memory.strip() else "(empty — nothing recorded yet)")
        effective_prompt = (
            "[Cross-session memory — facts you've learned about this user "
            "across past sessions. Read first so you don't re-ask things "
            "they've already told you. To UPDATE memory after answering, "
            "end your response with EXACTLY this block (it will be stripped "
            "before the user sees it):\n"
            "<memory_update>\n"
            "...full new memory content here, replacing the old...\n"
            "</memory_update>\n"
            "Only emit the block when there's something genuinely worth "
            "recording (the user's role, ongoing projects, preferences, "
            "facts they've stated). Skip it for casual exchanges. Keep "
            "memory under 8000 chars total.]\n"
            "<memory>\n"
            f"{memory_block}\n"
            "</memory>\n\n"
            + effective_prompt
        )
    return effective_prompt


def resolve_dispatch(role: str | None, container: str | None) -> str:
    """Dispatch precedence, identical to ``run_turn``: a provisioned container
    (any role) → ``user``; admin without a container → ``host``; a ``user`` role
    missing its container → ``user-fail-closed`` (no spawn); else ``local``."""
    if container:
        return "user"
    if role == "admin":
        return "host"
    if role == "user":
        return "user-fail-closed"
    return "local"


def artifacts_path_for(dispatch: str, chat_session_id: str | None) -> str | None:
    """The in-claude path for this turn's inline artifacts, by dispatch — the
    same resolution ``run_turn`` does inline (host tenant dir / per-user
    /workspace / local /data). All resolve chat-side to /data/generated/<sid>."""
    if not chat_session_id or dispatch == "user-fail-closed":
        return None
    if dispatch == "host":
        host_root = os.environ.get(
            "CHAT_GENERATED_HOST_DIR", "/home/felix/projects/chat/generated"
        )
        return host_root.rstrip("/") + "/" + chat_session_id
    if dispatch == "user":
        return "/workspace/.artifacts/" + chat_session_id
    return "/data/generated/" + chat_session_id


def build_streaming_prompt(
    user_text: str,
    *,
    prior_history: str | None = None,
    persona: str | None = None,
    output_language: str | None = None,
    memory: str | None = None,
    artifacts_path: str | None = None,
) -> str:
    """Full per-turn prompt for the persistent path: ``build_effective_prompt``
    plus the artifacts-instructions prefix (outermost), matching exactly what
    ``run_turn`` assembles before spawning."""
    p = build_effective_prompt(
        user_text,
        prior_history=prior_history,
        persona=persona,
        output_language=output_language,
        memory=memory,
    )
    if artifacts_path:
        p = prompt_blocks.artifacts_instructions(artifacts_path) + p
    return p


def make_streaming_args_env(
    claude_session_id: str,
    *,
    is_first_turn: bool,
    attachments_dir: Path | None = None,
    model: str | None = None,
    role: str | None = None,
    container: str | None = None,
    account: str | None = None,
    user_email: str | None = None,
) -> tuple[list[str], dict[str, str] | None]:
    """Build (argv, env) for a session's PERSISTENT streaming-input claude
    process — the spawn factory for ``session_process``.

    Mirrors ``run_turn``'s dispatch resolution + ``spawn_claude``'s wrapping
    (host-shell / per-user docker-exec with the loopback auth proxy / local
    HOME override), but in streaming-input mode (prompt arrives via stdin, not
    argv). VERIFIED LIVE against a real per-user container's docker-exec +
    auth-proxy path. ``--user`` role without a container is rejected (the
    fail-closed contract — never silently fall back to the chat container)."""
    dispatch = resolve_dispatch(role, container)
    if dispatch == "user-fail-closed":
        raise ClaudeRunnerError(
            "make_streaming_args_env: role='user' requires a provisioned container"
        )

    claude_attach_dir: str | None = None
    if attachments_dir is not None:
        try:
            has_any = attachments_dir.exists() and any(attachments_dir.iterdir())
        except OSError:
            has_any = False
        if has_any:
            claude_attach_dir = _claude_attachments_path(
                attachments_dir, dispatch=dispatch, container_name=container,
            )

    args = _build_run_args(
        claude_session_id, "", attachments_dir,
        is_first_turn=is_first_turn, model=model, dispatch=dispatch,
        container_name=container, claude_attach_dir=claude_attach_dir,
        streaming_input=True,
        identity_prompt=build_identity_system_prompt(user_email),
    )
    home = _resolve_home_for_account(
        account,
        claude_session_id=claude_session_id, is_first_turn=is_first_turn,
    )
    env: dict[str, str] | None = None

    if dispatch == "host":
        argv = _wrap_for_host_shell(args, home=home)
    elif dispatch == "user":
        # Refresh the in-container credential (rate-limited) before the
        # long-lived spawn — same as spawn_claude's user path.
        _account = account or "account-1"
        try:
            user_container.refresh_credentials_if_stale(container, _account)
        except Exception as exc:  # noqa: BLE001 — never crash spawn on refresh
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "refresh_credentials_if_stale failed for %s: %s", container, exc,
            )
        prefix = list(DOCKER_EXEC_PREFIX) + [
            "--user", "1000:1000",
            "-e", f"ANTHROPIC_BASE_URL=http://127.0.0.1:{user_container.AUTH_PROXY_PORT}",
            # Read-only bearer via env so claude can't self-wipe its credential
            # on the post-turn OAuth check — see the matching block in
            # spawn_claude. Proxy strips it and injects the real pooled token.
            "-e", f"CLAUDE_CODE_OAUTH_TOKEN={user_container.DUMMY_ACCESS_TOKEN}",
        ]
        argv = [*prefix, container, *args]
    else:  # local
        argv = args
        if home:
            env = dict(os.environ)
            env["HOME"] = home

    return argv, env


async def run_turn(
    claude_session_id: str,
    prompt: str,
    attachments_dir: Path | None = None,
    timeout: float = 1200.0,
    *,
    is_first_turn: bool = False,
    account: str | None = None,
    role: str | None = None,
    container: str | None = None,
    model: str | None = None,
    prior_history: str | None = None,
    resume_heal_history: str | None = None,
    persona: str | None = None,
    memory: str | None = None,
    chat_session_id: str | None = None,
    output_language: str | None = None,
    user_email: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Stream normalised events for one user turn.

    ``is_first_turn`` selects between ``--session-id`` (create) and
    ``--resume`` (continue) on the underlying claude invocation; see
    ``_build_run_args`` for the why.

    ``account`` (optional) names which Claude account this turn runs on.
    None / "main" / unknown → bridge user's primary ~/.claude. Sessions
    are locked per-account at creation time; this kwarg is plumbed
    through from app's worker so every turn of a session reads the same
    credentials file.

    ``role`` (optional) is the session's role at create time. Dispatch
    selection:
      * ``"admin"`` → claude runs in the chat-host-shell sidecar with host
        PID / network / IPC namespaces, matching the Discord bridge's host
        visibility (so the web AI sees real `ps`, `systemctl --user`,
        host network listeners).
      * ``"user"`` (with a non-empty ``container``) → claude runs inside
        the per-user docker container provisioned at session-create time.
        The container has /workspace as HOME, no /home/felix mount, no
        docker socket, and lives on its own per-user bridge network.
      * any other value (None, legacy, missing container for "user") →
        in-chat-container path (the legacy local dispatch). This keeps
        pre-multi-user-containers sessions runnable.

    ``container`` (optional) is the per-user container name persisted at
    session-create time. Required when ``role == "user"`` for the user
    dispatch to take effect; if absent we fall back to local dispatch
    (a logged warning is the worst case — never crash the turn).

    Emits exactly one terminal event: ``done`` on success, ``error`` on any
    failure. Callers can rely on that contract — no need to track state
    across iterations beyond "have I seen a terminal event yet".
    """
    effective_prompt = prompt
    if prior_history:
        effective_prompt = (
            "[Conversation history — please continue as if you'd been part of "
            "this conversation. The user's NEW message follows after the "
            "history block.]\n\n"
            f"{prior_history}\n\n"
            "[End of history. The user's new message:]\n\n"
            f"{prompt}"
        )
    # Persona / personal-system-prompt (per-user setting). Prepended to
    # every turn's prompt so claude reads it before the user's text.
    # Same pattern as the attachment preamble — chat storage still
    # records the user's original text; only the prompt sent to claude
    # gets the prefix. is_first_turn doesn't matter — claude won't
    # remember the persona across turns of a session unless we resend
    # it, so we send it every turn.
    if persona and persona.strip():
        effective_prompt = (
            "[Personal system prompt from this user — apply to every turn:]\n"
            f"{persona.strip()}\n"
            "[End of personal system prompt.]\n\n"
            + effective_prompt
        )
    # Output language (independent from UI language since the language-
    # split). Skipped for "en" — model defaults to English-ish behavior,
    # no token overhead. For non-English, instructs the model to always
    # reply in the named language even when the user writes in another.
    _LANGUAGE_NAMES = {
        "en": "English",
        "zh-CN": "Simplified Chinese (简体中文)",
        "zh-TW": "Traditional Chinese (繁體中文)",
        "de": "German (Deutsch)",
        "tl": "Tagalog (Filipino)",
    }
    if output_language and output_language != "en" and output_language in _LANGUAGE_NAMES:
        lang_name = _LANGUAGE_NAMES[output_language]
        effective_prompt = (
            f"[Response language: respond in {lang_name}, regardless of "
            "what language the user writes in. Use natural, idiomatic "
            "phrasing — not a machine-translated style. Code, file paths, "
            "and English technical terms (function names, library names, "
            "command-line flags) stay in their original form.]\n\n"
            + effective_prompt
        )
    # Cross-session memory. Read in front of every turn so the model has
    # context across sessions; updates flow back via a sentinel block at
    # the end of the response that app.py parses and strips before the
    # message is persisted (so the user never sees the tag). One file per
    # user, capped in storage.py at MAX_MEMORY_BYTES.
    if memory is not None:
        memory_block = (memory.strip() if memory and memory.strip() else "(empty — nothing recorded yet)")
        effective_prompt = (
            "[Cross-session memory — facts you've learned about this user "
            "across past sessions. Read first so you don't re-ask things "
            "they've already told you. To UPDATE memory after answering, "
            "end your response with EXACTLY this block (it will be stripped "
            "before the user sees it):\n"
            "<memory_update>\n"
            "...full new memory content here, replacing the old...\n"
            "</memory_update>\n"
            "Only emit the block when there's something genuinely worth "
            "recording (the user's role, ongoing projects, preferences, "
            "facts they've stated). Skip it for casual exchanges. Keep "
            "memory under 8000 chars total.]\n"
            "<memory>\n"
            f"{memory_block}\n"
            "</memory>\n\n"
            + effective_prompt
        )
    home = _resolve_home_for_account(
        account,
        claude_session_id=claude_session_id, is_first_turn=is_first_turn,
    )
    # Dispatch is decided before building argv so attachment-path
    # rewriting can target the right filesystem view (host-shell sees
    # /home/felix; chat sees /data/attachments; per-user containers see
    # neither directly).
    # Dispatch is keyed on container presence first — an admin in the
    # "personal" / "shared" workspace was provisioned a per-user / shared
    # container (see app.create_session) and dispatches into it the same
    # way a regular user does. Only admin sessions in the "admin"
    # workspace (no container) fall through to the legacy host dispatch.
    if container:
        dispatch = "user"
    elif role == "admin":
        dispatch = "host"
    elif role == "user":
        dispatch = "user-fail-closed"  # signaled below; no claude spawn
    else:
        dispatch = "local"

    # Inline artifacts hint. Path differs by dispatch (host has
    # /home/felix mounted rw; user's /workspace is its container's HOME;
    # local sees /data directly), but all three resolve to the SAME
    # chat-side per-session dir under /data/generated/<sid>/ — see
    # app._collect_user_container_artifacts + _scan_new_artifacts.
    if chat_session_id and dispatch != "user-fail-closed":
        if dispatch == "host":
            # Tenant-aware host path. The chat-wizerith service binds
            # /home/felix/projects/chat-wizerith/generated to /data/generated;
            # admin sessions there must write to that host dir, not the ald3
            # one. The env var is set in docker-compose.yml per tenant and
            # defaults to the historical ald3 path if unset.
            host_generated_root = os.environ.get(
                "CHAT_GENERATED_HOST_DIR",
                "/home/felix/projects/chat/generated",
            )
            artifacts_path = host_generated_root.rstrip("/") + "/" + chat_session_id
        elif dispatch == "user":
            artifacts_path = "/workspace/.artifacts/" + chat_session_id
        else:
            artifacts_path = "/data/generated/" + chat_session_id
        effective_prompt = (
            prompt_blocks.artifacts_instructions(artifacts_path)
            + effective_prompt
        )

    # Resolve the claude-side attachments path ONCE (the resolution is
    # the only place that triggers staging into per-user containers, so
    # double-calling it would tar-stream files twice). Then both the
    # prompt preamble and --add-dir use the same resolved path.
    claude_attach_dir: str | None = None
    if attachments_dir is not None:
        try:
            has_any_attachments = (
                attachments_dir.exists() and any(attachments_dir.iterdir())
            )
        except OSError:
            has_any_attachments = False
        if has_any_attachments:
            claude_attach_dir = _claude_attachments_path(
                attachments_dir,
                dispatch=dispatch,
                container_name=container,
            )
            if claude_attach_dir:
                preamble = _attachment_preamble(attachments_dir, claude_attach_dir)
                if preamble:
                    effective_prompt = preamble + effective_prompt

    args = _build_run_args(
        claude_session_id, effective_prompt, attachments_dir,
        is_first_turn=is_first_turn,
        model=model,
        dispatch=dispatch,
        container_name=container,
        claude_attach_dir=claude_attach_dir,
        identity_prompt=build_identity_system_prompt(user_email),
    )
    # Spawn-kwarg shape mirrors the dispatch above:
    #  - container present → user dispatch into that container (works for
    #    both regular users and admins-in-personal/shared workspace).
    #  - admin role without container → legacy host dispatch.
    #  - user role without container → fail-closed (no claude spawn).
    if container:
        # Pass ``home`` so spawn_claude appends ``-e HOME=<home>`` to the
        # docker exec wrapper. claude reads
        # ``$HOME/.claude/.credentials.json`` and the per-user volume is
        # empty — without this the turn streams nothing and the user
        # sees a blank assistant message.
        spawn_kwargs: dict[str, Any] = {
            "home": home,
            "dispatch": dispatch,
            "container_name": container,
            # Thread the account through so spawn_claude can call
            # refresh_credentials_if_stale with the correct source-
            # credential account.
            "account": account,
        }
    elif role == "admin":
        spawn_kwargs = {"home": home, "dispatch": dispatch}
    elif role == "user":
        # role=user but no container — fail closed. Falling through to
        # "local" dispatch would run claude in the chat container itself,
        # which has /home/felix:rw mounted and host credentials reachable
        # by the model's tool calls — exactly the leak the per-user-
        # container model exists to prevent. Surface the failure to the
        # user; app.py's resume path now auto-heals legacy sessions by
        # calling ensure_user_container before dispatch, so reaching this
        # branch means provisioning genuinely failed.
        yield {
            "type": "error",
            "message": (
                "this session has no provisioned per-user container. "
                "reload the page to retry; if the problem persists, "
                "contact the operator."
            ),
        }
        return
    else:
        # role is None (legacy admin session pre-role-locking) or some other
        # value. Treat as legacy local dispatch — runs in chat container with
        # operator-level access. ONLY safe because this branch is now
        # unreachable for non-admin emails: app.py session-create assigns
        # role=admin/user based on email allowlist, and the resume path
        # auto-heals legacy non-admin sessions by re-resolving role.
        dispatch = "local"
        spawn_kwargs = {"home": home, "dispatch": dispatch}
    full_text_parts: list[str] = []
    # Token usage from the stream's terminal ``result`` frame, if present.
    # The claude CLI does not always expose usage (older versions / certain
    # dispatch paths); we leave this None and emit null tokens in that case.
    usage: dict[str, int | None] | None = None
    turn_start = time.monotonic()
    tracker = token_ledger.ClaudeStreamTracker()

    async def _stream_attempt(
        attempt_args: list[str], *, allow_heal: bool,
    ) -> AsyncIterator[dict[str, Any]]:
        """One claude spawn → normalised events.

        Raises ``_NeedsHeal`` (BEFORE emitting any event) when a
        ``--resume`` turn hits a CLI session the backend no longer has and
        a heal retry is permitted; otherwise emits exactly one terminal
        event (``done`` / ``error``). We hold an explicit iterator handle
        so timeout / cancellation can ``aclose()`` the subprocess.
        """
        nonlocal usage
        gen = spawn_claude(attempt_args, **spawn_kwargs)
        iterator = gen.__aiter__()
        emitted = False  # have we surfaced any user-visible output yet?
        # A terminal ``result`` frame with is_error=true that the CLI emits
        # WITHOUT a non-zero exit (auth refusal, some rate-limit responses).
        # Captured here, surfaced as a clean error below if the turn produced
        # no other output — otherwise it would end as a blank assistant bubble.
        result_error: str | None = None
        # Live-429 feedback to the account router's cooldown. We fire once, as
        # soon as a frame shows the served account actually rate-limited, using
        # the freshest reset time captured from any rate_limit_event this turn.
        rate_reset_hint: float | None = None
        rate_fed_back = False

        async def _next_with_timeout() -> bytes | None:
            try:
                return await asyncio.wait_for(iterator.__anext__(), timeout=timeout)
            except StopAsyncIteration:
                return None

        try:
            while True:
                try:
                    line = await _next_with_timeout()
                except asyncio.TimeoutError:
                    yield {"type": "error", "message": f"timeout after {timeout}s"}
                    return
                except ClaudeRunnerError as exc:
                    # Stale --resume against a session the CLI doesn't have
                    # (model switched mid-session / container recreated).
                    # Only heal if nothing was streamed yet, so a retry can't
                    # duplicate partial output.
                    if allow_heal and not emitted and _is_missing_session_error(exc):
                        raise _NeedsHeal() from exc
                    yield {"type": "error", "message": _humanize_claude_error(exc)}
                    return
                if line is None:
                    break
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    yield {"type": "error", "message": f"malformed stream-json: {exc}"}
                    return
                tracker.feed(obj)

                reasoning = _extract_reasoning_text(obj)
                if reasoning:
                    emitted = True
                    yield {"type": "reasoning", "text": reasoning}
                    continue

                delta = _extract_delta_text(obj)
                if delta:
                    full_text_parts.append(delta)
                    emitted = True
                    yield {"type": "delta", "text": delta}
                    continue

                ts = _extract_tool_start(obj)
                if ts is not None:
                    emitted = True
                    yield {"type": "tool_start", **ts}
                    continue

                te = _extract_tool_end(obj)
                if te is not None:
                    emitted = True
                    yield {"type": "tool_end", **te}
                    continue

                u = _extract_usage(obj)
                if u is not None:
                    # Later frames (the terminal ``result``) supersede earlier
                    # partials — the last usage we see is authoritative.
                    usage = u

                rerr = _extract_result_error(obj)
                if rerr is not None:
                    # Remember it; don't break — let the loop drain so the
                    # subprocess finishes cleanly and usage is captured.
                    result_error = rerr

                blocked, rl_reset = _classify_rate_limit(obj)
                if rl_reset is not None:
                    rate_reset_hint = rl_reset
                if blocked and not rate_fed_back and container is not None:
                    # Authoritative saturation signal — park the served account
                    # so the router stops routing into it. Once per turn.
                    rate_fed_back = True
                    try:
                        user_container.note_turn_rate_limited(
                            container, account, rate_reset_hint,
                        )
                    except Exception:
                        _logger.exception("router rate-limit feedback failed")
                # Unrecognised frames are silently ignored; future stream-json
                # versions may add fields we don't care about.

            # An is-error result with no streamed output would otherwise end as
            # a blank assistant bubble — surface the clean message instead.
            if result_error is not None and not emitted:
                yield {"type": "error", "message": result_error}
                return

            elapsed = max(time.monotonic() - turn_start, 0.0)
            if usage is None:
                tokens: dict[str, int | None] = {"input": None, "output": None, "total": None}
            else:
                tokens = usage
            out_tok = tokens.get("output")
            tok_s = (out_tok / elapsed) if (isinstance(out_tok, int) and elapsed > 0) else None
            meta = {
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "model": model,
                "tokens": tokens,
                "tok_s": tok_s,
            }
            # A clean completion proves the served account is healthy — clear
            # any stale cooldown so the router can use it again immediately.
            if container is not None and not rate_fed_back:
                try:
                    user_container.note_turn_success(container, account)
                except Exception:
                    _logger.exception("router success feedback failed")
            yield {"type": "done", "full_text": "".join(full_text_parts), "meta": meta}
        finally:
            tracker.flush(model_hint=model)
            # Ensure the subprocess is reaped whether we exited cleanly, hit
            # an error, or were cancelled by the SSE consumer disconnecting.
            try:
                await gen.aclose()
            except Exception:
                # aclose() can re-raise the GeneratorExit-equivalent; we're
                # already at the terminal event, swallow.
                pass

    # First attempt uses the resume/create flag the caller chose. Heal is
    # only meaningful on a --resume turn for which we have a transcript to
    # replay into a fresh session.
    can_heal = (not is_first_turn) and bool(resume_heal_history)
    try:
        async for ev in _stream_attempt(args, allow_heal=can_heal):
            yield ev
    except _NeedsHeal:
        _logger.warning(
            "claude --resume missed session %s; healing as a fresh session "
            "(model=%s, container=%s)",
            claude_session_id, model, container,
        )
        # The fresh session has no memory of prior turns, so replay the
        # transcript as a leading history block. effective_prompt already
        # carries memory/persona/language/attachment layers; prepending
        # history keeps those intact, just after the conversation context.
        healed_prompt = (
            "[Conversation history — please continue as if you'd been part "
            "of this conversation. The user's NEW message follows after the "
            "history block.]\n\n"
            f"{resume_heal_history}\n\n"
            "[End of history. The user's new message:]\n\n"
            + effective_prompt
        )
        heal_args = _build_run_args(
            claude_session_id, healed_prompt, attachments_dir,
            is_first_turn=True,
            model=model,
            dispatch=dispatch,
            container_name=container,
            claude_attach_dir=claude_attach_dir,
            # Same identity directive as the primary attempt — without it the
            # healed turn could surface the pooled account owner's name.
            identity_prompt=build_identity_system_prompt(user_email),
        )
        async for ev in _stream_attempt(heal_args, allow_heal=False):
            yield ev


_TITLE_PROMPT_TEMPLATE = (
    "Summarize this chat in 6 words or fewer, no punctuation. "
    "Reply with only the title text, nothing else.\n\n"
    "User: {user}\n\n"
    "Assistant: {assistant}"
)

_TITLE_PROMPT_USER_ONLY = (
    "Summarize this chat topic in 6 words or fewer, no punctuation. "
    "Reply with only the title text, nothing else.\n\n"
    "User: {user}"
)


async def generate_title(
    claude_session_id: str,
    first_user_msg: str,
    first_assistant_msg: str = "",
    timeout: float = 60.0,
) -> str:
    """Best-effort title generator. Never raises; returns "" on failure.

    Uses a *separate* claude session-id (a fresh uuid-shaped string)
    derived from the original by suffix, so the title call doesn't
    pollute the user's chat session history. Goes through ``spawn_claude``
    so the same test seam covers it.

    ``first_assistant_msg`` is optional: when the title is generated at
    session start (in parallel with the first turn) we don't have an
    assistant reply yet, so we title from the user prompt alone.
    """
    if first_assistant_msg:
        prompt = _TITLE_PROMPT_TEMPLATE.format(
            user=_truncate_for_prompt(first_user_msg),
            assistant=_truncate_for_prompt(first_assistant_msg),
        )
    else:
        prompt = _TITLE_PROMPT_USER_ONLY.format(
            user=_truncate_for_prompt(first_user_msg),
        )
    # Use a fresh UUID-shaped session id so the title generation is a fresh
    # turn that does not appended to the user's session. claude code 2.x
    # rejects non-UUID session ids with "Error: Invalid session ID. Must be
    # a valid UUID." (and exits 0, so the failure is silent), so a `-title`
    # suffix on the original UUID is *not* a valid id — use a separate uuid4.
    title_session_id = str(uuid.uuid4())
    args = [
        "claude",
        "--session-id", title_session_id,
        "--output-format", "stream-json",
        # See run_turn comment on the --verbose / -p / stream-json
        # combination — required by the CLI, exit 1 without it.
        "--verbose",
        # See run_turn for the bypass rationale; same applies here.
        # Title generation never uses tools that need permission
        # (it's a single text completion), but keeping the flag
        # consistent across both invocations avoids surprises if the
        # title prompt ever evolves.
        "--permission-mode", "bypassPermissions",
        # Consistency with run_turn — the chat surface never exposes the
        # interactive question tool.
        "--disallowedTools", "AskUserQuestion",
        "-p", prompt,
    ]
    chunks: list[str] = []
    gen = spawn_claude(args)
    tracker = token_ledger.ClaudeStreamTracker(purpose="title")
    try:
        async def _drain() -> None:
            async for line in gen:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                tracker.feed(obj)
                t = _extract_delta_text(obj)
                if t:
                    chunks.append(t)

        try:
            await asyncio.wait_for(_drain(), timeout=timeout)
        except Exception:
            # generate_title MUST NOT raise per the brief. The catch covers
            # asyncio.TimeoutError, ClaudeRunnerError, JSON decode errors,
            # network glitches — every failure mode produces "" so the
            # caller's set_title no-op path takes over silently.
            return ""
    finally:
        tracker.flush()
        try:
            await gen.aclose()
        except Exception:
            pass

    title = "".join(chunks).strip()
    return _normalise_title(title)


def _truncate_for_prompt(s: str, limit: int = 1000) -> str:
    """Cap any single message we feed to the title prompt — keeps tokens bounded."""
    if not isinstance(s, str):
        return ""
    if len(s) <= limit:
        return s
    return s[:limit] + "\u2026"


def _normalise_title(s: str) -> str:
    """Trim, strip wrapping quotes, and clamp to 6 words / 80 chars."""
    if not s:
        return ""
    s = s.strip().strip('"').strip("'").strip()
    # Drop any trailing punctuation per the prompt's instruction.
    while s and s[-1] in ".!?,;:":
        s = s[:-1].rstrip()
    words = s.split()
    if len(words) > 6:
        words = words[:6]
    out = " ".join(words)
    if len(out) > 80:
        out = out[:80].rstrip()
    return out


# ---------------------------------------------------------------------------
# Multi-user dispatch helpers.
#
# Added in the multi-user-containers branch alongside services.chat.run_turn,
# the per-session orchestrator that decides which dispatch path each turn
# takes. These helpers are intentionally argv-only: the new sync orchestrator
# in run_turn.py builds an argv, hands it to a `runner` callable (defaulting
# to subprocess.run), and returns the CompletedProcess. That keeps the new
# code path independent of the existing async streaming machinery above —
# the admin / host-shell streaming dispatch is preserved byte-for-byte.
#
# DOCKER_EXEC_PREFIX is exposed as a module constant so:
#   - tests can assert "the per-user prefix is byte-stable across releases"
#     without re-deriving it themselves;
#   - the term-router service can reference the same constant when it picks
#     a docker exec invocation shape for the user shell (currently it
#     hard-codes its own argv, but the constant is here when alignment
#     becomes useful).
# ---------------------------------------------------------------------------

DOCKER_EXEC_PREFIX = [
    "docker", "exec",
    "-i",
    "-w", "/workspace",
    "-e", "HOME=/workspace",
]

# Single source of truth for valid dispatch modes in the new sync path.
# The async streaming path above uses ("local", "host") because "user" is
# meaningless there (the per-user containers are not the streaming target —
# the chat backend dispatches a single per-turn claude invocation into
# them, which is what the sync path covers).
_VALID_USER_DISPATCH = ("host", "user")


def build_argv(
    claude_argv: list[str],
    *,
    dispatch: str,
    user_container_name: str | None = None,
) -> list[str]:
    """Compose the argv for the chosen per-user dispatch mode.

    - dispatch="host": admin path. Argv passes through byte-identically — the
      elements and their order are unchanged. We return a copy so callers
      cannot mutate our return value into the caller's input list, but the
      elements compare element-wise equal.
    - dispatch="user": prepend DOCKER_EXEC_PREFIX + [user_container_name]
      so the claude invocation runs inside the per-user container provisioned
      by user_container.ensure_user_container.

    Raises ValueError on unknown dispatch, or on dispatch="user" without a
    non-empty container name.
    """
    if dispatch not in _VALID_USER_DISPATCH:
        raise ValueError(
            f"unknown dispatch={dispatch!r}; expected one of {_VALID_USER_DISPATCH}"
        )

    if dispatch == "host":
        return list(claude_argv)

    # dispatch == "user"
    if not user_container_name:
        raise ValueError(
            "dispatch='user' requires a non-empty user_container_name"
        )
    return [*DOCKER_EXEC_PREFIX, user_container_name, *claude_argv]


__all__ = [
    "ClaudeRunnerError",
    "DOCKER_EXEC_PREFIX",
    "build_argv",
    "generate_title",
    "run_turn",
    "spawn_claude",
]
