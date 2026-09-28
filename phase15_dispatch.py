"""Bot dispatch — minimal stand-in for the prior-build dispatcher.

See agent_state.py header for the workspace-emptiness note.

Phase 1 registered !confirm, !quota set, !quota show under section
"QUOTAS & CONFIRMATIONS". Phase 2 adds !killall, !pauseall, !resumeall
under section "FLEET" via register_phase2_commands().

Inline-default invariant (non-!-prefixed messages route to the bridge chat
and never spawn workers / never trigger admin paths) is enforced in
dispatch() — see the early return below.
"""

from __future__ import annotations

import asyncio
import shlex
from typing import Any, Optional

import help_registry
from confirmations import ConfirmationManager
# fleet skipped — agent-runtime module not integrated
VALID_RESUME_CAUSES = ()  # placeholder (used only by !resumeall path which returns "not configured")
from quotas import QuotaManager


SECTION = "QUOTAS & CONFIRMATIONS"
FLEET_SECTION = "FLEET"
OBS_SECTION = "OBSERVABILITY"
ARTIFACTS_SECTION = "ARTIFACTS"
AUTOMATION_SECTION = "AUTOMATION"


_CONFIRM_DOC = (
    "!confirm — confirm the most recent destructive command you issued in "
    "this channel. Pending actions expire after 60 seconds."
)
# One-line docs per command. help_registry.render_help() shows only the
# first line of each doc; splitting !quota set / !quota show into separate
# strings ensures both lines are visible in the full !help dump.
_QUOTA_SET_DOC = (
    "!quota set <id> <token-budget> — set a per-agent token budget."
)
_QUOTA_SHOW_DOC = (
    "!quota show <id> — show the budget and current consumption for an agent."
)
# Combined doc returned by `!help quota` (two-line response is fine here
# because help_for() returns the full doc string, not just the first line).
_QUOTA_DOC = _QUOTA_SET_DOC + "\n" + _QUOTA_SHOW_DOC


# Phase 2 — FLEET docs. Each one-line summary is the first line so
# render_help() shows them cleanly; the per-command !help <cmd> returns
# the full multi-line doc.
_KILLALL_DOC = (
    "!killall — SIGTERM every running agent. Requires confirmation: "
    "the bot will register a pending action and you must follow up with "
    "!confirm within 60 seconds."
)
_PAUSEALL_DOC = (
    "!pauseall [reason] — gracefully pause every running agent under "
    ".user_paused. Optional <reason> is recorded in each agent's pause "
    "snapshot; defaults to \"pauseall (no reason given)\"."
)
_RESUMEALL_DOC = (
    "!resumeall <cause> — resume agents whose effective pause cause "
    "equals <cause>. Valid causes: "
    + ", ".join(VALID_RESUME_CAUSES) + "."
)


# Phase 3 — OBSERVABILITY docs.
_USAGE_DOC = (
    "!usage — show 5-hour and weekly token bucket consumption (with %% "
    "and reset times) plus current rate limits if known. "
    "Example: "
)
_COST_DOC = (
    "!cost [day|week|month] — show per-agent USD spend over the period. "
    "Default period is . Valid periods: day, week, month. "
    "Example: "
)
_LOGS_DOC = (
    "!logs <id> [n] — tail the last <n> journalctl lines for an agent. "
    "Default n=50; runs with a 10s timeout. "
    "Example: "
)
_HEALTH_DOC = (
    "!health — system health: disk, memory, load, GPU, agent unit "
    "status, and Anthropic API reachability. Returns within 5s even if "
    "the network is slow. Example: "
)
_CONTEXT_DOC = (
    "!context <id> — show an agent's context size and pause-snapshot "
    "summary (if any). Falls back to a token-log estimate marked "
    " when the snapshot lacks the field. Example: "
    ""
)


def register_phase3_commands(observability) -> None:
    """Register Phase-3 observability commands with the !help registry."""
    help_registry.register(OBS_SECTION, "!usage", _USAGE_DOC)
    help_registry.register(OBS_SECTION, "!cost", _COST_DOC)
    help_registry.register(OBS_SECTION, "!logs", _LOGS_DOC)
    help_registry.register(OBS_SECTION, "!health", _HEALTH_DOC)
    help_registry.register(OBS_SECTION, "!context", _CONTEXT_DOC)
    # bare aliases so !help <cmd> returns the doc
    help_registry.register(OBS_SECTION, "usage", _USAGE_DOC)
    help_registry.register(OBS_SECTION, "cost", _COST_DOC)
    help_registry.register(OBS_SECTION, "logs", _LOGS_DOC)
    help_registry.register(OBS_SECTION, "health", _HEALTH_DOC)
    help_registry.register(OBS_SECTION, "context", _CONTEXT_DOC)


