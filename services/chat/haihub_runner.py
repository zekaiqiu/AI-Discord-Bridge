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
import re
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable

import docker
import httpx
from concurrent.futures import ThreadPoolExecutor

import prompt_blocks
import token_ledger

logger = logging.getLogger("chat.haihub_runner")

def _provider_error_suffix(body: str) -> str:
    """": <provider message>" from an OpenAI-style error body, else "".
    Keeps the user-facing error actionable (TokenHub 403002 says which model
    the key is not scoped for) without echoing an arbitrary 300-char body."""
    try:
        err = json.loads(body).get("error")
    except Exception:
        return ""
    if isinstance(err, dict):
        msg = str(err.get("message") or "").strip()
    elif isinstance(err, str):
        msg = err.strip()
    else:
        msg = ""
    return f": {msg[:200]}" if msg else ""


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
# Seconds ONE run_bash command may run before it is killed (enforced
# in-container). A per-command budget, not a turn budget: long data pulls and
# builds are normal, so it is an hour. The turn itself has no time limit.
_TOOL_TIMEOUT = int(os.environ.get("CHAT_TOOL_TIMEOUT_SEC", "3600"))
# Tool round-trips per turn. 0 = UNLIMITED (the default, 2026-09-29): a long
# agent session runs until the model answers or the user presses Stop. The
# failure modes a cap used to paper over are handled directly instead: a
# degenerate loop by _RepetitionGuard, context growth by
# _compact_tool_history. Set CHAT_MAX_TOOL_STEPS to reinstate a cap; when it
# is reached the model gets one final no-tools call to answer from its work.
_MAX_STEPS = int(os.environ.get("CHAT_MAX_TOOL_STEPS", "0"))
# run_bash blocks a thread for up to _TOOL_TIMEOUT, and Stop cannot cancel the
# thread. On asyncio's shared default pool (min(32, cpus+4) threads) a few
# long or abandoned commands would starve every other to_thread user in the
# app (container ensure, wake claims, artifact collection). Own pool instead.
_BASH_POOL = ThreadPoolExecutor(
    max_workers=int(os.environ.get("CHAT_BASH_THREADS", "64")),
    thread_name_prefix="run_bash",
)

# CONTEXT BUDGET for one turn's message list. With no step cap, tool output
# accumulates without bound (16k chars per call) and would eventually overflow
# the model's context, ending the turn on a provider 400. Before every step,
# if the messages exceed the budget, the OLDEST tool results are replaced by a
# short elision note (the model's own narration and tool-call arguments stay,
# so it knows what it ran). The most recent results are never elided. If the
# provider still rejects the request for length, the budget is halved and the
# step retried.
_CONTEXT_CHAR_BUDGET = int(os.environ.get("CHAT_CONTEXT_CHAR_BUDGET", "400000"))
_CONTEXT_KEEP_RECENT_TOOLS = 8
_CONTEXT_MIN_BUDGET = 40000
# Specific phrases only: bare "exceeds" / "maximum" also match rate-limit and
# max_tokens errors, which would then be "fixed" by eliding history and
# retrying ~4 times before the real error surfaced.
_CONTEXT_OVERFLOW_HINTS = (
    "context length", "context window", "maximum context", "context_length",
    "too long", "input length", "prompt is too long", "input tokens exceed",
    "prompt tokens exceed",
    "token limit", "too many tokens",
)
_CAP_NUDGE = (
    "You have used the maximum number of tool calls allowed in one turn. "
    "Do not call any more tools. Give the user your final answer now from "
    "the work already completed, and state clearly what (if anything) is "
    "still unfinished so they can ask you to continue in a follow-up."
)
# Effort level used for the one retry after a step ends with
# finish_reason="length" and NO visible text: the model spent the whole output
# budget on hidden reasoning. "low" is accepted by every lineup model.
_RETRY_EFFORT = "low"
_LENGTH_RETRY_NUDGE = (
    "Your previous attempt ran out of output budget before writing a reply "
    "(the reasoning consumed it all). Answer the user now, directly and "
    "concisely, without further tool calls."
)
_NO_REPLY_NOTICE = (
    "[The model produced no visible reply: its reasoning used the entire "
    "output budget twice (finish_reason=length). Try again with a lower "
    "effort level or a narrower question.]"
)
_TRUNCATED_NOTICE = "[reply truncated: the model hit its output limit]"

