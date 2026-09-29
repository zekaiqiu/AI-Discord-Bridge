"""Token ledger: one SQLite row per model API call, one per user turn.

Shared by chat.wizerith.ai (imported from /app inside the container) and the
Discord bridge (bot.py puts services/chat on sys.path). Both write the same
database, by default ``/home/felix/.local/state/token-ledger/ledger.db``
(/home/felix is bind-mounted at the same path into chat-wizerith, uid 1000 on
both sides). Override with ``TOKEN_LEDGER_DB``; ``TOKEN_LEDGER_DB=off``
disables recording.

Attribution comes from a context variable: the caller that owns a turn calls
``begin_turn(...)`` before iterating the runner and ``end_turn(...)`` after
it, and every model call made inside that task (the OpenAI-compatible stream
step, the claude CLI frame tracker) is recorded against the bound turn. A
call made with no bound turn is still recorded, unattributed.

Nothing in here may break a turn: every public function swallows its own
errors and logs them.

Token fields are normalised across providers:
  input_tokens        uncached prompt tokens
  cache_read_tokens   prompt tokens served from cache
  cache_write_tokens  prompt tokens written to cache (Anthropic only)
  output_tokens       completion tokens, reasoning included
  reasoning_tokens    the reasoning share of output_tokens, when reported
  total_tokens        all of the above except reasoning (already in output)

``cost_usd`` is the list-price equivalent from ``PRICES`` (plus the optional
JSON override file). NULL means no price is configured for the model, not
zero: pooled Claude OAuth accounts and the TokenHub / MiMo token plans are
subscriptions, so for them the figure is what the same tokens would cost on
the pay-as-you-go API.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("token_ledger")

DEFAULT_DB = "/home/felix/.local/state/token-ledger/ledger.db"
DEFAULT_PRICES = "/home/felix/.config/token-ledger/prices.json"


# USD per million tokens: (input, output, cache_read, cache_write_5m).
# A 1-hour cache write (what Claude Code uses) bills at 2x input instead; see
# cost_usd. Anthropic rates mirror claude-bridge/pricing.py; keep the two in
# step. Opus 5.5, Fable 5.1, Sonnet 5 and Haiku 4.5 were checked against
# the CLI's own total_cost_usd on 2026-09-29 (exact match). Non-Anthropic models have no built-in price; set
# them in the JSON override file, e.g. {"glm-5.3": [0.6, 2.2, 0.11, 0]}.
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-fable-5-1": (10.00, 50.00, 0.25, 12.50),
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
    "claude-opus-4-8": (5.00, 25.00, 0.50, 6.25),
    "claude-opus-4-7": (5.00, 25.00, 0.50, 6.25),
    "claude-sonnet-5": (2.00, 10.00, 0.20, 2.50),
    "claude-sonnet-4-6": (3.00, 15.00, 0.30, 3.75),
    "claude-haiku-4-5": (1.00, 5.00, 0.10, 1.25),
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_start           TEXT NOT NULL,
    ts_end             TEXT NOT NULL,
    app                TEXT,
    user               TEXT,
    session            TEXT,
    turn_id            TEXT,
    call_idx           INTEGER,
    purpose            TEXT,
    provider           TEXT,
    model              TEXT,
    effort             TEXT,
    account            TEXT,
    status             TEXT,
    finish_reason      TEXT,
    error              TEXT,
    input_tokens       INTEGER,
    cache_read_tokens  INTEGER,
    cache_write_tokens INTEGER,
    cache_write_1h_tokens INTEGER,
    output_tokens      INTEGER,
    reasoning_tokens   INTEGER,
    total_tokens       INTEGER,
    estimated          INTEGER NOT NULL DEFAULT 0,
    cost_usd           REAL,
    latency_ms         INTEGER,
    ttft_ms            INTEGER,
    request_chars      INTEGER,
    request_messages   INTEGER,
    out_chars          INTEGER,
    reasoning_chars    INTEGER,
    tool_calls         INTEGER,
    has_tools          INTEGER,
    max_tokens         INTEGER,
    raw_usage          TEXT
);
CREATE INDEX IF NOT EXISTS calls_ts ON calls(ts_start);
CREATE INDEX IF NOT EXISTS calls_turn ON calls(turn_id);
CREATE INDEX IF NOT EXISTS calls_user ON calls(user, ts_start);
CREATE TABLE IF NOT EXISTS turns (
    turn_id            TEXT PRIMARY KEY,
    ts_start           TEXT NOT NULL,
    ts_end             TEXT,
    app                TEXT,
    user               TEXT,
    session            TEXT,
    provider           TEXT,
    model              TEXT,
    effort             TEXT,
    account            TEXT,
    status             TEXT,
    n_calls            INTEGER,
    n_tool_calls       INTEGER,
    input_tokens       INTEGER,
    cache_read_tokens  INTEGER,
    cache_write_tokens INTEGER,
    output_tokens      INTEGER,
    reasoning_tokens   INTEGER,
    total_tokens       INTEGER,
    estimated_calls    INTEGER,
    cost_usd           REAL,
    cli_cost_usd       REAL,
    duration_ms        INTEGER,
    prompt_chars       INTEGER
);
CREATE INDEX IF NOT EXISTS turns_ts ON turns(ts_start);
CREATE INDEX IF NOT EXISTS turns_user ON turns(user, ts_start);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def db_path() -> str | None:
    p = os.environ.get("TOKEN_LEDGER_DB", DEFAULT_DB)
    return None if p.strip().lower() in ("", "off", "0", "none") else p


_conn_lock = threading.Lock()
_conns: dict[str, sqlite3.Connection] = {}


def connect(path: str | None = None) -> sqlite3.Connection | None:
    """Shared connection for ``path`` (created with schema on first use)."""
    path = path or db_path()
    if not path:
        return None
    with _conn_lock:
        conn = _conns.get(path)
        if conn is None:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            conn = sqlite3.connect(path, timeout=10, check_same_thread=False,
                                   isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            conn.executescript(_SCHEMA)
            _conns[path] = conn
        return conn


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------

_price_cache: tuple[float, dict[str, tuple[float, ...]]] | None = None


def _override_prices() -> dict[str, tuple[float, ...]]:
    global _price_cache
    path = os.environ.get("TOKEN_LEDGER_PRICES", DEFAULT_PRICES)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    if _price_cache is not None and _price_cache[0] == mtime:
        return _price_cache[1]
    out: dict[str, tuple[float, ...]] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        for k, v in (raw or {}).items():
            if isinstance(v, (list, tuple)) and len(v) >= 2:
                vals = [float(x) for x in v] + [0.0] * (4 - len(v))
                out[str(k).lower()] = tuple(vals[:4])
    except Exception:  # noqa: BLE001
        logger.warning("token_ledger: unreadable price file %s", path)
    _price_cache = (mtime, out)
    return out


def price_for(model: str | None) -> tuple[float, ...] | None:
    """(input, output, cache_read, cache_write) USD/MTok, or None."""
    if not model:
        return None
    m = model.lower().strip()
    m = m.split("[", 1)[0]  # claude-opus-5-5[1m]
    table = {**PRICES, **_override_prices()}
    if m in table:
        return table[m]
    # Dated / suffixed ids: claude-haiku-4-5-20251001, us.anthropic.claude-...
    best = None
    for k in table:
        if k in m and (best is None or len(k) > len(best)):
            best = k
    return table[best] if best else None


def cost_usd(model: str | None, inp: int | None, out: int | None,
             cache_read: int | None, cache_write: int | None,
             cache_write_1h: int | None = None) -> float | None:
    """``cache_write`` is the total written; ``cache_write_1h`` the part of it
    written with the 1-hour TTL (2x input rather than the 5-minute rate)."""
    rates = price_for(model)
    if rates is None:
        return None
    w1h = min(cache_write_1h or 0, cache_write or 0)
    w5m = (cache_write or 0) - w1h
    return ((inp or 0) * rates[0] + (out or 0) * rates[1]
            + (cache_read or 0) * rates[2] + w5m * rates[3] + w1h * 2 * rates[0]) / 1e6


# ---------------------------------------------------------------------------
# Usage normalisation
# ---------------------------------------------------------------------------

def _int(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    return None


def normalize_openai_usage(u: dict[str, Any] | None) -> dict[str, int | None]:
    """OpenAI-compatible ``usage`` -> ledger fields. prompt_tokens there
    INCLUDES cached tokens; completion_tokens INCLUDES reasoning."""
    if not isinstance(u, dict):
        return {}
    prompt = _int(u.get("prompt_tokens"))
    completion = _int(u.get("completion_tokens"))
    pd = u.get("prompt_tokens_details") or {}
    cd = u.get("completion_tokens_details") or {}
    cached = _int(pd.get("cached_tokens")) if isinstance(pd, dict) else None
    if cached is None:
        # Moonshot / some gateways report it top-level.
        cached = _int(u.get("cached_tokens")) or _int(u.get("prompt_cache_hit_tokens"))
    reasoning = _int(cd.get("reasoning_tokens")) if isinstance(cd, dict) else None
    if reasoning is None:
        reasoning = _int(u.get("reasoning_tokens"))
    uncached = None if prompt is None else max(prompt - (cached or 0), 0)
    total = _int(u.get("total_tokens"))
    if total is None and prompt is not None and completion is not None:
        total = prompt + completion
    return {
        "input_tokens": uncached,
        "cache_read_tokens": (cached or 0) if prompt is not None else None,
        "cache_write_tokens": 0 if prompt is not None else None,
        "output_tokens": completion,
        "reasoning_tokens": reasoning,
        "total_tokens": total,
    }


def normalize_anthropic_usage(u: dict[str, Any] | None) -> dict[str, int | None]:
    """Anthropic Messages ``usage`` -> ledger fields (input is uncached)."""
    if not isinstance(u, dict):
        return {}
    inp = _int(u.get("input_tokens"))
    cw = _int(u.get("cache_creation_input_tokens")) or 0
    cr = _int(u.get("cache_read_input_tokens")) or 0
    cc = u.get("cache_creation")
    w1h = _int(cc.get("ephemeral_1h_input_tokens")) if isinstance(cc, dict) else None
    if w1h is None:
        w1h = _int(u.get("cache_write_1h"))
    out = _int(u.get("output_tokens"))
    td = u.get("output_tokens_details")
    thinking = _int(td.get("thinking_tokens")) if isinstance(td, dict) else None
    total = None
    if inp is not None or out is not None:
        total = (inp or 0) + cw + cr + (out or 0)
    return {
        "input_tokens": inp,
        "cache_read_tokens": cr,
        "cache_write_tokens": cw,
        "cache_write_1h_tokens": w1h or 0,
        "output_tokens": out,
        "reasoning_tokens": thinking,
        "total_tokens": total,
    }


# ---------------------------------------------------------------------------
# Turn context
# ---------------------------------------------------------------------------

@dataclass
class TurnCtx:
    turn_id: str
    app: str
    user: str | None = None
    session: str | None = None
    provider: str | None = None
    model: str | None = None
    effort: str | None = None
    account: str | None = None
    prompt_chars: int | None = None
    started_mono: float = field(default_factory=time.monotonic)
    ts_start: str = field(default_factory=_now_iso)
    n_calls: int = 0
    n_tool_calls: int = 0
    tok: dict[str, int] = field(default_factory=lambda: {
        "input_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0,
        "output_tokens": 0, "reasoning_tokens": 0, "total_tokens": 0})
    cost: float = 0.0
    priced: bool = False
    estimated_calls: int = 0
    cli_cost_usd: float | None = None
    ended: bool = False
    status: str = "running"


_current: contextvars.ContextVar[TurnCtx | None] = contextvars.ContextVar(
    "token_ledger_turn", default=None)


def current() -> TurnCtx | None:
    return _current.get()


def begin_turn(*, app: str, user: str | None = None, session: str | None = None,
               turn_id: str | None = None, provider: str | None = None,
               model: str | None = None, effort: str | None = None,
               account: str | None = None, prompt_chars: int | None = None) -> TurnCtx:
    """Bind a turn to the current task's context and return it. The row in
    ``turns`` is written (and rewritten) as calls land and by ``end_turn``,
    so a turn that dies with the process still shows its spend."""
    ctx = TurnCtx(turn_id=turn_id or uuid.uuid4().hex, app=app, user=user,
                  session=session, provider=provider, model=model,
                  effort=effort, account=account, prompt_chars=prompt_chars)
    _current.set(ctx)
    _upsert_turn(ctx, status="running")
    return ctx


def end_turn(status: str, ctx: TurnCtx | None = None, *,
             cli_cost_usd: float | None = None) -> None:
    ctx = ctx or _current.get()
    if ctx is None:
        return
    if cli_cost_usd is not None:
        ctx.cli_cost_usd = cli_cost_usd
    ctx.ended = True
    ctx.status = status
    _upsert_turn(ctx, status=status, final=True)
    if _current.get() is ctx:
        _current.set(None)


def note_cli_cost(value: Any) -> None:
    """The claude CLI's own ``total_cost_usd`` for the turn (cross-check)."""
    ctx = _current.get()
    if ctx is not None and isinstance(value, (int, float)):
        ctx.cli_cost_usd = (ctx.cli_cost_usd or 0.0) + float(value)


