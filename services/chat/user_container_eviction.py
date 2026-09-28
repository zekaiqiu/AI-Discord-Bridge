"""Per-user container auto-eviction.

Identifies idle ``portfolio-user-*`` containers and their named volumes
and applies an age-based eviction policy. Two thresholds drive the
policy: ``idle_threshold`` (running container with no recent activity →
stop and remove) and ``volume_threshold`` (container already gone for
this long → remove its volume too). The two thresholds are independent;
while a container exists, its volume is preserved regardless of
``volume_threshold``.

Scheduling: an internal asyncio daily task runs inside the chat
container itself. The chat service already mounts ``/var/run/docker.sock``
(see docker-compose.yml: ``services.chat.volumes``) and already has
the ``docker`` SDK installed (used by ``user_container.py``), so wiring
a startup hook on the existing FastAPI app is the smallest possible
change — no new systemd unit, no new cron, no new image, no new
service. The fallback systemd-timer path documented in the brief
remains available if a future deploy strips the docker.sock mount;
that fallback is NOT shipped here because the prerequisite (no
docker.sock in chat) is not currently true.

Default behavior: dry-run. Real evictions require BOTH
``CHAT_USER_EVICTION_ENABLED=1`` in the chat service environment AND
``dry_run=False`` at the call site. Either gate missing → no
destructive action; the function still returns the planned action
list so the caller can log it.

Non-goals:
  * Does NOT touch ``/data/sessions/<email_slug>/`` — session JSON is
    preserved across container/volume eviction so a returning user's
    history survives.
  * Does NOT alert / page / emit metrics — INFO-level logging only.
  * Does NOT evict containers carrying the
    ``portfolio.do-not-evict=true`` label.

The label-writing side (a ``LastUsedAt`` label updated when a per-user
container is touched) is intentionally NOT implemented here; this
module reads the label if it exists and falls back to ``State.StartedAt``
otherwise.
"""

from __future__ import annotations

import dataclasses
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

import docker
import docker.errors

import user_container

_log = logging.getLogger(__name__)

# Action vocabulary — see EvictionAction.
ACTION_STOP_AND_REMOVE = "stop_and_remove_container"
ACTION_REMOVE_VOLUME = "remove_volume"
ACTION_SKIP = "skip"

# Label keys read (not written) by this module.
DO_NOT_EVICT_LABEL = "portfolio.do-not-evict"
LAST_USED_LABEL = "portfolio.last-used-at"

# Env-var gate: must be exactly "1" for destructive actions to fire.
ENV_ENABLED = "CHAT_USER_EVICTION_ENABLED"


@dataclasses.dataclass
class EvictionAction:
    """A single planned (or executed) action against one container.

    ``action`` is one of:
      * ``"stop_and_remove_container"`` — the container was running and
        idle past ``idle_threshold``; stop + remove was planned/done.
      * ``"remove_volume"`` — the container had been gone for at least
        ``volume_threshold``; the matching named volume was planned/
        removed.
      * ``"skip"`` — no action; ``reason`` explains why
        (do-not-evict label, within retention window, error, etc.).
    """

    container_name: str
    action: str
    reason: str


def _parse_docker_ts(value: Optional[str]) -> Optional[datetime]:
    """Parse a Docker-style ISO 8601 timestamp.

    Docker emits timestamps like ``2026-04-30T12:34:56.123456789Z`` —
    Python's ``fromisoformat`` accepts the trailing ``Z`` from 3.11
    onwards but chokes on the 9-digit nanosecond fractional. Trim to
    microseconds and replace ``Z`` with ``+00:00`` for portability.
    """
    if not value or not isinstance(value, str):
        return None
    s = value.strip()
    if s in ("", "0001-01-01T00:00:00Z"):
        # Docker uses the zero time to mean "never started/finished".
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # Trim nanoseconds to microseconds if present.
    if "." in s:
        head, _, tail = s.partition(".")
        # tail may be "123456789+00:00" — split on +/- to isolate frac
        for sep in ("+", "-"):
            if sep in tail:
                frac, sep2, tz = tail.partition(sep)
                tail = frac[:6] + sep2 + tz
                break
        else:
            tail = tail[:6]
        s = f"{head}.{tail}"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _last_activity(container: Any) -> Optional[datetime]:
    """Best-effort 'last activity' timestamp for an idle decision.

    Prefers the ``portfolio.last-used-at`` label (set by future code that
    touches the container); falls back to ``State.StartedAt`` from
    ``container.attrs``. Returns None if neither is parseable.
    """
    attrs = getattr(container, "attrs", None) or {}
    config = attrs.get("Config") or {}
    labels = config.get("Labels") or {}
    label_ts = _parse_docker_ts(labels.get(LAST_USED_LABEL))
    state = attrs.get("State") or {}
    started_ts = _parse_docker_ts(state.get("StartedAt"))
    # Note: ``FinishedAt`` is intentionally NOT a candidate. This helper is
    # only consulted on the running-container branch in
    # ``evict_idle_containers``; the stopped-container branch reads
    # ``FinishedAt`` directly. Including it here would mean a container
    # that crashed once and was never restarted would look "recently
    # active" if its ``FinishedAt`` was newer than its ``StartedAt``.
    candidates = [t for t in (label_ts, started_ts) if t is not None]
    if not candidates:
        return None
    return max(candidates)


