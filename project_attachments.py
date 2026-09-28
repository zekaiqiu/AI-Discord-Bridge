"""Attachment extraction + brief assembly for the !project command.

The Discord-side download is async (one helper here, `read_attachments`),
but classification, extraction and brief assembly are pure functions so
the file-type matrix can be tested without Discord objects.

Brief format (matches the spec the user signed off on):

    <user's !project text, if any>

    ---
    Attached: <filename1>
    <content1 — fenced ``` for code, raw for text>
    ---
    Attached: <filename2>
    <content2>

If user provided no text the leading text block is omitted but the
"Attached:" structure is preserved.
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from typing import Iterable

log = logging.getLogger(__name__)

# Total inlined-text budget across all attachments. Anything beyond this
# is truncated with a marker so Lead Engineer's brief stays a manageable
# size — the spec calls for a 100 KB soft cap.
ATTACHMENT_BUDGET_BYTES = 100 * 1024

# Plain-text extensions: inline as-is, no fence.
TEXT_EXTS = {".md", ".txt", ".rst", ".adoc"}

# Code extensions: inline inside a fenced block with a language tag so the
# LLM has a strong signal that the content is code, not prose.
CODE_EXTS: dict[str, str] = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".go": "go", ".rs": "rust", ".java": "java",
    ".cpp": "cpp", ".c": "c", ".h": "c",
    ".sh": "bash", ".toml": "toml", ".yaml": "yaml", ".yml": "yaml",
    ".json": "json", ".html": "html", ".css": "css",
}

PDF_EXTS = {".pdf"}


@dataclass
class ProcessedAttachment:
    """One attachment after classification + extraction.

    `extracted_text` is the inlinable form (PDF text for PDFs, decoded
    bytes for text/code). `skip_reason` non-empty means the attachment is
    NOT inlined — caller surfaces it under "skipped:" in the Discord reply.
    """
    filename: str
    raw_bytes: int
    extracted_text: str
    lang_tag: str | None
    skip_reason: str | None


def classify(filename: str, content_type: str | None) -> tuple[str, str | None]:
    """Decide what to do with an attachment by extension first, MIME second.

    Returns (kind, lang_tag). kind ∈ {"text", "code", "pdf", "binary"}.
    lang_tag is the fenced-block language for "code", else None.
    """
    name = filename.lower()
    for ext in TEXT_EXTS:
        if name.endswith(ext):
            return "text", None
    for ext, lang in CODE_EXTS.items():
        if name.endswith(ext):
            return "code", lang
    for ext in PDF_EXTS:
        if name.endswith(ext):
            return "pdf", None
    if content_type and content_type.startswith("text/"):
        return "text", None
    return "binary", None


def extract_pdf_text(data: bytes) -> str:
    """Best-effort PDF text extraction. Empty string on any failure.

    pypdf is the only attempted backend; the caller treats an empty
    return as "extraction failed, mark as skipped".
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        log.warning("pypdf not installed; cannot extract PDF text")
        return ""
    try:
        reader = PdfReader(io.BytesIO(data))
        parts: list[str] = []
        for page in reader.pages:
            try:
                t = page.extract_text() or ""
            except Exception:
                t = ""
            if t.strip():
                parts.append(t)
        return "\n".join(parts)
    except Exception:
        log.exception("PDF extraction failed")
        return ""


def process_attachment(
    filename: str, data: bytes, content_type: str | None
) -> ProcessedAttachment:
    """Classify + extract one attachment to its inlinable text form.

    No size capping here — that's `build_brief`'s job, since the cap is a
    *total* budget and only that function knows the running total.
    """
    kind, lang = classify(filename, content_type)
    raw_bytes = len(data)
    if kind in ("text", "code"):
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            return ProcessedAttachment(filename, raw_bytes, "", None, "decode failed")
        return ProcessedAttachment(filename, raw_bytes, text, lang, None)
    if kind == "pdf":
        text = extract_pdf_text(data)
        if not text.strip():
            return ProcessedAttachment(
                filename, raw_bytes, "", None, "PDF extraction failed"
            )
        return ProcessedAttachment(filename, raw_bytes, text, None, None)
    # binary / unsupported
    return ProcessedAttachment(
        filename, raw_bytes, "", None, "binary, not supported in v1"
    )


def _format_block(
    att: ProcessedAttachment, body: str, *, truncated: bool
) -> str:
    """Render one attachment's section in the brief."""
    header = f"---\nAttached: {att.filename}\n"
    if att.lang_tag:
        body_block = f"```{att.lang_tag}\n{body}\n```"
    else:
        body_block = body
    if truncated:
        body_block = body_block + "\n[truncated]"
    return header + body_block


def _human(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    return f"{n / 1024:.1f} KB"


def build_brief(
    user_text: str,
    attachments: Iterable[ProcessedAttachment],
    *,
    budget: int = ATTACHMENT_BUDGET_BYTES,
) -> tuple[str, list[str], list[str]]:
    """Assemble the combined brief and reply-line summaries.

    Returns (brief, attached_summaries, skipped_summaries). The two
    summary lists drive the Discord reply ("attached: a, b, c" /
    "skipped: x (reason)"). Skipped attachments are inlined into the
    skipped list with their reason; included ones get a size summary.
    """
    parts: list[str] = []
    attached: list[str] = []
    skipped: list[str] = []
    if user_text and user_text.strip():
        parts.append(user_text.strip())

    used = 0
    dropped_count = 0
    for att in attachments:
        if att.skip_reason:
            skipped.append(f"{att.filename} ({att.skip_reason})")
            continue
        text = att.extracted_text
        room = budget - used
        if room <= 0:
            dropped_count += 1
            continue
        body = text
        truncated = False
        encoded = text.encode("utf-8")
        if len(encoded) > room:
            # Truncate at a UTF-8-safe byte boundary.
            body = encoded[:room].decode("utf-8", errors="ignore")
            truncated = True
        used += len(body.encode("utf-8"))
        parts.append(_format_block(att, body, truncated=truncated))

        extracted_size = len(text.encode("utf-8"))
        # When extraction shrank the file (PDFs), spell out both numbers.
        if att.lang_tag is None and extracted_size != att.raw_bytes and att.raw_bytes > 0:
            attached.append(
                f"{att.filename} ({_human(extracted_size)} extracted from {_human(att.raw_bytes)})"
            )
        else:
            attached.append(f"{att.filename} ({_human(att.raw_bytes)})")

    if dropped_count:
        parts.append(
            f"[truncated: {dropped_count} attachments dropped due to size cap]"
        )

    return "\n\n".join(parts), attached, skipped


# ---------- async I/O wrapper (Discord side) ----------


async def read_attachments(msg) -> list[ProcessedAttachment]:
    """Download every Discord attachment and process each in turn.

    Reads bytes into memory directly (no temp files); v1 caps at 10 MB
    per attachment via the existing MAX_ATTACHMENT_BYTES check on the
    Discord side. Failures are recorded as `skip_reason` rather than
    raised, so a bad attachment doesn't kill project creation.
    """
    out: list[ProcessedAttachment] = []
    for att in msg.attachments:
        try:
            data = await att.read()
        except Exception:
            log.exception("attachment %s download failed", att.filename)
            out.append(ProcessedAttachment(att.filename, 0, "", None, "download failed"))
            continue
        out.append(
            process_attachment(att.filename, data, getattr(att, "content_type", None))
        )
    return out