# REPETITION GUARD. Two live turns (2026-09-28, 2026-09-29) degenerated the
# same way: deep into a long tool-loop turn at high/max effort the model
# stopped calling tools and narrated its own intent ("EMIT. GO. FINAL. GO.")
# in the visible reply until the 64k output ceiling -- 223 KB and 96 KB of
# garbage and no answer. The signal that separates that from real prose,
# code, tables and link footers (measured over every long reply in the
# store, 32k windows) is the share of short sentence units in the recent
# text that are EXACT repeats: the loops cross 0.5 tens of thousands of
# characters before the pure "GO." tail; no normal window with >= 40 units
# came near it (max 0.44 at 25 units). Two consecutive checks must agree.
_REP_WINDOW = 3000        # chars of recent text examined
_REP_CHECK_EVERY = 250    # re-examine after this many new chars
_REP_MIN_UNITS = 40       # fewer sentence units than this: no verdict
_REP_MAX_UNIT_LEN = 60    # longer units are prose, never loop fodder
_REP_THRESHOLD = 0.5      # duplicate fraction at/above which a check strikes
_REP_STRIKES = 2          # consecutive strikes before the step is cut
_REPETITION_NOTICE = "[the model's output degenerated into repetition and was cut off]"
_REPETITION_RETRY_NUDGE = (
    "Your previous attempt degenerated into repetitive text and was cut off. "
    "Answer the user now, directly and concisely, from the work already "
    "completed, without further tool calls, and say what is still unfinished."
)
_REPETITION_GIVEUP_NOTICE = (
    "[The model's output degenerated into repetition twice. Try again with a "
    "lower effort level or a narrower question.]"
)
# An unclosed fence (code still streaming) runs to the end of the window.
_REP_FENCE = re.compile(r"```.*?(```|$)", re.S)
_REP_SPLIT = re.compile(r"(?<=[.!?。！？])\s+|\n+")
_REP_NORM = re.compile(r"[^a-z0-9\u4e00-\u9fff]+")
# Only SENTENCES count: a unit must end in terminal punctuation. Code lines
# (``pass``, ``else:``, ``return x``) repeat by nature and carry none, so a
# window that starts inside a code block whose opening fence has scrolled
# away still yields no verdict. The loops are all sentences ("GO." "EMIT."
# "DONE."). Measured with no fence stripping at all over every reply in the
# store: the highest normal window stayed well below the threshold.
_REP_TERMINAL = re.compile(r"[.!?。！？][\"'”’)\]]*$")


def _message_chars(m: dict[str, Any]) -> int:
    n = len(m.get("content") or "") if isinstance(m.get("content"), str) else 0
    for tc in m.get("tool_calls") or []:
        n += len(((tc.get("function") or {}).get("arguments")) or "")
    rc = m.get("reasoning_content")
    if isinstance(rc, str):
        n += len(rc)
    return n


def _compact_tool_history(
    messages: list[dict[str, Any]], budget: int,
    keep_recent: int = _CONTEXT_KEEP_RECENT_TOOLS,
) -> int:
    """Elide the oldest tool results IN PLACE until ``messages`` fits
    ``budget`` chars. Never touches system/user messages, the model's own
    text, or the ``keep_recent`` newest tool results. Returns how many results
    were elided (0 if already within budget)."""
    total = sum(_message_chars(m) for m in messages)
    if total <= budget:
        return 0
    tool_idx = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    candidates = tool_idx[:-keep_recent] if keep_recent else tool_idx
    elided = 0
    for i in candidates:
        if total <= budget:
            break
        body = messages[i].get("content") or ""
        if body.startswith("[earlier tool output elided"):
            continue
        note = (f"[earlier tool output elided to keep this long session within "
                f"the context window: {len(body)} chars. Re-run the command if "
                f"you need it again.]")
        total -= len(body) - len(note)
        messages[i]["content"] = note
        elided += 1
    # Older echoed reasoning is the next largest thing and the least useful.
    if total > budget:
        asst = [i for i, m in enumerate(messages) if m.get("role") == "assistant"]
        for i in asst[:-2]:
            if total <= budget:
                break
            rc = messages[i].pop("reasoning_content", None)
            if isinstance(rc, str):
                total -= len(rc)
    # Then old tool-call arguments (heredoc file writes grow without bound).
    # Ids and the call/result pairing are kept; only the argument text goes.
    if total > budget:
        asst = [i for i, m in enumerate(messages)
                if m.get("role") == "assistant" and m.get("tool_calls")]
        for i in asst[:-keep_recent] if keep_recent else asst:
            if total <= budget:
                break
            for tc in messages[i]["tool_calls"]:
                fn = tc.get("function") or {}
                args = fn.get("arguments") or ""
                if len(args) <= 200 or args.startswith('{"elided"'):
                    continue
                stub = json.dumps({"elided": f"{len(args)} chars of earlier arguments"})
                total -= len(args) - len(stub)
                fn["arguments"] = stub
    return elided


