"""WebSocket ↔ pyright-langserver bridge.

Each WS connection spawns one `docker exec -i <container> pyright-langserver
--stdio` inside the user's per-user container and pipes JSON-RPC messages
both ways.

  Browser ⇄ WS frames (one JSON message per frame, no headers)
             │
             ▼
  Server: parse / wrap with Content-Length, write to pyright stdin
  Server: read Content-Length-framed stdout, send body as a WS frame

pyright is lazy-installed on first connection into /workspace/.dev-wizerith/
node_modules. /workspace is the user's persistent volume — survives container
recreates so the install is one-time per user (~10-15 s, ~30 MB).
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import subprocess
from typing import Optional

logger = logging.getLogger("dev-wizerith.lsp")

# Inside each per-user container. The .dev-wizerith dir is conventionally
# owned by uid 1000 (the `app` user) since /workspace is 2775 owned by
# app:claude-runner — see services.chat.user_container for the why.
PYRIGHT_DIR = "/workspace/.dev-wizerith"
PYRIGHT_BIN = f"{PYRIGHT_DIR}/node_modules/.bin/pyright-langserver"

# Install command. --no-audit/--no-fund keeps the install quiet; --silent
# suppresses npm's progress chatter so the captured stderr stays scannable.
# We pin to a specific major to avoid surprise breakages — bump deliberately.
PYRIGHT_NPM_SPEC = "pyright@1.1"

EXEC_UID = "1000:1000"


# ---------------------------------------------------------------------------
# Install.
# ---------------------------------------------------------------------------

# Per-container locks so two concurrent WS connects from the same user
# don't race on `npm install`. Keyed by container name.
_install_locks: dict[str, asyncio.Lock] = {}
_install_done: set[str] = set()


def _install_lock(container: str) -> asyncio.Lock:
    lock = _install_locks.get(container)
    if lock is None:
        lock = asyncio.Lock()
        _install_locks[container] = lock
    return lock


async def ensure_pyright(container: str) -> None:
    """Idempotently install pyright into /workspace/.dev-wizerith inside the
    user's container. No-op if the binary already exists.
    """
    if container in _install_done:
        return
    async with _install_lock(container):
        # Re-check after lock — a concurrent connect may have finished while
        # we waited.
        if container in _install_done:
            return
        check = await asyncio.create_subprocess_exec(
            "docker", "exec", "--user", EXEC_UID, container,
            "test", "-x", PYRIGHT_BIN,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        if (await check.wait()) == 0:
            _install_done.add(container)
            return

        logger.info("pyright not found in %s — installing", container)
        # Single shell so mkdir/npm chain cleanly. npm cache lives at
        # $HOME/.npm = /workspace/.npm; persists with the volume.
        script = (
            f"set -e\n"
            f"mkdir -p {shlex.quote(PYRIGHT_DIR)}\n"
            f"cd {shlex.quote(PYRIGHT_DIR)}\n"
            f"[ -f package.json ] || echo '{{}}' > package.json\n"
            f"npm install --no-audit --no-fund --silent {shlex.quote(PYRIGHT_NPM_SPEC)}\n"
            f"test -x {shlex.quote(PYRIGHT_BIN)}\n"
        )
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", "--user", EXEC_UID, container,
            "sh", "-c", script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=180.0)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("pyright install timed out after 180s")
        if proc.returncode != 0:
            raise RuntimeError(
                f"pyright install failed (exit {proc.returncode}): "
                f"{err.decode('utf-8', 'replace')[:500]}"
            )
        logger.info("pyright installed in %s", container)
        _install_done.add(container)


# ---------------------------------------------------------------------------
# Bridge.
# ---------------------------------------------------------------------------


class LspSession:
    """One pyright-langserver subprocess + its two pump tasks."""

    def __init__(self, container: str) -> None:
        self.container = container
        self.proc: Optional[asyncio.subprocess.Process] = None

    async def spawn(self) -> None:
        # Workdir = /workspace so pyright's default rootUri inference (if the
        # client doesn't send workspace folders) still points at the user's
        # files. We also send workspace folders explicitly from the browser.
        argv = [
            "docker", "exec", "-i",
            "--user", EXEC_UID,
            "--workdir", "/workspace",
            self.container,
            PYRIGHT_BIN, "--stdio",
        ]
        self.proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    async def stop(self) -> None:
        if self.proc is None:
            return
        try:
            self.proc.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            try:
                self.proc.kill()
            except ProcessLookupError:
                pass


async def _read_lsp_message(stdout: asyncio.StreamReader) -> Optional[bytes]:
    """Read one Content-Length-framed JSON-RPC message. Returns the JSON body
    bytes (no headers), or None on EOF.
    """
    # Read headers until \r\n\r\n.
    try:
        headers_blob = await stdout.readuntil(b"\r\n\r\n")
    except asyncio.IncompleteReadError:
        return None
    except asyncio.LimitOverrunError:
        # Header section ran past StreamReader buffer (default 64 KiB). Pyright
        # never emits header sections this large; treat as a protocol error.
        raise RuntimeError("LSP header section exceeded buffer limit")

    length: Optional[int] = None
    for raw in headers_blob.split(b"\r\n"):
        if not raw:
            continue
        if raw.lower().startswith(b"content-length:"):
            try:
                length = int(raw.split(b":", 1)[1].strip())
            except ValueError:
                length = None
            break
    if length is None or length < 0:
        # Unknown / bad framing — skip this message.
        return b""
    return await stdout.readexactly(length)


async def pump_proc_to_ws(session: LspSession, ws) -> None:
    """Read JSON-RPC messages from pyright stdout, forward as WS text frames."""
    from starlette.websockets import WebSocketState

    assert session.proc is not None
    stdout = session.proc.stdout
    assert stdout is not None
    while True:
        try:
            body = await _read_lsp_message(stdout)
        except Exception as exc:
            logger.warning("LSP read error: %s", exc)
            return
        if body is None:
            return
        if not body:
            continue
        if ws.application_state != WebSocketState.CONNECTED:
            return
        try:
            await ws.send_text(body.decode("utf-8", errors="replace"))
        except Exception:
            return


async def pump_ws_to_proc(session: LspSession, ws) -> None:
    """Read WS frames (one JSON-RPC message per frame), wrap with
    Content-Length, write to pyright stdin.
    """
    from starlette.websockets import WebSocketDisconnect

    assert session.proc is not None
    stdin = session.proc.stdin
    assert stdin is not None
    while True:
        try:
            msg = await ws.receive_text()
        except WebSocketDisconnect:
            return
        except Exception as exc:
            logger.warning("WS receive error: %s", exc)
            return
        data = msg.encode("utf-8")
        header = f"Content-Length: {len(data)}\r\n\r\n".encode("ascii")
        try:
            stdin.write(header)
            stdin.write(data)
            await stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            return


async def drain_stderr(session: LspSession) -> None:
    """Best-effort stderr logger so pyright crashes don't go silent."""
    assert session.proc is not None
    stderr = session.proc.stderr
    assert stderr is not None
    while True:
        try:
            line = await stderr.readline()
        except Exception:
            return
        if not line:
            return
        logger.info("pyright[%s]: %s", session.container[-8:],
                    line.decode("utf-8", "replace").rstrip())
