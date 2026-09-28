"""Cron-based scheduler for !schedule / !schedules / !unschedule.

Runtime dependency: croniter (added as a project requirement in
PREFLIGHT.md §4 — there is no central pyproject.toml in the
scaffolded workspace).

Discord-attachment specs are rejected at the dispatcher boundary with:
    "v1 schedules accept text-only specs; attachments are not serialized."
This module accepts only string specs.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, List, Optional

from croniter import croniter, CroniterBadCronError

from _time import parse_iso, utcnow


VALID_KINDS = ("task", "project")

ATTACHMENT_REJECT_MSG = (
    "v1 schedules accept text-only specs; attachments are not serialized."
)


def _kind_error(kind: str) -> str:
    return f"unknown agent kind '{kind}'. Valid: task, project."


@dataclass
class ScheduleRecord:
    handle: str
    cron: str
    agent_kind: str
    spec: str
    owner_user_id: str
    created_at: str
    last_fired_at: Optional[str] = None
    # next_fire_time is computed on demand — never persisted.
    next_fire_time: Optional[datetime] = field(default=None, compare=False)


def _next_fire(cron_expr: str, base: datetime) -> datetime:
    return croniter(cron_expr, base).get_next(datetime)


class Scheduler:
    """Cron scheduler with persistent state.

    `tick(now)` is the test-friendly entry point: it walks all schedules
    and fires any whose next_fire_time <= now. Production runs a
    background task that periodically calls tick().
    """

    log = logging.getLogger("scheduler")

    def __init__(
        self,
        state_store,
        spawn_callable: Callable[[str, str], Awaitable[str]],
        clock: Callable[[], datetime] = utcnow,
    ):
        self.store = state_store
        self.spawn = spawn_callable
        self.clock = clock
        # in-memory mirror; loaded lazily from state_store
        self._records: List[ScheduleRecord] = []
        self._loaded = False

    # -------------------------------------------------- internal helpers
    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load_from_state()

    def _persist(self) -> None:
        out = []
        for r in self._records:
            out.append({
                "handle": r.handle,
                "cron": r.cron,
                "agent_kind": r.agent_kind,
                "spec": r.spec,
                "owner_user_id": r.owner_user_id,
                "created_at": r.created_at,
                "last_fired_at": r.last_fired_at,
            })
        self.store.set_schedules(out)
        self.store.save()

    def _arm(self, record: ScheduleRecord, base: datetime) -> None:
        record.next_fire_time = _next_fire(record.cron, base)

    # -------------------------------------------------- load
    def load_from_state(self) -> None:
        records: List[ScheduleRecord] = []
        for entry in self.store.get_schedules():
            try:
                rec = ScheduleRecord(
                    handle=entry["handle"],
                    cron=entry["cron"],
                    agent_kind=entry["agent_kind"],
                    spec=entry["spec"],
                    owner_user_id=entry["owner_user_id"],
                    created_at=entry.get("created_at", self.clock().isoformat()),
                    last_fired_at=entry.get("last_fired_at"),
                )
                # state_store.load already validates cron; double-check here
                # to defend against direct-set callers.
                if not croniter.is_valid(rec.cron):
                    self.log.warning(
                        "scheduler: dropping %s (invalid cron %r)",
                        rec.handle, rec.cron,
                    )
                    continue
                self._arm(rec, self.clock())
            except (KeyError, TypeError) as e:
                self.log.warning("scheduler: skipping malformed entry: %s", e)
                continue
            records.append(rec)
        self._records = records
        self._loaded = True

    # -------------------------------------------------- register
    async def register(
        self,
        cron_expr: str,
        agent_kind: str,
        spec: str,
        owner_user_id: str,
    ) -> str:
        """Returns the user-facing response string."""
        self._ensure_loaded()
        if agent_kind not in VALID_KINDS:
            return _kind_error(agent_kind)
        try:
            if not croniter.is_valid(cron_expr):
                raise CroniterBadCronError(f"invalid cron expression: {cron_expr}")
        except CroniterBadCronError as e:
            return f"invalid cron expression: {e}"
        except Exception as e:
            return f"invalid cron expression: {e}"

        handle = self.store.next_schedule_handle()
        now = self.clock()
        rec = ScheduleRecord(
            handle=handle,
            cron=cron_expr,
            agent_kind=agent_kind,
            spec=spec,
            owner_user_id=owner_user_id,
            created_at=now.isoformat(),
        )
        self._arm(rec, now)
        self._records.append(rec)
        self._persist()
        return (
            f"scheduled {handle}: cron={cron_expr} kind={agent_kind} "
            f"next-fire={rec.next_fire_time.isoformat()}"
        )

    # -------------------------------------------------- list
    def list_all(self) -> List[ScheduleRecord]:
        self._ensure_loaded()
        # ensure next_fire_time is fresh (could be None for newly loaded)
        for r in self._records:
            if r.next_fire_time is None:
                self._arm(r, self.clock())
        # sorted ascending by next_fire_time
        return sorted(self._records, key=lambda r: r.next_fire_time)

    # -------------------------------------------------- unregister
    def unregister(self, handle: str) -> bool:
        self._ensure_loaded()
        before = len(self._records)
        self._records = [r for r in self._records if r.handle != handle]
        removed = len(self._records) < before
        if removed:
            self._persist()
        return removed

    # -------------------------------------------------- tick (firing)
    async def tick(self, now: datetime) -> List[str]:
        """Fire any schedules whose next_fire_time <= now.

        Returns the list of handles that fired this tick. Re-arms each
        fired schedule for its next croniter time. Spawn failures are
        logged but do NOT remove the schedule (transient failures
        shouldn't be silently destructive).
        """
        self._ensure_loaded()
        fired: List[str] = []
        dirty = False
        for rec in self._records:
            if rec.next_fire_time is None:
                self._arm(rec, now)
            # Loop in case multiple fires are due (e.g., catching up after
            # a restart with several missed intervals). Per brief: each
            # fire spawns once; re-arm to next.
            while rec.next_fire_time is not None and rec.next_fire_time <= now:
                try:
                    await self.spawn(rec.agent_kind, rec.spec)
                    rec.last_fired_at = rec.next_fire_time.isoformat()
                except Exception as e:
                    # transient failure -> log, do not remove, do not record
                    # last_fired_at (so the next tick will retry the same fire
                    # only if we don't advance; we DO advance to next so we
                    # don't busy-loop on a permanently-broken spec)
                    self.log.warning(
                        "scheduler: spawn failed for %s: %s", rec.handle, e,
                    )
                fired.append(rec.handle)
                self._arm(rec, rec.next_fire_time)
                dirty = True
        if dirty:
            self._persist()
        return fired