def _is_context_overflow(message: str) -> bool:
    m = (message or "").lower()
    return ("http 400" in m or "http 413" in m or "provider error" in m) and any(
        h in m for h in _CONTEXT_OVERFLOW_HINTS
    )


# Plan/key exhaustion: the signal to fail over to a caller-supplied fallback
# endpoint (kimi_runner: Kimi Code plan -> TokenHub). Auth failures count too,
# so a revoked or rotated-away primary key also falls through.
_LIMIT_STATUS_RE = re.compile(r"\bHTTP (401|402|403|429)\b", re.IGNORECASE)
_LIMIT_HINTS = (
    "rate limit", "rate_limit", "quota", "usage limit", "too many requests",
    "insufficient", "limit exceeded", "limit reached", "billing", "membership",
)


def _is_limit_error(message: str) -> bool:
    """True when a step error means the endpoint's plan or key is exhausted
    or refused (not a context overflow, not a transport failure)."""
    msg = message or ""
    if _is_context_overflow(msg):
        return False
    m = msg.lower()
    return bool(_LIMIT_STATUS_RE.search(msg)) or any(h in m for h in _LIMIT_HINTS)


def _duplicate_unit_fraction(window: str) -> tuple[float, int]:
    """(fraction of short sentence units that are exact repeats, unit count)
    over ``window``. Fenced code is dropped and only punctuation-terminated
    units are counted -- see the notes above."""
    text = _REP_FENCE.sub(" ", window)
    units: list[str] = []
    for raw in _REP_SPLIT.split(text):
        raw = raw.strip()
        if not _REP_TERMINAL.search(raw):
            continue
        u = _REP_NORM.sub("", raw.lower())
        if 2 <= len(u) <= _REP_MAX_UNIT_LEN:
            units.append(u)
    if len(units) < _REP_MIN_UNITS:
        return 0.0, len(units)
    return 1.0 - len(set(units)) / len(units), len(units)


class _RepetitionGuard:
    """Incremental degenerate-output detector over one streamed text.

    ``feed(chunk)`` returns True the moment repetition is confirmed
    (``_REP_STRIKES`` consecutive checks at/above ``_REP_THRESHOLD``).
    ``cut_at`` is then the length of text to KEEP: everything from the first
    striking window onward is the loop.
    """

    def __init__(self) -> None:
        self._tail = ""
        self._total = 0
        self._since_check = 0
        self._strikes = 0
        self._first_strike_at: int | None = None
        self.cut_at: int | None = None
        self.tripped = False

    def feed(self, chunk: str) -> bool:
        if self.tripped or not chunk:
            return self.tripped
        self._total += len(chunk)
        self._since_check += len(chunk)
        self._tail = (self._tail + chunk)[-_REP_WINDOW:]
        if self._since_check < _REP_CHECK_EVERY or len(self._tail) < _REP_WINDOW:
            return False
        self._since_check = 0
        frac, _n = _duplicate_unit_fraction(self._tail)
        if frac >= _REP_THRESHOLD:
            self._strikes += 1
            if self._first_strike_at is None:
                self._first_strike_at = max(0, self._total - _REP_WINDOW)
            if self._strikes >= _REP_STRIKES:
                self.tripped = True
                self.cut_at = self._first_strike_at
        else:
            self._strikes = 0
            self._first_strike_at = None
        return self.tripped
_MAX_TOOL_OUTPUT = 16000     # chars of tool output fed back to the model

