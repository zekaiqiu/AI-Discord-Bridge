"""kimi_runner: OpenAI-compatible runner for Kimi K3 on the Kimi Code plan.

Thin wrapper over ``haihub_runner`` (same pattern as ``mimo_runner``) that
points at Moonshot's Kimi Code endpoint. Serves the ``kimi`` picker alias,
which ran on Tencent TokenHub (``kimi-k3``) until 2026-09-29.

Provider facts (probed 2026-09-29):
  * OpenAI-compatible base ``https://api.kimi.ai/coding/v1`` (overseas; the
    China host ``api.kimi.com`` serves the same key at the same latency).
  * Model ids ``k3`` (1M context), ``k3-256k`` (same model, 256k context,
    about half the plan quota per call), ``kimi-for-coding``,
    ``kimi-for-coding-highspeed``.
  * ``/models`` declares think efforts low/high/max for k3 (default high).
    Other strings are accepted silently.
  * Cloudflare rejects Python's urllib User-Agent (error 1010); httpx's
    default is accepted. The plan terms forbid spoofing the client identity,
    so the runner sends httpx's own.
  * Streams tool calls normally; tool turns work without echoing
    ``reasoning_content`` back.

Key: ``KIMI_API_KEY`` env, else the 0600 key file ``KIMI_KEY_FILE`` (default
``~/.kimi_code_key``), read **per turn** so a rotated key needs no restart.

Fallback: the TokenHub plan key (``tokenhub_runner.resolve_key``) serving
``kimi-k3``. A step that fails with a limit/auth error on Kimi Code moves the
rest of the turn to TokenHub, and later turns skip Kimi Code for
``KIMI_FAILOVER_COOLDOWN_SEC`` (default 900 s) before trying it again. With
no Kimi Code key at all, turns go straight to TokenHub.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, AsyncIterator

import haihub_runner
import tokenhub_runner

logger = logging.getLogger(__name__)

KIMI_BASE_URL = os.environ.get(
    "KIMI_BASE_URL", "https://api.kimi.ai/coding/v1"
).rstrip("/")

_KIMI_KEY_FILE = Path(os.environ.get(
    "KIMI_KEY_FILE", str(Path.home() / ".kimi_code_key")
))

# Frontend alias -> Kimi Code model id (exact, case-sensitive).
_KIMI_MODELS: dict[str, str] = {
    "kimi": os.environ.get("KIMI_MODEL", "k3"),
}

# TokenHub's id for the same model (the fallback endpoint).
_TOKENHUB_KIMI_MODEL = "kimi-k3"

_FAILOVER_COOLDOWN_SEC = float(os.environ.get("KIMI_FAILOVER_COOLDOWN_SEC", "900"))
# monotonic() before which turns skip Kimi Code (set by a failover).
_primary_down_until = 0.0

# Same ceiling as the TokenHub / MiMo runners — a cap, not a target.
_KIMI_MAX_TOKENS = 65536

NOT_CONFIGURED_MESSAGE = (
    "Kimi K3 is not configured: no Kimi Code key (KIMI_API_KEY / "
    "~/.kimi_code_key) and no TokenHub key on the host. Pick another model "
    "or ask the operator to add a key."
)


def is_kimi_model(model: str | None) -> bool:
    """True iff ``model`` is one of the Kimi aliases this module serves."""
    return bool(model) and model in _KIMI_MODELS


def resolve_key() -> str | None:
    """Kimi Code key from ``KIMI_API_KEY`` env, else the key file. Per call."""
    key = os.environ.get("KIMI_API_KEY", "").strip()
    if key:
        return key
    try:
        return _KIMI_KEY_FILE.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _mark_primary_down(message: str) -> None:
    global _primary_down_until
    _primary_down_until = time.monotonic() + _FAILOVER_COOLDOWN_SEC
    logger.warning("kimi: Kimi Code unavailable (%s); using TokenHub for %.0fs",
                   message[:160], _FAILOVER_COOLDOWN_SEC)


def resolve_endpoints() -> tuple[dict[str, str] | None, dict[str, str] | None]:
    """``(primary, fallback)`` endpoints for this turn, each
    ``{"base_url", "api_key", "model"}`` or None. Kimi Code leads unless it
    has no key or is cooling down after a limit error; TokenHub backs it up."""
    kimi_key = resolve_key()
    th_key = tokenhub_runner.resolve_key()
    kimi = ({"base_url": KIMI_BASE_URL, "api_key": kimi_key, "model": _KIMI_MODELS["kimi"]}
            if kimi_key else None)
    th = ({"base_url": tokenhub_runner.TOKENHUB_BASE_URL, "api_key": th_key,
           "model": _TOKENHUB_KIMI_MODEL} if th_key else None)
    if kimi and time.monotonic() >= _primary_down_until:
        return kimi, th
    if th:
        return th, None
    return kimi, None


async def run_turn(
    *, model: str | None = None, effort: str | None = None, **kwargs: Any
) -> AsyncIterator[dict[str, Any]]:
    """Stream one turn from Kimi K3 — delegates to ``haihub_runner.run_turn``
    on the primary endpoint with TokenHub as the fallback. Emits the same
    event contract app._run_turn_worker expects."""
    primary, fallback = resolve_endpoints()
    if primary is None:
        yield {"type": "error", "message": NOT_CONFIGURED_MESSAGE}
        return
    async for ev in haihub_runner.run_turn(
        model=model,
        base_url=primary["base_url"],
        api_key=primary["api_key"],
        models_map={model or "kimi": primary["model"]} if is_kimi_model(model) else _KIMI_MODELS,
        max_tokens=_KIMI_MAX_TOKENS,
        effort=effort,
        fallback=fallback,
        on_failover=_mark_primary_down,
        **kwargs,
    ):
        yield ev
