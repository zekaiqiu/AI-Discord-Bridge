"""mimo_runner: OpenAI-compatible runner for Xiaomi's MiMo models.

Thin wrapper over ``haihub_runner`` (same pattern as ``tokenhub_runner`` /
``local_runner``) that points at Xiaomi's MiMo OpenAI-compatible endpoint.
Serves the ``mimo`` picker alias (MiMo V2.6 Pro, third option in the lineup
since 2026-09-28). Same normalized event contract, same agent / tool loop,
same ``_ThinkStripper`` for reasoning models.

Provider facts (probed 2026-09-28):
  * Xiaomi direct: base ``https://api.xiaomimimo.com/v1``, model id
    ``mimo-v2.6-pro``, keys at platform.xiaomimimo.com/console/api-keys.
  * haihub does NOT serve it (404 on every MiMo id).
  * Tencent TokenHub's Token Plan key (``~/.glm_key``) is NOT scoped for it
    (403002 "not authorized to access model mimo-v2.6-pro"); TokenHub's
    pay-as-you-go gateway (``.../v1``) lists MiMo but needs its own key.
    Point ``MIMO_BASE_URL`` there if that key is the one that gets created.

Key handling: ``MIMO_API_KEY`` env if set, else the 0600 key file
``MIMO_KEY_FILE`` (default ``~/.mimo_key``) read **per turn** — the chat
container bind-mounts the operator home, so dropping the key on the host
takes effect on the next turn with no container recreate. Until a key exists
the turn ends with a clear "not configured" error instead of a blank reply.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, AsyncIterator

import haihub_runner

MIMO_BASE_URL = os.environ.get(
    "MIMO_BASE_URL", "https://api.xiaomimimo.com/v1"
).rstrip("/")

# Resolved per call (see module docstring) — NOT at import time.
_MIMO_KEY_FILE = Path(os.environ.get(
    "MIMO_KEY_FILE", str(Path.home() / ".mimo_key")
))

# Frontend alias -> MiMo model id (exact, case-sensitive).
_MIMO_MODELS: dict[str, str] = {
    "mimo": "mimo-v2.6-pro",
}

# MiMo V2.6 Pro advertises up to 128K output tokens; same ceiling as the
# TokenHub models — a cap, not a target (see tokenhub_runner).
_MIMO_MAX_TOKENS = 65536

NOT_CONFIGURED_MESSAGE = (
    "MiMo V2.6 Pro is not configured yet: no MIMO_API_KEY / ~/.mimo_key on the "
    "host. Pick another model or ask the operator to add the key."
)


def is_mimo_model(model: str | None) -> bool:
    """True iff ``model`` is one of the MiMo aliases this module serves."""
    return bool(model) and model in _MIMO_MODELS


def resolve_key() -> str | None:
    """API key from ``MIMO_API_KEY`` env, else the key file. Per call."""
    key = os.environ.get("MIMO_API_KEY", "").strip()
    if key:
        return key
    try:
        return _MIMO_KEY_FILE.read_text(encoding="utf-8").strip() or None
    except OSError:
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
    key = resolve_key()
    if not key:
        yield {"type": "error", "message": NOT_CONFIGURED_MESSAGE}
        return
    async for ev in haihub_runner.run_turn(
        model=model,
        base_url=MIMO_BASE_URL,
        api_key=key,
        models_map=_MIMO_MODELS,
        max_tokens=_MIMO_MAX_TOKENS,
        effort=effort,
        **kwargs,
    ):
        yield ev
