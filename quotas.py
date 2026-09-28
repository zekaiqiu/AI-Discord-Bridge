"""Per-agent token quotas.

Reads consumption from usage.json:token_log[]. The HR6 "extra_usage" field
is intentionally NOT consulted in any decision path here — quota
enforcement uses ONLY plan-window / token_log data per the brief.
"""

# DO NOT read framework HR6 "extra_usage" here. Quota enforcement is plan-
# window only (see brief, "Quota enforcement uses ONLY plan-window /
# token_log data"). This comment is the only permitted reference to that
# string in this file.

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from _time import utcnow
from agent_state import PauseCause, request_pause
from handle_resolver import resolve as resolve_handle


class QuotaManager:
    def __init__(
        self,
        state_store,
        usage_path: str = "usage.json",
        clock: Callable[[], datetime] = utcnow,
        pause_hook: Callable[[str, PauseCause], bool] = request_pause,
    ):
        self.store = state_store
        self.usage_path = usage_path
        self.clock = clock
        self.pause_hook = pause_hook

    # ---------- !quota set ----------
    def set_budget(self, handle_or_id: str, budget_tokens: int, set_by: str) -> str:
        canonical = resolve_handle(handle_or_id)
        if canonical is None:
            return f"unknown agent: {handle_or_id}"
        if budget_tokens <= 0:
            return "budget must be a positive integer"
        self.store.set_quota(canonical, budget_tokens, set_by)
        self.store.save()
        return f"quota for {canonical} set to {budget_tokens} tokens"

    # ---------- !quota show ----------
    def show(self, handle_or_id: str) -> str:
        canonical = resolve_handle(handle_or_id)
        if canonical is None:
            return f"unknown agent: {handle_or_id}"
        quota = self.store.get_quota(canonical)
        consumed = self._consumed_tokens(canonical)
        if quota is None:
            return f"{canonical}: no quota set; consumed={consumed}"
        return (
            f"{canonical}: budget={quota['budget_tokens']} consumed={consumed}"
        )

    # ---------- consumption ----------
    def _consumed_tokens(self, canonical_id: str) -> int:
        if not os.path.exists(self.usage_path):
            return 0
        try:
            with open(self.usage_path, "r", encoding="utf-8") as f:
                usage = json.load(f)
        except (OSError, json.JSONDecodeError):
            return 0
        total = 0
        for entry in usage.get("token_log", []) or []:
            if entry.get("agent_id") == canonical_id:
                total += int(entry.get("tokens", 0))
        return total

    # ---------- enforcement ----------
    def check_and_pause(self, handle_or_id: str) -> Optional[bool]:
        """If the agent's consumption exceeds its budget, request a
        .quota_exceeded pause via the existing hook.

        Returns:
            None  -> no quota set or unknown agent
            False -> within budget (no action)
            True  -> over budget; pause hook invoked (return value of hook
                     determines whether the cause was applied or kept)
        """
        canonical = resolve_handle(handle_or_id)
        if canonical is None:
            return None
        quota = self.store.get_quota(canonical)
        if quota is None:
            return None
        consumed = self._consumed_tokens(canonical)
        if consumed > int(quota["budget_tokens"]):
            self.pause_hook(canonical, PauseCause.QUOTA_EXCEEDED)
            return True
        return False

    def check_all(self) -> Dict[str, bool]:
        """Run check_and_pause for every agent with a recorded quota."""
        out: Dict[str, bool] = {}
        for canonical in list(self.store.get_quotas().keys()):
            consumed = self._consumed_tokens(canonical)
            quota = self.store.get_quota(canonical)
            if quota is None:
                continue
            over = consumed > int(quota["budget_tokens"])
            if over:
                self.pause_hook(canonical, PauseCause.QUOTA_EXCEEDED)
            out[canonical] = over
        return out