# Generous read timeout: thinking models lag before first token.
# read= is a silence detector between streamed chunks, not a turn budget. 600s:
# a max-effort reasoning step can stay quiet for minutes before its first
# visible token on providers that don't stream reasoning deltas.
_REQUEST_TIMEOUT = httpx.Timeout(connect=10.0, read=600.0, write=30.0, pool=10.0)

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
        # Text seen since the last <think>: discarded when the tag closes,
        # returned verbatim by flush() if it never does (the model was
        # quoting the tag, not reasoning).
        self._hidden = ""
        # Hidden text not yet handed out by drain_thought(): the caller
        # streams it as ``reasoning`` events so a <think>-tag model's
        # thinking is visible live, exactly like ``reasoning_content``.
        self._thought_new = ""

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
                self._hidden = ""
            else:
                idx = self._carry.find(_THINK_CLOSE)
                if idx == -1:
                    keep = len(_THINK_CLOSE) - 1
                    if len(self._carry) > keep:
                        self._hidden += self._carry[:-keep]
                        self._thought_new += self._carry[:-keep]
                        self._carry = self._carry[-keep:]
                    break
                self._hidden += self._carry[:idx]
                self._thought_new += self._carry[:idx]
                self._carry = self._carry[idx + len(_THINK_CLOSE):]
                self._inside = False
                self._hidden = ""
        return "".join(out)

    def drain_thought(self) -> str:
        """Return (and clear) <think> text captured since the last call."""
        t, self._thought_new = self._thought_new, ""
        return t

    def flush(self) -> str:
        tail = self._carry
        self._carry = ""
        if self._inside:
            # An unclosed <think> at end of stream is far more likely literal
            # text (the model quoting the tag, a code sample) than reasoning:
            # give the held text back rather than swallowing the reply.
            self._inside = False
            hidden, self._hidden = self._hidden, ""
            return _THINK_OPEN + hidden + tail
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
            cmd=["timeout", "-k", "5", str(_TOOL_TIMEOUT), "bash", "-lc", command],
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
            asyncio.get_running_loop().run_in_executor(_BASH_POOL, _run),
            timeout=_TOOL_TIMEOUT + 15
        )
    except asyncio.TimeoutError:
        return f"[run_bash timed out after {_TOOL_TIMEOUT}s]"
    except Exception as exc:  # noqa: BLE001
        logger.warning("run_bash exec failed in %s: %s", container_name, exc)
        return f"[run_bash failed: {type(exc).__name__}: {exc}]"

    if len(out) > _MAX_TOOL_OUTPUT:
        out = out[:_MAX_TOOL_OUTPUT] + f"\n[...output truncated at {_MAX_TOOL_OUTPUT} chars...]"
    if code in (124, 137):
        out += f"\n[command exceeded {_TOOL_TIMEOUT}s and was killed]"
    elif code not in (0, None) and out.strip():
        # Non-zero with output used to read as success to the model.
        out += f"\n[exit code {code}]"
    return out if out.strip() else f"(exit code {code}, no output)"


def provider_of(base_url: str) -> str:
    """Short provider tag for the token ledger, from the endpoint URL."""
    u = (base_url or "").lower()
    for needle, tag in (("tokenhub", "tokenhub"), ("xiaomimimo", "mimo"),
                        ("kimi", "kimi"),
                        ("haihub", "haihub"), ("localhost", "local"),
                        ("host.docker.internal", "local")):
        if needle in u:
            return tag
    return u.split("//", 1)[-1].split("/", 1)[0] or "openai-compatible"


async def _stream_step(
    client: httpx.AsyncClient,
    payload: dict[str, Any],
    *,
    base_url: str,
    api_key: str,
) -> AsyncIterator[dict[str, Any]]:
    """``_stream_step_raw`` plus a token-ledger row for the call. The row is
    written as the terminal sentinel passes (before it is yielded, so a
    consumer that stops right after it still gets it recorded), or from the
    ``finally`` when the stream is cancelled or closed early."""
    timer = token_ledger.OpenAICallTimer(provider_of(base_url), payload)
    try:
        async for ev in _stream_step_raw(client, payload, base_url=base_url, api_key=api_key):
            t = ev["type"]
            if t == "delta":
                timer.first_token()
                timer.out_chars += len(ev.get("text") or "")
            elif t == "reasoning":
                timer.first_token()
                timer.reasoning_chars += len(ev.get("text") or "")
            elif t == "_meta":
                timer.usage = ev.get("usage")
                timer.finish_reason = ev.get("finish_reason")
                timer.tool_calls = len(ev.get("tool_calls") or [])
                timer.finish("repetition" if ev.get("degenerate") else "ok")
            elif t == "_error":
                timer.finish("error", ev.get("message"))
            yield ev
    finally:
        timer.finish("cancelled")


