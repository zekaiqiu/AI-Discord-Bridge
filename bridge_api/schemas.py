"""Pydantic schemas for the bridge ops API.

These are the wire types — keep them stable across releases. Fields that
are missing from a particular agent kind (e.g. ``phase`` on a task) are
typed as ``Optional`` so a single ``Agent`` model covers both kinds.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


# ---- agents -----------------------------------------------------------------

class Agent(BaseModel):
    id: str
    kind: str  # "task" | "project"
    handle: Optional[str] = None
    status: str
    description: Optional[str] = None
    created_at: Optional[str] = None  # ISO 8601
    working_dir: Optional[str] = None
    last_artifact: Optional[str] = None  # for projects, last phase artifact path


class AgentDetail(Agent):
    log_tail: Optional[list[str]] = None
    phase: Optional[int] = None
    total_phases: Optional[int] = None
    pause_cause: Optional[str] = None


class AgentList(BaseModel):
    active: list[Agent] = Field(default_factory=list)
    archived: list[Agent] = Field(default_factory=list)


# ---- create payloads --------------------------------------------------------

class TaskCreatePayload(BaseModel):
    prompt: str = Field(..., min_length=1)
    verbosity: Optional[str] = None  # quiet|normal|verbose|firehose


class ProjectCreatePayload(BaseModel):
    brief: str = Field(..., min_length=1)


class VerbosePayload(BaseModel):
    level: str  # quiet|normal|verbose|firehose


class AgentCreated(BaseModel):
    id: str
    handle: Optional[str] = None
    status: str


class AgentMutated(BaseModel):
    id: str
    status: str


# ---- usage ------------------------------------------------------------------

class UsageWindow(BaseModel):
    key: str
    label: str
    utilization: float
    resets_at: Optional[str] = None
    resets_in_minutes: Optional[float] = None


class ExtraUsage(BaseModel):
    used_credits: float
    monthly_limit: float
    currency: str


class AccountUsage(BaseModel):
    name: str
    is_active: bool
    available: bool
    error: Optional[str] = None
    windows: list[UsageWindow] = Field(default_factory=list)
    extra_usage: Optional[ExtraUsage] = None


class TodayModelCost(BaseModel):
    model: str
    is_known_pricing: bool
    cost_usd: float
    input_cost_usd: float
    output_cost_usd: float
    cache_write_cost_usd: float
    cache_read_cost_usd: float


class TodayUsage(BaseModel):
    date: Optional[str] = None
    total_cost_usd: float = 0.0
    models: list[TodayModelCost] = Field(default_factory=list)


class ActiveBlockUsage(BaseModel):
    start_time: str
    end_time: str
    remaining_minutes: float
    cost_usd_so_far: float
    tokens_per_minute: Optional[float] = None
    cost_per_hour: Optional[float] = None
    projected_cost_usd: Optional[float] = None
    projected_tokens: Optional[int] = None


class RollingUsage(BaseModel):
    days: int = 5
    cost_usd: float = 0.0
    tokens: int = 0


class UsagePayload(BaseModel):
    accounts: list[AccountUsage]
    today_utc: TodayUsage
    active_block: Optional[ActiveBlockUsage] = None
    rolling_5d: RollingUsage


__all__ = [
    "AccountUsage",
    "ActiveBlockUsage",
    "Agent",
    "AgentCreated",
    "AgentDetail",
    "AgentList",
    "AgentMutated",
    "ExtraUsage",
    "ProjectCreatePayload",
    "RollingUsage",
    "TaskCreatePayload",
    "TodayModelCost",
    "TodayUsage",
    "UsagePayload",
    "UsageWindow",
    "VerbosePayload",
]
