"""local_runner: OpenAI-compatible runner for the on-prem (home GPU) LLM.

Thin wrapper over ``haihub_runner`` that points at the self-hosted LM Studio
endpoint (felix's RTX 5090, reached via the ``vm-tailscale`` TCP forward) instead
of haihub. Same normalized event contract, same agent/tool loop, same
``_ThinkStripper`` for reasoning models (gemma-4 emits ``reasoning_content`` /
``<think>`` spans). Used for the "Gemma4 (Local)" model so sensitive documents
are processed on the local GPU rather than a cloud API.

Reachability: the chat container resolves ``vm-tailscale`` on the
``portfolio-tool_internal`` docker network; socat there forwards :1234 over the
tailnet (``tailscale nc``) to the home host running LM Studio.
"""

from __future__ import annotations

import os
from typing import Any, AsyncIterator

import haihub_runner

# Default points at the in-cluster forward (socat on vm-tailscale -> tailnet ->
# home LM Studio). Override with LOCAL_LLM_BASE_URL if the topology changes.
LOCAL_LLM_BASE_URL = os.environ.get(
    "LOCAL_LLM_BASE_URL", "http://vm-tailscale:1234/v1"
).rstrip("/")
# LM Studio ignores the bearer token; send a non-empty dummy so any
# auth-header machinery is satisfied. require_key=False below means an empty
# value is fine regardless.
LOCAL_LLM_API_KEY = os.environ.get("LOCAL_LLM_API_KEY", "lm-studio")

# Frontend alias -> exact model id served by LM Studio's OpenAI endpoint.
_LOCAL_MODELS: dict[str, str] = {
    "gemma4-local": "google/gemma-4-31b-qat",
}


def is_local_model(model: str | None) -> bool:
    """True iff ``model`` is one of the local (home-GPU) aliases."""
    return bool(model) and model in _LOCAL_MODELS


async def run_turn(
    *, model: str | None = None, **kwargs: Any
) -> AsyncIterator[dict[str, Any]]:
    """Stream one turn from the local model — delegates to haihub_runner with
    the LM Studio base URL and the local model map. Emits the identical
    delta/tool_start/tool_end/done/error contract app._run_turn_worker expects.
    """
    async for ev in haihub_runner.run_turn(
        model=model,
        base_url=LOCAL_LLM_BASE_URL,
        api_key=LOCAL_LLM_API_KEY,
        models_map=_LOCAL_MODELS,
        require_key=False,
        **kwargs,
    ):
        yield ev