# Phase 4 — ARTIFACTS docs.
_ARTIFACTS_DOC = (
    "!artifacts <id> — list files in an agent's workspace, sorted by "
    "mtime (newest first). Excludes .git/__pycache__/node_modules/.venv "
    "and .pyc files. Example: `!artifacts p1`"
)
_GRAB_DOC = (
    "!grab <id> <path> — fetch a single file from the agent's workspace. "
    "Files up to 8 MiB DM cap are sent inline; larger files use the "
    "paste-link uploader fallback. Example: `!grab p1 src/main.py`"
)
_DIFF_DOC = (
    "!diff <id> — show the agent workspace's git diff (unstaged + staged). "
    "Does NOT auto-init: a non-repo workspace returns "
    "\"not a git repo \u2014 `!diff` requires the workspace to be initialized "
    "as a git repo.\" Example: `!diff p1`"
)


def register_phase4_commands(artifacts) -> None:
    """Register Phase-4 artifact commands with the !help registry."""
    help_registry.register(ARTIFACTS_SECTION, "!artifacts", _ARTIFACTS_DOC)
    help_registry.register(ARTIFACTS_SECTION, "!grab", _GRAB_DOC)
    help_registry.register(ARTIFACTS_SECTION, "!diff", _DIFF_DOC)
    # bare aliases so !help <cmd> returns the doc
    help_registry.register(ARTIFACTS_SECTION, "artifacts", _ARTIFACTS_DOC)
    help_registry.register(ARTIFACTS_SECTION, "grab", _GRAB_DOC)
    help_registry.register(ARTIFACTS_SECTION, "diff", _DIFF_DOC)


# Phase 5 — AUTOMATION docs.
_SCHEDULE_DOC = (
    "!schedule <cron-expr> <kind> <spec...> — schedule an agent to spawn "
    "on a cron. <kind> is task or project. v1 accepts text-only specs; "
    "attachments are not serialized. Example: "
    "`!schedule \"0 9 * * 1\" task weekly review`"
)
_SCHEDULES_DOC = (
    "!schedules — list active schedules sorted by next-fire ascending. "
    "Example: `!schedules`"
)
_UNSCHEDULE_DOC = (
    "!unschedule <handle> — remove a schedule by its sched-N handle. "
    "Example: `!unschedule sched-3`"
)
_NOTIFY_DOC = (
    "!notify <id> on:<event> — DM you when the agent emits <event>. "
    "Valid events: complete, fail, rate_limited, quota_exceeded. "
    "Example: `!notify p1 on:complete`"
)
_HANDOFF_DOC = (
    "!handoff <from-id> <to-id> — task->task session transfer. v1 "
    "supports task->task only; rejects projects with "
    "\"v1 supports task->task only.\" Source is NOT terminated. "
    "Example: `!handoff t1 t2`"
)


def register_phase5_commands(scheduler=None, notifications=None, handoff=None) -> None:
    """Register Phase-5 automation commands with the !help registry."""
    help_registry.register(AUTOMATION_SECTION, "!schedule", _SCHEDULE_DOC)
    help_registry.register(AUTOMATION_SECTION, "!schedules", _SCHEDULES_DOC)
    help_registry.register(AUTOMATION_SECTION, "!unschedule", _UNSCHEDULE_DOC)
    help_registry.register(AUTOMATION_SECTION, "!notify", _NOTIFY_DOC)
    help_registry.register(AUTOMATION_SECTION, "!handoff", _HANDOFF_DOC)
    # bare aliases for !help <cmd>
    help_registry.register(AUTOMATION_SECTION, "schedule", _SCHEDULE_DOC)
    help_registry.register(AUTOMATION_SECTION, "schedules", _SCHEDULES_DOC)
    help_registry.register(AUTOMATION_SECTION, "unschedule", _UNSCHEDULE_DOC)
    help_registry.register(AUTOMATION_SECTION, "notify", _NOTIFY_DOC)
    help_registry.register(AUTOMATION_SECTION, "handoff", _HANDOFF_DOC)