async def _stream_step_raw(
    client: httpx.AsyncClient, payload: dict[str, Any],
    *, base_url: str = HAIHUB_BASE_URL, api_key: str = HAIHUB_API_KEY,
) -> AsyncIterator[dict[str, Any]]:
    """Stream one model call.

    Yields ``{"type":"delta","text":...}`` for visible content as it arrives,
    ``{"type":"reasoning","text":...}`` for hidden-reasoning text (OpenAI-style
    ``reasoning_content`` / ``reasoning`` delta fields, and ``<think>`` spans
    a model inlines into ``content``) so the UI can show thinking live,
    then exactly one terminal sentinel: ``{"type":"_meta","tool_calls":[...],
    "content":str}`` on success or ``{"type":"_error","message":str}`` on a
    non-200 / transport failure. Sentinels are consumed by ``run_turn`` and not
    forwarded to the worker.
    """
    stripper = _ThinkStripper()
    content_parts: list[str] = []
    # Degenerate-output guards, one per stream (a loop in hidden reasoning
    # burns the same budget as one in the reply).
    cguard = _RepetitionGuard()
    rguard = _RepetitionGuard()
    degenerate: str | None = None
    tool_acc: dict[int, dict[str, Any]] = {}
    id_slots: dict[str, int] = {}
    finish_reason: str | None = None
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
                yield {"type": "_error",
                       "message": f"haihub HTTP {resp.status_code}{_provider_error_suffix(body)}"}
                return
            # Split on "\n" ourselves: httpx's aiter_lines() uses str.splitlines(),
            # which also breaks on U+2028/U+2029/U+0085 — characters JSON does
            # not escape and models do emit — and a "data:" line split there
            # fails json.loads twice and silently drops the chunk.
            buf = b""
            stream_done = False
            async for raw_chunk in resp.aiter_bytes():
                buf += raw_chunk
                while not stream_done:
                    nl = buf.find(b"\n")
                    if nl == -1:
                        break
                    line = buf[:nl].decode("utf-8", "replace").rstrip("\r")
                    buf = buf[nl + 1:]
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        stream_done = True
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        logger.warning("haihub: undecodable SSE chunk (%d bytes) skipped", len(data))
                        continue
                    if not isinstance(obj, dict):
                        continue
                    # Gateways that fail after sending 200 headers report it
                    # in-band; without this the step looked like a clean empty
                    # reply.
                    err = obj.get("error")
                    if isinstance(err, dict) or (err and not obj.get("choices")):
                        msg = (err.get("message") if isinstance(err, dict) else str(err)) or "provider error"
                        yield {"type": "_error", "message": f"provider error: {str(msg)[:200]}"}
                        return
                    # The usage object rides on the final chunk before [DONE]
                    # (and that chunk usually has an empty ``choices`` list).
                    if isinstance(obj.get("usage"), dict):
                        usage = obj["usage"]
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    if choices[0].get("finish_reason"):
                        finish_reason = str(choices[0]["finish_reason"])
                    delta = choices[0].get("delta") or {}
                    # Hidden reasoning. Display-only downstream: it never
                    # joins ``content`` (so it can't leak into the reply or
                    # the history) and never changes what is requested.
                    rtext = delta.get("reasoning_content")
                    if not isinstance(rtext, str) or not rtext:
                        rtext = delta.get("reasoning")
                    if isinstance(rtext, str) and rtext:
                        yield {"type": "reasoning", "text": rtext}
                        if rguard.feed(rtext):
                            degenerate = "reasoning"
                            stream_done = True
                            break
                    ctext = delta.get("content")
                    if ctext:
                        clean = stripper.feed(ctext)
                        thought = stripper.drain_thought()
                        if thought:
                            yield {"type": "reasoning", "text": thought}
                            if rguard.feed(thought):
                                degenerate = "reasoning"
                                stream_done = True
                                break
                        if clean:
                            content_parts.append(clean)
                            yield {"type": "delta", "text": clean}
                            if cguard.feed(clean):
                                degenerate = "content"
                                stream_done = True
                                break
                    for pos, tc in enumerate(delta.get("tool_calls") or []):
                        idx = tc.get("index")
                        if idx is None:
                            # No index: key by call id so parallel calls streamed
                            # as whole objects don't all collapse into slot 0.
                            cid = tc.get("id")
                            idx = id_slots.setdefault(cid, len(tool_acc) + pos) if cid else len(tool_acc) + pos
                        slot = tool_acc.setdefault(idx, {"id": None, "name": None, "args": ""})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] = fn["name"]
                        if fn.get("arguments"):
                            slot["args"] += fn["arguments"]
                if stream_done:
                    break
    except Exception as exc:  # noqa: BLE001
        logger.exception("haihub stream failed")
        yield {"type": "_error", "message": f"haihub request failed: {type(exc).__name__}"}
        return

    if degenerate:
        # Leaving the `async with client.stream(...)` block above closed the
        # response, so the gateway stops generating. Report the step as
        # finish_reason="repetition": run_turn retries it once (low effort,
        # wrap-up nudge, no tools). Content is trimmed to what preceded the
        # loop; tool calls parsed alongside a degenerate stream are not
        # trusted.
        # The stripper may hold back a few chars pending a possible tag;
        # they are real text and belong to the step (before any trim).
        # If the loop was inside an unclosed <think>, flush() would return the
        # whole hidden reasoning as visible text: drop it instead.
        tail = "" if stripper._inside else stripper.flush()
        if tail:
            content_parts.append(tail)
            yield {"type": "delta", "text": tail}
        content = "".join(content_parts)
        if degenerate == "content" and cguard.cut_at is not None:
            content = content[:cguard.cut_at]
        logger.warning(
            "haihub: %s stream degenerated into repetition (%s) after %d chars; step cut",
            payload.get("model"), degenerate, len("".join(content_parts)),
        )
        yield {
            "type": "_meta",
            "tool_calls": [],
            "content": content,
            "usage": usage,
            "finish_reason": "repetition",
            "degenerate": degenerate,
        }
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
        "finish_reason": finish_reason,
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
    fallback: dict[str, str] | None = None,
    on_failover: Callable[[str], None] | None = None,
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

    ``fallback`` (``{"base_url", "api_key", "model"}``) is a second endpoint
    for the same model: when a step fails with a limit/auth error before
    any output, the rest of the turn moves there (once) and the step is
    redone. ``on_failover(message)`` is called when that happens.
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

    # Visible text of every model step, in order. The reply the user keeps is
    # ALL of it joined with blank lines — not just the last step's content —
    # so narration streamed between tool calls survives into the persisted
    # message, and a final step that only calls tools / says nothing does not
    # wipe the reply ("No reply — this turn produced no text").
    step_texts: list[str] = []
    final_text = ""
    length_retried = False
    repetition_retried = False
    # Set by the repetition branch below: the NEXT step is a wrap-up call
    # (low effort, this nudge appended, tools removed). The payload is
    # rebuilt every step, so the override lives here rather than in it.
    wrap_up_nudge: str | None = None
    context_budget = _CONTEXT_CHAR_BUDGET
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
                if _MAX_STEPS and _step >= _MAX_STEPS:
                    # Budget exhausted: one last call with tools removed so
                    # the user gets an answer from the completed work.
                    cap_payload: dict[str, Any] = {
                        "model": display,
                        "stream": True,
                        "max_tokens": max_tokens or _MAX_TOKENS,
                        "messages": messages + [{"role": "system", "content": _CAP_NUDGE}],
                        "stream_options": {"include_usage": True},
                    }
                    if effort:
                        cap_payload["reasoning_effort"] = effort
                    meta = None
                    step_started = False
                    async for ev in _stream_step(client, cap_payload, base_url=base, api_key=key):
                        t = ev["type"]
                        if t == "delta":
                            if not step_started:
                                step_started = True
                                if step_texts:
                                    yield {"type": "delta", "text": "\n\n"}
                            yield ev
                        elif t == "reasoning":
                            yield ev
                        elif t == "_error":
                            yield {"type": "error", "message": ev["message"]}
                            return
                        elif t == "_meta":
                            meta = ev
                    if meta is not None:
                        cap_usage = meta.get("usage")
                        if isinstance(cap_usage, dict):
                            saw_usage = True
                            acc_prompt += int(cap_usage.get("prompt_tokens") or 0)
                            acc_completion += int(cap_usage.get("completion_tokens") or 0)
                        if meta["content"].strip():
                            step_texts.append(meta["content"])
                    cap_note = (
                        f"[tool-call limit reached after {_MAX_STEPS} calls — "
                        "answered from the work completed so far; ask to continue "
                        "for the rest]"
                    )
                    if step_texts:
                        yield {"type": "delta", "text": "\n\n"}
                    yield {"type": "delta", "text": cap_note}
                    step_texts.append(cap_note)
                    break
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
                if use_tools and wrap_up_nudge is None:
                    payload["tools"] = _TOOLS
                    payload["tool_choice"] = "auto"
                # OpenAI-style reasoning-effort control. Callers pre-validate
                # the level against the model's supported set (app.py), so
                # an unset/empty value simply means "provider default".
                pending_nudge = wrap_up_nudge
                if wrap_up_nudge is not None:
                    payload["reasoning_effort"] = _RETRY_EFFORT
                    payload["messages"] = messages + [
                        {"role": "system", "content": wrap_up_nudge},
                    ]
                    wrap_up_nudge = None
                elif effort:
                    payload["reasoning_effort"] = effort

                # Keep the growing tool history inside the context window
                # (no step cap any more -- see _CONTEXT_CHAR_BUDGET).
                n_el = _compact_tool_history(messages, context_budget)
                if n_el:
                    # In place: payload["messages"] holds the same dicts
                    # (a wrap-up payload is messages + [nudge]).
                    logger.info("haihub: %s step %d: elided %d old tool result(s) to fit %d chars",
                                display, _step, n_el, context_budget)

                meta: dict[str, Any] | None = None
                step_started = False
                overflow_retry = False
                failover_retry = False
                async for ev in _stream_step(client, payload, base_url=base, api_key=key):
                    t = ev["type"]
                    if t == "delta":
                        if not step_started:
                            step_started = True
                            # Separate this step's text from the previous
                            # step's in the live stream exactly as the
                            # persisted join below does.
                            if step_texts:
                                yield {"type": "delta", "text": "\n\n"}
                        yield ev
                    elif t == "reasoning":
                        # Hidden reasoning, streamed for display only. Not
                        # part of step_texts / the persisted reply.
                        yield ev
                    elif t == "_error":
                        if (not step_started and _is_context_overflow(ev["message"])
                                and context_budget > _CONTEXT_MIN_BUDGET):
                            # The provider rejected the request for length:
                            # shrink the budget and redo this step rather
                            # than ending a long session.
                            # Size from what was actually rejected, not the
                            # budget: the request may already sit well under it.
                            _sent = sum(_message_chars(m) for m in messages)
                            context_budget = max(_CONTEXT_MIN_BUDGET,
                                                 min(context_budget // 2, int(_sent * 0.6)))
                            logger.warning(
                                "haihub: %s context overflow (%s); compacting to %d chars and retrying",
                                display, ev["message"][:160], context_budget,
                            )
                            _compact_tool_history(messages, context_budget, keep_recent=2)
                            overflow_retry = True
                            break
                        if (not step_started and fallback is not None
                                and _is_limit_error(ev["message"])):
                            # Primary plan/key exhausted: finish the turn on
                            # the fallback endpoint (same model, other key).
                            logger.warning(
                                "haihub: %s failed on %s (%s); failing over to %s model=%s",
                                display, base, ev["message"][:160],
                                fallback["base_url"], fallback["model"],
                            )
                            base = fallback["base_url"].rstrip("/")
                            key = fallback["api_key"]
                            display = fallback["model"]
                            fallback = None
                            if on_failover is not None:
                                try:
                                    on_failover(ev["message"])
                                except Exception:
                                    logger.exception("haihub: on_failover callback failed")
                            failover_retry = True
                            break
                        yield {"type": "error", "message": ev["message"]}
                        return
                    elif t == "_meta":
                        meta = ev
                if overflow_retry or failover_retry:
                    # Retry the SAME step: a wrap-up stays a wrap-up.
                    wrap_up_nudge = pending_nudge
                    continue
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
                finish = meta.get("finish_reason")

                if finish is None and not tool_calls and not step_content.strip():
                    yield {"type": "error", "message": f"{display}: stream ended without a reply or finish reason"}
                    return

                if finish == "repetition":
                    # DEGENERATE OUTPUT (see _RepetitionGuard). The step's
                    # content was trimmed to what preceded the loop; the live
                    # stream already showed the loop, and ``done``'s
                    # full_text replaces it on the client. Retry ONCE as a
                    # wrap-up (low effort, nudge, no tools) so the user gets
                    # an answer from the work completed; a second
                    # degeneration gives up with a visible notice.
                    logger.warning(
                        "haihub: %s step degenerated (%s); %s",
                        display, meta.get("degenerate"),
                        "giving up" if repetition_retried else "retrying as a wrap-up",
                    )
                    tool_calls = []
                    if step_texts or step_content.strip():
                        yield {"type": "delta", "text": "\n\n"}
                    yield {"type": "delta", "text": _REPETITION_NOTICE}
                    step_texts.append(
                        (step_content.rstrip() + "\n\n" + _REPETITION_NOTICE)
                        if step_content.strip() else _REPETITION_NOTICE
                    )
                    if repetition_retried:
                        yield {"type": "delta", "text": "\n\n" + _REPETITION_GIVEUP_NOTICE}
                        step_texts.append(_REPETITION_GIVEUP_NOTICE)
                        break
                    repetition_retried = True
                    wrap_up_nudge = _REPETITION_RETRY_NUDGE
                    continue

                if finish == "length" and not tool_calls and not step_content.strip():
                    # The whole output budget went to hidden reasoning: the
                    # user would get a blank bubble. Retry once at low effort
                    # with an explicit nudge; if that also comes back empty,
                    # say so instead of persisting nothing.
                    if not length_retried:
                        length_retried = True
                        logger.warning(
                            "haihub: %s returned no text (finish_reason=length, "
                            "%s completion tokens); retrying at effort=%s",
                            display, (step_usage or {}).get("completion_tokens"),
                            _RETRY_EFFORT,
                        )
                        payload["reasoning_effort"] = _RETRY_EFFORT
                        payload["messages"] = messages + [
                            {"role": "system", "content": _LENGTH_RETRY_NUDGE},
                        ]
                        payload.pop("tools", None)
                        payload.pop("tool_choice", None)
                        meta = None
                        step_started = False
                        async for ev in _stream_step(client, payload, base_url=base, api_key=key):
                            t = ev["type"]
                            if t == "delta":
                                if not step_started:
                                    step_started = True
                                    if step_texts:
                                        yield {"type": "delta", "text": "\n\n"}
                                yield ev
                            elif t == "reasoning":
                                yield ev
                            elif t == "_error":
                                yield {"type": "error", "message": ev["message"]}
                                return
                            elif t == "_meta":
                                meta = ev
                        if meta is None:
                            yield {"type": "error", "message": "haihub: empty response stream"}
                            return
                        retry_usage = meta.get("usage")
                        if isinstance(retry_usage, dict):
                            acc_prompt += int(retry_usage.get("prompt_tokens") or 0)
                            acc_completion += int(retry_usage.get("completion_tokens") or 0)
                        step_content = meta["content"]
                        finish = meta.get("finish_reason")
                        tool_calls = []
                    if not step_content.strip():
                        if step_texts:
                            yield {"type": "delta", "text": "\n\n"}
                        yield {"type": "delta", "text": _NO_REPLY_NOTICE}
                        step_texts.append(_NO_REPLY_NOTICE)
                        break

                if step_content:
                    if finish == "length":
                        yield {"type": "delta", "text": "\n\n" + _TRUNCATED_NOTICE}
                        step_content = step_content.rstrip() + "\n\n" + _TRUNCATED_NOTICE
                    step_texts.append(step_content)

                if not tool_calls:
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

    final_text = "\n\n".join(step_texts)
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
