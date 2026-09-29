"""mimo_runner: OpenAI-compatible runner for Xiaomi's MiMo models.

Thin wrapper over ``haihub_runner`` (same pattern as ``tokenhub_runner`` /
``local_runner``) that points at Xiaomi's MiMo OpenAI-compatible endpoint.
Serves the ``mimo`` / ``mimo-flash`` picker aliases (MiMo V2.6 Pro / Flash, in the lineup
since 2026-09-28). Same normalized event contract, same agent / tool loop,
same ``_ThinkStripper`` for reasoning models.

Provider facts (probed 2026-09-28):
  * Xiaomi Token Plan (default since 2026-09-29): base
    ``https://token-plan-sgp.xiaomimimo.com/v1``, model ids ``mimo-v2.6-pro`` and
    ``mimo-v2.6-flash``; key in ``~/.mimo_key``. Accepts reasoning_effort
    low/medium/high (max and anything else -> HTTP 400).
  * Xiaomi pay-as-you-go: ``https://api.xiaomimimo.com/v1`` (different key).
  * haihub does NOT serve it (404 on every MiMo id).
  * Tencent TokenHub's Token Plan key (``~/.glm_key``) is NOT scoped for it
    (403002 "not authorized to access model mimo-v2.6-pro"); TokenHub's
    pay-as-you-go gateway (``.../v1``) lists MiMo but needs its own key.
    Point ``MIMO_BASE_URL`` there if that key is the one that gets created.

Endpoint resolution, **per turn** (the chat container bind-mounts the
operator home, so a key dropped on the host takes effect on the next turn):
  1. ``MIMO_API_KEY`` env / ``MIMO_KEY_FILE`` (default ``~/.mimo_key``)
     against ``MIMO_BASE_URL`` (default Xiaomi direct) — explicit MiMo key.
  2. else the TokenHub key (``tokenhub_runner.resolve_key``) against the
     TokenHub plan endpoint — TokenHub lists ``mimo-v2.6-pro``, so this works
     the moment the key's model scope includes MiMo in the TokenHub console.
     A 403002 from TokenHub is surfaced with that exact hint.
  3. else a clear "not configured" error instead of a blank reply.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, AsyncIterator

import haihub_runner
import tokenhub_runner

MIMO_BASE_URL = os.environ.get(
    "MIMO_BASE_URL", "https://token-plan-sgp.xiaomimimo.com/v1"
).rstrip("/")

# Resolved per call (see module docstring) — NOT at import time.
_MIMO_KEY_FILE = Path(os.environ.get(
    "MIMO_KEY_FILE", str(Path.home() / ".mimo_key")
))

# Frontend alias -> MiMo model id (exact, case-sensitive).
_MIMO_MODELS: dict[str, str] = {
    "mimo": "mimo-v2.6-pro",
    "mimo-flash": "mimo-v2.6-flash",
}

# MiMo V2.6 advertises up to 128K output tokens; same ceiling as the
# TokenHub models — a cap, not a target (see tokenhub_runner).
_MIMO_MAX_TOKENS = 65536

NOT_CONFIGURED_MESSAGE = (
    "MiMo V2.6 is not configured yet: no MIMO_API_KEY / ~/.mimo_key and no "
    "TokenHub key on the host. Pick another model or ask the operator to add a key."
)

# Appended when TokenHub answers 403 for a MiMo model: the key exists but its
# model scope (TokenHub console -> API Key management) excludes MiMo.
TOKENHUB_SCOPE_HINT = (
    "The TokenHub key is not scoped for mimo-v2.6-pro. Fix: in the TokenHub "
    "console (API Key management) widen the key's model scope to include MiMo, "
    "or put a MiMo-scoped key in ~/.mimo_key (set MIMO_BASE_URL if it is a "
    "TokenHub pay-as-you-go key)."
)


def is_mimo_model(model: str | None) -> bool:
    """True iff ``model`` is one of the MiMo aliases this module serves."""
    return bool(model) and model in _MIMO_MODELS


def resolve_key() -> str | None:
    """Explicit MiMo key from ``MIMO_API_KEY`` env, else the key file. Per call."""
    key = os.environ.get("MIMO_API_KEY", "").strip()
    if key:
        return key
    try:
        return _MIMO_KEY_FILE.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def resolve_endpoint() -> tuple[str, str, str] | None:
    """``(base_url, api_key, source)`` for this turn, or None when no key
    exists anywhere. ``source`` is ``"mimo"`` (explicit key) or
    ``"tokenhub"`` (fallback onto the TokenHub plan key)."""
    key = resolve_key()
    if key:
        return MIMO_BASE_URL, key, "mimo"
    th_key = tokenhub_runner.resolve_key()
    if th_key:
        return tokenhub_runner.TOKENHUB_BASE_URL, th_key, "tokenhub"
    return None


async def run_turn(
    *, model: str | None = None, effort: str | None = None, **kwargs: Any
) -> AsyncIterator[dict[str, Any]]:
    """Stream one turn from a MiMo model — delegates to
    ``haihub_runner.run_turn`` with the MiMo base URL, the per-call key and
    the MiMo model map. Emits the identical delta/tool_start/tool_end/done/
    error contract app._run_turn_worker expects. ``effort`` is forwarded as
    OpenAI-style ``reasoning_effort`` (app.py validates it first; MiMo has no
    served levels yet, so it arrives as None).
    """
    endpoint = resolve_endpoint()
    if endpoint is None:
        yield {"type": "error", "message": NOT_CONFIGURED_MESSAGE}
        return
    base_url, key, source = endpoint
    async for ev in haihub_runner.run_turn(
        model=model,
        base_url=base_url,
        api_key=key,
        models_map=_MIMO_MODELS,
        max_tokens=_MIMO_MAX_TOKENS,
        effort=effort,
        **kwargs,
    ):
        if (
            source == "tokenhub"
            and ev.get("type") == "error"
            and "HTTP 403" in str(ev.get("message", ""))
        ):
            ev = {**ev, "message": f"{ev['message']} — {TOKENHUB_SCOPE_HINT}"}
        yield ev
