"""haihub_runner: OpenAI-compatible runner for haihub-hosted models.

Parallel to ``claude_runner.run_turn``. It yields the SAME normalized event
contract — ``{"type": "delta"|"tool_start"|"tool_end"|"done"|"error", ...}`` —
so ``app._run_turn_worker`` consumes it byte-for-byte identically; only the
producer differs.

Two modes, chosen by whether a per-user ``container`` is supplied:

  * **Agent mode** (container present): a function-calling loop. The model is
    given a single ``run_bash`` tool that executes inside the user's sandboxed
    container (uid 1000, HOME=/workspace, no host mounts, squid-gated egress) —
    the SAME isolation the claude path runs in. We stream each model step,
    assemble streamed ``tool_calls``, exec them in the container, feed results
    back, and loop until the model returns a final answer (or hits the step
    cap). This is what gives Qwen/DeepSeek/MiniMax tool access "just like the
    other models."
  * **Plain-chat mode** (no container — admin/local sessions): no tools, just a
    streaming chat completion.

Conversation continuity is the caller's job: haihub is stateless per request,
so ``app.py`` passes the FULL prior transcript as ``prior_history`` every turn.

The haihub API is case-sensitive on model id and 404s on any non-exact display
name. ``_HAIHUB_MODELS`` maps the short frontend alias to the exact name.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, AsyncIterator

import docker
import httpx

import prompt_blocks

logger = logging.getLogger("chat.haihub_runner")

HAIHUB_BASE_URL = os.environ.get(
    "HAIHUB_BASE_URL", "https://api.model.haihub.cn/v1"
).rstrip("/")
HAIHUB_API_KEY = os.environ.get("HAIHUB_API_KEY", "")

# Frontend alias -> exact haihub display name. The API routes on
# case-sensitive display names; keep these EXACT.
_HAIHUB_MODELS: dict[str, str] = {
    "qwen": "Qwen3.5-397B-A17B-FP8",
    "deepseek": "DeepSeek-V4-Flash",
    "minimax": "MiniMax-M2.7",
}

_MAX_TOKENS = 8192
# No hard cap on model<->tool round-trips: the loop runs until the model
# returns a tool-free answer. Each step is still bounded by _REQUEST_TIMEOUT
# (model call) and _TOOL_TIMEOUT (run_bash), so a turn can't hang indefinitely.
_TOOL_TIMEOUT = 90           # seconds per run_bash command (enforced in-container)
_MAX_TOOL_OUTPUT = 16000     # chars of tool output fed back to the model

# Generous read timeout: thinking models lag before first token.
_REQUEST_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)

_SYSTEM_PROMPT = (
    "You are a helpful AI assistant in the Wizerith chat. "
    "Format responses using GitHub-flavored Markdown."
)
_SYSTEM_PROMPT_TOOLS = (
    "You are a helpful AI assistant in the Wizerith chat with access to a "
    "sandboxed Linux workspace. You can run shell commands with the run_bash "
    "tool; each call executes in /workspace inside an isolated per-user "
    "container (your HOME is /workspace, no access to the host). Use it to "
    "read and write files, run code, install packages, and inspect the "
    "environment. Persist anything you create under /workspace. Take as many "
    "tool steps as you need, then reply to the user with a normal answer. "
    "Format responses using GitHub-flavored Markdown."
)

# Admin sessions have no per-user container; their tools run in the
# tenant's host-shell container instead (parity with the claude path's
# "host" dispatch). The environment note must say so — telling the model
# it's in an isolated /workspace sandbox when it is actually on the
# operator host produces confidently wrong file paths and commands.
_SYSTEM_PROMPT_TOOLS_HOST = (
    "You are a helpful AI assistant in the Wizerith chat with access to a "
    "Linux host shell. You can run shell commands with the run_bash tool; "
    "each call executes as uid 1000 with HOME=/home/felix on the operator "
    "host. Use it to read and write files, run code, and inspect services. "
    "Files you create for the user belong under the artifacts directory you "
    "were given. Take as many tool steps as you need, then reply to the "
    "user with a normal answer. Format responses using GitHub-flavored "
    "Markdown."
)

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_bash",
            "description": (
                "Run a bash command in the user's sandboxed /workspace "
                "container and return combined stdout+stderr. Use for file "
                "operations, running code, installing packages, and inspecting "
                "the environment."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The bash command to execute.",
                    }
                },
                "required": ["command"],
            },
        },
    }
]

_LANGUAGE_NAMES = {
    "en": "English",
    "zh-CN": "Simplified Chinese (简体中文)",
    "zh-TW": "Traditional Chinese (繁體中文)",
    "de": "German (Deutsch)",
    "tl": "Tagalog (Filipino)",
}


def is_haihub_model(model: str | None) -> bool:
    """True iff ``model`` is one of the haihub aliases this module serves."""
    return bool(model) and model in _HAIHUB_MODELS


_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"


class _ThinkStripper:
    """Incrementally strip ``<think>...</think>`` spans from streamed content.

    MiniMax (and sometimes others) emit reasoning inline inside ``<think>``
    tags in ``content`` rather than ``reasoning_content``. Streaming-safe: a
    short carry holds back a possible tag straddling a chunk boundary.
    """

    def __init__(self) -> None:
        self._inside = False
        self._carry = ""

    def feed(self, chunk: str) -> str:
        self._carry += chunk
        out: list[str] = []
        while True:
            if not self._inside:
                idx = self._carry.find(_THINK_OPEN)
                if idx == -1:
                    keep = len(_THINK_OPEN) - 1
                    if len(self._carry) > keep:
                        out.append(self._carry[:-keep] if keep else self._carry)
                        self._carry = self._carry[-keep:] if keep else ""
                    break
                out.append(self._carry[:idx])
                self._carry = self._carry[idx + len(_THINK_OPEN):]
                self._inside = True
            else:
                idx = self._carry.find(_THINK_CLOSE)
                if idx == -1:
                    keep = len(_THINK_CLOSE) - 1
                    self._carry = self._carry[-keep:] if len(self._carry) > keep else self._carry
                    break
                self._carry = self._carry[idx + len(_THINK_CLOSE):]
                self._inside = False
        return "".join(out)

    def flush(self) -> str:
        if self._inside:
            return ""
        tail = self._carry
        self._carry = ""
        return tail


def _build_user_prompt(
    prompt: str,
    *,
    prior_history: str | None,
    persona: str | None,
    memory: str | None,
    output_language: str | None,
    artifacts_path: str | None = None,
) -> str:
    """Assemble the single user message (mirrors claude_runner's layering).

    When ``artifacts_path`` is set (tool-enabled turn with a container), the
    shared inline-artifacts / pre-installed-libs / quant-CLI / image-gen
    instruction block is prepended — identical to the claude path — so these
    models produce inline plots/files/spreadsheets the same way.
    """
    eff = prompt
    if prior_history:
        eff = (
            "[Conversation history — please continue as if you'd been part of "
            "this conversation. The user's NEW message follows after the "
            "history block.]\n\n"
            f"{prior_history}\n\n"
            "[End of history. The user's new message:]\n\n"
            f"{prompt}"
        )
    if persona and persona.strip():
        eff = (
            "[Personal system prompt from this user — apply to every turn:]\n"
            f"{persona.strip()}\n"
            "[End of personal system prompt.]\n\n"
            + eff
        )
    if output_language and output_language != "en" and output_language in _LANGUAGE_NAMES:
        lang_name = _LANGUAGE_NAMES[output_language]
        eff = (
            f"[Response language: respond in {lang_name}, regardless of "
            "what language the user writes in. Use natural, idiomatic "
            "phrasing — not a machine-translated style. Code, file paths, "
            "and English technical terms (function names, library names, "
            "command-line flags) stay in their original form.]\n\n"
            + eff
        )
    if memory is not None:
        memory_block = (
            memory.strip() if memory and memory.strip()
            else "(empty — nothing recorded yet)"
        )
        eff = (
            "[Cross-session memory — facts you've learned about this user "
            "across past sessions. Read first so you don't re-ask things "
            "they've already told you. To UPDATE memory after answering, "
            "end your response with EXACTLY this block (it will be stripped "
            "before the user sees it):\n"
            "<memory_update>\n"
            "...full new memory content here, replacing the old...\n"
            "</memory_update>\n"
            "Only emit the block when there's something genuinely worth "
            "recording (the user's role, ongoing projects, preferences, "
            "facts they've stated). Skip it for casual exchanges. Keep "
            "memory under 8000 chars total.]\n"
            "<memory>\n"
            f"{memory_block}\n"
            "</memory>\n\n"
            + eff
        )
    # Outermost (read first): the artifacts contract, only when tools+container
    # are available so the model can actually write files into the sandbox.
    if artifacts_path:
        eff = prompt_blocks.artifacts_instructions(artifacts_path) + eff
    return eff


async def _exec_bash(
    container_name: str, command: str,
    *, workdir: str = "/workspace", home: str = "/workspace",
) -> str:
    """Run ``command`` in the tool container and return output.

    Default target is the per-user container's /workspace (same sandbox as
    the claude path: uid 1000:1000, HOME=/workspace). Admin sessions pass
    the host-shell container with workdir/home /home/felix instead —
    parity with the claude path's "host" dispatch. ``timeout`` enforces the
    wall-clock limit IN the container (so a runaway command is killed
    server-side, not just locally abandoned).
    """
    def _run() -> tuple[int | None, str]:
        client = docker.from_env()
        container = client.containers.get(container_name)
        res = container.exec_run(
            cmd=["timeout", str(_TOOL_TIMEOUT), "bash", "-lc", command],
            workdir=workdir,
            user="1000:1000",
            environment={"HOME": home},
            stdout=True,
            stderr=True,
        )
        out = res.output.decode("utf-8", "replace") if res.output else ""
        return res.exit_code, out

    try:
        code, out = await asyncio.wait_for(
            asyncio.to_thread(_run), timeout=_TOOL_TIMEOUT + 15
        )
    except asyncio.TimeoutError:
        return f"[run_bash timed out after {_TOOL_TIMEOUT}s]"
    except Exception as exc:  # noqa: BLE001
        logger.warning("run_bash exec failed in %s: %s", container_name, exc)
        return f"[run_bash failed: {type(exc).__name__}: {exc}]"

    if len(out) > _MAX_TOOL_OUTPUT:
        out = out[:_MAX_TOOL_OUTPUT] + f"\n[...output truncated at {_MAX_TOOL_OUTPUT} chars...]"
    if code == 124:
        out += f"\n[command exceeded {_TOOL_TIMEOUT}s and was killed]"
    return out if out.strip() else f"(exit code {code}, no output)"


async def _stream_step(
    client: httpx.AsyncClient, payload: dict[str, Any],
    *, base_url: str = HAIHUB_BASE_URL, api_key: str = HAIHUB_API_KEY,
) -> AsyncIterator[dict[str, Any]]:
    """Stream one model call.

    Yields ``{"type":"delta","text":...}`` for visible content as it arrives,
    then exactly one terminal sentinel: ``{"type":"_meta","tool_calls":[...],
    "content":str}`` on success or ``{"type":"_error","message":str}`` on a
    non-200 / transport failure. Sentinels are consumed by ``run_turn`` and not
    forwarded to the worker.
    """
    stripper = _ThinkStripper()
    content_parts: list[str] = []
    tool_acc: dict[int, dict[str, Any]] = {}
    # OpenAI-compatible usage object from the final pre-[DONE] chunk, when
    # the server honours stream_options.include_usage. None if absent.
    usage: dict[str, Any] | None = None
    url = f"{base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        async with client.stream("POST", url, headers=headers, json=payload) as resp:
            if resp.status_code != 200:
                body = (await resp.aread()).decode("utf-8", "replace")[:300]
                logger.warning("haihub HTTP %s: %s", resp.status_code, body)
                yield {"type": "_error", "message": f"haihub HTTP {resp.status_code}"}
                return
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                # The usage object rides on the final chunk before [DONE]
                # (and that chunk usually has an empty ``choices`` list).
                if isinstance(obj.get("usage"), dict):
                    usage = obj["usage"]
                choices = obj.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                ctext = delta.get("content")
                if ctext:
                    clean = stripper.feed(ctext)
                    if clean:
                        content_parts.append(clean)
                        yield {"type": "delta", "text": clean}
                for tc in (delta.get("tool_calls") or []):
                    idx = tc.get("index", 0)
                    slot = tool_acc.setdefault(idx, {"id": None, "name": None, "args": ""})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["args"] += fn["arguments"]
    except Exception as exc:  # noqa: BLE001
        logger.exception("haihub stream failed")
        yield {"type": "_error", "message": f"haihub request failed: {type(exc).__name__}"}
        return

    tail = stripper.flush()
    if tail:
        content_parts.append(tail)
        yield {"type": "delta", "text": tail}
    tool_calls = [tool_acc[i] for i in sorted(tool_acc)]
    yield {
        "type": "_meta",
        "tool_calls": tool_calls,
        "content": "".join(content_parts),
        "usage": usage,
    }


async def run_turn(
    *,
    prompt: str,
    model: str | None = None,
    prior_history: str | None = None,
    persona: str | None = None,
    memory: str | None = None,
    output_language: str | None = None,
    container: str | None = None,
    chat_session_id: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    models_map: dict[str, str] | None = None,
    require_key: bool = True,
    effort: str | None = None,
    max_tokens: int | None = None,
    tool_workdir: str = "/workspace",
    tool_home: str = "/workspace",
    artifacts_path: str | None = None,
    **_ignored: Any,
) -> AsyncIterator[dict[str, Any]]:
    """Stream one turn from an OpenAI-compatible model as normalized events.

    With a ``container`` the model gets the run_bash tool (agent loop); without
    one it's plain chat. Emits ``delta`` / ``tool_start`` / ``tool_end`` events
    and exactly one terminal ``done`` (success) or ``error`` (failure) — the
    same contract app._run_turn_worker expects from claude_runner.run_turn.

    ``effort`` (pre-validated by the caller against the model's supported
    levels) is sent as OpenAI-style ``reasoning_effort``; unset/empty sends
    nothing so the provider default applies. ``max_tokens`` overrides
    ``_MAX_TOKENS``. ``tool_workdir``/``tool_home`` retarget the tool execs
    (admin host-shell dispatch passes /home/felix). ``artifacts_path``
    overrides the default ``/workspace/.artifacts/<sid>`` block target.
    """
    mmap = models_map or _HAIHUB_MODELS
    base = (base_url or HAIHUB_BASE_URL).rstrip("/")
    key = api_key if api_key is not None else HAIHUB_API_KEY
    display = mmap.get(model or "")
    if not display:
        yield {"type": "error", "message": f"unknown model: {model!r}"}
        return
    if require_key and not key:
        logger.error("model API key empty; cannot dispatch model=%s", model)
        yield {"type": "error", "message": "model API key not configured"}
        return

    use_tools = bool(container)
    # Tool-enabled turns can write inline artifacts for the user; app.py
    # collects/scans them after the turn. Plain-chat turns (no container)
    # can't, so the block is omitted. Default target is the per-user
    # container's /workspace/.artifacts/<sid> (same path the claude path
    # uses); admin host-shell dispatch overrides via ``artifacts_path``.
    if artifacts_path is None:
        artifacts_path = (
            f"/workspace/.artifacts/{chat_session_id}"
            if use_tools and chat_session_id else None
        )
    user_content = _build_user_prompt(
        prompt,
        prior_history=prior_history,
        persona=persona,
        memory=memory,
        output_language=output_language,
        artifacts_path=artifacts_path,
    )
    # Tell the model which model it actually is — third-party models otherwise
    # self-identify from their training prior (often wrong, sometimes claiming
    # to be GPT/Claude). Prepended to the system message so it leads the turn.
    if use_tools and tool_home == "/home/felix":
        _system = _SYSTEM_PROMPT_TOOLS_HOST
    else:
        _system = _SYSTEM_PROMPT_TOOLS if use_tools else _SYSTEM_PROMPT
    _identity = prompt_blocks.model_identity_directive(model)
    if _identity:
        _system = f"{_identity}\n\n{_system}"
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": _system},
        {"role": "user", "content": user_content},
    ]

    final_text = ""
    turn_start = time.monotonic()
    # Accumulate usage across tool round-trips so the reported total covers
    # the whole turn (each step bills its own tokens). None entries when the
    # server doesn't return usage at all.
    acc_prompt = 0
    acc_completion = 0
    saw_usage = False
    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            for _step in itertools.count():
                payload: dict[str, Any] = {
                    "model": display,
                    "stream": True,
                    "max_tokens": max_tokens or _MAX_TOKENS,
                    "messages": messages,
                    # Ask the OpenAI-compatible endpoint to append a final
                    # chunk carrying token usage before [DONE]. Servers that
                    # don't support it simply omit the field — we fall back to
                    # a length-based estimate below.
                    "stream_options": {"include_usage": True},
                }
                if use_tools:
                    payload["tools"] = _TOOLS
                    payload["tool_choice"] = "auto"
                # OpenAI-style reasoning-effort control. Callers pre-validate
                # the level against the model's supported set (app.py), so
                # an unset/empty value simply means "provider default".
                if effort:
                    payload["reasoning_effort"] = effort

                meta: dict[str, Any] | None = None
                async for ev in _stream_step(client, payload, base_url=base, api_key=key):
                    t = ev["type"]
                    if t == "delta":
                        yield ev
                    elif t == "_error":
                        yield {"type": "error", "message": ev["message"]}
                        return
                    elif t == "_meta":
                        meta = ev
                if meta is None:
                    yield {"type": "error", "message": "haihub: empty response stream"}
                    return

                step_usage = meta.get("usage")
                if isinstance(step_usage, dict):
                    saw_usage = True
                    pt = step_usage.get("prompt_tokens")
                    ct = step_usage.get("completion_tokens")
                    if isinstance(pt, int):
                        acc_prompt += pt
                    if isinstance(ct, int):
                        acc_completion += ct

                tool_calls = meta["tool_calls"]
                step_content = meta["content"]

                if not tool_calls:
                    final_text = step_content
                    break

                # Record the assistant's tool-call message, then execute each.
                messages.append({
                    "role": "assistant",
                    "content": step_content or None,
                    "tool_calls": [
                        {
                            "id": tc["id"] or f"call_{i}",
                            "type": "function",
                            "function": {
                                "name": tc["name"] or "",
                                "arguments": tc["args"] or "{}",
                            },
                        }
                        for i, tc in enumerate(tool_calls)
                    ],
                })
                for i, tc in enumerate(tool_calls):
                    cid = tc["id"] or f"call_{i}"
                    name = tc["name"]
                    try:
                        args = json.loads(tc["args"] or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    command = args.get("command", "") if isinstance(args, dict) else ""
                    if name != "run_bash":
                        result = f"[error: unknown tool {name!r}]"
                    elif not command:
                        result = "[error: run_bash called without a 'command']"
                    else:
                        yield {
                            "type": "tool_start",
                            "name": "run_bash",
                            "input_summary": command[:200],
                        }
                        result = await _exec_bash(
                            container, command,
                            workdir=tool_workdir, home=tool_home,
                        )
                        yield {"type": "tool_end", "name": "run_bash"}
                    messages.append({
                        "role": "tool",
                        "tool_call_id": cid,
                        "content": result,
                    })
    except Exception as exc:  # noqa: BLE001
        logger.exception("haihub run_turn failed for model=%s", display)
        yield {"type": "error", "message": f"haihub run failed: {type(exc).__name__}"}
        return

    elapsed = max(time.monotonic() - turn_start, 0.0)
    if saw_usage:
        input_tok: int | None = acc_prompt
        output_tok: int | None = acc_completion
        total_tok: int | None = acc_prompt + acc_completion
    else:
        # Fallback estimate when the endpoint returns no usage object.
        # Heuristic: ~4 chars per token over the assembled prompt and the
        # visible answer. Marked approximate so the contract stays honest;
        # tok_s is still derived from the (estimated) output count.
        input_tok = max(len(user_content) // 4, 0)
        output_tok = max(len(final_text) // 4, 0)
        total_tok = input_tok + output_tok
    tok_s = (output_tok / elapsed) if (output_tok and elapsed > 0) else None
    meta_out = {
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "model": display,
        "tokens": {"input": input_tok, "output": output_tok, "total": total_tok},
        "tok_s": tok_s,
        "tokens_estimated": not saw_usage,
    }
    yield {"type": "done", "full_text": final_text, "meta": meta_out}
