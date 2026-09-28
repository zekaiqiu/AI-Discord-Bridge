"""Persistent `docker exec -i` channel per user container.

Why this exists: every one-shot `docker exec` pays ~80 ms of pure startup
overhead (process fork, namespace setup, docker API round-trip), and the
in-container python interpreter pays another ~20 ms of import time. For
the file tree's list_dir hot path that means ~100 ms of pure wait before
any actual work happens — visible as the "Loading…" stutter when the
user expands a folder.

This module keeps a long-running `docker exec -i CONTAINER python3 -u -c
<helper>` alive per (container) and sends JSON-line requests to it.
Subsequent ops bypass docker exec startup and python boot entirely; they
cost a few ms each (stdin write + scandir loop + stdout read).

Concurrency: one in-flight request per channel at a time, serialized
under a lock. The hot path is short enough that this is fine; if we
ever need parallelism, the channel can be pooled.

Liveness: on EOF / non-zero exit / unparseable response, the channel is
marked dead and recreated on the next call. The caller can also catch
ChannelError and fall back to one-shot `run_exec`.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)


class ChannelError(Exception):
    """Raised on protocol-level channel failures (process died, malformed
    response, etc.). Caller should fall back to one-shot `run_exec`."""


# The in-container helper. Reads `{"op": "...", "..."}` lines on stdin,
# writes one `{"ok": ...}` or `{"error": ...}` line on stdout per request.
# `os.scandir` instead of `listdir + stat` to skip an extra syscall per
# entry — on ext4/xfs the dirent already carries the d_type, so kind
# resolves without a stat at all in the common case.
_HELPER_SRC = r"""
import json, os, shutil, stat as _stat, sys, tempfile

WORKSPACE_ROOT = "/workspace"

# Symlink guard (Fix 6): realpath-resolve a path and verify it stays under
# /workspace. Returns resolved path on success, None on escape. For new-file
# targets (write/mkdir/rename dst), realpath resolves parent symlinks then
# appends the basename, so a symlinked parent still gets caught.
def _safe(path):
    if not isinstance(path, str) or not path:
        return None
    rp = os.path.realpath(path)
    if rp == WORKSPACE_ROOT or rp.startswith(WORKSPACE_ROOT + "/"):
        return rp
    return None

def list_dir(root, hidden_top):
    root = _safe(root)
    if root is None:
        return {"error": "path_escapes_workspace"}
    try:
        it = os.scandir(root)
    except FileNotFoundError:
        return {"error": "not_found"}
    except PermissionError:
        return {"error": "permission_denied"}
    entries = []
    hidden = set(hidden_top or ())
    try:
        for de in it:
            name = de.name
            if root == "/workspace" and name in hidden:
                continue
            try:
                if de.is_symlink():
                    kind = "link"
                elif de.is_dir(follow_symlinks=False):
                    kind = "dir"
                elif de.is_file(follow_symlinks=False):
                    kind = "file"
                else:
                    kind = "other"
                st = de.stat(follow_symlinks=False)
            except OSError:
                continue
            entries.append({
                "name": name,
                "kind": kind,
                "size": st.st_size,
                "mtime": st.st_mtime,
            })
    finally:
        it.close()
    entries.sort(key=lambda e: e["name"])
    return {"entries": entries}

def stat_path(path):
    path = _safe(path)
    if path is None:
        return {"error": "path_escapes_workspace"}
    try:
        st = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return {"error": "not_found"}
    except PermissionError:
        return {"error": "permission_denied"}
    if _stat.S_ISREG(st.st_mode):
        kind = "file"
    elif _stat.S_ISDIR(st.st_mode):
        kind = "dir"
    elif _stat.S_ISLNK(st.st_mode):
        kind = "link"
    else:
        kind = "other"
    return {"size": st.st_size, "mtime": st.st_mtime, "kind": kind}

def read_file(path, max_bytes):
    path = _safe(path)
    if path is None:
        return {"error": "path_escapes_workspace"}
    try:
        st = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return {"error": "not_found"}
    except PermissionError:
        return {"error": "permission_denied"}
    if not _stat.S_ISREG(st.st_mode):
        return {"error": "not_a_file"}
    size = st.st_size
    truncated = size > max_bytes
    n = min(size, max_bytes)
    if n == 0:
        return {"content": "", "size": size, "truncated": False}
    try:
        with open(path, "rb") as f:
            data = f.read(n)
    except PermissionError:
        return {"error": "permission_denied"}
    except OSError as exc:
        return {"error": "read_failed: " + str(exc)}
    # Same decoding the one-shot path uses — utf-8 with replacement so
    # the editor can at least open binary-ish files without 500ing.
    text = data.decode("utf-8", errors="replace")
    return {"content": text, "size": size, "truncated": truncated}

def write_file(path, content, max_bytes):
    path = _safe(path)
    if path is None:
        return {"error": "path_escapes_workspace"}
    payload = content.encode("utf-8")
    if len(payload) > max_bytes:
        return {"error": "too_large", "size": len(payload)}
    parent = os.path.dirname(path) or "/"
    try:
        os.makedirs(parent, exist_ok=True)
    except PermissionError:
        return {"error": "permission_denied"}
    except OSError as exc:
        return {"error": "mkdir_parent_failed: " + str(exc)}
    try:
        fd, tmp = tempfile.mkstemp(prefix=".tmp.dev-wizerith.", dir=parent)
    except OSError as exc:
        return {"error": "tmp_create_failed: " + str(exc)}
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
        os.rename(tmp, path)
    except OSError as exc:
        try: os.unlink(tmp)
        except OSError: pass
        return {"error": "write_failed: " + str(exc)}
    return {"size": len(payload)}

