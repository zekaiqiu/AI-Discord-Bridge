"""Tests for quotas.py.

Covers (per brief): set, show, consumption-triggered transition, precedence
(usage_high not downgraded), restart persistence.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

import agent_state
import handle_resolver
from agent_state import PauseCause
from quotas import QuotaManager
from state_store import StateStore

from _helpers import FakeClock


@pytest.fixture(autouse=True)
def _reset_globals():
    agent_state.reset_registry()
    handle_resolver.reset()
    yield
    agent_state.reset_registry()
    handle_resolver.reset()


@pytest.fixture
def clock():
    # Same FakeClock pattern as test_confirmations.py for consistency, even
    # though no quota test currently advances time — keeps the two files
    # using the same fixture shape.
    return FakeClock(datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc))


@pytest.fixture
def store(tmp_path, clock):
    return StateStore(str(tmp_path / "s.json"), clock=clock)


@pytest.fixture
def usage_path(tmp_path):
    return str(tmp_path / "usage.json")


def _write_usage(path, entries):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"token_log": entries}, f)


def test_quota_set_records_budget(store, clock, usage_path):
    handle_resolver.register_agent("p1", "agent-planner-1")
    qm = QuotaManager(store, usage_path=usage_path, clock=clock)
    msg = qm.set_budget("p1", 5000, set_by="alice")
    assert "agent-planner-1" in msg and "5000" in msg
    quota = store.get_quota("agent-planner-1")
    assert quota["budget_tokens"] == 5000
    assert quota["set_by"] == "alice"


def test_quota_show_displays_budget_and_consumption(store, clock, usage_path):
    handle_resolver.register_agent("t3", "agent-tester-3")
    _write_usage(usage_path, [
        {"agent_id": "agent-tester-3", "tokens": 200},
        {"agent_id": "agent-tester-3", "tokens": 350},
        {"agent_id": "other", "tokens": 9999},
    ])
    qm = QuotaManager(store, usage_path=usage_path, clock=clock)
    qm.set_budget("t3", 1000, set_by="alice")
    msg = qm.show("t3")
    assert "budget=1000" in msg
    assert "consumed=550" in msg


def test_consumption_triggers_quota_exceeded_pause(store, clock, usage_path):
    handle_resolver.register_agent("p1", "agent-planner-1")
    _write_usage(usage_path, [
        {"agent_id": "agent-planner-1", "tokens": 1500},
    ])
    qm = QuotaManager(store, usage_path=usage_path, clock=clock)
    qm.set_budget("p1", 1000, set_by="alice")
    out = qm.check_and_pause("p1")
    assert out is True
    s = agent_state.get_or_create("agent-planner-1")
    assert s.paused is True
    assert s.pause_cause == PauseCause.QUOTA_EXCEEDED


def test_within_budget_does_not_pause(store, clock, usage_path):
    handle_resolver.register_agent("p1", "agent-planner-1")
    _write_usage(usage_path, [
        {"agent_id": "agent-planner-1", "tokens": 100},
    ])
    qm = QuotaManager(store, usage_path=usage_path, clock=clock)
    qm.set_budget("p1", 1000, set_by="alice")
    assert qm.check_and_pause("p1") is False
    s = agent_state.get_or_create("agent-planner-1")
    assert s.pause_cause is None


def test_precedence_usage_high_not_downgraded(store, clock, usage_path):
    handle_resolver.register_agent("p1", "agent-planner-1")
    _write_usage(usage_path, [
        {"agent_id": "agent-planner-1", "tokens": 99999},
    ])
    # pre-existing usage_high state
    s = agent_state.get_or_create("agent-planner-1")
    s.paused = True
    s.pause_cause = PauseCause.USAGE_HIGH

    qm = QuotaManager(store, usage_path=usage_path, clock=clock)
    qm.set_budget("p1", 100, set_by="alice")
    qm.check_and_pause("p1")

    # still usage_high; not downgraded to quota_exceeded
    assert s.pause_cause == PauseCause.USAGE_HIGH


def test_precedence_user_paused_upgrades_to_quota_exceeded(store, clock, usage_path):
    handle_resolver.register_agent("p1", "agent-planner-1")
    _write_usage(usage_path, [
        {"agent_id": "agent-planner-1", "tokens": 99999},
    ])
    s = agent_state.get_or_create("agent-planner-1")
    s.paused = True
    s.pause_cause = PauseCause.USER_PAUSED

    qm = QuotaManager(store, usage_path=usage_path, clock=clock)
    qm.set_budget("p1", 100, set_by="alice")
    qm.check_and_pause("p1")
    assert s.pause_cause == PauseCause.QUOTA_EXCEEDED


def test_precedence_rate_limited_equal_with_quota_exceeded(
    store, clock, usage_path
):
    """rate_limited and quota_exceeded are equal-precedence; the existing
    one stays (per brief)."""
    handle_resolver.register_agent("p1", "agent-planner-1")
    _write_usage(usage_path, [
        {"agent_id": "agent-planner-1", "tokens": 99999},
    ])
    s = agent_state.get_or_create("agent-planner-1")
    s.paused = True
    s.pause_cause = PauseCause.RATE_LIMITED

    qm = QuotaManager(store, usage_path=usage_path, clock=clock)
    qm.set_budget("p1", 100, set_by="alice")
    qm.check_and_pause("p1")
    # equal-precedence -> existing kept
    assert s.pause_cause == PauseCause.RATE_LIMITED


def test_quota_persists_across_reload(tmp_path, clock, usage_path):
    handle_resolver.register_agent("p1", "agent-planner-1")
    path = str(tmp_path / "s.json")

    s1 = StateStore(path, clock=clock)
    qm1 = QuotaManager(s1, usage_path=usage_path, clock=clock)
    qm1.set_budget("p1", 4242, set_by="alice")

    s2 = StateStore(path, clock=clock)
    quota = s2.get_quota("agent-planner-1")
    assert quota is not None
    assert quota["budget_tokens"] == 4242
    assert quota["set_by"] == "alice"


def test_unknown_handle_reports_error(store, clock, usage_path):
    qm = QuotaManager(store, usage_path=usage_path, clock=clock)
    msg = qm.set_budget("nonesuch", 1000, set_by="alice")
    assert "unknown agent" in msg
    msg2 = qm.show("nonesuch")
    assert "unknown agent" in msg2
