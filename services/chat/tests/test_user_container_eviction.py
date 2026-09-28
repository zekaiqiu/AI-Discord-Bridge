"""Tests for user_container_eviction.

All tests use a MagicMock docker client; no real daemon is touched.
Mirrors the conventions in test_user_container.py (unittest.mock,
pytest, no pytest-mock dep).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import docker.errors
import pytest

from user_container_eviction import (
    ACTION_REMOVE_VOLUME,
    ACTION_SKIP,
    ACTION_STOP_AND_REMOVE,
    DO_NOT_EVICT_LABEL,
    LAST_USED_LABEL,
    ENV_ENABLED,
    EvictionAction,
    evict_idle_containers,
)


NOW = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)


def _ts(dt: datetime) -> str:
    """Format a datetime back into a Docker-style timestamp."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _fake_container(
    *,
    name: str = "portfolio-user-aaaa11112222",
    running: bool = True,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    last_used_at: datetime | None = None,
    do_not_evict: bool = False,
):
    """Build a MagicMock container with attrs shaped like the docker SDK."""
    labels: dict[str, str] = {}
    if do_not_evict:
        labels[DO_NOT_EVICT_LABEL] = "true"
    if last_used_at is not None:
        labels[LAST_USED_LABEL] = _ts(last_used_at)
    state: dict[str, object] = {"Running": running}
    if started_at is not None:
        state["StartedAt"] = _ts(started_at)
    if finished_at is not None:
        state["FinishedAt"] = _ts(finished_at)
    # spec= locks the mock surface to the docker SDK Container shape we
    # actually exercise; an attribute typo in a future test fails loudly
    # instead of returning a silently-truthy auto-attr.
    c = MagicMock(spec=["name", "attrs", "stop", "remove"])
    c.name = name
    c.attrs = {"State": state, "Config": {"Labels": labels}}
    return c


def _fake_client(containers):
    client = MagicMock()
    client.containers.list.return_value = list(containers)
    # Default: volume.get returns a fake volume with a remove method.
    fake_volume = MagicMock()
    client.volumes.get.return_value = fake_volume
    client._fake_volume = fake_volume  # convenience handle for assertions
    return client


# ---------------------------------------------------------------------------
# 1. dry_run=True returns planned actions without invoking destructive APIs.
# ---------------------------------------------------------------------------


def test_dry_run_returns_actions_without_acting(monkeypatch):
    monkeypatch.setenv(ENV_ENABLED, "1")  # gate ON so we are sure dry_run alone blocks
    idle = NOW - timedelta(days=10)
    c = _fake_container(running=True, started_at=idle)
    client = _fake_client([c])

    actions = evict_idle_containers(
        idle_threshold=timedelta(days=7),
        volume_threshold=timedelta(days=30),
        dry_run=True,
        client=client,
        now=NOW,
    )

    assert len(actions) == 1
    assert actions[0].action == ACTION_STOP_AND_REMOVE
    # No destructive call made:
    c.stop.assert_not_called()
    c.remove.assert_not_called()
    client._fake_volume.remove.assert_not_called()


# ---------------------------------------------------------------------------
# 2. Idle running container → stop_and_remove; with both gates open it acts.
# ---------------------------------------------------------------------------


def test_idle_running_container_is_stopped_and_removed_when_enabled(monkeypatch):
    monkeypatch.setenv(ENV_ENABLED, "1")
    idle = NOW - timedelta(days=10)
    c = _fake_container(running=True, started_at=idle)
    client = _fake_client([c])

    actions = evict_idle_containers(
        idle_threshold=timedelta(days=7),
        dry_run=False,
        client=client,
        now=NOW,
    )

    assert [a.action for a in actions] == [ACTION_STOP_AND_REMOVE]
    c.stop.assert_called_once_with()
    c.remove.assert_called_once_with()


# ---------------------------------------------------------------------------
# 3. do-not-evict label → skip; never acted on even if idle.
# ---------------------------------------------------------------------------


