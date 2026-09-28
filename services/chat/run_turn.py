"""Per-session dispatch orchestrator: session creation + per-turn routing.

This module is the new (multi-user-containers) per-session orchestrator. It
is DISTINCT from claude_runner.run_turn (the async streaming generator that
yields stream-json events) — they coexist and own different layers:

  - claude_runner.run_turn  -> async streaming of one claude turn's output;
                               speaks the SSE / message-send protocol.
  - run_turn.run_turn       -> sync; decides which container/argv to dispatch
                               into for a given session, then hands off to
                               a `runner` callable (defaults to subprocess.run).

Three top-level functions:

  - create_session: one-time, called when a chat session is first opened.
    Resolves role from auth.resolve_role, provisions the per-user container
    if role is "user" (admins skip provisioning entirely — they keep using
    the existing chat-host-shell admin path byte-for-byte), and persists a
    session JSON to PORTFOLIO_SESSIONS_DIR.

  - run_turn: called on every user turn. RE-READS the session JSON each
    time (no caching) so a chat-service restart still picks up the
    persisted container name. Routes through claude_runner.build_argv +
    the supplied runner callable.

  - run_title_call: convenience for the title-generation call. Same role/
    container as run_turn — implemented by delegation so any change to
    dispatch logic stays in one place.

Per-user resource overrides are read by user_container on every
ensure_user_container call; they do not flow through the session JSON.
"""

from __future__ import annotations

import datetime as _dt
import subprocess  # for CompletedProcess return type + subprocess.run default
from typing import Callable, Optional

import auth
import claude_runner
import sessions
import user_container


def _now_iso_utc() -> str:
    """ISO8601 UTC with trailing 'Z'."""
    # datetime.utcnow() (not datetime.now(timezone.utc)) so the on-disk
    # format matches what the schema specifies for session JSON.
    return _dt.datetime.utcnow().isoformat() + "Z"


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def create_session(
    session_id: str,
    email: str,
    *,
    ensure_container: Optional[Callable[[str], str]] = None,
) -> dict:
    """Resolve role, optionally provision the user container, persist + return.

    `ensure_container` is dependency-injected for tests; in production it
    falls through to user_container.ensure_user_container.
    """
    normalized_email = _normalize_email(email)
    role = auth.resolve_role(normalized_email)

    session: dict = {
        "session_id": session_id,
        "email": normalized_email,
        "role": role,
        "created_at": _now_iso_utc(),
    }

    if role == "user":
        provisioner = ensure_container or user_container.ensure_user_container
        # Pass the already-normalized email; ensure_user_container also
        # normalizes internally, so this is belt-and-braces.
        session["container"] = provisioner(normalized_email)
    # Admin path: NO "container" key. Tests assert its absence.

    sessions.save_session(session_id, session)
    return session


def run_turn(
    session_id: str,
    claude_argv: list[str],
    *,
    runner: Optional[Callable[[list[str]], subprocess.CompletedProcess]] = None,
) -> subprocess.CompletedProcess:
    """Dispatch a single turn based on the persisted session role.

    Re-reads session JSON every call — no caching, so a process restart
    picks up the persisted container name without state recovery.
    """
    session = sessions.load_session(session_id)

    if session.get("role") == "user":
        dispatch = "user"
        # KeyError here = session was saved without a "container" key for a
        # user role; that indicates a create_session bug (or hand-edited JSON),
        # so we let it surface rather than silently falling back to host.
        user_container_name = session["container"]
    else:
        dispatch = "host"
        user_container_name = None

    argv = claude_runner.build_argv(
        claude_argv,
        dispatch=dispatch,
        user_container_name=user_container_name,
    )
    return (runner or subprocess.run)(argv)


def run_title_call(
    session_id: str,
    claude_argv: list[str],
    *,
    runner: Optional[Callable[[list[str]], subprocess.CompletedProcess]] = None,
) -> subprocess.CompletedProcess:
    """Title-generation call. Same dispatch behavior as run_turn — delegate
    so the two paths can never drift."""
    return run_turn(session_id, claude_argv, runner=runner)
