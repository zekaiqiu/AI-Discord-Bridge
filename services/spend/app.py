"""portfolio-spend — unified spend dashboard at cost.ald3.com.

v0 surface:
  GET /healthz                  — liveness; no auth.
  GET /api/anthropic/usage      — JSON, structured Anthropic usage payload
                                  (per-account quota + today/yesterday cost
                                  + active 5h block + 5d rolling).
  GET /api/aws/cost             — JSON, current-month + month-to-date AWS
                                  cost from Cost Explorer. 503 if no creds.
  GET /api/agents/overview      — JSON, recent pipeline projects + active.
  GET /api/agents/project/{id}  — JSON, full state for one pipeline project.
  GET /api/agents/project/{id}/transcript/{role}
                                — JSON, parsed sessions/<role>.jsonl for one
                                  pipeline agent (capped + truncated).
  GET /                         — Jinja-rendered cost dashboard.
  GET /agents                   — Jinja-rendered agent-pipeline dashboard.

All routes except /healthz are gated by Cloudflare Access JWT, verified via
``services.chat.auth.require_user`` (bind-mounted at /app/services/chat).
The Anthropic data is produced by ``usage_report.collect_usage_data`` from
the bind-mounted ``/app/claude-bridge``. The agents data is produced by the
local ``agents`` module reading the Multi-Agent-Framework's on-disk state
under ``/home/felix/multi-agent-pipeline/projects/`` (visible inside the
container via the existing ``/home/felix:ro`` bind-mount).
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

logger = logging.getLogger("spend")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))


# ---------------------------------------------------------------------------
# Bind-mount imports.
#
# /app/services/chat is a read-only bind mount of the chat backend's source
# (compose: ./services/chat:/app/services/chat:ro). We add /app to sys.path
# so ``from services.chat import auth`` resolves the same way it does in
# services/term-router. Same convention; same trust model: chat/auth.py is
# the single source of truth for CF Access JWT verification across services.
#
# /app/claude-bridge is a read-only bind mount of /home/felix/projects/
# claude-bridge. usage_report.collect_usage_data() returns the structured
# dict that backs the Discord !usage command.
# ---------------------------------------------------------------------------
sys.path.insert(0, "/app")
sys.path.insert(0, "/app/claude-bridge")


def _import_chat_auth():
    # Lazy import so unit tests can monkeypatch a stub without needing the
    # bind-mount to exist at import time.
    from services.chat import auth as chat_auth  # type: ignore
    return chat_auth


def _import_collect_usage_data():
    from usage_report import collect_usage_data  # type: ignore
    return collect_usage_data


# ---------------------------------------------------------------------------
# AWS Cost Explorer client.
#
# v0 expects AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY in the env (loaded
# from the operator-managed .env). If neither is set we return a structured
# 503 from /api/aws/cost so the UI can render a "creds not provisioned"
# state without breaking. No silent fallback to IMDS — IMDS is iptables-
# blocked from the per-user pool but reachable from the spend container's
# ``internal`` network, and we want failure-to-configure to be loud.
# ---------------------------------------------------------------------------
def _aws_creds_present() -> bool:
    return bool(os.environ.get("AWS_ACCESS_KEY_ID")) and bool(
        os.environ.get("AWS_SECRET_ACCESS_KEY")
    )


def _aws_cost_payload() -> dict[str, Any]:
    """Pull current-month and prior-month totals from Cost Explorer.

    Raises HTTPException(503) when credentials are not configured. Other
    boto3 errors propagate as 502.
    """
    if not _aws_creds_present():
        raise HTTPException(
            status_code=503,
            detail={
                "error": "aws_credentials_missing",
                "hint": (
                    "Set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY in "
                    "portfolio-tool/.env. Required IAM permissions: "
                    "ce:GetCostAndUsage, ec2:DescribeInstances, "
                    "ec2:DescribeVolumes, ec2:DescribeAddresses."
                ),
            },
        )

    import boto3  # local import — keeps app importable without boto3 wired
    from botocore.exceptions import BotoCoreError, ClientError
    from datetime import date, timedelta

    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    ce = boto3.client("ce", region_name=region)

    today = date.today()
    month_start = today.replace(day=1)
    prev_month_end = month_start
    prev_month_start = (month_start - timedelta(days=1)).replace(day=1)

    try:
        cur = ce.get_cost_and_usage(
            TimePeriod={
                "Start": month_start.isoformat(),
                "End": (today + timedelta(days=1)).isoformat(),
            },
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        )
        prev = ce.get_cost_and_usage(
            TimePeriod={
                "Start": prev_month_start.isoformat(),
                "End": prev_month_end.isoformat(),
            },
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
        )
    except (BotoCoreError, ClientError) as exc:
        raise HTTPException(status_code=502, detail=f"cost-explorer error: {exc}")

    def _sum_groups(resp: dict[str, Any]) -> tuple[float, list[dict[str, Any]]]:
        total = 0.0
        services: list[dict[str, Any]] = []
        for period in resp.get("ResultsByTime", []):
            for grp in period.get("Groups", []):
                amt = float(grp["Metrics"]["UnblendedCost"]["Amount"])
                total += amt
                services.append({"service": grp["Keys"][0], "cost_usd": amt})
            for k in ("Total",):
                if k in period and "UnblendedCost" in period[k]:
                    total += float(period[k]["UnblendedCost"]["Amount"])
        services.sort(key=lambda s: s["cost_usd"], reverse=True)
        return total, services

    cur_total, cur_services = _sum_groups(cur)
    prev_total = 0.0
    for period in prev.get("ResultsByTime", []):
        if "Total" in period and "UnblendedCost" in period["Total"]:
            prev_total += float(period["Total"]["UnblendedCost"]["Amount"])

    return {
        "month_to_date_usd": round(cur_total, 2),
        "prev_month_usd": round(prev_total, 2),
        "by_service": [
            {"service": s["service"], "cost_usd": round(s["cost_usd"], 2)}
            for s in cur_services
        ],
        "month_start": month_start.isoformat(),
        "as_of": today.isoformat(),
    }


# ---------------------------------------------------------------------------
# App.
# ---------------------------------------------------------------------------
app = FastAPI(title="portfolio-spend", version="0.1.0")

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _fmt_minutes(minutes: float | int | None) -> str:
    """Format a minute count as h/m. Mirrors usage_report._fmt_duration so
    the dashboard's "resets in" / "block remaining" labels match the
    Discord !usage output the operator already uses."""
    if minutes is None:
        return "—"
    minutes = float(minutes)
    if minutes < 60:
        return f"{int(round(minutes))}m"
    hours, mins = divmod(int(round(minutes)), 60)
    if mins == 0:
        return f"{hours}h"
    return f"{hours}h {mins}m"


templates.env.globals["fmt_minutes"] = _fmt_minutes


async def require_user(request: Request) -> dict[str, Any]:
    chat_auth = _import_chat_auth()
    return await chat_auth.require_user(request)


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"ok": True})


@app.get("/api/anthropic/usage")
async def api_anthropic_usage(_user: dict = Depends(require_user)) -> JSONResponse:
    try:
        collect = _import_collect_usage_data()
        data = collect()
    except Exception as exc:
        logger.exception("collect_usage_data failed")
        raise HTTPException(status_code=502, detail=f"usage_report error: {exc}")
    return JSONResponse(data)


@app.get("/api/aws/cost")
async def api_aws_cost(_user: dict = Depends(require_user)) -> JSONResponse:
    return JSONResponse(_aws_cost_payload())


@app.get("/api/agents/overview")
async def api_agents_overview(_user: dict = Depends(require_user)) -> JSONResponse:
    import agents  # local module, lazy import keeps unit tests light
    return JSONResponse(agents.overview())


@app.get("/api/agents/project/{project_id}")
async def api_agents_project(
    project_id: str, _user: dict = Depends(require_user)
) -> JSONResponse:
    import agents
    detail = agents.project_detail(project_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="project not found")
    return JSONResponse(detail)


@app.get("/api/agents/project/{project_id}/transcript/{role}")
async def api_agents_transcript(
    project_id: str,
    role: str,
    _user: dict = Depends(require_user),
) -> JSONResponse:
    import agents
    data = agents.read_transcript(project_id, role)
    if data is None:
        raise HTTPException(status_code=404, detail="transcript not found")
    return JSONResponse(data)


@app.get("/agents", response_class=HTMLResponse)
async def agents_index(
    request: Request,
    _user: dict = Depends(require_user),
) -> HTMLResponse:
    import agents
    return templates.TemplateResponse(
        "agents.html",
        {
            "request": request,
            "user_email": _user.get("email", "unknown"),
            "overview": agents.overview(),
        },
    )


@app.get("/agents/{project_id}", response_class=HTMLResponse)
async def agents_project(
    request: Request,
    project_id: str,
    _user: dict = Depends(require_user),
) -> HTMLResponse:
    import agents
    detail = agents.project_detail(project_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="project not found")
    return templates.TemplateResponse(
        "agent_project.html",
        {
            "request": request,
            "user_email": _user.get("email", "unknown"),
            "detail": detail,
        },
    )


@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    _user: dict = Depends(require_user),
) -> HTMLResponse:
    # Render-side errors are surfaced in the template, not raised, so the
    # dashboard remains useful when one data source is down.
    anthropic_data: dict[str, Any] | None = None
    anthropic_err: str | None = None
    try:
        anthropic_data = _import_collect_usage_data()()
    except Exception as exc:
        anthropic_err = f"{type(exc).__name__}: {exc}"

    aws_data: dict[str, Any] | None = None
    aws_err: str | None = None
    try:
        aws_data = _aws_cost_payload()
    except HTTPException as exc:
        aws_err = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    except Exception as exc:
        aws_err = f"{type(exc).__name__}: {exc}"

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "user_email": _user.get("email", "unknown"),
            "anthropic": anthropic_data,
            "anthropic_err": anthropic_err,
            "aws": aws_data,
            "aws_err": aws_err,
        },
    )