def register_phase1_commands(
    confirm_mgr: ConfirmationManager,
    quota_mgr: QuotaManager,
) -> None:
    """Register Phase-1 commands with the !help registry."""
    help_registry.register(SECTION, "!confirm", _CONFIRM_DOC)
    help_registry.register(SECTION, "!quota set", _QUOTA_SET_DOC)
    help_registry.register(SECTION, "!quota show", _QUOTA_SHOW_DOC)
    # also expose under bare "confirm" / "quota" so !help confirm and
    # !help quota return non-empty doc strings
    help_registry.register(SECTION, "confirm", _CONFIRM_DOC)
    help_registry.register(SECTION, "quota", _QUOTA_DOC)


def register_phase2_commands(fleet: Any) -> None:
    """Register Phase-2 fleet commands with the !help registry under FLEET."""
    help_registry.register(FLEET_SECTION, "!killall", _KILLALL_DOC)
    help_registry.register(FLEET_SECTION, "!pauseall", _PAUSEALL_DOC)
    help_registry.register(FLEET_SECTION, "!resumeall", _RESUMEALL_DOC)
    # bare aliases so `!help killall|pauseall|resumeall` return docs
    help_registry.register(FLEET_SECTION, "killall", _KILLALL_DOC)
    help_registry.register(FLEET_SECTION, "pauseall", _PAUSEALL_DOC)
    help_registry.register(FLEET_SECTION, "resumeall", _RESUMEALL_DOC)


def _run(coro):
    """Run an async handler from the sync dispatch path.

    Phase-2 fleet handlers are async; the Phase-1 dispatcher is sync.
    asyncio.run() is the standard bridge.

    TODO: this raises RuntimeError("This event loop is already running")
    if dispatch() is ever called from a thread that already has a running
    event loop (e.g., an async test harness, or once the bot framework
    moves to a true async loop). The migration path is to convert
    Dispatcher.dispatch to  and  fleet handlers
    directly; at that point this helper and the asyncio import can be
    deleted. Filed against a later phase — do not paper over with
    nest_asyncio or run_until_complete tricks.
    """
    return asyncio.run(coro)


