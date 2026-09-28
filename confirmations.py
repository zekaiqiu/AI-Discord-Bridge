"""Confirmation manager for destructive commands.

Implements the (user, channel) pending-action ledger and the !confirm
resolution path. TTL is fixed at 60 seconds per the brief. Clock is
injectable for tests (no real sleeping).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Optional, Tuple

from _time import parse_iso, utcnow


TTL_SECONDS = 60


class ConfirmationManager:
    """Manages pending destructive actions awaiting !confirm.

    Storage is delegated to the state_store so entries persist across
    restarts. Action handlers (callables that execute the destructive
    work when !confirm fires) are kept in-memory only — they're re-
    registered by the dispatch layer at startup.
    """

    def __init__(self, state_store, clock: Callable[[], datetime] = utcnow):
        self.store = state_store
        self.clock = clock
        # action_name -> async-or-sync handler taking (payload) -> result str
        self._handlers: Dict[str, Callable[[Any], Any]] = {}

    # ---------- handler registry ----------
    def register_action(self, action_name: str, handler: Callable[[Any], Any]) -> None:
        self._handlers[action_name] = handler

    # ---------- registration of pending action ----------
    def register_pending(
        self,
        user_id: str,
        channel_id: str,
        action_name: str,
        description: str,
        action_payload: Any = None,
    ) -> str:
        """Register a pending destructive action.

        Returns the user-facing prompt. If a prior pending action existed
        for this (user, channel), it is discarded and the returned prompt
        is prefixed with the exact discard notice the brief mandates.
        """
        prior = self.store.get_pending(user_id, channel_id)
        expires = self.clock() + timedelta(seconds=TTL_SECONDS)
        self.store.set_pending(
            user_id, channel_id, action_name, action_payload, expires
        )
        self.store.save()

        prompt = (
            f"{action_name} will {description}. Type !confirm within {TTL_SECONDS}s."
        )
        if prior is not None:
            prior_name = prior.get("action_name", "<unknown>")
            return (
                f"previous pending action {prior_name} discarded; "
                f"type !confirm to confirm {action_name}\n"
                + prompt
            )
        return prompt

    # ---------- prompt rendering (remaining seconds computed live) ----------
    def render_prompt(self, user_id: str, channel_id: str) -> Optional[str]:
        entry = self.store.get_pending(user_id, channel_id)
        if entry is None:
            return None
        expires = parse_iso(entry["expires_at"])
        remaining = int((expires - self.clock()).total_seconds())
        if remaining < 0:
            remaining = 0
        return (
            f"{entry['action_name']} pending. "
            f"Type !confirm within {remaining}s."
        )

    # ---------- !confirm ----------
    def confirm(self, user_id: str, channel_id: str) -> Tuple[bool, str]:
        """Resolve !confirm for (user, channel).

        Returns (executed, message). `executed` is True iff the registered
        handler ran. Errors (no-pending, expired) return (False, msg).

        Uses StateStore.get_pending_raw() — a lock-safe accessor that returns
        both an unfiltered entry copy and an `existed` flag — so we can
        distinguish "never registered" from "expired" without holding a
        live reference to the internal pending dict (AR2 round-1 fix).
        """
        entry, existed = self.store.get_pending_raw(user_id, channel_id)
        if not existed:
            return False, "no pending action to confirm"

        # entry is a deep copy under lock; safe to read without further sync.
        try:
            expires = parse_iso(entry["expires_at"])
        except (KeyError, ValueError, TypeError):
            self.store.clear_pending(user_id, channel_id)
            self.store.save()
            return False, "no pending action to confirm"

        if expires < self.clock():
            # expired — drop and report
            self.store.clear_pending(user_id, channel_id)
            self.store.save()
            return False, f"pending action {entry['action_name']} expired"

        # consume the pending entry whether or not a handler is registered
        self.store.clear_pending(user_id, channel_id)
        self.store.save()

        handler = self._handlers.get(entry["action_name"])
        if handler is None:
            return False, (
                f"no handler registered for action {entry['action_name']}"
            )

        result = handler(entry.get("action_payload"))
        msg = result if isinstance(result, str) else f"{entry['action_name']} executed"
        return True, msg