def mkdir(path):
    path = _safe(path)
    if path is None:
        return {"error": "path_escapes_workspace"}
    try:
        os.makedirs(path, exist_ok=True)
    except PermissionError:
        return {"error": "permission_denied"}
    except OSError as exc:
        return {"error": "mkdir_failed: " + str(exc)}
    return {"ok": True}

def rename(src, dst):
    src = _safe(src)
    dst = _safe(dst)
    if src is None or dst is None:
        return {"error": "path_escapes_workspace"}
    if not os.path.lexists(src):
        return {"error": "not_found"}
    parent = os.path.dirname(dst) or "/"
    try:
        os.makedirs(parent, exist_ok=True)
        os.rename(src, dst)
    except PermissionError:
        return {"error": "permission_denied"}
    except OSError as exc:
        return {"error": "rename_failed: " + str(exc)}
    return {"ok": True}

def copy(src, dst):
    src = _safe(src)
    dst = _safe(dst)
    if src is None or dst is None:
        return {"error": "path_escapes_workspace"}
    if not os.path.lexists(src):
        return {"error": "not_found"}
    if os.path.lexists(dst):
        return {"error": "exists"}
    parent = os.path.dirname(dst) or "/"
    try:
        os.makedirs(parent, exist_ok=True)
        # cp -R semantics: directories recurse, symlinks copied as symlinks,
        # mtime preserved via copy2.
        if os.path.isdir(src) and not os.path.islink(src):
            shutil.copytree(src, dst, symlinks=True)
        else:
            shutil.copy2(src, dst, follow_symlinks=False)
    except PermissionError:
        return {"error": "permission_denied"}
    except OSError as exc:
        return {"error": "copy_failed: " + str(exc)}
    return {"ok": True}

def delete(path):
    path = _safe(path)
    if path is None:
        return {"error": "path_escapes_workspace"}
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return {"error": "not_found"}
    except PermissionError:
        return {"error": "permission_denied"}
    try:
        if _stat.S_ISDIR(st.st_mode):
            shutil.rmtree(path)
        else:
            os.unlink(path)
    except PermissionError:
        return {"error": "permission_denied"}
    except OSError as exc:
        return {"error": "delete_failed: " + str(exc)}
    return {"ok": True}

OPS = {
    "list_dir": list_dir,
    "stat_path": stat_path,
    "read_file": read_file,
    "write_file": write_file,
    "mkdir": mkdir,
    "rename": rename,
    "copy": copy,
    "delete": delete,
    "ping": lambda: {"pong": True},
}

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        req = json.loads(line)
        op = OPS.get(req.get("op"))
        if op is None:
            res = {"error": "unknown_op"}
        else:
            kwargs = {k: v for k, v in req.items() if k != "op"}
            res = op(**kwargs)
    except Exception as exc:
        res = {"error": f"helper: {type(exc).__name__}: {exc}"}
    sys.stdout.write(json.dumps(res) + "\n")
    sys.stdout.flush()
"""


class ExecChannel:
    """Long-running docker exec into a single container with a JSON-line protocol."""

    def __init__(self, container: str):
        self.container = container
        self.proc: Optional[subprocess.Popen[bytes]] = None
        self.lock = threading.Lock()
        self.opened_at: float = 0.0

    def _open(self) -> None:
        # `python3 -u` keeps stdout unbuffered so each response lands
        # immediately. The helper source is passed via -c so we don't
        # need a writable path inside the container.
        self.proc = subprocess.Popen(
            [
                "docker", "exec", "-i", self.container,
                "/usr/local/bin/python3", "-u", "-c", _HELPER_SRC,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self.opened_at = time.monotonic()

    def _alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def call(self, op: str, **kwargs: Any) -> dict:
        with self.lock:
            if not self._alive():
                self._open()
            assert self.proc is not None and self.proc.stdin is not None and self.proc.stdout is not None
            payload = json.dumps({"op": op, **kwargs}, separators=(",", ":")) + "\n"
            try:
                self.proc.stdin.write(payload.encode("utf-8"))
                self.proc.stdin.flush()
                line = self.proc.stdout.readline()
            except (BrokenPipeError, OSError) as exc:
                self._kill()
                raise ChannelError(f"channel write failed: {exc}") from exc
            if not line:
                stderr = b""
                try:
                    if self.proc.stderr is not None:
                        stderr = self.proc.stderr.read(2048) or b""
                except Exception:  # noqa: BLE001
                    pass
                self._kill()
                raise ChannelError(
                    f"channel EOF (helper died); stderr={stderr.decode('utf-8', 'replace')[:400]!r}"
                )
            try:
                return json.loads(line.decode("utf-8"))
            except json.JSONDecodeError as exc:
                self._kill()
                raise ChannelError(f"channel returned invalid JSON: {line!r}") from exc

    def _kill(self) -> None:
        if self.proc is None:
            return
        try:
            self.proc.kill()
        except Exception:  # noqa: BLE001
            pass
        self.proc = None


# Process-wide registry of channels by container name.
_channels: dict[str, ExecChannel] = {}
_registry_lock = threading.Lock()


def get_channel(container: str) -> ExecChannel:
    """Return the persistent channel for `container`, opening it if needed."""
    with _registry_lock:
        ch = _channels.get(container)
        if ch is None or not ch._alive():
            ch = ExecChannel(container)
            _channels[container] = ch
        return ch


def drop_channel(container: str) -> None:
    """Tear down the channel for a container (called when the underlying
    container is destroyed or the cached container name is invalidated)."""
    with _registry_lock:
        ch = _channels.pop(container, None)
    if ch is not None:
        ch._kill()
