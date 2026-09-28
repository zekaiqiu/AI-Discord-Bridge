"""Per-session file attachments.

Layout:

    <CHAT_ATTACHMENTS_DIR>/<session_id>/<filename>

Notes for the next phases:
  * No email-slug nesting. Session ids are unguessable UUIDs and the
    storage layer already namespaces sessions per-email; a second slug
    nesting would just make backup/inspection awkward.
  * The session-deletion hook in ``app.py`` calls ``delete_attachments_dir``
    AFTER the storage delete succeeds, so a 404'd delete never touches
    attachments.
  * Phase 3's frontend will render attachment chips above the composer;
    the dict shape returned by ``save_uploads`` (``filename/size/mime``)
    is what the SPA expects, so don't change it without coordinating.

Validation philosophy: stream-and-count rather than trust ``content-length``.
A malicious client can lie in the header; the only honest measurement is
bytes-on-the-wire.
"""

from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

_PREVIEW_DIR_ENV = "CHAT_ATTACHMENT_PREVIEWS_DIR"
_DEFAULT_PREVIEW_DIR = "/data/attachment_previews"

if TYPE_CHECKING:  # avoid runtime import of fastapi from a "pure" module
    from fastapi import UploadFile

# ---------------------------------------------------------------------------
# Limits / whitelist. ``MAX_FILE_BYTES`` is enforced by streaming; do not
# bump it past the in-memory safety threshold without revisiting how the
# upload is read (currently we ``await f.read(chunk)`` so memory stays bounded
# regardless of file size).
# ---------------------------------------------------------------------------

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_FILES_PER_TURN = 5
ALLOWED_MIME_PREFIXES: tuple[str, ...] = ("image/", "text/", "audio/", "video/")
# Document/spreadsheet formats commonly attached in chat. Claude's Read
# tool handles PDFs natively (vision pass over rendered pages); the
# Office formats are zip+XML and the model unzips/parses via Bash tool
# calls when needed. CSV often arrives as application/vnd.ms-excel from
# browsers, so we list the spreadsheet mimes explicitly.
ALLOWED_MIME_EXACT: set[str] = {
    "application/pdf",
    "application/json",
    "application/xml",
    "application/zip",
    "application/x-tar",
    "application/gzip",
    "application/x-gzip",
    "application/vnd.ms-excel",                                                  # .xls
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",         # .xlsx
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",   # .docx
    "application/vnd.openxmlformats-officedocument.presentationml.presentation", # .pptx
    "application/msword",                                                        # .doc
    "application/vnd.ms-powerpoint",                                             # .ppt
    "application/vnd.oasis.opendocument.text",                                   # .odt
    "application/vnd.oasis.opendocument.spreadsheet",                            # .ods
    "application/octet-stream",  # browsers send this for unknown extensions
}

# Browser-supplied MIME types are unreliable (they vary by OS/browser and are
# often a bogus specific type instead of octet-stream), which used to 415 many
# perfectly ordinary files. So we ALSO accept by file extension: if the name
# ends in a known-good doc/data/code/media/archive extension we let it through
# even when the MIME isn't allow-listed. Real safety is the filename guard
# (no traversal / hidden files), the size cap, and the per-user sandbox — not
# this type filter — so a generous extension set is the right call.
ALLOWED_EXTENSIONS: frozenset[str] = frozenset({
    # images
    "png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff", "svg", "ico",
    "heic", "heif", "avif",
    # documents
    "pdf", "txt", "md", "markdown", "rst", "rtf", "tex", "doc", "docx", "odt",
    "epub",
    # spreadsheets / tabular
    "csv", "tsv", "xls", "xlsx", "ods", "parquet", "feather", "arrow",
    # presentations
    "ppt", "pptx", "odp", "key",
    # structured data
    "json", "jsonl", "ndjson", "xml", "yaml", "yml", "toml", "ini", "cfg",
    "conf", "properties", "env",
    # code / notebooks
    "py", "ipynb", "js", "jsx", "ts", "tsx", "mjs", "cjs", "c", "h", "cc",
    "cpp", "cxx", "hpp", "hh", "cs", "java", "kt", "kts", "go", "rs", "rb",
    "php", "swift", "scala", "sh", "bash", "zsh", "fish", "ps1", "bat", "lua",
    "pl", "pm", "r", "jl", "sql", "html", "htm", "css", "scss", "sass", "less",
    "vue", "svelte", "dart", "m", "mm", "gradle", "dockerfile", "makefile",
    # logs / plain
    "log", "out", "err", "diff", "patch",
    # archives
    "zip", "tar", "gz", "tgz", "bz2", "tbz2", "xz", "txz", "7z", "rar", "zst",
    # audio
    "mp3", "wav", "m4a", "aac", "ogg", "oga", "flac", "opus",
    # video
    "mp4", "mov", "m4v", "avi", "mkv", "webm", "wmv", "flv",
})

