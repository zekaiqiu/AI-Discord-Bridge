"""Tests for confirmations.py.

Covers (per brief): no-pending, expired (clock injection — no real sleeps),
cross-user, second-command-discards-first, channel-isolation,
persistence-across-reload. Also includes an inline-default regression test.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from confirmations import ConfirmationManager, TTL_SECONDS
from state_store import StateStore
from bot import Dispatcher, register_phase1_commands
from quotas import QuotaManager
import help_registry

from _helpers import FakeClock


@pytest.fixture
def clock():
    return FakeClock(datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc))


@pytest.fixture
def store(tmp_path, clock):
    p = tmp_path / "state.json"
    return StateStore(str(p), clock=clock)


@pytest.fixture
def cm(store, clock):
    m = ConfirmationManager(store, clock=clock)

    # Two example destructive actions registered as test-only handlers
    # so the discard-and-notify path can be exercised end-to-end (per
    # brief: "Phase 1 should provide a tiny test-only or example
    # destructive action so the test suite can exercise the discard-and-
    # notify path end-to-end").
    m.register_action("test_destroy", lambda payload: f"executed:{payload}")
    m.register_action("test_destroy2", lambda payload: f"executed2:{payload}")
    return m


def test_confirm_with_no_pending_returns_clear_error(cm):
    executed, msg = cm.confirm("alice", "C1")
    assert executed is False
    assert "no pending action" in msg


def test_expired_pending_returns_expired_error(cm, clock):
    cm.register_pending("alice", "C1", "test_destroy", "wipe everything")
    clock.advance(TTL_SECONDS + 1)
    executed, msg = cm.confirm("alice", "C1")
    assert executed is False
    assert "expired" in msg.lower()


def test_cross_user_cannot_resolve_other_users_pending(cm):
    cm.register_pending("alice", "C1", "test_destroy", "wipe")
    executed, msg = cm.confirm("bob", "C1")
    assert executed is False
    assert "no pending action" in msg
    # alice's is still resolvable
    executed_a, _ = cm.confirm("alice", "C1")
    assert executed_a is True


def test_second_command_discards_first_with_exact_notice(cm):
    """End-to-end discard-and-notify exercise: register a first pending
    action, register a second one (which must discard the first and emit
    the exact mandated notice), then !confirm and verify the SECOND
    action's handler runs (not the first)."""
    cm.register_pending("alice", "C1", "test_destroy", "wipe v1")
    msg = cm.register_pending(
        "alice", "C1", "test_destroy2", "wipe v2", action_payload="v2payload"
    )
    expected_prefix = (
        "previous pending action test_destroy discarded; "
        "type !confirm to confirm test_destroy2"
    )
    assert msg.startswith(expected_prefix)

    # End-to-end: !confirm now executes test_destroy2 (the new action),
    # not test_destroy. Both handlers are registered on the fixture's
    # ConfirmationManager so we can distinguish which one ran by output.
    executed, result = cm.confirm("alice", "C1")
    assert executed is True
    assert result == "executed2:v2payload"

    # And after consumption, no pending action remains.
    executed_again, msg_again = cm.confirm("alice", "C1")
    assert executed_again is False
    assert "no pending action" in msg_again


def test_channels_are_independent(cm):
    cm.register_pending("alice", "DM", "test_destroy", "wipe DM")
    cm.register_pending("alice", "general", "test_destroy", "wipe general")
    # Resolving in #bot should still see nothing for alice
    executed, msg = cm.confirm("alice", "bot")
    assert executed is False and "no pending" in msg
    # Resolving in DM consumes only DM
    executed_dm, _ = cm.confirm("alice", "DM")
    assert executed_dm is True
    # general is still there
    executed_g, _ = cm.confirm("alice", "general")
    assert executed_g is True


def test_persistence_across_reload(tmp_path, clock):
    path = str(tmp_path / "s.json")
    s1 = StateStore(path, clock=clock)
    cm1 = ConfirmationManager(s1, clock=clock)
    cm1.register_action("test_destroy", lambda p: "ok")
    cm1.register_pending("alice", "C1", "test_destroy", "wipe")

    # Reload into a fresh store. Unexpired entry must survive.
    s2 = StateStore(path, clock=clock)
    cm2 = ConfirmationManager(s2, clock=clock)
    cm2.register_action("test_destroy", lambda p: "ok2")
    executed, msg = cm2.confirm("alice", "C1")
    assert executed is True
    assert msg == "ok2"


def test_persistence_drops_expired_on_load(tmp_path, clock):
    path = str(tmp_path / "s.json")
    s1 = StateStore(path, clock=clock)
    cm1 = ConfirmationManager(s1, clock=clock)
    cm1.register_pending("alice", "C1", "test_destroy", "wipe")

    # Advance clock past TTL, then reload
    clock.advance(TTL_SECONDS + 5)
    s2 = StateStore(path, clock=clock)
    # entry is dropped on load
    assert s2.get_pending("alice", "C1") is None
    # raw store also empty
    assert s2.get("pending_confirmations") == {}


def test_prompt_remaining_seconds_computed_at_render(cm, clock):
    msg = cm.register_pending("alice", "C1", "test_destroy", "wipe everything")
    assert f"within {TTL_SECONDS}s" in msg
    clock.advance(20)
    rendered = cm.render_prompt("alice", "C1")
    # 60 - 20 = 40 remaining
    assert "40s" in rendered


def test_inline_default_non_bang_prefix_is_not_admin_path(store, clock):
    """A non-!-prefixed message must not trigger any new admin handler."""
    cm = ConfirmationManager(store, clock=clock)
    qm = QuotaManager(store, usage_path=os.devnull, clock=clock)
    d = Dispatcher(cm, qm)
    # plain chat — even if it spells out 'confirm' or 'quota set'
    assert d.dispatch("confirm please", "alice", "C1") is None
    assert d.dispatch("quota set p1 1000", "alice", "C1") is None
    assert d.dispatch("hello world", "alice", "C1") is None


def test_help_registers_section_and_per_command_docs(store, clock):
    help_registry.reset()
    cm = ConfirmationManager(store, clock=clock)
    qm = QuotaManager(store, usage_path=os.devnull, clock=clock)
    register_phase1_commands(cm, qm)

    sections = help_registry.help_sections()
    assert "QUOTAS & CONFIRMATIONS" in sections
    cmds = [c for c, _ in sections["QUOTAS & CONFIRMATIONS"]]
    assert "!confirm" in cmds
    assert "!quota set" in cmds
    assert "!quota show" in cmds

    assert help_registry.help_for("confirm")
    assert help_registry.help_for("quota")
