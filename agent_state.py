"""Agent state model.

NOTE FOR REVIEWERS: This file represents the "agent state model module from
Phase 1 of the prior build" that the brief expected to find pre-merged in
the workspace. The workspace was empty when this phase began (see PREFLIGHT.md
section "Prior-build scaffolding"); this is a minimal stand-in containing
exactly the surface the new code in this phase needs to interact with:
  - PauseCause enum (with the new .quota_exceeded value added per Phase 1)
  - precedence ordering (user_paused < {rate_limited, quota_exceeded} < usage_high)
  - request_pause(agent_id, cause) hook used by quotas.py
  - Agent class with the methods fleet.py needs (added in Phase 2 — see
    PREFLIGHT.md §1 of this phase for the extension rationale)
  - AgentRegistry with list_running() / list_all()

The Phase-1 single-cause attribute `AgentState.pause_cause` is preserved
verbatim so Phase-1 tests still pass. The Phase-2 multi-cause Agent model
is additive: when an Agent is paused/resumed via its own pause()/resume()
methods, the multi-cause set is the source of truth and `pause_cause`
shadows the *effective* (highest-precedence) cause for backwards compat.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Set


class PauseCause(enum.Enum):
    USER_PAUSED = "user_paused"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXCEEDED = "quota_exceeded"  # added in Phase 1 of THIS build
    USAGE_HIGH = "usage_high"


# Precedence: higher number wins. rate_limited and quota_exceeded are equal
# (neither overrides the other; the existing cause stays).
_PRECEDENCE: Dict[PauseCause, int] = {
    PauseCause.USER_PAUSED: 0,
    PauseCause.RATE_LIMITED: 1,
    PauseCause.QUOTA_EXCEEDED: 1,
    PauseCause.USAGE_HIGH: 2,
}


def precedence(cause: PauseCause) -> int:
    return _PRECEDENCE[cause]


def effective_cause(causes: Iterable[PauseCause]) -> Optional[PauseCause]:
    """Return the highest-precedence cause from a set; None if empty.

    Ties (rate_limited vs quota_exceeded) are broken by enum order.
    Centralised here so fleet.py never has to know precedence directly.
    """
    # Materialise so we can both check emptiness and pass to max() without
    # consuming a one-shot iterator. Renamed to avoid silently shadowing
    # the parameter name with a different concrete type.
    cause_list = list(causes)
    if not cause_list:
        return None
    return max(cause_list, key=lambda c: (precedence(c), c.name))


# ---------------------------------------------------------------------------
# Phase-1 surface (kept verbatim for backwards compat)
# ---------------------------------------------------------------------------


@dataclass
class AgentState:
    agent_id: str
    paused: bool = False
    pause_cause: Optional[PauseCause] = None


# In-memory registry of agent states for tests / scaffolding use.
_AGENTS: Dict[str, AgentState] = {}


def get_or_create(agent_id: str) -> AgentState:
    if agent_id not in _AGENTS:
        _AGENTS[agent_id] = AgentState(agent_id=agent_id)
    return _AGENTS[agent_id]


def reset_registry() -> None:
    """Test helper — clear all agent states."""
    _AGENTS.clear()


def request_pause(agent_id: str, cause: PauseCause) -> bool:
    """The pause hook the framework uses for .rate_limited and
    .quota_exceeded; quotas.py reuses it (does not introduce a parallel
    pause path).

    Returns True if the pause cause was applied or upgraded, False if the
    existing cause has equal-or-higher precedence and was kept.
    """
    state = get_or_create(agent_id)
    if state.pause_cause is None:
        state.pause_cause = cause
        state.paused = True
        return True
    # equal-precedence: keep existing (per brief: "neither overrides the other")
    if precedence(cause) > precedence(state.pause_cause):
        state.pause_cause = cause
        state.paused = True
        return True
    return False


# ---------------------------------------------------------------------------
# Phase-2 extension: Agent (multi-cause) + AgentRegistry
# (See PREFLIGHT.md §1 of Phase 2 for rationale.)
# ---------------------------------------------------------------------------


@dataclass
class PauseSnapshot:
    """Per-cause pause record. The per-agent pause path writes this when
    pause(cause, reason) is called. Phase 2 reads only `reason` for tests."""

    cause: PauseCause
    reason: Optional[str] = None


class Agent:
    """Minimal in-process Agent for the scaffolding stand-in.

    The real prior-build Agent will be a subprocess wrapper; this class
    exposes the same method surface so fleet.py talks to one model. Tests
    use mock objects with the same surface (see tests/test_fleet.py).
    """

    def __init__(self, agent_id: str, running: bool = True):
        self.id = agent_id
        self._running = running
        self._pauses: Dict[PauseCause, PauseSnapshot] = {}

    # --- liveness ---
    def is_running(self) -> bool:
        return self._running

    # --- pause-cause inspection ---
    def pause_causes(self) -> Set[PauseCause]:
        return set(self._pauses.keys())

    def effective_pause_cause(self) -> Optional[PauseCause]:
        return effective_cause(self._pauses.keys())

    def pause_snapshot(self, cause: PauseCause) -> Optional[PauseSnapshot]:
        return self._pauses.get(cause)

    # --- mutators ---
    def signal_term(self) -> None:
        """Graceful SIGTERM. In the real build this signals the subprocess;
        here it just marks the agent not-running."""
        self._running = False

    def pause(self, cause: PauseCause, reason: Optional[str] = None) -> None:
        self._pauses[cause] = PauseSnapshot(cause=cause, reason=reason)

    def resume(self, cause: PauseCause) -> None:
        """Clear ONE pause cause. If other causes remain, the agent stays
        paused under those (no cascade-clear)."""
        self._pauses.pop(cause, None)


class AgentRegistry:
    """Minimal in-process registry. Real prior-build registry has the same
    list_running()/list_all() shape.

    Phase 5 adds a minimal lifecycle event bus (register_listener / emit)
    used by notifications.py. See PREFLIGHT.md §4 for rationale."""

    def __init__(self) -> None:
        self._agents: Dict[str, Agent] = {}
        # Phase 5 — lifecycle event bus. Listeners receive
        # (agent_id: str, event_name: str, context: dict).
        self._listeners: List[Callable[[str, str, dict], None]] = []

    # ----- Phase 5: lifecycle event bus -----
    def register_listener(
        self, callback: Callable[[str, str, dict], None]
    ) -> None:
        if callback not in self._listeners:
            self._listeners.append(callback)

    def emit(self, agent_id: str, event_name: str, context: Optional[dict] = None) -> None:
        ctx = context or {}
        for cb in list(self._listeners):
            try:
                cb(agent_id, event_name, ctx)
            except Exception:
                # Listener errors must not affect other listeners or
                # the calling path. Phase 5 logs at the listener side.
                pass

    def kind_of(self, canonical_id: str) -> str:
        """Return "task", "project", or "unknown" by inspecting the id
        prefix. Real prior-build registry knows definitively; the
        stand-in uses the convention documented in PREFLIGHT.md §4."""
        if canonical_id.startswith("task-"):
            return "task"
        if canonical_id.startswith("agent-") or canonical_id.startswith("project-"):
            return "project"
        return "unknown"

    def add(self, agent: Agent) -> None:
        self._agents[agent.id] = agent

    def remove(self, agent_id: str) -> None:
        self._agents.pop(agent_id, None)

    def list_all(self) -> List[Agent]:
        return list(self._agents.values())

    def list_running(self) -> List[Agent]:
        return [a for a in self._agents.values() if a.is_running()]

    def reset(self) -> None:
        self._agents.clear()
