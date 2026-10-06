"""read_file tool for the chat tool-loop: turn any file into model-consumable text.

The chat's API runners (kimi / glm / qwen / deepseek / minimax / mimo /
gemma4-local) drive an OpenAI-compatible loop whose only tool was
``run_bash``. run_bash returns TEXT, so any attachment a model cannot
decode from bytes was effectively invisible:

  * raster images       -> handled separately (inlined as image parts on
                           vision-capable endpoints, or pre-converted to a
                           Gemini description for text-only ones — see
                           vision.py). This module is the fallback + the
                           on-demand "zoom into one image" path.
  * PDFs with a text layer        -> pypdf extraction (host-side)
  * scanned PDFs (no text layer)  -> Gemini vision transcription
  * Office docs (docx/pptx/xlsx)  -> stdlib zip+XML extraction
  * audio / video                 -> Gemini multimodal transcription
  * archives (zip/tar/gz)         -> content listing
  * directories                   -> ls-style listing
  * text / code / data            -> verbatim (capped)
  * unknown binaries              -> file(1)-style identification + hex head

The tool runs CHAT-BACKEND-side (not inside the per-user sandbox): the
sandbox has no pdftotext / tesseract / whisper / office renderer, and the
multimodal fallback needs the backend's GEMINI_API_KEY (never injected into
the sandbox). The model passes the sandbox path it was given in the
attachment preamble (e.g. ``/workspace/.attachments/<sid>/report.pdf``);
this module maps that back to the chat-side attachments dir.

Security: the tool only resolves paths under the current session's
attachments dir (or, when none, the workdir root) — a model cannot use it
to read arbitrary host files. All file reads are size-capped before any
base64/network work.
"""
from __future__ import annotations

import base64
import logging
import os
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# gemini-flash-latest tracks the current flash model — pinned versioned
# names (gemini-2.5-flash) get retired and start 404ing. Override with
# CHAT_READFILE_VISION_MODEL.
_VISION_MODEL = os.environ.get("CHAT_READFILE_VISION_MODEL", "gemini-flash-latest")
_GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/"
    f"models/{_VISION_MODEL}:generateContent"
)
_GEMINI_MAX_BYTES = 18 * 1024 * 1024   # inline-request ceiling
_TEXT_MAX = 30000                      # chars of file text fed back per call
_LIST_MAX = 500                        # dir/archive entries fed back
_VISION_TIMEOUT = 180.0

_IMAGE_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
    ".tiff": "image/tiff", ".tif": "image/tiff", ".heic": "image/heic",
    ".heif": "image/heif", ".avif": "image/avif",
}
_AUDIO_MIME = {
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
    ".ogg": "audio/ogg", ".oga": "audio/ogg", ".flac": "audio/flac",
    ".aac": "audio/aac", ".opus": "audio/ogg",
}
_VIDEO_MIME = {
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
    ".mkv": "video/x-matroska", ".avi": "video/x-msvideo", ".m4v": "video/mp4",
}
_OFFICE_EXTS = {".docx", ".docm", ".pptx", ".pptm", ".xlsx", ".xlsm"}

_PROMPTS = {
    "image": (
        "You are the eyes of a text-only model working with this image. "
        "Describe it thoroughly: transcribe ALL visible text verbatim "
        "(preserve rough layout), render any tables as markdown, describe "
        "charts/diagrams (axes, values, trends), UI elements, and anything "
        "else a reader would need. Be complete and literal."
    ),
    "audio": (
        "You are the ears of a text-only model working with this audio. "
        "Transcribe all speech verbatim, then describe any non-speech "
        "sounds, tone, and context."
    ),
    "video": (
        "You are the eyes of a text-only model working with this video. "
        "Describe it: scenes and key events with rough timestamps, "
        "transcribe on-screen text and speech verbatim, describe charts or "
        "UI shown."
    ),
    "pdf": (
        "You are the eyes of a text-only model working with this scanned "
        "PDF. Transcribe each page's text verbatim (preserve rough layout), "
        "render tables as markdown, and describe any figures."
    ),
}


