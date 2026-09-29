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
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, AsyncIterator

import haihub_runner

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

# Same ceiling as the TokenHub / MiMo runners — a cap, not a target.
_KIMI_MAX_TOKENS = 65536

NOT_CONFIGURED_MESSAGE = (
    "Kimi K3 is not configured: no KIMI_API_KEY / ~/.kimi_code_key on the "
    "host. Pick another model or ask the operator to add the Kimi Code key."
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


async def run_turn(
    *, model: str | None = None, effort: str | None = None, **kwargs: Any
) -> AsyncIterator[dict[str, Any]]:
    """Stream one turn from Kimi K3 — delegates to ``haihub_runner.run_turn``
    with the Kimi Code base URL, the per-call key and the Kimi model map.
    Emits the same event contract app._run_turn_worker expects."""
    key = resolve_key()
    if not key:
        yield {"type": "error", "message": NOT_CONFIGURED_MESSAGE}
        return
    async for ev in haihub_runner.run_turn(
        model=model,
        base_url=KIMI_BASE_URL,
        api_key=key,
        models_map=_KIMI_MODELS,
        max_tokens=_KIMI_MAX_TOKENS,
        effort=effort,
        **kwargs,
    ):
        yield ev
