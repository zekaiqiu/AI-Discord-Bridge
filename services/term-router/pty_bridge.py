"""Bidirectional pump between a FastAPI WebSocket and a docker exec socket.

Kept FastAPI-free at import time: only `asyncio` + stdlib. Tests inject
fakes for `ws`, `exec_stream`, and `docker_client` — which is the whole
point of this module. The browser xterm.js client speaks two kinds of
frames to us:

  - text frames: either a JSON resize envelope ({"type":"resize",...}) OR
    keyboard input from xterm.js's `terminal.onData(...)` callback (which
    sends UTF-8 strings via `ws.send(string)` — so they arrive as text
    frames, not binary). Resize envelopes are intercepted; ALL other text
    is forwarded as stdin bytes.
  - binary frames: raw stdin bytes for the user's shell (e.g. paste of
    binary content, or any client that prefers `ws.send(bytes)`).

Output direction is always binary: bytes from exec_stream → ws.send_bytes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from typing import Any

logger = logging.getLogger(__name__)

# Read chunk size for the docker exec stream. Big enough that interactive
# typing isn't chunked, small enough that a flood doesn't sit in our buffer.
_READ_CHUNK = 4096


def _parse_resize(text: str) -> tuple[int, int] | None:
    """Return (cols, rows) for a well-formed resize envelope, else None.

    Anything that isn't a JSON object with type=="resize" and integer
    cols/rows is ignored — we never raise from here.
    """
    try:
        msg = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(msg, dict) or msg.get("type") != "resize":
        return None
    cols = msg.get("cols")
    rows = msg.get("rows")
    if not isinstance(cols, int) or not isinstance(rows, int):
        return None
    if cols <= 0 or rows <= 0:
        return None
    return cols, rows


def _is_ping(text: str) -> bool:
    """True if `text` is the client's keepalive envelope.

    Distinct from resize because the response shape differs (resize calls
    docker exec_resize; ping is a no-op). Same defensive parse: never
    raise. The client sends one of these every ~20s so intermediate
    proxies (cloudflared, CF edge) see traffic and don't tear the
    WebSocket down on idle.
    """
    try:
        msg = json.loads(text)
    except (TypeError, ValueError):
        return False
    return isinstance(msg, dict) and msg.get("type") == "ping"


def _send_to_stream(exec_stream, data: bytes) -> None:
    """Write to the docker exec stream regardless of underlying type.

    docker-py's exec_start(socket=True) returns a SocketIO-like object whose
    write surface varies by version: most expose `.write()`/`.flush()`,
    older releases expose `.sendall()`. We pick whichever the object offers.
    Test fakes implement `.write()` only.
    """
    if hasattr(exec_stream, "write"):
        exec_stream.write(data)
        if hasattr(exec_stream, "flush"):
            exec_stream.flush()
        return
    if hasattr(exec_stream, "sendall"):
        exec_stream.sendall(data)
        return
    raise RuntimeError("exec_stream has no write/sendall surface")


async def _write_stdin(exec_stream, data: bytes) -> None:
    """Write `data` to the exec stream's stdin, off-loop if the stream
    exposes a raw socket (so a slow socket doesn't stall the event loop).

    Single helper used by both the text and binary frame paths in
    `_ws_to_exec`, which is the contract from the brief: "All other frames
    are forwarded as bytes to the exec stdin." Refactored out of an inline
    `await ... if ... else ...` ternary because (a) Python parses that
    ternary surprisingly — the `await` only binds to the body branch — and
    (b) even when correct, it's hostile to readers.
    """
    if hasattr(exec_stream, "_sock"):
        # docker-py's SocketIO wraps a raw socket; sendall is blocking, so
        # run it in the default executor. `_sock` is a private attribute —
        # see the helper docstring for why we touch it deliberately.
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, exec_stream._sock.sendall, data)  # noqa: SLF001 — docker-py SocketIO private attr; see docstring
    else:
        _send_to_stream(exec_stream, data)


async def _ws_to_exec(ws, exec_stream, exec_id, docker_client) -> None:
    """Pump ws → exec_stream. Closes when ws closes."""
    while True:
        msg = await ws.receive()
        # FastAPI's WebSocket.receive() returns a dict shape:
        #   {"type": "websocket.receive", "text": "..."} or {"bytes": b"..."}
        #   {"type": "websocket.disconnect", "code": N}
        # Tests pass the same shape via a FakeWebSocket — see test_term_router.
        if msg.get("type") == "websocket.disconnect":
            return
        text = msg.get("text")
        data = msg.get("bytes")
        if text is not None:
            resize = _parse_resize(text)
            if resize is not None:
                cols, rows = resize
                try:
                    docker_client.api.exec_resize(exec_id, height=rows, width=cols)
                except Exception:  # docker-side failure shouldn't kill the session
                    logger.exception("exec_resize failed for %s", exec_id)
                continue
            if _is_ping(text):
                # Keepalive frame from the client — the round-trip alone
                # is the whole point (forces traffic through every proxy
                # hop), so we just drop it and continue. No response
                # needed: the client only cares that the WebSocket is
                # still healthy enough for `ws.send` to succeed.
                continue
            # Non-resize text: treat as stdin bytes (UTF-8). xterm.js's
            # `terminal.onData(d => ws.send(d))` produces these for every
            # keystroke, so this is the hot path for keyboard input.
            try:
                await _write_stdin(exec_stream, text.encode("utf-8"))
            except Exception:
                logger.exception("write text to exec stdin failed")
                return
            continue
        if data is not None:
            try:
                await _write_stdin(exec_stream, data)
            except Exception:
                logger.exception("write bytes to exec stdin failed")
                return


async def _exec_to_ws(ws, exec_stream) -> None:
    """Pump exec_stream → ws. Closes when exec_stream returns 0 bytes.

    Tolerant of idle-read timeouts: app.py clears the docker exec
    socket's timeout to None so reads block forever, but if a future
    SDK reintroduces a deadline we still want an idle shell to NOT
    masquerade as EOF. Real EOF still surfaces as zero-byte reads.
    """
    loop = asyncio.get_running_loop()
    while True:
        try:
            chunk = await loop.run_in_executor(None, _read_chunk, exec_stream)
        except (socket.timeout, TimeoutError):
            # Idle on the docker exec socket — not an error, just no
            # output yet. Retry. A truly broken socket will surface as
            # a different exception next iteration.
            continue
        except Exception:
            logger.exception("read from exec stream failed")
            return
        if not chunk:
            return
        try:
            await ws.send_bytes(chunk)
        except Exception:
            # ws closed mid-write; let the caller cancel the partner task.
            return


def _read_chunk(exec_stream) -> bytes:
    """Read up to _READ_CHUNK bytes from the exec stream.

    Same surface-detection pattern as `_send_to_stream`: docker-py's socket
    wrapper exposes .read(n) on some versions and .recv(n) on others.
    """
    if hasattr(exec_stream, "read"):
        return exec_stream.read(_READ_CHUNK) or b""
    if hasattr(exec_stream, "recv"):
        return exec_stream.recv(_READ_CHUNK) or b""
    raise RuntimeError("exec_stream has no read/recv surface")


async def bridge(ws: Any, exec_stream: Any, exec_id: str, *, docker_client: Any) -> None:
    """Bidirectional pump. Returns when either side closes.

    Cancels the partner task on first completion so we never leak a hung
    reader. Catches CancelledError (so cancellation doesn't propagate as an
    error) but does NOT swallow other exceptions silently — those bubble.
    """
    incoming = asyncio.create_task(
        _ws_to_exec(ws, exec_stream, exec_id, docker_client),
        name="pty-bridge-ws-to-exec",
    )
    outgoing = asyncio.create_task(
        _exec_to_ws(ws, exec_stream),
        name="pty-bridge-exec-to-ws",
    )
    try:
        done, pending = await asyncio.wait(
            {incoming, outgoing}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        # Surface any non-cancellation error from the completed task.
        for task in done:
            try:
                task.result()
            except asyncio.CancelledError:
                pass
        # Drain cancellations cleanly.
        for task in pending:
            try:
                await task
            except asyncio.CancelledError:
                pass
    except asyncio.CancelledError:
        for task in (incoming, outgoing):
            task.cancel()
        raise