def _upsert_turn(ctx: TurnCtx, *, status: str, final: bool = False) -> None:
    try:
        conn = connect()
        if conn is None:
            return
        dur = int((time.monotonic() - ctx.started_mono) * 1000)
        conn.execute(
            """INSERT INTO turns (turn_id, ts_start, ts_end, app, user, session,
                   provider, model, effort, account, status, n_calls, n_tool_calls,
                   input_tokens, cache_read_tokens, cache_write_tokens, output_tokens,
                   reasoning_tokens, total_tokens, estimated_calls, cost_usd,
                   cli_cost_usd, duration_ms, prompt_chars)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(turn_id) DO UPDATE SET
                   ts_end=excluded.ts_end, status=excluded.status,
                   model=COALESCE(excluded.model, turns.model),
                   provider=COALESCE(excluded.provider, turns.provider),
                   n_calls=excluded.n_calls, n_tool_calls=excluded.n_tool_calls,
                   input_tokens=excluded.input_tokens,
                   cache_read_tokens=excluded.cache_read_tokens,
                   cache_write_tokens=excluded.cache_write_tokens,
                   output_tokens=excluded.output_tokens,
                   reasoning_tokens=excluded.reasoning_tokens,
                   total_tokens=excluded.total_tokens,
                   estimated_calls=excluded.estimated_calls,
                   cost_usd=excluded.cost_usd, cli_cost_usd=excluded.cli_cost_usd,
                   duration_ms=excluded.duration_ms""",
            (ctx.turn_id, ctx.ts_start, _now_iso() if final else None, ctx.app,
             ctx.user, ctx.session, ctx.provider, ctx.model, ctx.effort,
             ctx.account, status, ctx.n_calls, ctx.n_tool_calls,
             ctx.tok["input_tokens"], ctx.tok["cache_read_tokens"],
             ctx.tok["cache_write_tokens"], ctx.tok["output_tokens"],
             ctx.tok["reasoning_tokens"], ctx.tok["total_tokens"],
             ctx.estimated_calls, ctx.cost if ctx.priced else None,
             ctx.cli_cost_usd, dur, ctx.prompt_chars),
        )
    except Exception:  # noqa: BLE001
        logger.exception("token_ledger: turn upsert failed")