_DIR_MODE = 0o750
_FILE_MODE = 0o640
_READ_CHUNK = 64 * 1024

# Filename guard: reject anything that could escape the session dir.
# We reject on path separators, NULs, and leading dots (hidden files).
_BAD_FILENAME_CHARS = re.compile(r"[/\\\x00]")


class AttachmentError(Exception):
    """Raised by ``save_uploads`` for any client-correctable failure.

    The ``status_code`` attribute mirrors the HTTP status the caller should
    return: 400 (count), 413 (size), 415 (mime).
    """

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def _attachments_root() -> str:
    return os.environ.get("CHAT_ATTACHMENTS_DIR", "/data/attachments")


def session_attachments_dir(session_id: str) -> Path:
    """Return the per-session attachments path. Does not create it."""
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session_id must be a non-empty string")
    # Belt-and-braces: we never want a traversing session_id to reach the
    # filesystem here. The storage module's UUID gate is the primary
    # defence, but assert it independently.
    if "/" in session_id or "\\" in session_id or session_id in (".", "..") or session_id.startswith("."):
        raise ValueError("session_id must not contain path separators")
    return Path(_attachments_root()) / session_id


def has_attachments(session_id: str) -> bool:
    """True iff the per-session dir exists and contains at least one file."""
    try:
        d = session_attachments_dir(session_id)
    except ValueError:
        return False
    if not d.is_dir():
        return False
    try:
        return any(d.iterdir())
    except OSError:
        return False


def delete_attachments_dir(session_id: str) -> None:
    """Idempotent recursive purge. Never raises on a missing dir."""
    try:
        d = session_attachments_dir(session_id)
    except ValueError:
        return
    shutil.rmtree(d, ignore_errors=True)


def _previews_root() -> str:
    return os.environ.get(_PREVIEW_DIR_ENV, _DEFAULT_PREVIEW_DIR)


def session_preview_dir(session_id: str, seq: int) -> Path:
    """Return the per-(session, user-message-seq) preview dir. Does not create it.

    Preview files survive the per-turn purge that wipes ``session_attachments_dir``,
    so the SPA can render the user's uploaded files inline on the historical
    message bubble (claude.ai-style).
    """
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session_id must be a non-empty string")
    if "/" in session_id or "\\" in session_id or session_id in (".", "..") or session_id.startswith("."):
        raise ValueError("session_id must not contain path separators")
    if not isinstance(seq, int) or seq < 0:
        raise ValueError("seq must be a non-negative int")
    return Path(_previews_root()) / session_id / str(seq)


def stash_previews_for_seq(session_id: str, seq: int) -> list[str]:
    """Copy current session_attachments_dir contents into the per-seq preview dir.

    Called from the post_message handler immediately after the user message
    has been appended (so we know ``seq``) and before the worker's terminal
    runs ``delete_attachments_dir``. Returns the filenames copied. Best-effort:
    failure is logged by the caller, never raised.
    """
    src = session_attachments_dir(session_id)
    if not src.is_dir():
        return []
    dst = session_preview_dir(session_id, seq)
    dst.mkdir(parents=True, mode=_DIR_MODE, exist_ok=True)
    saved: list[str] = []
    for entry in src.iterdir():
        if not entry.is_file():
            continue
        try:
            shutil.copy2(entry, dst / entry.name)
            os.chmod(dst / entry.name, _FILE_MODE)
        except OSError:
            continue
        saved.append(entry.name)
    return saved


def delete_preview_dir(session_id: str) -> None:
    """Idempotent recursive purge of the per-session preview tree (all seqs)."""
    try:
        d = Path(_previews_root()) / session_id
    except (TypeError, ValueError):
        return
    shutil.rmtree(d, ignore_errors=True)