def test_do_not_evict_label_blocks_action_even_when_idle(monkeypatch):
    monkeypatch.setenv(ENV_ENABLED, "1")
    idle = NOW - timedelta(days=365)  # very old
    c = _fake_container(running=True, started_at=idle, do_not_evict=True)
    client = _fake_client([c])

    actions = evict_idle_containers(
        idle_threshold=timedelta(days=7),
        dry_run=False,
        client=client,
        now=NOW,
    )

    assert len(actions) == 1
    assert actions[0].action == ACTION_SKIP
    assert "do-not-evict" in actions[0].reason
    c.stop.assert_not_called()
    c.remove.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Volume retention: young volume preserved; old volume removed.
# ---------------------------------------------------------------------------


def test_stopped_container_with_young_volume_is_skipped(monkeypatch):
    monkeypatch.setenv(ENV_ENABLED, "1")
    finished = NOW - timedelta(days=5)  # within 30-day retention
    c = _fake_container(running=False, started_at=finished, finished_at=finished)
    client = _fake_client([c])

    actions = evict_idle_containers(
        idle_threshold=timedelta(days=7),
        volume_threshold=timedelta(days=30),
        dry_run=False,
        client=client,
        now=NOW,
    )

    assert len(actions) == 1
    assert actions[0].action == ACTION_SKIP
    assert "retention" in actions[0].reason
    c.remove.assert_not_called()
    client._fake_volume.remove.assert_not_called()


def test_stopped_container_with_old_volume_is_removed(monkeypatch):
    monkeypatch.setenv(ENV_ENABLED, "1")
    finished = NOW - timedelta(days=45)  # past 30-day retention
    name = "portfolio-user-deadbeef0000"
    c = _fake_container(name=name, running=False, started_at=finished, finished_at=finished)
    client = _fake_client([c])

    actions = evict_idle_containers(
        idle_threshold=timedelta(days=7),
        volume_threshold=timedelta(days=30),
        dry_run=False,
        client=client,
        now=NOW,
    )

    assert len(actions) == 1
    assert actions[0].action == ACTION_REMOVE_VOLUME
    # Container removed before volume.
    c.remove.assert_called_once_with()
    client.volumes.get.assert_called_once_with(f"{name}-home")
    client._fake_volume.remove.assert_called_once_with()


# ---------------------------------------------------------------------------
# 5. Env-var gate forces dry_run when unset, even if caller passed False.
# ---------------------------------------------------------------------------


def test_env_var_unset_forces_dry_run(monkeypatch):
    monkeypatch.delenv(ENV_ENABLED, raising=False)
    idle = NOW - timedelta(days=10)
    c = _fake_container(running=True, started_at=idle)
    client = _fake_client([c])

    actions = evict_idle_containers(
        idle_threshold=timedelta(days=7),
        dry_run=False,  # caller asked to act, but env gate is shut
        client=client,
        now=NOW,
    )

    assert [a.action for a in actions] == [ACTION_STOP_AND_REMOVE]
    # Despite dry_run=False at the call site, no destructive method ran:
    c.stop.assert_not_called()
    c.remove.assert_not_called()
    client._fake_volume.remove.assert_not_called()


# ---------------------------------------------------------------------------
# 6. Per-container error handling: one bad apple does not kill the loop.
# ---------------------------------------------------------------------------


def test_per_container_error_yields_skip_and_continues(monkeypatch):
    monkeypatch.setenv(ENV_ENABLED, "1")
    idle = NOW - timedelta(days=10)
    bad = _fake_container(name="portfolio-user-bad000000000", running=True, started_at=idle)
    bad.stop.side_effect = RuntimeError("boom")
    good = _fake_container(name="portfolio-user-good00000000", running=True, started_at=idle)
    client = _fake_client([bad, good])

    actions = evict_idle_containers(
        idle_threshold=timedelta(days=7),
        dry_run=False,
        client=client,
        now=NOW,
    )

    assert len(actions) == 2
    by_name = {a.container_name: a for a in actions}
    assert by_name["portfolio-user-bad000000000"].action == ACTION_SKIP
    assert "error" in by_name["portfolio-user-bad000000000"].reason
    assert by_name["portfolio-user-good00000000"].action == ACTION_STOP_AND_REMOVE
    good.stop.assert_called_once_with()
    good.remove.assert_called_once_with()