def _gemini_key() -> str | None:
    return os.environ.get("GEMINI_API_KEY", "").strip() or None


def _gemini_file_to_text(data: bytes, mime: str, prompt: str, api_key: str) -> str:
    """Synchronous Gemini multimodal call; returns the text reply (httpx)."""
    import httpx
    body = {
        "contents": [{"parts": [
            {"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode("ascii")}},
            {"text": prompt},
        ]}],
    }
    resp = httpx.post(
        _GEMINI_URL, params={"key": api_key}, json=body, timeout=_VISION_TIMEOUT,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"gemini HTTP {resp.status_code}: {resp.text[:200]}")
    payload = resp.json()
    bits: list[str] = []
    for c in payload.get("candidates") or []:
        for part in (c.get("content") or {}).get("parts") or []:
            t = part.get("text")
            if t:
                bits.append(t)
    if not bits:
        raise RuntimeError(f"gemini returned no text: {str(payload)[:200]}")
    return "".join(bits)


def _unescape(text: str) -> str:
    import html
    return html.unescape(text)


def _office_text(path: Path) -> str:
    """Stdlib zip+XML text extraction for docx / pptx / xlsx."""
    import zipfile
    ext = path.suffix.lower()
    out: list[str] = []
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        if ext in (".docx", ".docm"):
            txt = z.read("word/document.xml").decode("utf-8", "replace")
            txt = re.sub(r"<w:tab\b[^>]*/?>", "\t", txt)
            txt = re.sub(r"<w:br\b[^>]*/?>", "\n", txt)
            txt = re.sub(r"</w:p>", "\n", txt)
            txt = re.sub(r"<[^>]+>", "", txt)
            out.append(_unescape(txt))
        elif ext in (".pptx", ".pptm"):
            slides = sorted(
                (n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                key=lambda n: int(re.search(r"\d+", n.rsplit("/", 1)[1]).group()),
            )
            for i, name in enumerate(slides, 1):
                txt = z.read(name).decode("utf-8", "replace")
                texts = [_unescape(t) for t in re.findall(r"<a:t>(.*?)</a:t>", txt, re.S)]
                out.append(f"--- slide {i} ---\n" + "\n".join(texts))
        else:  # xlsx / xlsm
            shared: list[str] = []
            if "xl/sharedStrings.xml" in names:
                sx = z.read("xl/sharedStrings.xml").decode("utf-8", "replace")
                for si in re.findall(r"<si>(.*?)</si>", sx, re.S):
                    shared.append(_unescape(
                        "".join(re.findall(r"<t[^>]*>(.*?)</t>", si, re.S))))
            wb = z.read("xl/workbook.xml").decode("utf-8", "replace")
            sheet_names = [_unescape(n) for n in
                           re.findall(r'<sheet[^>]*\bname="([^"]+)"', wb)]
            sheets = sorted(
                (n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)),
                key=lambda n: int(re.search(r"\d+", n.rsplit("/", 1)[1]).group()),
            )
            for i, name in enumerate(sheets):
                label = sheet_names[i] if i < len(sheet_names) else name
                out.append(f"--- sheet: {label} ---")
                sx = z.read(name).decode("utf-8", "replace")
                rows = re.findall(r"<row[^>]*>(.*?)</row>", sx, re.S)
                for row in rows[:2000]:
                    cells: list[str] = []
                    for attrs, bodyc in re.findall(r"<c\b([^>]*)>(.*?)</c>", row, re.S):
                        t = re.search(r'\bt="(\w+)"', attrs)
                        t = t.group(1) if t else ""
                        v = re.search(r"<v>(.*?)</v>", bodyc, re.S)
                        inline = re.search(r"<is>.*?<t[^>]*>(.*?)</t>.*?</is>", bodyc, re.S)
                        if t == "s" and v:
                            idx = int(v.group(1))
                            cells.append(shared[idx] if idx < len(shared) else "")
                        elif inline:
                            cells.append(_unescape(inline.group(1)))
                        elif v:
                            cells.append(_unescape(v.group(1)))
                        else:
                            cells.append("")
                    out.append("\t".join(cells).rstrip())
                if len(rows) > 2000:
                    out.append(f"[...sheet truncated at 2000 of {len(rows)} rows...]")
    return "\n".join(out)


def _pdf_text(path: Path) -> tuple[str, int]:
    """(extracted text, page count) via pypdf."""
    import pypdf
    reader = pypdf.PdfReader(str(path))
    parts = [(pg.extract_text() or "") for pg in reader.pages[:200]]
    return "\n".join(parts), len(reader.pages)


def _archive_listing(path: Path) -> str:
    import gzip
    import tarfile
    import zipfile
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
            lines = [f"zip archive: {len(infos)} entries"]
            lines += [f"{i.file_size:>10}  {i.filename}" for i in infos[:_LIST_MAX]]
            if len(infos) > _LIST_MAX:
                lines.append(f"[...{len(infos) - _LIST_MAX} more entries...]")
            return "\n".join(lines)
    if tarfile.is_tarfile(path):
        with tarfile.open(path) as t:
            members = t.getmembers()
            lines = [f"tar archive: {len(members)} entries"]
            lines += [f"{m.size:>10}  {m.name}" for m in members[:_LIST_MAX]]
            if len(members) > _LIST_MAX:
                lines.append(f"[...{len(members) - _LIST_MAX} more entries...]")
            return "\n".join(lines)
    if path.name.lower().endswith(".gz"):
        with gzip.open(path, "rb") as f:
            raw = f.read(_TEXT_MAX * 2)
        try:
            return raw.decode("utf-8")[:_TEXT_MAX]
        except UnicodeDecodeError:
            return f"[gzip-compressed binary, {len(raw)}+ bytes decompressed — use run_bash to inspect]"
    return ""


def _binary_info(path: Path) -> str:
    try:
        desc = subprocess.run(
            ["file", "-b", str(path)], capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except Exception:
        desc = ""
    try:
        head = path.read_bytes()[:256].hex(" ")
    except OSError:
        head = ""
    return (
        f"[binary file: {desc or 'unknown type'}]\nfirst 256 bytes (hex):\n{head}\n"
        "Use run_bash for deeper inspection (strings, xxd, etc.)."
    )


def _cap(text: str, what: str) -> str:
    if len(text) > _TEXT_MAX:
        return (text[:_TEXT_MAX] +
                f"\n[...{what} truncated at {_TEXT_MAX} chars — "
                "use run_bash (sed/grep/dd) to read further slices...]")
    return text


def _resolve_path(path_arg: str, allowed_root: Path | None) -> Path | None:
    """Resolve a model-supplied path to a real file, confined to allowed_root.

    Returns None when the path escapes the allowed root or does not exist.
    ``allowed_root`` is the session's chat-side attachments dir (or workdir).
    """
    p = Path(path_arg).expanduser()
    if not p.is_absolute():
        if allowed_root is None:
            return None
        p = allowed_root / p
    try:
        rp = p.resolve()
    except OSError:
        return None
    if allowed_root is not None:
        try:
            root = allowed_root.resolve()
        except OSError:
            return None
        if rp != root and root not in rp.parents:
            return None
    return rp if rp.exists() else None


def read_file(path_arg: str, question: str = "",
              allowed_root: Path | None = None) -> str:
    """The read_file tool: turn any file into text the model can consume.

    Synchronous; the runner wraps it in a thread. ``allowed_root`` confines
    resolution to the session's attachments dir (or workdir) — see module
    docstring.
    """
    if not isinstance(path_arg, str) or not path_arg.strip():
        return "[read_file: called without a 'path']"
    p = _resolve_path(path_arg.strip(), allowed_root)
    if p is None:
        return (f"[read_file: no such file (or outside the readable area): "
                f"{path_arg!r}]")
    if p.is_dir():
        try:
            entries = sorted(p.iterdir(), key=lambda e: e.name)
            lines = [f"directory listing of {p} ({len(entries)} entries):"]
            for e in entries[:_LIST_MAX]:
                kind = "d" if e.is_dir() else "f"
                try:
                    size = e.stat().st_size
                except OSError:
                    size = -1
                lines.append(f"{kind} {size:>12}  {e.name}")
            if len(entries) > _LIST_MAX:
                lines.append(f"[...{len(entries) - _LIST_MAX} more entries...]")
            return "\n".join(lines)
        except OSError as exc:
            return f"[read_file: cannot list {p}: {exc}]"
    try:
        size = p.stat().st_size
    except OSError as exc:
        return f"[read_file: cannot stat {p}: {exc}]"
    ext = p.suffix.lower()
    name_l = p.name.lower()
    header = f"[{p.name} — {size} bytes]"

    def _via_gemini(mime: str, kind: str) -> str:
        if size > _GEMINI_MAX_BYTES:
            return (f"[read_file: {p.name} is {size / 1e6:.1f} MB — too large for the "
                    f"vision model (cap {_GEMINI_MAX_BYTES // (1024 * 1024)} MB). "
                    "Use run_bash to slice/sample it first (e.g. ffmpeg frames for video).]")
        key = _gemini_key()
        if not key:
            return (f"[read_file: cannot interpret {kind} files — GEMINI_API_KEY is not "
                    "configured on the chat backend]")
        prompt = _PROMPTS[kind]
        if question:
            prompt += (f"\n\nThe reader's specific question: {question}\n"
                       "Answer it directly first, then give the full description/transcription.")
        try:
            data = p.read_bytes()
            text = _gemini_file_to_text(data, mime, prompt, key)
        except Exception as exc:  # noqa: BLE001
            logger.exception("read_file vision call failed for %s", p)
            return f"[read_file: vision model failed on {p.name}: {type(exc).__name__}: {exc}]"
        return _cap(text.strip(), "vision reply") or "[read_file: vision model returned no text]"

    try:
        if ext in _OFFICE_EXTS:
            text = _office_text(p)
            return header + "\n" + _cap(text.strip() or "(no extractable text)", "document text")
        if (ext in {".zip", ".jar", ".tar", ".tgz", ".tbz2", ".gz"}
                or name_l.endswith((".tar.gz", ".tar.bz2", ".tar.xz"))):
            listing = _archive_listing(p)
            if listing:
                return header + "\n" + listing
        if ext == ".pdf":
            text, npages = _pdf_text(p)
            if len(text.strip()) >= max(40, 10 * npages):
                return (f"{header} [{npages} page(s), text layer]\n"
                        + _cap(text.strip(), "pdf text"))
            return _via_gemini("application/pdf", "pdf")
        if ext in _IMAGE_MIME:
            return _via_gemini(_IMAGE_MIME[ext], "image")
        if ext in _AUDIO_MIME:
            return _via_gemini(_AUDIO_MIME[ext], "audio")
        if ext in _VIDEO_MIME:
            return _via_gemini(_VIDEO_MIME[ext], "video")
        raw = p.read_bytes()
        if b"\x00" not in raw[:8192]:
            try:
                return header + "\n" + _cap(raw.decode("utf-8"), "file text")
            except UnicodeDecodeError:
                pass
        return header + "\n" + _binary_info(p)
    except Exception as exc:  # noqa: BLE001
        logger.exception("read_file failed for %s", p)
        return f"[read_file: failed to read {p.name}: {type(exc).__name__}: {exc}]"


__all__ = ["read_file"]
