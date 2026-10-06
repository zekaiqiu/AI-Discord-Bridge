"""Inline image delivery for OpenAI-compatible (stateless) model turns.

The claude CLI path reads attachment images natively via its Read tool;
the API runners (haihub / kimi / tokenhub / mimo / local) historically
received only a TEXT preamble naming the files, so on any model without a
working image-capable shell toolchain — and even with one, after a decode
failure — the model answered without ever seeing the picture. This module
builds the multimodal ``content`` array (image_url data-URI parts + text
part) that the OpenAI chat-completions schema carries inline, for providers
probed to accept it.

Facts (probed live against each provider, 2026-10-05):
  * Kimi Code k3 / k3-256k / kimi-for-coding(-highspeed): /models declares
    input modalities text+image(+video); an image turn answers correctly.
  * TokenHub kimi-k3 and every haihub model (Qwen3.5-397B, DeepSeek-V4-Flash,
    MiniMax-M2.7) accept image_url parts and answer from the picture.
  * TokenHub glm-5.3 400s on any image_url part ("rejected by an internal
    MaaS component") — text-only, so it is NOT in the capability set.
    (Its images still arrive via the run_bash preamble + sandbox tooling.)
  * MiMo v2.6 flash/pro accept the payload and their reasoning references
    the image; visible captions are unreliable, so the preamble below
    tells the model the answer must be grounded in what it can see.

Safety / robustness rules:
  * Real bytes are sniffed, not the uploader's declared mime: a text or
    script file renamed ``.png`` must never become an image part (it would
    leak arbitrary file content into a data URI and confuse providers).
    The allowed raster types are the ones the sandbox Pillow can also
    decode for the text-only fallback path (PNG/JPEG/GIF/WebP).
  * SVG is intentionally never inlined (it's markup, not pixels; XSS-class
    content in a data URI).
  * A hard byte budget keeps base64 (4/3 expansion) from blowing the
    provider's request-size or context limits; oversized images stay
    reachable through the run_bash path instead.
"""
from __future__ import annotations

import base64
import logging
import mimetypes
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Total RAW bytes inlined per turn across all images (not per file): one
# 8 MP photo ~ 3 MB JPEG -> ~4 MB base64 ~ 1 M tokens territory is already
# absurd; 12 MB raw keeps worst-case requests well under provider limits
# while covering a full batch of normal photos/screenshots.
MAX_INLINE_BYTES_TOTAL = 12 * 1024 * 1024

_EXT_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".avif": "image/avif",
}


def sniff_image_mime(path: Path) -> str | None:
    """Mime of the ACTUAL bytes, or None when not an inlinable raster.

    Magic-byte check first (a renamed .txt must fail), then the extension
    must agree with the sniffed family (a JPEG named .png is served as
    image/jpeg only when the ext is a jpeg ext, etc.). Extension required
    at all: a magic-less or double-extension oddity stays text-path.
    """
    ext = path.suffix.lower()
    if ext not in _EXT_MIME:
        return None
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
    except OSError:
        return None
    sniffed: str | None = None
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        sniffed = "image/png"
    elif head.startswith(b"\xff\xd8\xff"):
        sniffed = "image/jpeg"
    elif head[:6] in (b"GIF87a", b"GIF89a"):
        sniffed = "image/gif"
    elif head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        sniffed = "image/webp"
    elif head.startswith(b"BM"):
        sniffed = "image/bmp"
    elif head[:4] in (b"II*\x00", b"MM\x00*"):
        sniffed = "image/tiff"
    elif head[4:12] in (b"ftypavif", b"ftypavis"):
        sniffed = "image/avif"
    if sniffed is None:
        return None
    # Extension family must match the sniffed family (jpeg/jpg are one).
    fam = lambda m: {"image/jpeg": "jpeg", "image/jpg": "jpeg",
                     "image/tiff": "tiff"}.get(m, m.split("/")[1])
    if fam(_EXT_MIME[ext]) != fam(sniffed):
        return None
    return sniffed


def _to_png(raw: bytes) -> bytes | None:
    """Transcode non-web raster bytes to PNG via Pillow; None on any failure.

    Kept import-lazy so importing vision.py never requires Pillow — only
    paths that actually meet a BMP/TIFF/AVIF pay the import (and Pillow is
    baked into the chat image, so that import is cheap there).
    Animated/multi-frame sources collapse to their first frame; that is the
    right trade for an analysis snapshot.
    """
    try:
        import io
        from PIL import Image
    except Exception:
        return None
    try:
        with Image.open(io.BytesIO(raw)) as im:
            if getattr(im, "is_animated", False):
                im.seek(0)
            out = io.BytesIO()
            # PNG has no CMYK/LA/P modes quirks-free path for every
            # provider; normalise to RGB/RGBA.
            if im.mode not in ("RGB", "RGBA", "L", "P"):
                im = im.convert("RGB")
            im.save(out, format="PNG")
            return out.getvalue()
    except Exception:
        return None


