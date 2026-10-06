"""tokenhub_runner: OpenAI-compatible runner for Tencent TokenHub models.

Thin wrapper over ``haihub_runner`` (same pattern as ``local_runner``) that
points at Tencent Cloud's TokenHub "Token Plan" endpoint instead of haihub.
Serves the ``glm`` (GLM-5.3) and ``kimi`` (Kimi K3) picker aliases — the two
models that lead the wizerith lineup since the Anthropic models were removed
from the picker (2026-09-28). Same normalized event contract, same agent /
tool loop, same ``_ThinkStripper`` for reasoning models.

Key handling: ``TOKENHUB_API_KEY`` env if set, else the 0600 key file
``TOKENHUB_KEY_FILE`` (default ``~/.glm_key``) read **per turn** — the chat
container bind-mounts the operator home, so rotating the key on the host
takes effect on the next turn with no container recreate. The key never
enters the environment of a turn's tool execs, the code, or the logs.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, AsyncIterator

import haihub_runner

TOKENHUB_BASE_URL = os.environ.get(
    "TOKENHUB_BASE_URL", "https://tokenhub-intl.tencentcloudmaas.com/plan/v3"
).rstrip("/")

# Resolved per call (see module docstring) — NOT at import time.
_TOKENHUB_KEY_FILE = Path(os.environ.get(
    "TOKENHUB_KEY_FILE", str(Path.home() / ".glm_key")
))

# Frontend alias -> TokenHub model id (exact, case-sensitive).
_TOKENHUB_MODELS: dict[str, str] = {
    "glm": "glm-5.3",
}

# Probed 2026-10-05: glm-5.3 400s on any image_url content part
# ("rejected by an internal MaaS component") — attached images must NOT
# be inlined for it; they stay reachable via the run_bash preamble +
# sandbox tooling. (TokenHub's kimi-k3 DOES accept image parts; that
# path is kimi_runner's fallback, gated on kimi_runner.SUPPORTS_VISION.)
SUPPORTS_VISION = False


def supports_vision(model: str | None) -> bool:
    """glm-5.3 rejects image_url parts (400) — never inline for it."""
    return False
# "kimi" moved to kimi_runner (Kimi Code plan key) on 2026-09-29.

# TokenHub accepts (and serves) larger completions than the haihub default;
# long report-style chat turns with high effort need the headroom.
# TokenHub accepts up to 131072 for glm-5.3 / kimi-k3 (probed 2026-09-28).
# 16384 was too small: at effort=max the hidden reasoning alone could eat it
# and the visible reply came back empty (finish_reason=length). This is a
# ceiling, not a target — the model stops when it is done.
_TOKENHUB_MAX_TOKENS = 65536


def is_tokenhub_model(model: str | None) -> bool:
    """True iff ``model`` is one of the TokenHub aliases this module serves."""
    return bool(model) and model in _TOKENHUB_MODELS


def resolve_key() -> str | None:
    """API key from ``TOKENHUB_API_KEY`` env, else the key file. Per call."""
    key = os.environ.get("TOKENHUB_API_KEY", "").strip()
    if key:
        return key
    try:
        return _TOKENHUB_KEY_FILE.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


async def run_turn(
    *, model: str | None = None, effort: str | None = None, **kwargs: Any
) -> AsyncIterator[dict[str, Any]]:
    """Stream one turn from a TokenHub model — delegates to
    ``haihub_runner.run_turn`` with the TokenHub base URL, the per-call key
    and the TokenHub model map. Emits the identical
    delta/tool_start/tool_end/done/error contract app._run_turn_worker
    expects. ``effort`` (pre-validated by app.py against the per-model
    levels) is forwarded as OpenAI-style ``reasoning_effort``.
    """
    async for ev in haihub_runner.run_turn(
        model=model,
        base_url=TOKENHUB_BASE_URL,
        api_key=resolve_key(),
        models_map=_TOKENHUB_MODELS,
        max_tokens=_TOKENHUB_MAX_TOKENS,
        effort=effort,
        **kwargs,
    ):
        yield ev