class Dispatcher:
    """Routes incoming messages to handlers.

    Non-!-prefixed messages return None from dispatch() — they go to the
    bridge chat session and never reach admin handlers. This is the
    inline-default invariant the brief asks PREFLIGHT.md to verify.
    """

    def __init__(
        self,
        confirm_mgr: ConfirmationManager,
        quota_mgr: QuotaManager,
        fleet: Optional[Any] = None,
        observability=None,
        artifacts=None,
        scheduler=None,
        notifications=None,
        handoff=None,
    ):
        self.confirm = confirm_mgr
        self.quota = quota_mgr
        self.fleet = fleet
        self.observability = observability
        self.artifacts = artifacts
        self.scheduler = scheduler
        self.notifications = notifications
        self.handoff = handoff

    def dispatch(
        self, text: str, user_id: str, channel_id: str
    ) -> Optional[str]:
        # INLINE-DEFAULT INVARIANT: non-!-prefixed messages do not route here.
        if not text.startswith("!"):
            return None

        try:
            parts = shlex.split(text)
        except ValueError:
            parts = text.split()
        if not parts:
            return None
        cmd = parts[0]
        args = parts[1:]

        if cmd == "!confirm":
            _, msg = self.confirm.confirm(user_id, channel_id)
            return msg

        if cmd == "!quota":
            if len(args) >= 1 and args[0] == "set" and len(args) == 3:
                try:
                    budget = int(args[2])
                except ValueError:
                    return "usage: !quota set <id> <token-budget>"
                return self.quota.set_budget(args[1], budget, user_id)
            if len(args) == 2 and args[0] == "show":
                return self.quota.show(args[1])
            return (
                "usage: !quota set <id> <token-budget>  |  "
                "!quota show <id>"
            )

        if cmd == "!killall":
            if self.fleet is None:
                return "fleet ops not configured"
            return _run(self.fleet.handle_killall(user_id, channel_id))

        if cmd == "!pauseall":
            if self.fleet is None:
                return "fleet ops not configured"
            reason = " ".join(args) if args else None
            return _run(self.fleet.handle_pauseall(user_id, channel_id, reason))

        if cmd == "!resumeall":
            if self.fleet is None:
                return "fleet ops not configured"
            if len(args) != 1:
                return "usage: !resumeall <cause>"
            return _run(self.fleet.handle_resumeall(user_id, channel_id, args[0]))

        if cmd == "!usage":
            # !usage is a built-in: it shells out to ccusage to read local
            # Claude Code transcripts and produce a numeric report. It does
            # NOT require the (currently unconfigured) observability module.
            try:
                from usage_report import format_usage_report
                return format_usage_report()
            except Exception as exc:  # pragma: no cover - defensive
                return f"!usage failed: {exc}"

        if cmd in ("!cost", "!logs", "!health", "!context"):
            if self.observability is None:
                return "observability not configured"
            if cmd == "!cost":
                period = args[0] if args else "day"
                return _run(self.observability.handle_cost(period))
            if cmd == "!logs":
                if len(args) < 1:
                    return "usage: !logs <id> [n]"
                aid = args[0]
                try:
                    n = int(args[1]) if len(args) >= 2 else 50
                except ValueError:
                    return "usage: !logs <id> [n]"
                return _run(self.observability.handle_logs(aid, n))
            if cmd == "!health":
                return _run(self.observability.handle_health())
            if cmd == "!context":
                if len(args) != 1:
                    return "usage: !context <id>"
                return _run(self.observability.handle_context(args[0]))

        if cmd in ("!artifacts", "!grab", "!diff"):
            if self.artifacts is None:
                return "artifacts not configured"
            if cmd == "!artifacts":
                if len(args) != 1:
                    return "usage: !artifacts <id>"
                return _run(self.artifacts.handle_artifacts(args[0]))
            if cmd == "!grab":
                if len(args) != 2:
                    return "usage: !grab <id> <path>"
                return _run(self.artifacts.handle_grab(args[0], args[1]))
            if cmd == "!diff":
                if len(args) != 1:
                    return "usage: !diff <id>"
                return _run(self.artifacts.handle_diff(args[0]))

        if cmd == "!schedule":
            if self.scheduler is None:
                return "scheduler not configured"
            # attachment rejection (Phase 5 brief): the dispatcher is the
            # boundary. The presence of an attachment is signalled out-of-
            # band by the bot framework; absent that signal, we accept
            # text. If the dispatcher were extended to detect attachments,
            # it would return ATTACHMENT_REJECT_MSG here.
            if len(args) < 3:
                return "usage: !schedule <cron-expr> <kind> <spec...>"
            cron_expr = args[0]
            kind = args[1]
            spec = " ".join(args[2:])
            return _run(self.scheduler.register(cron_expr, kind, spec, user_id))

        if cmd == "!schedules":
            if self.scheduler is None:
                return "scheduler not configured"
            recs = self.scheduler.list_all()
            if not recs:
                return "no schedules registered."
            lines = []
            for r in recs:
                spec_disp = r.spec if len(r.spec) <= 60 else r.spec[:60] + "…"
                nf = r.next_fire_time.isoformat() if r.next_fire_time else "<unarmed>"
                lines.append(
                    f"{r.handle}  cron={r.cron}  kind={r.agent_kind}  "
                    f"next-fire={nf}  spec={spec_disp}"
                )
            return "\n".join(lines)

        if cmd == "!unschedule":
            if self.scheduler is None:
                return "scheduler not configured"
            if len(args) != 1:
                return "usage: !unschedule <handle>"
            handle = args[0]
            if self.scheduler.unregister(handle):
                return f"unscheduled {handle}."
            return f"unknown schedule \'{handle}\'."

        if cmd == "!notify":
            if self.notifications is None:
                return "notifications not configured"
            if len(args) != 2 or not args[1].startswith("on:"):
                return "usage: !notify <id> on:<event>"
            agent_id = args[0]
            event_name = args[1][len("on:"):]
            return self.notifications.register(agent_id, event_name, user_id)

        if cmd == "!handoff":
            if self.handoff is None:
                return "handoff not configured"
            if len(args) != 2:
                return "usage: !handoff <from-id> <to-id>"
            return _run(self.handoff.transfer(args[0], args[1]))

        if cmd == "!help":
            if len(args) == 0:
                return help_registry.render_help()
            doc = help_registry.help_for(args[0])
            return doc or f"no help for {args[0]}"

        return None  # unknown command — let upstream fall through