def _is_running(container: Any) -> bool:
    state = (getattr(container, "attrs", None) or {}).get("State") or {}
    return bool(state.get("Running"))


def _has_do_not_evict(container: Any) -> bool:
    config = (getattr(container, "attrs", None) or {}).get("Config") or {}
    labels = config.get("Labels") or {}
    return labels.get(DO_NOT_EVICT_LABEL) == "true"


def _list_user_containers(client: Any) -> Iterable[Any]:
    """All containers (running or stopped) whose name matches the prefix."""
    try:
        containers = client.containers.list(all=True)
    except Exception as exc:  # noqa: BLE001 — Docker SDK errors vary
        _log.warning("eviction: containers.list failed: %r", exc)
        return []
    matched = []
    for c in containers:
        name = getattr(c, "name", None) or ""
        if name.startswith(user_container.CONTAINER_NAME_PREFIX):
            matched.append(c)
    return matched


def _volume_name_for_container(container_name: str) -> str:
    return f"{container_name}{user_container.VOLUME_SUFFIX}"


def evict_idle_containers(
    *,
    idle_threshold: timedelta = timedelta(days=7),
    volume_threshold: timedelta = timedelta(days=30),
    dry_run: bool = True,
    # ``client`` and ``now`` below are test seams — production callers
    # omit both and let ``docker.from_env()`` / ``datetime.now`` run.
    client: Optional[Any] = None,
    now: Optional[datetime] = None,
) -> list[EvictionAction]:
    """Walk per-user containers and apply age-based eviction policy.

    Returns the full action list (including ``skip`` actions) so callers
    can log every decision.

    Env-var gate: when ``CHAT_USER_EVICTION_ENABLED`` is not exactly
    ``"1"`` AND the caller passed ``dry_run=False``, ``dry_run`` is
    forced back to True and a single INFO log line records the override.
    A caller that already passed ``dry_run=True`` does not log because
    no override happened.
    """
    if os.environ.get(ENV_ENABLED) != "1" and not dry_run:
        _log.info("eviction disabled by env; forcing dry-run")
        dry_run = True

    if client is None:
        client = docker.from_env()
    if now is None:
        now = datetime.now(timezone.utc)

    actions: list[EvictionAction] = []
    containers = _list_user_containers(client)

    for c in containers:
        name = getattr(c, "name", None) or "<unknown>"
        try:
            if _has_do_not_evict(c):
                actions.append(EvictionAction(name, ACTION_SKIP, "do-not-evict label set"))
                continue

            running = _is_running(c)
            last = _last_activity(c)

            if running:
                if last is None:
                    actions.append(EvictionAction(
                        name, ACTION_SKIP, "running with no parseable activity timestamp",
                    ))
                    continue
                age = now - last
                if age >= idle_threshold:
                    if not dry_run:
                        # Act first; on failure the per-container except
                        # below records the skip. Appending the planned
                        # action only after success keeps the action list
                        # one entry per container.
                        c.stop()
                        c.remove()
                    actions.append(EvictionAction(name, ACTION_STOP_AND_REMOVE, f"idle for {age}"))
                else:
                    actions.append(EvictionAction(
                        name, ACTION_SKIP, f"within idle threshold ({age} < {idle_threshold})",
                    ))
                continue

            # Container is not running. Volume retention applies.
            # Use FinishedAt if available, else fall back to StartedAt.
            attrs = getattr(c, "attrs", None) or {}
            state = attrs.get("State") or {}
            stopped_at = _parse_docker_ts(state.get("FinishedAt")) or last
            if stopped_at is None:
                actions.append(EvictionAction(
                    name, ACTION_SKIP, "stopped with no parseable timestamp",
                ))
                continue
            age = now - stopped_at
            if age < volume_threshold:
                actions.append(EvictionAction(
                    name, ACTION_SKIP,
                    f"volume within retention window ({age} < {volume_threshold})",
                ))
                continue
            volume_name = _volume_name_for_container(name)
            if not dry_run:
                # Remove the stopped container first so its mount is
                # released, then the volume. Per-container except
                # below catches any failure and records the skip in
                # place of the volume-removal action.
                try:
                    c.remove()
                except docker.errors.NotFound:
                    pass
                try:
                    vol = client.volumes.get(volume_name)
                    vol.remove()
                except docker.errors.NotFound:
                    pass
            actions.append(EvictionAction(
                name, ACTION_REMOVE_VOLUME, f"stopped {age} ago; volume past retention",
            ))
        except Exception as exc:  # noqa: BLE001 — never let one container kill the loop
            _log.warning("eviction: error processing %s: %r", name, exc)
            actions.append(EvictionAction(name, ACTION_SKIP, f"error: {type(exc).__name__}"))

    return actions
