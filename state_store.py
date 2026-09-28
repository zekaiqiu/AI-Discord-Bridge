"""state_store — JSON-backed persistence.

Originally from Phase 1 of the prior build. This phase EXTENDS the schema
with two new top-level keys per the brief:
    "quotas": { "<canonical-agent-id>": {budget_tokens, set_at, set_by} }
    "pending_confirmations": { "<user_id>:<channel_id>": {action_name, action_payload, expires_at} }

On load, pending_confirmations entries whose expires_at < now() are dropped.
"""

from __future__ import annotations

import copy
import json
import os
import threading
from datetime import datetime
from typing import Any, Callable, Dict, Optional, Tuple

from _time import parse_iso, utcnow


_DEFAULT_SCHEMA: Dict[str, Any] = {
    # TODO: merge real prior-build keys here when prior modules are integrated.
    # "agents" is a placeholder so the schema has a recognisable shape during
    # this phase's stand-in period.
    "agents": {},
    "quotas": {},
    "pending_confirmations": {},
    # Phase 5 — automation
    "schedules": [],            # list of schedule records (see scheduler.py)
    "notify_rules": {},         # canonical-agent-id -> list of NotifyRule dicts
    "schedule_counter": 0,      # monotonic; persists across restart
}


class StateStore:
    """Thread-safe JSON state store.

    The dispatch path uses the async/threadpool style of the existing bot.py;
    here we expose synchronous get/set/save and let bot.py wrap them in
    asyncio.to_thread (matching the pattern the brief calls out).
    """

    def __init__(self, path: str, clock: Callable[[], datetime] = utcnow):
        self.path = path
        self._clock = clock
        self._lock = threading.RLock()
        self._data: Dict[str, Any] = {}
        self.load()

    # ---------- persistence ----------
    def load(self) -> None:
        with self._lock:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
            else:
                raw = {}
            # merge defaults so missing keys are present
            data: Dict[str, Any] = {}
            for k, v in _DEFAULT_SCHEMA.items():
                data[k] = raw.get(k, json.loads(json.dumps(v)))
            # carry forward any other prior-build keys
            for k, v in raw.items():
                if k not in data:
                    data[k] = v
            # drop expired pending_confirmations
            now = self._clock()
            pending = data.get("pending_confirmations", {})
            kept: Dict[str, Any] = {}
            for key, entry in pending.items():
                try:
                    expires = parse_iso(entry["expires_at"])
                except (KeyError, ValueError, TypeError):
                    continue  # malformed -> drop
                if expires >= now:
                    kept[key] = entry
            data["pending_confirmations"] = kept

            # Phase 5 — re-validate cron expressions on persisted schedules.
            # Bad expressions are dropped with a warning, NOT a crash.
            sched_in = data.get("schedules", []) or []
            sched_out = []
            for entry in sched_in:
                cron_expr = entry.get("cron", "")
                try:
                    # Local import keeps croniter optional for codepaths
                    # that don't touch schedules.
                    from croniter import croniter, CroniterBadCronError
                    if not croniter.is_valid(cron_expr):
                        raise CroniterBadCronError(f"invalid: {cron_expr!r}")
                except Exception as e:
                    import logging
                    logging.getLogger("state_store").warning(
                        "dropping schedule %s with invalid cron %r: %s",
                        entry.get("handle"), cron_expr, e,
                    )
                    continue
                sched_out.append(entry)
            data["schedules"] = sched_out

            self._data = data

    def save(self) -> None:
        with self._lock:
            tmp = self.path + ".tmp"
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, sort_keys=True)
            os.replace(tmp, self.path)

    # ---------- generic key access ----------
    def get(self, key: str, default: Any = None) -> Any:
        # Returns a deep copy for container values so callers cannot race
        # on the live internal dict reference (AR2 round-1 finding).
        with self._lock:
            if key not in self._data:
                return default
            return copy.deepcopy(self._data[key])

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value

    # ---------- quotas ----------
    def get_quotas(self) -> Dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data.get("quotas", {}))

    def set_quota(self, canonical_id: str, budget_tokens: int, set_by: str) -> None:
        with self._lock:
            quotas = self._data.setdefault("quotas", {})
            quotas[canonical_id] = {
                "budget_tokens": int(budget_tokens),
                "set_at": self._clock().isoformat(),
                "set_by": set_by,
            }

    def get_quota(self, canonical_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            entry = self._data.get("quotas", {}).get(canonical_id)
            return copy.deepcopy(entry) if entry is not None else None

    # ---------- pending confirmations ----------
    def get_pending(self, user_id: str, channel_id: str) -> Optional[Dict[str, Any]]:
        """Return the pending entry if it exists AND is unexpired; else None.

        Holds the lock for the entire check + copy so the returned dict
        cannot race with set_pending / clear_pending.
        """
        key = f"{user_id}:{channel_id}"
        with self._lock:
            entry = self._data.get("pending_confirmations", {}).get(key)
            if entry is None:
                return None
            try:
                expires = parse_iso(entry["expires_at"])
            except (KeyError, ValueError, TypeError):
                return None
            if expires < self._clock():
                return None
            return copy.deepcopy(entry)

    def get_pending_raw(
        self, user_id: str, channel_id: str
    ) -> Tuple[Optional[Dict[str, Any]], bool]:
        """Lock-safe accessor used by confirmations.confirm() to distinguish
        "never registered" from "expired".

        Returns (entry_copy_or_None, existed). `existed` is True iff a key
        for (user, channel) is present in the raw dict regardless of expiry;
        the entry copy is returned unfiltered so the caller can apply its
        own expiry check. Holds the RLock for the entire read+copy.
        """
        key = f"{user_id}:{channel_id}"
        with self._lock:
            pending = self._data.get("pending_confirmations", {})
            if key not in pending:
                return None, False
            return copy.deepcopy(pending[key]), True

    def set_pending(
        self,
        user_id: str,
        channel_id: str,
        action_name: str,
        action_payload: Any,
        expires_at: datetime,
    ) -> None:
        key = f"{user_id}:{channel_id}"
        with self._lock:
            pending = self._data.setdefault("pending_confirmations", {})
            pending[key] = {
                "action_name": action_name,
                "action_payload": action_payload,
                "expires_at": expires_at.isoformat(),
            }

    def clear_pending(self, user_id: str, channel_id: str) -> None:
        key = f"{user_id}:{channel_id}"
        with self._lock:
            pending = self._data.get("pending_confirmations", {})
            pending.pop(key, None)

    # ---------- schedules (Phase 5) ----------
    def get_schedules(self) -> list:
        with self._lock:
            return copy.deepcopy(self._data.get("schedules", []))

    def set_schedules(self, schedules: list) -> None:
        with self._lock:
            self._data["schedules"] = list(schedules)

    def next_schedule_handle(self) -> str:
        """Atomically increment the persistent counter and return the
        next sched-N handle. Counter persists across restart (per brief).
        """
        with self._lock:
            n = int(self._data.get("schedule_counter", 0)) + 1
            self._data["schedule_counter"] = n
            return f"sched-{n}"

    # ---------- notify rules (Phase 5) ----------
    def get_notify_rules(self) -> Dict[str, list]:
        with self._lock:
            return copy.deepcopy(self._data.get("notify_rules", {}))

    def set_notify_rules(self, rules: Dict[str, list]) -> None:
        with self._lock:
            self._data["notify_rules"] = dict(rules)
