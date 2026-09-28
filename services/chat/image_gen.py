"""Gemini image-generation client.

One async function ``generate_image(prompt)`` that calls the Gemini
``gemini-2.5-flash-image`` model with the prompt and returns the first
inline-image part as ``(bytes, mime_type)``.

The model is multimodal — its response can include both text and image
parts. We pick the first ``inlineData`` part and discard text. If the
model returns no image (rare; happens when prompt is filtered), the
function raises ``ImageGenError`` with the model's text reply, which
the worker turns into a user-visible error event.

Why it's a separate module from ``claude_runner``:
  * Different transport (HTTP vs subprocess), different auth (API key
    vs OAuth), different error model.
  * Tests can monkeypatch this seam without touching the claude path.

Env:
  * ``GEMINI_API_KEY`` — required. Read at call time (not import) so
    the chat service still imports cleanly when the key is absent.
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Any

import httpx


logger = logging.getLogger("chat.image_gen")


_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/"
    "models/gemini-2.5-flash-image:generateContent"
)
_DEFAULT_TIMEOUT = 120.0


class ImageGenError(Exception):
    """Raised for any client-correctable failure: missing key, HTTP
    non-2xx, no image in the response. ``message`` is safe to surface
    to the user (no key material, no internal stack)."""


def _gemini_key() -> str:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise ImageGenError(
            "image generation is not configured (GEMINI_API_KEY unset)"
        )
    return key


async def generate_image(prompt: str, *, timeout: float = _DEFAULT_TIMEOUT) -> tuple[bytes, str]:
    """Call Gemini and return the first inline image as ``(bytes, mime)``.

    Raises ``ImageGenError`` for missing key, HTTP failures, malformed
    responses, and prompt-filtered responses. Caller is expected to
    translate that into an SSE ``error`` event.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise ImageGenError("prompt must be a non-empty string")

    key = _gemini_key()
    body = {
        "contents": [{"parts": [{"text": prompt.strip()}]}],
        "generationConfig": {"responseModalities": ["IMAGE", "TEXT"]},
    }
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": key,
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(_API_URL, headers=headers, json=body)
    except httpx.HTTPError as exc:
        raise ImageGenError(f"upstream request failed: {exc}") from exc

    if resp.status_code != 200:
        # Try to surface the upstream error message; fall back to status.
        upstream_msg = ""
        try:
            j = resp.json()
            err = j.get("error") if isinstance(j, dict) else None
            if isinstance(err, dict):
                upstream_msg = str(err.get("message") or err.get("status") or "")
        except Exception:
            upstream_msg = ""
        raise ImageGenError(
            f"gemini returned HTTP {resp.status_code}"
            + (f": {upstream_msg}" if upstream_msg else "")
        )

    try:
        payload: dict[str, Any] = resp.json()
    except ValueError as exc:
        raise ImageGenError(f"gemini response was not JSON: {exc}") from exc

    # Walk candidates → content.parts → inlineData.
    candidates = payload.get("candidates") or []
    text_replies: list[str] = []
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        parts = (cand.get("content") or {}).get("parts") or []
        for p in parts:
            if not isinstance(p, dict):
                continue
            if "inlineData" in p and isinstance(p["inlineData"], dict):
                ind = p["inlineData"]
                data_b64 = ind.get("data")
                mime = ind.get("mimeType") or "image/png"
                if not isinstance(data_b64, str) or not data_b64:
                    continue
                try:
                    raw = base64.b64decode(data_b64)
                except Exception as exc:
                    raise ImageGenError(
                        f"gemini inline data was not valid base64: {exc}"
                    ) from exc
                return raw, mime
            elif "text" in p and isinstance(p["text"], str):
                text_replies.append(p["text"])

    # No image in any candidate. Surface the model's text reply (often
    # explains the refusal — content policy / unsupported prompt).
    msg = " ".join(t.strip() for t in text_replies if t.strip()).strip()
    raise ImageGenError(
        f"gemini returned no image{f': {msg[:300]}' if msg else ''}"
    )


__all__ = ["ImageGenError", "generate_image"]
