"""read_file tool for the OpenAI-compatible bridge loop (kimi/haihub/mimo).

Regression tests for "some models can't read pictures": the tool-loop models
are text-only, so read_file converts any file (image, PDF, Office doc, audio,
archive, text) into model-consumable text — vision via a mocked Gemini call.
"""

import asyncio
import zipfile
from pathlib import Path

import pytest

import bot


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture
def no_gemini(monkeypatch):
    monkeypatch.setattr(bot, "_resolve_gemini_key", lambda: None)


@pytest.fixture
def fake_gemini(monkeypatch):
    """Vision model stub: records (mime, prompt), returns a description."""
    calls = []

    def _fake(data: bytes, mime: str, prompt: str, api_key: str, **kw) -> str:
        calls.append({"mime": mime, "prompt": prompt, "nbytes": len(data)})
        return "VISION: a screenshot showing the number 42"

    monkeypatch.setattr(bot, "_resolve_gemini_key", lambda: "test-key")
    monkeypatch.setattr(bot, "_gemini_file_to_text", _fake)
    return calls


def test_tools_table_has_read_file():
    names = [t["function"]["name"] for t in bot._QWEN_TOOLS]
    assert names == ["run_bash", "read_file"]


def test_text_file(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("hello bridge\n" * 3)
    out = run(bot._qwen_read_file(str(f)))
    assert "hello bridge" in out
    assert str(f.name) in out


def test_missing_file():
    out = run(bot._qwen_read_file("/no/such/file.xyz"))
    assert "no such file" in out


def test_directory_listing(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "sub").mkdir()
    out = run(bot._qwen_read_file(str(tmp_path)))
    assert "directory listing" in out and "a.txt" in out and "sub" in out


def test_image_goes_to_vision(tmp_path, fake_gemini):
    f = tmp_path / "shot.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
    out = run(bot._qwen_read_file(str(f), question="what number is shown?"))
    assert "number 42" in out
    assert fake_gemini[0]["mime"] == "image/png"
    assert "what number is shown?" in fake_gemini[0]["prompt"]


def test_image_without_gemini_key(tmp_path, no_gemini):
    f = tmp_path / "shot.jpg"
    f.write_bytes(b"\xff\xd8\xff" + b"\x00" * 100)
    out = run(bot._qwen_read_file(str(f)))
    assert "GEMINI_API_KEY" in out


def test_oversized_image_not_sent(tmp_path, fake_gemini, monkeypatch):
    f = tmp_path / "big.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n")
    monkeypatch.setattr(bot, "_READFILE_GEMINI_MAX_BYTES", 4)
    out = run(bot._qwen_read_file(str(f)))
    assert "too large" in out and not fake_gemini


def test_pdf_with_text_layer_skips_vision(tmp_path, fake_gemini, monkeypatch):
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(bot, "_readfile_pdf_text",
                        lambda p: ("full text layer here " * 20, 2))
    out = run(bot._qwen_read_file(str(f)))
    assert "full text layer" in out and "text layer" in out
    assert not fake_gemini  # extraction sufficed — no vision call


def test_scanned_pdf_falls_back_to_vision(tmp_path, fake_gemini, monkeypatch):
    f = tmp_path / "scan.pdf"
    f.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(bot, "_readfile_pdf_text", lambda p: ("", 3))
    out = run(bot._qwen_read_file(str(f)))
    assert "number 42" in out
    assert fake_gemini[0]["mime"] == "application/pdf"


def test_docx_extraction(tmp_path):
    f = tmp_path / "doc.docx"
    with zipfile.ZipFile(f, "w") as z:
        z.writestr("word/document.xml",
                   "<w:body><w:p><w:r><w:t>Hello</w:t></w:r></w:p>"
                   "<w:p><w:r><w:t>World &amp; Friends</w:t></w:r></w:p></w:body>")
    out = run(bot._qwen_read_file(str(f)))
    assert "Hello" in out and "World & Friends" in out


def test_xlsx_extraction(tmp_path):
    f = tmp_path / "book.xlsx"
    with zipfile.ZipFile(f, "w") as z:
        z.writestr("xl/workbook.xml", '<sheets><sheet name="P&amp;L" sheetId="1"/></sheets>')
        z.writestr("xl/sharedStrings.xml",
                   "<sst><si><t>ticker</t></si><si><t>AAPL</t></si></sst>")
        z.writestr("xl/worksheets/sheet1.xml",
                   '<sheetData><row r="1"><c r="A1" t="s"><v>0</v></c>'
                   '<c r="B1" t="s"><v>1</v></c><c r="C1"><v>227.5</v></c>'
                   "</row></sheetData>")
    out = run(bot._qwen_read_file(str(f)))
    assert "sheet: P&L" in out
    assert "ticker\tAAPL\t227.5" in out


def test_zip_listing(tmp_path):
    f = tmp_path / "bundle.zip"
    with zipfile.ZipFile(f, "w") as z:
        z.writestr("inner/a.txt", "aaa")
        z.writestr("inner/b.csv", "bbb")
    out = run(bot._qwen_read_file(str(f)))
    assert "zip archive: 2 entries" in out
    assert "inner/a.txt" in out


def test_unknown_binary_identified(tmp_path):
    f = tmp_path / "mystery.bin"
    f.write_bytes(bytes(range(256)))
    out = run(bot._qwen_read_file(str(f)))
    assert "binary file" in out and "hex" in out


def test_audio_routes_to_vision(tmp_path, fake_gemini):
    f = tmp_path / "memo.ogg"
    f.write_bytes(b"OggS" + b"\x00" * 50)
    out = run(bot._qwen_read_file(str(f)))
    assert "number 42" in out
    assert fake_gemini[0]["mime"] == "audio/ogg"


def test_relative_path_resolves_against_working_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(bot, "WORKING_DIR", tmp_path)
    (tmp_path / "rel.txt").write_text("relative ok")
    out = run(bot._qwen_read_file("rel.txt"))
    assert "relative ok" in out


def test_dispatch_unknown_tool():
    out = run(bot._qwen_dispatch_tool("nope", {}, None))
    assert "unknown tool" in out and "read_file" in out


def test_dispatch_read_file(tmp_path, monkeypatch):
    f = tmp_path / "d.txt"
    f.write_text("dispatch ok")
    out = run(bot._qwen_dispatch_tool("read_file", {"path": str(f)}, None))
    assert "dispatch ok" in out
    out = run(bot._qwen_dispatch_tool("read_file", {}, None))
    assert "without a 'path'" in out
