"""Read-only view of the Multi-Agent-Framework pipeline state.

The orchestrator runs as a host-side systemd unit and stores per-project
state under ``/home/felix/multi-agent-pipeline/projects/<id>/``. Because
the spend container bind-mounts ``/home/felix:ro``, this module reaches
that state at the same absolute paths the orchestrator writes to.

Surface:
  list_projects()       — recent projects, mtime-sorted, with a single-line
                          summary (active phase + step + last activity).
  project_detail(id)    — full state for one project: phase tree, per-role
                          token totals, cost estimate, recent events,
                          recent invocations from token_log.
  list_transcripts(id)  — which roles have a session JSONL on disk + size.
  read_transcript(id, role) — parsed message list for one role's session.
  active_project_id()   — what active_project.txt points at (None if no
                          run is in progress).

Pricing table is local — the framework's records carry the model name on
each invoke, so we don't need to call out to Anthropic. Numbers are
rounded to cents and presented as "best-effort estimate", not billing
truth.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("spend.agents")

# Root of the pipeline state. Containerised path equals host path
# because /home/felix is bind-mounted read-only.
PIPELINE_ROOT = Path(
    os.environ.get("PIPELINE_ROOT", "/home/felix/multi-agent-pipeline")
)

# The orchestrator's PhaseStep.value list, in canonical execution order.
# Hardcoded to avoid importing the framework (it has runtime deps not
# present in this container). Kept in lockstep manually — if a step is
# added in orchestrator.py, mirror it here.
PHASE_STEPS: tuple[str, ...] = (
    "briefing",
    "coding",
    "ar1",
    "ar2_round",
    "hygiene",
    "hygiene_apply",
    "ar2_final",
    "report",
    "done",
)

# USD per 1M tokens. Approximate — for display only. Source: Anthropic
# public pricing as of 2026-05; cache_creation = input rate, cache_read
# = 10% of input rate (per Anthropic's prompt-caching docs).
_PRICING: dict[str, dict[str, float]] = {
    "claude-opus-5-5": {"in": 4.0, "out": 20.0, "cw": 5.0, "cr": 0.20},
    "claude-opus-4-8": {"in": 5.0, "out": 25.0, "cw": 6.25, "cr": 0.50},
    "claude-opus-4-7": {"in": 15.0, "out": 75.0, "cw": 18.75, "cr": 1.50},
    "claude-opus-4-6": {"in": 15.0, "out": 75.0, "cw": 18.75, "cr": 1.50},
    "claude-opus-4-5": {"in": 15.0, "out": 75.0, "cw": 18.75, "cr": 1.50},
    "claude-sonnet-4-6": {"in": 3.0, "out": 15.0, "cw": 3.75, "cr": 0.30},
    "claude-sonnet-4-5": {"in": 3.0, "out": 15.0, "cw": 3.75, "cr": 0.30},
    "claude-haiku-4-5": {"in": 1.0, "out": 5.0, "cw": 1.25, "cr": 0.10},
}
_FALLBACK_PRICE = {"in": 3.0, "out": 15.0, "cw": 3.75, "cr": 0.30}


def _price_for(model: str) -> dict[str, float]:
    if not model:
        return _FALLBACK_PRICE
    # Strip any -YYYYMMDD or version suffix Anthropic appends to API model
    # ids; pricing buckets are keyed off the family.
    for key, price in _PRICING.items():
        if model.startswith(key):
            return price
    return _FALLBACK_PRICE


def _cost_for_record(rec: dict[str, Any]) -> float:
    p = _price_for(str(rec.get("model", "")))
    in_tok = int(rec.get("input_tokens", 0))
    out_tok = int(rec.get("output_tokens", 0))
    cw_tok = int(rec.get("cache_creation_input_tokens", 0))
    cr_tok = int(rec.get("cache_read_input_tokens", 0))
    return (
        in_tok * p["in"]
        + out_tok * p["out"]
        + cw_tok * p["cw"]
        + cr_tok * p["cr"]
    ) / 1_000_000


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _read_jsonl_tail(path: Path, limit: int) -> list[dict]:
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[dict] = []
    for ln in text.splitlines()[-limit:]:
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def active_project_id() -> str | None:
    p = PIPELINE_ROOT / "active_project.txt"
    if not p.exists():
        return None
    try:
        s = p.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return s or None


def _projects_dir() -> Path:
    return PIPELINE_ROOT / "projects"


def list_projects(limit: int = 10) -> list[dict[str, Any]]:
    """Recent projects, newest first. Each entry carries enough for the
    list view; the detail endpoint fills in the rest."""
    pdir = _projects_dir()
    if not pdir.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for entry in pdir.iterdir():
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        usage = _read_json(entry / "usage.json") or {}
        token_log = usage.get("token_log") or []
        last_event = (entry / "events.jsonl")
        last_event_ts = last_event.stat().st_mtime if last_event.exists() else 0.0
        last_log_ts = max((float(r.get("ts", 0)) for r in token_log), default=0.0)
        last_activity = max(last_event_ts, last_log_ts, entry.stat().st_mtime)
        pending = _read_json(entry / "pending_invocation.json") or {}
        cost = sum(_cost_for_record(r) for r in token_log)
        rows.append(
            {
                "id": entry.name,
                "last_activity": last_activity,
                "phase": pending.get("phase"),
                "step": pending.get("step"),
                "active_role": pending.get("role"),
                "calls": len(token_log),
                "cost_usd": round(cost, 2),
                "is_paused": (entry / ".user_paused").exists(),
                "is_rate_limited": (entry / ".rate_limited").exists(),
                "has_orchestrator_pid": (entry / ".orchestrator.pid").exists(),
            }
        )
    rows.sort(key=lambda r: r["last_activity"], reverse=True)
    return rows[:limit]


def _phase_completion(project_dir: Path) -> dict[int, dict[str, bool]]:
    """Walk phases/phase_<n>/ and return per-phase artifact presence.

    Used to mark the static phase-step tree green for completed steps
    on a project's detail view.
    """
    out: dict[int, dict[str, bool]] = {}
    phases_dir = project_dir / "phases"
    if not phases_dir.is_dir():
        return out
    for entry in sorted(phases_dir.iterdir()):
        if not entry.is_dir() or not entry.name.startswith("phase_"):
            continue
        try:
            n = int(entry.name.split("_", 1)[1])
        except (ValueError, IndexError):
            continue
        out[n] = {
            "brief": (entry / "brief.md").exists(),
            "coding_1": (entry / "coder_1_submission" / "submission.md").exists(),
            "coding_2": (entry / "coder_2_submission" / "submission.md").exists(),
            "ar1": (entry / "ar1_verdict.md").exists(),
            "report": (entry / "phase_report.md").exists(),
        }
    return out


def project_detail(project_id: str) -> dict[str, Any] | None:
    pdir = _projects_dir() / project_id
    # Validate to prevent path traversal — only accept the project id
    # format the framework's paths.py already enforces.
    import re
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$", project_id):
        return None
    if not pdir.is_dir():
        return None
    usage = _read_json(pdir / "usage.json") or {}
    token_log = list(usage.get("token_log") or [])
    pending = _read_json(pdir / "pending_invocation.json") or {}

    role_totals: dict[str, dict[str, Any]] = {}
    for r in token_log:
        role = str(r.get("agent_role", ""))
        b = role_totals.setdefault(
            role,
            {
                "calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "cost_usd": 0.0,
                "models": set(),
            },
        )
        b["calls"] += 1
        b["input_tokens"] += int(r.get("input_tokens", 0))
        b["output_tokens"] += int(r.get("output_tokens", 0))
        b["cache_creation_input_tokens"] += int(r.get("cache_creation_input_tokens", 0))
        b["cache_read_input_tokens"] += int(r.get("cache_read_input_tokens", 0))
        b["cost_usd"] += _cost_for_record(r)
        if r.get("model"):
            b["models"].add(str(r["model"]))
    # Render-friendly: stringify the models set, round cost.
    role_totals_out: list[dict[str, Any]] = []
    for role, b in sorted(role_totals.items()):
        role_totals_out.append(
            {
                "role": role,
                "calls": b["calls"],
                "input_tokens": b["input_tokens"],
                "output_tokens": b["output_tokens"],
                "cache_creation_input_tokens": b["cache_creation_input_tokens"],
                "cache_read_input_tokens": b["cache_read_input_tokens"],
                "cost_usd": round(b["cost_usd"], 4),
                "models": sorted(b["models"]),
            }
        )

    cost_by_phase: dict[int | None, float] = {}
    for r in token_log:
        ph = r.get("phase")
        cost_by_phase[ph] = cost_by_phase.get(ph, 0.0) + _cost_for_record(r)
    cost_by_phase_out = [
        {"phase": k, "cost_usd": round(v, 4)}
        for k, v in sorted(
            cost_by_phase.items(),
            key=lambda kv: (kv[0] is None, kv[0]),
        )
    ]

    events = _read_jsonl_tail(pdir / "events.jsonl", limit=200)
    recent_invokes = token_log[-20:][::-1]

    initial_brief = ""
    bp = pdir / "INITIAL_BRIEF.md"
    if bp.exists():
        try:
            initial_brief = bp.read_text(encoding="utf-8")[:4000]
        except OSError:
            pass

    return {
        "id": project_id,
        "pending": pending,
        "is_paused": (pdir / ".user_paused").exists(),
        "is_rate_limited": (pdir / ".rate_limited").exists(),
        "has_orchestrator_pid": (pdir / ".orchestrator.pid").exists(),
        "initial_brief": initial_brief,
        "phase_completion": _phase_completion(pdir),
        "role_totals": role_totals_out,
        "cost_by_phase": cost_by_phase_out,
        "total_cost_usd": round(sum(c["cost_usd"] for c in cost_by_phase_out), 4),
        "events": events,
        "recent_invokes": recent_invokes,
        "phase_steps": list(PHASE_STEPS),
        "transcripts": list_transcripts(project_id),
    }


# ---------------------------------------------------------------------------
# Transcripts.
#
# Each pipeline agent has a session JSONL at sessions/<role>.jsonl with one
# message per line — {"role": "user"|"assistant", "content": str}. The list
# helper is cheap (stat + line count); the read helper is per-role and only
# called when the user expands a particular agent's transcript, so the
# detail page stays small even for projects with megabytes of messages.
# ---------------------------------------------------------------------------

# Roles whose session files are produced by the pipeline orchestrator. Mirror
# of pipeline.session.VALID_ROLES; ordering is what we want in the UI tab
# list. "assistant" is an external Discord-side conversational role and is
# not relevant to the per-project pipeline view, so it's omitted here.
PIPELINE_ROLES: tuple[str, ...] = (
    "lead_engineer",
    "coder_1",
    "coder_2",
    "accuracy_reviewer_1",
    "accuracy_reviewer_2",
    "hygiene_reviewer",
    "test_user",
)

_SAFE_ROLE_RE = __import__("re").compile(r"^[A-Za-z0-9_]{1,64}$")


def list_transcripts(project_id: str) -> list[dict[str, Any]]:
    """Return per-role transcript metadata (which roles have files, sizes).

    Empty list if the project doesn't exist or has no sessions/ dir yet.
    """
    import re
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$", project_id):
        return []
    sdir = _projects_dir() / project_id / "sessions"
    if not sdir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for role in PIPELINE_ROLES:
        f = sdir / f"{role}.jsonl"
        if not f.exists():
            continue
        try:
            stat = f.stat()
        except OSError:
            continue
        # Cheap line count — read once, count newlines. Files are bounded
        # by the orchestrator's per-project session retention.
        try:
            with open(f, "rb") as fh:
                msg_count = sum(1 for _ in fh)
        except OSError:
            msg_count = 0
        out.append(
            {
                "role": role,
                "messages": msg_count,
                "size_bytes": stat.st_size,
                "mtime": stat.st_mtime,
            }
        )
    return out


def read_transcript(
    project_id: str,
    role: str,
    *,
    limit: int = 500,
    truncate_chars: int = 20_000,
) -> dict[str, Any] | None:
    """Return parsed messages for one role's session JSONL, newest last.

    Caps the returned list at `limit` messages (oldest dropped first) and
    truncates each message body at `truncate_chars` so a runaway-long
    coder turn doesn't ship 5 MiB to the browser.
    """
    import re
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$", project_id):
        return None
    if not _SAFE_ROLE_RE.match(role) or role not in PIPELINE_ROLES:
        return None
    f = _projects_dir() / project_id / "sessions" / f"{role}.jsonl"
    if not f.exists():
        return None
    messages: list[dict[str, Any]] = []
    try:
        with open(f, encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rec = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                content = rec.get("content")
                truncated = False
                if isinstance(content, str) and len(content) > truncate_chars:
                    content = content[:truncate_chars]
                    truncated = True
                messages.append(
                    {
                        "role": rec.get("role", "unknown"),
                        "content": content,
                        "truncated": truncated,
                    }
                )
    except OSError:
        return None
    omitted = max(0, len(messages) - limit)
    if omitted:
        messages = messages[-limit:]
    return {
        "project_id": project_id,
        "role": role,
        "messages": messages,
        "omitted_oldest": omitted,
        "truncate_chars": truncate_chars,
    }


def overview() -> dict[str, Any]:
    return {
        "active_project_id": active_project_id(),
        "projects": list_projects(),
        "phase_steps": list(PHASE_STEPS),
    }
