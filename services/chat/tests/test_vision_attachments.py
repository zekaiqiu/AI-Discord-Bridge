"""Inline-vision delivery for the OpenAI-compatible (stateless) models.

The bug: a user attaches a picture and an API model (kimi/glm/qwen/deepseek/
minimax/mimo/local) got only a TEXT preamble naming the file — on a model
whose sandbox image tooling is missing/uncalled, the reply was written
without the model ever seeing the pixels ("I can't view images", or a
confident answer from the filename). The claude CLI path never had this
bug (its Read tool renders images natively).

Fix under test:
  * vision.sniff_image_mime / build_content_parts — only real raster bytes
    become image parts; renamed text/scripts, ext/content mismatches, SVG
    and oversize files stay on the run_bash path; a per-turn byte budget
    caps the inline payload.
  * haihub_runner.run_turn — on a vision-capable endpoint the first user
    message becomes a content array (image parts + the SAME text the
    string form would carry) and the system prompt gains the "you can see
    these" note; text-only turns are byte-identical to before.
  * _stream_step_raw — a provider that 400s the multimodal payload gets ONE
    demoted text-only retry instead of a failed turn.
  * app._api_model_turn_gen — the capability flag comes from the resolved
    runner module (TokenHub glm is probed text-only and must NOT inline).
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

import app as app_module
import claude_runner
import haihub_runner
import kimi_runner
import local_runner
import mimo_runner
import tokenhub_runner
import vision
from helpers import consume_sse, create_session

USER = "vision-user@example.com"

# 2x2 opaque-red PNG, and a 1x1 GIF — real raster bytes in tiny files.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAEklEQVR4nGP8"
    "z8DwnwEKmBgQAAA9+v8Bxf4ysAAAAABJRU5ErkJggg=="
)
_GIF = base64.b64decode(
    "R0lGODdhAQABAIAAAP///////ywAAAAAAQABAAACAkQBADs="
)

# 1x1 BMP / TIFF / AVIF (Pillow-generated on a dev host, frozen here so the
# suite needs no Pillow): real headers + pixels, tiny.
_BMP = base64.b64decode(
    "Qk06AAAAAAAAADYAAAAoAAAAAQAAAAEAAAABABgAAAAAAAQAAADEDgAAxA4AAAAAAAAAAAAAAAD/"
    "AA=="
)
_TIFF = base64.b64decode(
    "SUkqAAgAAAAKAAABBAABAAAAAQAAAAEBBAABAAAAAQAAAAIBAwADAAAAhgAAAAMBAwABAAAAAQAA"
    "AAYBAwABAAAAAgAAABEBBAABAAAAjAAAABUBAwABAAAAAwAAABYBBAABAAAAAQAAABcBBAABAAAA"
    "AwAAABwBAwABAAAAAQAAAAAAAAAIAAgACAD/AAA="
)
_AVIF = base64.b64decode(
    "AAAAIGZ0eXBhdmlmAAAAAGF2aWZtaWYxbWlhZk1BMUIAAADrbWV0YQAAAAAAAAAhaGRscgAAAAAA"
    "AAAAcGljdAAAAAAAAAAAAAAAAAAAAAAOcGl0bQAAAAAAAQAAAB5pbG9jAAAAAEQAAAEAAQAAAAEA"
    "AAETAAAAKgAAAChpaW5mAAAAAAABAAAAGmluZmUCAAAAAAEAAGF2MDFDb2xvcgAAAABqaXBycAAA"
    "AEtpcGNvAAAAFGlzcGUAAAAAAAAAAQAAAAEAAAAQcGl4aQAAAAADCAgIAAAADGF2MUOBAAwAAAAA"
    "E2NvbHJuY2x4AAEADQAGgAAAABdpcG1hAAAAAAAAAAEAAQQBAoMEAAAAMm1kYXQSAAoIGAAGiAho"
    "NCAyHBTHh4ZlAgggnlAAAABIWtlc1jIgMQsbXgqRN4A="
)


# ---------------------------------------------------------------- vision.py

def test_sniff_accepts_real_rasters(tmp_path: Path):
    (tmp_path / "a.png").write_bytes(_PNG)
    (tmp_path / "b.gif").write_bytes(_GIF)
    assert vision.sniff_image_mime(tmp_path / "a.png") == "image/png"
    assert vision.sniff_image_mime(tmp_path / "b.gif") == "image/gif"


def test_sniff_rejects_renamed_text_and_mismatch(tmp_path: Path):
    (tmp_path / "evil.png").write_bytes(b"#!/bin/sh\nrm -rf ~\n")
    (tmp_path / "mismatch.jpg").write_bytes(_PNG)          # png bytes, jpg ext
    (tmp_path / "vector.svg").write_bytes(b"<svg xmlns='x'/>")
    (tmp_path / "noext").write_bytes(_PNG)
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4 fake")
    for name in ("evil.png", "mismatch.jpg", "vector.svg", "noext", "doc.pdf"):
        assert vision.sniff_image_mime(tmp_path / name) is None, name


def test_content_parts_order_and_text_part(tmp_path: Path):
    (tmp_path / "a.png").write_bytes(_PNG)
    (tmp_path / "notes.txt").write_bytes(b"hello")
    parts, names = vision.build_content_parts("FULL PROMPT TEXT", tmp_path)
    assert names == ["a.png"]
    assert parts is not None and len(parts) == 2
    assert parts[0]["type"] == "image_url"
    assert parts[0]["image_url"]["url"].startswith("data:image/png;base64,")
    # The text part is the COMPLETE assembled prompt — same bytes the
    # string form would carry, so no instruction is lost in multimodal mode.
    assert parts[-1] == {"type": "text", "text": "FULL PROMPT TEXT"}


def test_content_parts_none_without_images(tmp_path: Path):
    (tmp_path / "notes.txt").write_bytes(b"hello")
    assert vision.build_content_parts("p", tmp_path) == (None, [])


def test_content_parts_respects_total_budget(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(vision, "MAX_INLINE_BYTES_TOTAL", len(_PNG) + 10)
    (tmp_path / "a.png").write_bytes(_PNG)
    big = bytearray(_PNG)
    big.extend(b"\x00" * 4096)  # valid header, oversized tail
    (tmp_path / "b.png").write_bytes(bytes(big))
    parts, names = vision.build_content_parts("p", tmp_path)
    assert names == ["a.png"], "over-budget image must be skipped, not truncate the batch"


def test_inline_preamble_note_names_files():
    note = vision.inline_preamble_note(["a.png", "b.gif"])
    assert "a.png" in note and "b.gif" in note
    assert "you CAN" in note


# ------------------------------------------------------- haihub_runner wiring

def _capture_payload(monkeypatch, status_first: int | None = None):
    """Replace httpx streaming with a recorder; returns {'payloads': [...]}.

    ``status_first`` (when set) makes the FIRST request return that HTTP
    status with an empty body, the rest a minimal one-chunk completion.
    """
    rec: dict[str, Any] = {"payloads": []}

    class _Resp:
        def __init__(self, status: int) -> None:
            self.status_code = status

        async def aread(self) -> bytes:
            return b'{"error":{"message":"bad request"}}'

        async def aiter_bytes(self):
            chunk = {
                "choices": [{
                    "delta": {"content": "seen"},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }
            yield b"data: " + json.dumps(chunk).encode() + b"\n\n"
            yield b"data: [DONE]\n\n"

    class _StreamCtx:
        def __init__(self, status: int) -> None:
            self._resp = _Resp(status)

        async def __aenter__(self) -> _Resp:
            return self._resp

        async def __aexit__(self, *a) -> None:
            return None

    class _Client:
        def __init__(self, **kw: Any) -> None:
            self._n = 0

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *a) -> None:
            return None

        def stream(self, method: str, url: str, *, headers: dict, json: dict):
            self._n += 1
            # Deep-copy: the runner DEMOTES the multimodal payload in place
            # before the retry, so recording the reference would show both
            # requests as the final (text-only) form.
            import copy as _copy
            rec["payloads"].append(_copy.deepcopy(json))
            status = status_first if (status_first and self._n == 1) else 200
            return _StreamCtx(status)

    monkeypatch.setattr(haihub_runner.httpx, "AsyncClient", _Client)
    return rec


async def _run(**kw):
    out = []
    async for ev in haihub_runner.run_turn(
        prompt="describe my image", model="qwen", api_key="k", **kw
    ):
        out.append(ev)
    return out


@pytest.mark.asyncio
async def test_vision_turn_sends_content_array(monkeypatch, tmp_path: Path):
    (tmp_path / "shot.png").write_bytes(_PNG)
    rec = _capture_payload(monkeypatch)
    events = await _run(attachments_dir=tmp_path, vision=True)
    assert any(e["type"] == "delta" for e in events), events
    payload = rec["payloads"][0]
    user = payload["messages"][1]
    assert isinstance(user["content"], list)
    kinds = [p["type"] for p in user["content"]]
    assert kinds == ["image_url", "text"]
    assert "describe my image" in user["content"][1]["text"]
    system = payload["messages"][0]["content"]
    assert "shot.png" in system and "you CAN" in system


@pytest.mark.asyncio
async def test_text_only_turn_unchanged(monkeypatch, tmp_path: Path):
    (tmp_path / "notes.txt").write_bytes(b"plain text attachment")
    rec = _capture_payload(monkeypatch)
    await _run(attachments_dir=tmp_path, vision=True)
    user = rec["payloads"][0]["messages"][1]
    assert isinstance(user["content"], str), (
        "a text attachment must NOT flip the turn multimodal"
    )
    system = rec["payloads"][0]["messages"][0]["content"]
    assert "you CAN" not in system


@pytest.mark.asyncio
async def test_vision_flag_off_keeps_string_form(monkeypatch, tmp_path: Path):
    (tmp_path / "shot.png").write_bytes(_PNG)
    rec = _capture_payload(monkeypatch)
    await _run(attachments_dir=tmp_path, vision=False)
    assert isinstance(rec["payloads"][0]["messages"][1]["content"], str)


@pytest.mark.asyncio
async def test_provider_400_demotes_and_retries(monkeypatch, tmp_path: Path):
    (tmp_path / "shot.png").write_bytes(_PNG)
    rec = _capture_payload(monkeypatch, status_first=400)
    events = await _run(attachments_dir=tmp_path, vision=True)
    assert not any(e["type"] == "error" for e in events), events
    assert any(e.get("text") == "seen" for e in events if e["type"] == "delta")
    assert len(rec["payloads"]) == 2, "expected exactly one demoted retry"
    first, second = rec["payloads"]
    assert isinstance(first["messages"][1]["content"], list)
    retry_user = second["messages"][1]["content"]
    assert isinstance(retry_user, str)
    assert "describe my image" in retry_user


# ------------------------------------------------------------- app dispatch

@pytest.fixture
def haihub_capture(monkeypatch):
    """Intercept the haihub delegate's run_turn (function fixture so the
    patch is active for the WHOLE test, fixture ordering included)."""
    captured: dict[str, Any] = {"calls": []}

    async def fake_run_turn(**kwargs):
        captured["calls"].append(kwargs)
        yield {"type": "delta", "text": "ok"}
        yield {"type": "done", "full_text": "ok", "meta": {"model": kwargs.get("model")}}

    # _api_model_turn_gen resolves the runner MODULE and calls its run_turn;
    # patch every delegate's entry point so the picked runner is intercepted
    # whichever alias the test selects. Autouse: a plain (opt-in) fixture's
    # monkeypatch can be installed AFTER the client fixture builds the app,
    # which is too late for a background-task turn; autouse fixtures run
    # first. ``captured`` is returned for assertions.
    for mod in (app_module.haihub_runner, haihub_runner):
        monkeypatch.setattr(mod, "run_turn", fake_run_turn)
    return captured


def test_app_passes_attachments_and_capability_to_runner(
    haihub_capture, client, auth_headers, fake_claude, monkeypatch,
    tmp_attachments_dir, drain_background_tasks,
):
    monkeypatch.setattr(app_module, "CHAT_DEFAULT_MODEL", "qwen")
    captured = haihub_capture
    monkeypatch.setattr(
        claude_runner, "_stage_attachments_into_user_container",
        lambda container, sid, d, **kw: f"/workspace/.attachments/{sid}",
    )
    headers = auth_headers(USER)
    sid = create_session(client, headers)
    r = client.post(
        f"/api/sessions/{sid}/attachments", headers=headers,
        files=[("files", ("shot.png", _PNG, "image/png"))],
    )
    assert r.status_code in (200, 201), r.text
    r = client.post(
        f"/api/sessions/{sid}/messages", headers=headers,
        json={"text": "what is in this picture?", "model": "qwen"},
    )
    assert r.status_code == 200
    consume_sse(r)
    call = captured["calls"][0]
    assert call["vision"] is True
    ad = call["attachments_dir"]
    # The per-turn purge has already deleted the uploads by assertion time
    # (app deletes the session attachments dir at every terminal event) —
    # assert the runner received THIS session's upload dir, not the file.
    assert ad is not None
    import attachments as attachments_mod
    assert Path(ad) == attachments_mod.session_attachments_dir(sid)
    # And the preamble must still name the staged file for the model.
    assert "shot.png" in call["prompt"]


def test_app_flags_tokenhub_glm_text_only():
    """Live probe (2026-10-05): glm-5.3 400s on image parts — never inline."""
    assert tokenhub_runner.SUPPORTS_VISION is False
    assert kimi_runner.SUPPORTS_VISION is True
    assert mimo_runner.SUPPORTS_VISION is True
    assert haihub_runner.SUPPORTS_VISION is True
    assert local_runner.SUPPORTS_VISION is True


def test_supports_vision_is_per_model():
    """Live probe (2026-10-05, image-token verified): haihub accepts the
    image_url SHAPE on every model, but DeepSeek bills 18 prompt tokens
    (text only — image dropped) and MiniMax bills 52 and reasons that it
    cannot see any image. Only Qwen actually reads the pixels. A
    module-level True would still leave deepseek/minimax turns blind, so
    the gate must be per resolved model."""
    assert haihub_runner.supports_vision("qwen") is True
    assert haihub_runner.supports_vision("deepseek") is False
    assert haihub_runner.supports_vision("minimax") is False
    assert haihub_runner.supports_vision("no-such-alias") is False
    assert haihub_runner.supports_vision(None) is False
    # Display names resolve through the alias map too.
    assert haihub_runner.supports_vision("Qwen3.5-397B-A17B-FP8") is True
    assert haihub_runner.supports_vision("DeepSeek-V4-Flash") is False
    # Single-model runners: their one alias is capable, others are not.
    assert kimi_runner.supports_vision("kimi") is True
    assert kimi_runner.supports_vision("glm") is False
    assert mimo_runner.supports_vision("mimo") is True
    assert mimo_runner.supports_vision("mimo-flash") is True
    assert local_runner.supports_vision("gemma4-local") is True
    assert tokenhub_runner.supports_vision("glm") is False


def test_sniff_accepts_bmp_tiff_avif(tmp_path: Path):
    (tmp_path / "a.bmp").write_bytes(_BMP)
    (tmp_path / "b.tif").write_bytes(_TIFF)
    (tmp_path / "c.tiff").write_bytes(_TIFF)
    (tmp_path / "d.avif").write_bytes(_AVIF)
    assert vision.sniff_image_mime(tmp_path / "a.bmp") == "image/bmp"
    assert vision.sniff_image_mime(tmp_path / "b.tif") == "image/tiff"
    assert vision.sniff_image_mime(tmp_path / "c.tiff") == "image/tiff"
    assert vision.sniff_image_mime(tmp_path / "d.avif") == "image/avif"


def test_sniff_rejects_mismatched_new_types(tmp_path: Path):
    (tmp_path / "x.bmp").write_bytes(_PNG)          # png bytes, bmp ext
    (tmp_path / "y.tif").write_bytes(b"BM" + b"\x00" * 30)   # bmp bytes, tif ext
    assert vision.sniff_image_mime(tmp_path / "x.bmp") is None
    assert vision.sniff_image_mime(tmp_path / "y.tif") is None


def test_non_web_raster_is_transcoded_to_png(tmp_path: Path, monkeypatch):
    """A BMP/TIFF/AVIF upload must reach the model as a PNG image part —
    gateways only promise PNG/JPEG/GIF/WebP decoding. The transcode is
    stubbed here (the Pillow round-trip itself is covered by the dev-host
    probe that froze the fixtures); what this pins is that the PART the
    provider receives is PNG while the user's filename is untouched."""
    (tmp_path / "scan.bmp").write_bytes(_BMP)
    monkeypatch.setattr(vision, "_to_png", lambda raw: _PNG)
    parts, names = vision.build_content_parts("PROMPT", tmp_path)
    assert names == ["scan.bmp"]          # the user's filename, unchanged
    assert parts is not None
    assert parts[0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_transcode_failure_drops_image_silently(tmp_path: Path, monkeypatch):
    """A file that sniffs as BMP but Pillow cannot open must not kill the
    turn or send corrupt bytes — it just stays on the run_bash path."""
    (tmp_path / "bad.bmp").write_bytes(_BMP)
    monkeypatch.setattr(vision, "_to_png", lambda raw: None)
    parts, names = vision.build_content_parts("PROMPT", tmp_path)
    assert parts is None and names == []


def test_to_png_real_pillow_roundtrip(tmp_path: Path):
    """Dev-host guard: where Pillow IS installed, _to_png really converts
    the frozen BMP/TIFF/AVIF fixtures to decodable PNGs."""
    pytest.importorskip("PIL")
    import io
    from PIL import Image
    for raw in (_BMP, _TIFF, _AVIF):
        out = vision._to_png(raw)
        assert out is not None
        im = Image.open(io.BytesIO(out))
        assert im.format == "PNG" and im.size == (1, 1)


def test_attachment_preamble_names_real_readers(tmp_path: Path):
    """The run_bash preamble must name tools that actually exist in the
    sandbox image (no pdftotext — the hallucination this wording caused
    was the original 'can't read my PDF' bug)."""
    (tmp_path / "report.pdf").write_bytes(b"%PDF-1.4 fake")
    note = app_module._api_attachment_preamble(tmp_path, "/workspace/.attachments/x")
    assert "report.pdf" in note
    assert "pypdf" in note
    assert "pdftotext" not in note
    assert "python-docx" in note