def _safe_filename(raw: str | None) -> str:
    """Reduce the client-supplied filename to a safe basename.

    Rules:
      * strip path components via ``Path(...).name``
      * reject ``/``, ``\\``, ``\x00``
      * reject leading ``.``
      * fall back to ``upload`` if nothing usable remains
    """
    if not raw:
        return "upload"
    base = Path(raw).name  # drops any directory component
    if not base or _BAD_FILENAME_CHARS.search(base):
        raise AttachmentError("invalid filename", status_code=400)
    if base.startswith("."):
        raise AttachmentError("invalid filename", status_code=400)
    return base


def _mime_allowed(mime: str | None) -> bool:
    if not mime:
        return False
    mime = mime.lower().split(";", 1)[0].strip()
    if mime in ALLOWED_MIME_EXACT:
        return True
    return any(mime.startswith(p) for p in ALLOWED_MIME_PREFIXES)


# Extensionless filenames we still want to accept (common repo files).
_ALLOWED_BARE_NAMES: frozenset[str] = frozenset({
    "dockerfile", "makefile", "readme", "license", "licence", "changelog",
    "authors", "contributors", "notice", "gitignore", "gitattributes",
})


def _ext_allowed(filename: str) -> bool:
    """Accept by file extension when the browser MIME is unreliable/unlisted."""
    name = filename.lower()
    if "." in name:
        ext = name.rsplit(".", 1)[-1]
        if ext in ALLOWED_EXTENSIONS:
            return True
    return name in _ALLOWED_BARE_NAMES


def _next_filename(target_dir: Path, filename: str) -> str:
    """Return ``filename`` if free, otherwise ``<unix-millis>_filename``."""
    if not (target_dir / filename).exists():
        return filename
    return f"{int(time.time() * 1000)}_{filename}"


async def save_uploads(
    session_id: str,
    files: list["UploadFile"],
) -> list[dict[str, Any]]:
    """Validate and persist a batch of uploads. See module docstring for limits.

    Atomicity note: on a per-file size overflow we delete the partial file
    before raising, so a rejected upload doesn't leave a half-written file
    on disk. We do NOT roll back files saved earlier in the same batch —
    the brief doesn't ask for batch atomicity, and the frontend retry
    semantics tolerate partial success.
    """
    if not isinstance(files, list):
        raise AttachmentError("files must be a list", status_code=400)
    if len(files) == 0:
        raise AttachmentError("no files provided", status_code=400)
    if len(files) > MAX_FILES_PER_TURN:
        raise AttachmentError(
            f"too many files (max {MAX_FILES_PER_TURN})",
            status_code=400,
        )

    target_dir = session_attachments_dir(session_id)
    target_dir.mkdir(parents=True, mode=_DIR_MODE, exist_ok=True)

    saved: list[dict[str, Any]] = []
    for upload in files:
        mime = (upload.content_type or "").strip()
        filename = _safe_filename(upload.filename)
        # No file-type restriction: EVERY format is accepted, including
        # unknown/unlisted ones. Safety comes from the filename guard (no
        # traversal / hidden files), the per-file size cap, and per-user
        # sandbox isolation — not from a type allow-list. (_mime_allowed /
        # _ext_allowed remain for metadata + other callers but no longer
        # gate uploads; the size cap below is still enforced.)
        final_name = _next_filename(target_dir, filename)
        final_path = target_dir / final_name

        size = 0
        # Write to the final path, but truncate on overflow before raising.
        with open(final_path, "wb") as out:
            while True:
                chunk = await upload.read(_READ_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_FILE_BYTES:
                    out.close()
                    try:
                        final_path.unlink()
                    except FileNotFoundError:
                        pass
                    raise AttachmentError(
                        f"file too large (max {MAX_FILE_BYTES} bytes)",
                        status_code=413,
                    )
                out.write(chunk)
        os.chmod(final_path, _FILE_MODE)
        # Reset the upload stream so the same UploadFile can be re-read by
        # later code (e.g. tests that inspect bytes); cheap and tidy.
        try:
            await upload.seek(0)
        except Exception:
            pass

        saved.append({
            "filename": final_name,
            "size": size,
            "mime": mime,
        })

    return saved


__all__ = [
    "ALLOWED_MIME_EXACT",
    "ALLOWED_MIME_PREFIXES",
    "AttachmentError",
    "MAX_FILES_PER_TURN",
    "MAX_FILE_BYTES",
    "delete_attachments_dir",
    "delete_preview_dir",
    "has_attachments",
    "save_uploads",
    "session_attachments_dir",
    "session_preview_dir",
    "stash_previews_for_seq",
]