# ---------------------------------------------------------------------------
# Recording one call
# ---------------------------------------------------------------------------

def record_call(
    *,
    provider: str | None,
    model: str | None,
    usage: dict[str, int | None],
    status: str = "ok",
    purpose: str | None = None,
    finish_reason: str | None = None,
    error: str | None = None,
    started_at: float | None = None,
    ended_at: float | None = None,
    ttft: float | None = None,
    ts_start: str | None = None,
    effort: str | None = None,
    request_chars: int | None = None,
    request_messages: int | None = None,
    out_chars: int | None = None,
    reasoning_chars: int | None = None,
    tool_calls: int | None = None,
    has_tools: bool | None = None,
    max_tokens: int | None = None,
    raw_usage: Any = None,
    estimated: bool = False,
    ctx: TurnCtx | None = None,
) -> None:
    """Write one ``calls`` row and fold it into the bound turn. ``usage`` is
    already normalised (see ``normalize_*``). Times are ``time.monotonic()``
    values."""
    try:
        ctx = ctx or _current.get()
        now_m = time.monotonic()
        ended_at = ended_at if ended_at is not None else now_m
        latency = int((ended_at - started_at) * 1000) if started_at else None
        ttft_ms = int((ttft - started_at) * 1000) if (ttft and started_at) else None
        u = {k: usage.get(k) for k in ("input_tokens", "cache_read_tokens",
                                        "cache_write_tokens", "cache_write_1h_tokens",
                                        "output_tokens", "reasoning_tokens",
                                        "total_tokens")}
        c = cost_usd(model, u["input_tokens"], u["output_tokens"],
                     u["cache_read_tokens"], u["cache_write_tokens"],
                     u["cache_write_1h_tokens"])
        idx = None
        if ctx is not None:
            ctx.n_calls += 1
            idx = ctx.n_calls
            ctx.n_tool_calls += int(tool_calls or 0)
            for k in ctx.tok:
                ctx.tok[k] += int(u.get(k) or 0)
            if c is not None:
                ctx.cost += c
                ctx.priced = True
            if estimated:
                ctx.estimated_calls += 1
            if model and not ctx.model:
                ctx.model = model
            if provider and not ctx.provider:
                ctx.provider = provider
        if ts_start is None and started_at is not None:
            ts_start = datetime.fromtimestamp(
                time.time() - (now_m - started_at), timezone.utc,
            ).isoformat(timespec="milliseconds")
        conn = connect()
        if conn is not None:
            conn.execute(
                """INSERT INTO calls (ts_start, ts_end, app, user, session, turn_id,
                       call_idx, purpose, provider, model, effort, account, status,
                       finish_reason, error, input_tokens, cache_read_tokens,
                       cache_write_tokens, cache_write_1h_tokens, output_tokens,
                       reasoning_tokens, total_tokens, estimated, cost_usd,
                       latency_ms, ttft_ms, request_chars, request_messages,
                       out_chars, reasoning_chars, tool_calls, has_tools,
                       max_tokens, raw_usage)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (ts_start or _now_iso(), _now_iso(),
                 ctx.app if ctx else None, ctx.user if ctx else None,
                 ctx.session if ctx else None, ctx.turn_id if ctx else None,
                 idx, purpose, provider, model,
                 effort if effort is not None else (ctx.effort if ctx else None),
                 ctx.account if ctx else None, status, finish_reason,
                 (error or None) and str(error)[:500],
                 u["input_tokens"], u["cache_read_tokens"], u["cache_write_tokens"],
                 u["cache_write_1h_tokens"], u["output_tokens"],
                 u["reasoning_tokens"], u["total_tokens"],
                 1 if estimated else 0, c, latency, ttft_ms, request_chars,
                 request_messages, out_chars, reasoning_chars, tool_calls,
                 None if has_tools is None else int(bool(has_tools)), max_tokens,
                 json.dumps(raw_usage, separators=(",", ":"))[:4000]
                 if raw_usage is not None else None),
            )
        if ctx is not None:
            # A cancelled step can be finalised after end_turn: keep the
            # turn's terminal status rather than flipping it back.
            _upsert_turn(ctx, status=ctx.status, final=ctx.ended)
        logger.info(
            "tokens app=%s user=%s turn=%s call=%s model=%s status=%s in=%s "
            "cache_r=%s cache_w=%s out=%s reason=%s cost=%s est=%s ms=%s",
            ctx.app if ctx else "-", ctx.user if ctx else "-",
            ctx.turn_id if ctx else "-", idx, model, status,
            u["input_tokens"], u["cache_read_tokens"], u["cache_write_tokens"],
            u["output_tokens"], u["reasoning_tokens"],
            f"{c:.4f}" if c is not None else "n/a", int(estimated), latency,
        )
    except Exception:  # noqa: BLE001
        logger.exception("token_ledger: record_call failed")


def openai_request_shape(payload: dict[str, Any]) -> tuple[int, int]:
    """(chars, message count) of an OpenAI-style request, for sizing."""
    msgs = payload.get("messages") or []
    chars = 0
    for m in msgs:
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        if isinstance(c, str):
            chars += len(c)
        elif isinstance(c, list):
            chars += sum(len(p.get("text") or "") for p in c if isinstance(p, dict))
        for tc in m.get("tool_calls") or []:
            chars += len(((tc or {}).get("function") or {}).get("arguments") or "")
        rc = m.get("reasoning_content")
        if isinstance(rc, str):
            chars += len(rc)
    return chars, len(msgs)


class OpenAICallTimer:
    """Collects one OpenAI-compatible streamed call; ``finish()`` records it.

    Usage: ``t = OpenAICallTimer(provider, payload)``; ``t.first_token()`` on
    the first delta; set ``t.usage`` / ``t.finish_reason`` / counters as the
    stream goes; ``t.finish(status, error)`` exactly once (idempotent)."""

    def __init__(self, provider: str | None, payload: dict[str, Any],
                 purpose: str | None = None) -> None:
        self.provider = provider
        self.payload = payload
        self.purpose = purpose
        self.started = time.monotonic()
        self.ts_start = _now_iso()
        self.ttft: float | None = None
        self.usage: dict[str, Any] | None = None
        self.finish_reason: str | None = None
        self.out_chars = 0
        self.reasoning_chars = 0
        self.tool_calls = 0
        self._done = False
        # Captured now: a cancelled stream may be finalised later from a
        # different task (async-generator GC), where the context is gone.
        self.ctx = current()

    def first_token(self) -> None:
        if self.ttft is None:
            self.ttft = time.monotonic()

    def finish(self, status: str, error: str | None = None) -> None:
        if self._done:
            return
        self._done = True
        p = self.payload
        req_chars, n_msgs = openai_request_shape(p)
        norm = normalize_openai_usage(self.usage)
        estimated = not norm or norm.get("total_tokens") is None
        if estimated:
            # No usage object (errored / cancelled mid-stream / gateway that
            # ignores include_usage): ~4 chars per token. A call rejected
            # before generating (non-200) billed nothing we can see.
            billed_nothing = status == "error" and not self.out_chars and not self.reasoning_chars
            inp = 0 if billed_nothing else req_chars // 4
            out = (self.out_chars + self.reasoning_chars) // 4
            norm = {"input_tokens": inp, "cache_read_tokens": 0,
                    "cache_write_tokens": 0, "output_tokens": out,
                    "reasoning_tokens": self.reasoning_chars // 4 or None,
                    "total_tokens": inp + out}
        purpose = self.purpose
        if purpose is None:
            purpose = "tool_step" if p.get("tools") else "step"
        record_call(
            provider=self.provider, model=p.get("model"), usage=norm,
            status=status, purpose=purpose, finish_reason=self.finish_reason,
            error=error, started_at=self.started, ttft=self.ttft,
            ts_start=self.ts_start, effort=p.get("reasoning_effort"),
            request_chars=req_chars, request_messages=n_msgs,
            out_chars=self.out_chars, reasoning_chars=self.reasoning_chars,
            tool_calls=self.tool_calls, has_tools=bool(p.get("tools")),
            max_tokens=p.get("max_tokens"), raw_usage=self.usage,
            estimated=estimated, ctx=self.ctx,
        )


class ClaudeStreamTracker:
    """Feed every claude CLI stream-json frame of one turn; ``flush()``
    records one ``calls`` row per API message.

    A message's usage arrives in pieces: ``stream_event`` message_start
    (input + cache), message_delta (final output count), and one or more
    ``assistant`` frames repeating the message's usage (split per content
    block, possibly with partial output counts). Keyed by message id, each
    field keeps its maximum, so repeats never double count. Subagent
    messages (``parent_tool_use_id`` set) are tagged purpose=subagent.
    ``result`` carries the CLI's own cost figure, kept as a cross-check."""

    def __init__(self, provider: str = "anthropic", purpose: str = "claude_msg") -> None:
        self.provider = provider
        self.purpose = purpose
        self.msgs: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []
        self._open_stream_id: str | None = None
        self.result_usage: dict[str, Any] | None = None
        self.result_cost: float | None = None
        self.result_model_usage: dict[str, Any] | None = None
        self._flushed = False
        self.ctx = current()

    def _slot(self, mid: str) -> dict[str, Any]:
        s = self.msgs.get(mid)
        if s is None:
            s = {"usage": {}, "model": None, "first": time.monotonic(),
                 "ts_start": _now_iso(), "last": time.monotonic(),
                 "ttft": None, "tool_uses": set(), "subagent": False,
                 "stop_reason": None, "out_chars": 0, "reasoning_chars": 0}
            self.msgs[mid] = s
            self._order.append(mid)
        return s

    @staticmethod
    def _merge(dst: dict[str, Any], usage: Any) -> None:
        if not isinstance(usage, dict):
            return
        for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                  "cache_read_input_tokens"):
            v = _int(usage.get(k))
            if v is not None and v > (dst.get(k) or 0):
                dst[k] = v
            elif v is not None and k not in dst:
                dst[k] = v
        cc = usage.get("cache_creation")
        if isinstance(cc, dict):
            v = _int(cc.get("ephemeral_1h_input_tokens"))
            if v is not None and v >= (dst.get("cache_write_1h") or 0):
                dst["cache_write_1h"] = v
        td = usage.get("output_tokens_details")
        if isinstance(td, dict):
            dst["output_tokens_details"] = td
        st = usage.get("server_tool_use")
        if isinstance(st, dict):
            dst["server_tool_use"] = st

    def feed(self, obj: Any) -> None:
        try:
            self._feed(obj)
        except Exception:  # noqa: BLE001
            logger.exception("token_ledger: claude frame parse failed")

    def _feed(self, obj: Any) -> None:
        if not isinstance(obj, dict):
            return
        t = obj.get("type")
        if t == "stream_event":
            ev = obj.get("event") or {}
            et = ev.get("type")
            if et == "message_start":
                m = ev.get("message") or {}
                mid = m.get("id")
                if not mid:
                    return
                self._open_stream_id = mid
                s = self._slot(mid)
                s["model"] = m.get("model") or s["model"]
                if obj.get("parent_tool_use_id"):
                    s["subagent"] = True
                self._merge(s["usage"], m.get("usage"))
            elif self._open_stream_id:
                s = self._slot(self._open_stream_id)
                s["last"] = time.monotonic()
                if et == "content_block_delta":
                    if s["ttft"] is None:
                        s["ttft"] = time.monotonic()
                    d = ev.get("delta") or {}
                    if d.get("type") == "text_delta":
                        s["out_chars"] += len(d.get("text") or "")
                    elif d.get("type") == "thinking_delta":
                        s["reasoning_chars"] += len(d.get("thinking") or "")
                elif et == "message_delta":
                    self._merge(s["usage"], ev.get("usage"))
                    sr = (ev.get("delta") or {}).get("stop_reason")
                    if sr:
                        s["stop_reason"] = sr
            return
        if t == "assistant":
            m = obj.get("message") or {}
            mid = m.get("id")
            if not mid:
                return
            s = self._slot(mid)
            s["last"] = time.monotonic()
            s["model"] = m.get("model") or s["model"]
            if obj.get("parent_tool_use_id"):
                s["subagent"] = True
            self._merge(s["usage"], m.get("usage"))
            if m.get("stop_reason"):
                s["stop_reason"] = m["stop_reason"]
            for block in m.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id"):
                    s["tool_uses"].add(block["id"])
            return
        if t == "result":
            if isinstance(obj.get("usage"), dict):
                self.result_usage = obj["usage"]
            if isinstance(obj.get("total_cost_usd"), (int, float)):
                self.result_cost = float(obj["total_cost_usd"])
            if isinstance(obj.get("modelUsage"), dict):
                self.result_model_usage = obj["modelUsage"]

    def flush(self, status: str = "ok", *, model_hint: str | None = None) -> None:
        """Record everything seen. Idempotent; call from a ``finally``."""
        if self._flushed:
            return
        self._flushed = True
        try:
            for mid in self._order:
                s = self.msgs[mid]
                raw = dict(s["usage"])
                norm = normalize_anthropic_usage(raw)
                if not norm or norm.get("total_tokens") is None:
                    continue
                record_call(
                    provider=self.provider, model=s["model"] or model_hint,
                    usage=norm, status=status if mid == self._order[-1] else "ok",
                    purpose="subagent" if s["subagent"] else self.purpose,
                    finish_reason=s["stop_reason"], started_at=s["first"],
                    ended_at=s["last"], ttft=s["ttft"], ts_start=s["ts_start"],
                    out_chars=s["out_chars"] or None,
                    reasoning_chars=s["reasoning_chars"] or None,
                    tool_calls=len(s["tool_uses"]), raw_usage={"id": mid, **raw},
                    ctx=self.ctx,
                )
            if not self._order and self.result_usage:
                # No per-message frames reached us: fall back to the turn
                # aggregate so the spend is not lost.
                record_call(
                    provider=self.provider, model=model_hint,
                    usage=normalize_anthropic_usage(self.result_usage),
                    status=status, purpose="turn_aggregate",
                    raw_usage=self.result_usage, ctx=self.ctx,
                )
            if self.result_cost is not None and self.ctx is not None:
                self.ctx.cli_cost_usd = (self.ctx.cli_cost_usd or 0.0) + self.result_cost
        except Exception:  # noqa: BLE001
            logger.exception("token_ledger: claude flush failed")