def build_content_parts(
    prompt: str, attachments_dir: Path,
) -> tuple[list[dict[str, Any]] | None, list[str]]:
    """``(content_parts, inlined_names)`` — None when nothing is inlinable.

    ``prompt`` is the FULLY-ASSEMBLED user text (preamble + history +
    persona + memory + language + artifacts block, per _build_user_prompt
    order) and becomes the single trailing text part, so part-based and
    string-based turns carry byte-identical instructions. Images are
    emitted first, in filename order, matching the visual-first reading
    order every provider documents.
    """
    try:
        files = sorted(p for p in attachments_dir.iterdir() if p.is_file())
    except OSError:
        return None, []
    parts: list[dict[str, Any]] = []
    names: list[str] = []
    budget = MAX_INLINE_BYTES_TOTAL
    for p in files:
        if budget <= 0:
            break
        mime = sniff_image_mime(p)
        if mime is None:
            continue
        try:
            size = p.stat().st_size
            if size == 0 or size > budget:
                continue
            raw = p.read_bytes()
        except OSError:
            continue
        # Normalise to a universally-decodable payload. The major
        # OpenAI-compatible gateways all take PNG/JPEG/GIF/WebP; BMP/TIFF/
        # AVIF are less certain on the wire, so anything Pillow can open
        # that is not already PNG/JPEG/GIF/WebP is transcoded to PNG.
        if mime not in ("image/png", "image/jpeg", "image/gif", "image/webp"):
            converted = _to_png(raw)
            if converted is None or len(converted) > budget:
                # Not transcodable here (no Pillow / decoder missing / the
                # PNG would overflow the budget): skip rather than send a
                # format the gateway may reject or silently misread.
                continue
            raw, mime, size = converted, "image/png", len(converted)
        budget -= size
        b64 = base64.b64encode(raw).decode("ascii")
        parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"},
        })
        names.append(p.name)
    if not parts:
        return None, []
    parts.append({"type": "text", "text": prompt})
    return parts, names


def inline_preamble_note(names: list[str]) -> str:
    """One system line so the model treats the pictures as input, not decor.

    Without this, some frontier models (observed: MiMo v2.6, Qwen3.5) see
    the image, reason about it in hidden thinking, then answer 'I can't
    view images' from their text-only training prior.
    """
    listing = ", ".join(names)
    return (
        f"[Vision: the user's message above includes {len(names)} attached "
        f"image(s) ({listing}), provided inline as image parts — you CAN "
        "see them directly. Describe and answer from their actual visual "
        "content; never claim you cannot view images, and do not answer "
        "about them from the filename alone.]"
    )

# ---------------------------------------------------------------------------
# OCR fallback for non-vision models (glm, deepseek, minimax)
# ---------------------------------------------------------------------------

_OCR_MODEL = "gemini-3.8-flash"  # Gemini vision model for OCR fallback
_OCR_TIMEOUT = 30.0


def _gemini_key() -> str | None:
    """Gemini API key from env; None when unset (OCR disabled)."""
    return os.environ.get("GEMINI_API_KEY", "").strip() or None


def describe_image_for_text_model(path: Path, question: str | None = None) -> str | None:
    """Use Gemini vision to describe an image for text-only models.

    Returns a text description like "The image shows: ..." or None on failure.
    This is the fallback for models that can't receive inline image parts
    (glm, deepseek, minimax) — their run_bash path can only return text,
    so we pre-convert the image to a description.

    ``question`` optionally focuses the description (e.g. "What text is in
    this image?"); otherwise a general description is produced.
    """
    key = _gemini_key()
    if not key:
        return None
    mime = sniff_image_mime(path)
    if mime is None:
        return None
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    # Transcode non-web formats to PNG for Gemini compatibility
    if mime not in ("image/png", "image/jpeg", "image/gif", "image/webp"):
        converted = _to_png(raw)
        if converted is None:
            return None
        raw, mime = converted, "image/png"
    b64 = base64.b64encode(raw).decode("ascii")

    prompt = question or "Describe this image in detail. What do you see?"
    try:
        import httpx
        resp = httpx.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{_OCR_MODEL}:generateContent",
            params={"key": key},
            json={
                "contents": [{
                    "parts": [
                        {"text": prompt},
                        {"inline_data": {"mime_type": mime, "data": b64}}
                    ]
                }]
            },
            timeout=_OCR_TIMEOUT,
        )
        if resp.status_code != 200:
            logger.warning("vision OCR: gemini HTTP %s", resp.status_code)
            return None
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return f"[Image description: {text}]"
    except Exception as exc:
        logger.warning("vision OCR failed: %s", exc)
        return None


def build_text_descriptions(
    attachments_dir: Path,
    question: str | None = None,
) -> list[tuple[str, str]]:
    """``[(filename, description), ...]`` for all sniffed raster images.

    Used by the runner when vision=False: instead of inline image parts,
    each image becomes a text block in the prompt. The model "reads" the
    image via its description.
    """
    try:
        files = sorted(p for p in attachments_dir.iterdir() if p.is_file())
    except OSError:
        return []
    out: list[tuple[str, str]] = []
    for p in files:
        if sniff_image_mime(p) is None:
            continue
        desc = describe_image_for_text_model(p, question)
        if desc:
            out.append((p.name, desc))
    return out
