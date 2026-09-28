"""File-system operations against a per-user docker container.

Every op runs as uid 1000 (same as the user's bash terminal) via
``docker exec --user 1000:1000``, so the same filesystem perms that
already hide /var/claude-runner/.claude/.credentials.json (mode 0400
owner uid 2000) from the bash shell also hide it from these endpoints.
That is the security boundary — we deliberately do NOT add an in-app
allowlist of paths, because the chmod boundary already exists and a
second filter would only obscure where the real check lives.

Path arguments come from the client and are passed as separate argv
elements (not joined into a shell command), so shell metacharacters in
filenames cannot inject. The one exception is the `cat > path` redirect
inside upload, which is built into a shell command — there we hard-quote
the destination via single-quote escaping.

NOTE: stream_upload + write_file wait for the docker exec to finish
before returning. Without that wait, ``client.api.exec_inspect`` can
return ``ExitCode=None`` (still-running) and the caller would happily
move on to the next operation while ``cat > path`` is still draining
its stdin — surfacing as "No such file or directory" on a follow-up
read against a file we just wrote.
"""
from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass
from typing import Any, Iterator

logger = logging.getLogger(__name__)

EXEC_USER = "1000:1000"

# Hard ceiling matching the Caddy and client-side limits. 1 GB.
MAX_UPLOAD_BYTES = 1 * 1024 * 1024 * 1024


class FileOpError(RuntimeError):
    """Raised when docker exec returns non-zero. Carries exit code + stderr
    so the HTTP layer can surface a useful 4xx/5xx."""

    def __init__(self, exit_code: int, stderr: str):
        super().__init__(f"exec failed (exit={exit_code}): {stderr}")
        self.exit_code = exit_code
        self.stderr = stderr


@dataclass(frozen=True)
class Entry:
    name: str
    type: str  # "f" | "d" | "l" | "?"
    size: int
    mtime: float


def _exec_collect(
    client: Any,
    container: str,
    argv: list[str],
    *,
    user: str = EXEC_USER,
    stdin: bytes | None = None,
) -> tuple[int, bytes, bytes]:
    """Run a one-shot exec, return (exit_code, stdout, stderr).

    Uses demux=True so stdout/stderr come back separately — important
    because the listing parser is whitespace-sensitive and we don't want
    shell warnings polluting it.
    """
    raw = client.api.exec_create(
        container,
        cmd=argv,
        user=user,
        stdin=stdin is not None,
        stdout=True,
        stderr=True,
        tty=False,
    )
    exec_id = raw["Id"] if isinstance(raw, dict) else raw

    if stdin is not None:
        sock = client.api.exec_start(exec_id, socket=True, demux=False)
        try:
            # docker-py's SocketIO wraps a raw socket; sendall + close the
            # write half so `cat` sees EOF and exits.
            inner = getattr(sock, "_sock", None)
            if inner is not None:
                inner.sendall(stdin)
                try:
                    inner.shutdown(1)  # SHUT_WR — done writing
                except OSError:
                    pass
            elif hasattr(sock, "write"):
                sock.write(stdin)
                if hasattr(sock, "flush"):
                    sock.flush()
            # Drain stdout/stderr from the same socket.
            chunks: list[bytes] = []
            while True:
                buf = b""
                if hasattr(sock, "read"):
                    buf = sock.read(65536) or b""
                elif inner is not None:
                    buf = inner.recv(65536) or b""
                if not buf:
                    break
                chunks.append(buf)
            # When demux is off we can't separate stdout/stderr; treat the
            # combined stream as stderr-on-failure (we only call this path
            # for write ops where success means empty output anyway).
            combined = b"".join(chunks)
        finally:
            try:
                sock.close()
            except Exception:
                pass
        info = client.api.exec_inspect(exec_id)
        return int(info.get("ExitCode") or 0), b"", combined

    out = client.api.exec_start(exec_id, demux=True)
    # demux=True returns (stdout_bytes, stderr_bytes); each may be None.
    if isinstance(out, tuple):
        stdout = out[0] or b""
        stderr = out[1] or b""
    else:
        stdout = out or b""
        stderr = b""
    info = client.api.exec_inspect(exec_id)
    return int(info.get("ExitCode") or 0), stdout, stderr


def list_dir(client: Any, container: str, path: str) -> list[Entry]:
    """Return entries directly under `path`. Hidden files included.

    Uses ``find`` with NUL separators to survive filenames containing
    newlines or tabs. Every record is exactly four NUL-terminated
    fields: type, size, mtime-seconds, name. Symlinks reported as 'l'
    (we don't follow — the `-H` flag is intentionally omitted).
    """
    argv = [
        "find", path,
        "-mindepth", "1", "-maxdepth", "1",
        "-printf", "%y\\0%s\\0%T@\\0%f\\0",
    ]
    code, out, err = _exec_collect(client, container, argv)
    if code != 0:
        raise FileOpError(code, err.decode("utf-8", errors="replace"))
    parts = out.split(b"\0")
    # Each entry is 4 fields → trailing empty after final NUL drops out.
    entries: list[Entry] = []
    for i in range(0, len(parts) - 1, 4):
        if i + 3 >= len(parts):
            break
        try:
            etype = parts[i].decode("utf-8", errors="replace") or "?"
            size = int(parts[i + 1] or b"0")
            mtime = float(parts[i + 2] or b"0")
            name = parts[i + 3].decode("utf-8", errors="replace")
        except (ValueError, UnicodeDecodeError):
            continue
        if not name:
            continue
        entries.append(Entry(name=name, type=etype, size=size, mtime=mtime))
    # Directories first, then alpha — matches the file-explorer convention.
    entries.sort(key=lambda e: (e.type != "d", e.name.lower()))
    return entries


def stat_path(client: Any, container: str, path: str) -> Entry | None:
    """Return Entry for `path` itself, or None if missing."""
    argv = ["stat", "-c", "%F\t%s\t%Y\t%n", "--", path]
    code, out, err = _exec_collect(client, container, argv)
    if code != 0:
        return None
    line = out.decode("utf-8", errors="replace").strip()
    try:
        ftype, size, mtime, name = line.split("\t", 3)
    except ValueError:
        return None
    # Map stat's verbose types to our single-char convention.
    t = "f"
    if "directory" in ftype:
        t = "d"
    elif "symbolic link" in ftype:
        t = "l"
    return Entry(
        name=name.rsplit("/", 1)[-1],
        type=t,
        size=int(size or 0),
        mtime=float(mtime or 0),
    )


def stream_download(client: Any, container: str, path: str) -> Iterator[bytes]:
    """Yield file bytes from the container. Caller is responsible for
    setting Content-Length / Content-Disposition before iterating.

    `cat` returns nonzero only on errors that are visible to the user
    *before* any bytes are streamed (missing file, perm denied), so we
    don't bother wrapping with an exit-code check after the fact — by
    that point the response has already started.
    """
    argv = ["cat", "--", path]
    raw = client.api.exec_create(
        container,
        cmd=argv,
        user=EXEC_USER,
        stdin=False,
        stdout=True,
        stderr=False,
        tty=False,
    )
    exec_id = raw["Id"] if isinstance(raw, dict) else raw
    return client.api.exec_start(exec_id, stream=True)


def stream_upload(
    client: Any,
    container: str,
    dest_path: str,
    chunks: Iterator[bytes],
) -> int:
    """Stream `chunks` to dest_path inside the container, return total bytes.

    Implementation detail: we run `sh -c 'cat > "<path>"'` and pump bytes
    into stdin. Single-quote escaping the destination keeps shell-injection
    impossible even with quote-bearing filenames. Enforces MAX_UPLOAD_BYTES
    by raising once the limit is hit; partial files are NOT cleaned up
    automatically — the user can retry, the destination is overwritten on
    next attempt by the same `>` redirect.
    """
    quoted = "'" + dest_path.replace("'", "'\\''") + "'"
    argv = ["sh", "-c", f"cat > {quoted}"]
    raw = client.api.exec_create(
        container,
        cmd=argv,
        user=EXEC_USER,
        stdin=True,
        stdout=True,
        stderr=True,
        tty=False,
    )
    exec_id = raw["Id"] if isinstance(raw, dict) else raw
    sock = client.api.exec_start(exec_id, socket=True, demux=False)
    inner = getattr(sock, "_sock", None)
    total = 0
    try:
        for chunk in chunks:
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                raise FileOpError(
                    413,
                    f"upload exceeded {MAX_UPLOAD_BYTES} bytes",
                )
            if inner is not None:
                inner.sendall(chunk)
            elif hasattr(sock, "write"):
                sock.write(chunk)
        if inner is not None:
            try:
                inner.shutdown(1)
            except OSError:
                pass
    finally:
        try:
            sock.close()
        except Exception:
            pass
    # Wait for the exec to actually finish before returning. Without
    # this, exec_inspect can come back with ExitCode=None (still
    # running) and the caller would race ahead to a read while `cat >`
    # hasn't flushed yet — see the module docstring for the symptom.
    import time as _time
    deadline = _time.time() + 10.0
    while True:
        info = client.api.exec_inspect(exec_id)
        ec = info.get("ExitCode")
        if ec is not None:
            break
        if _time.time() > deadline:
            raise FileOpError(-1, f"upload exec did not complete in 10s for {dest_path}")
        _time.sleep(0.01)
    code = int(ec)
    if code != 0:
        # Shell errors (no such directory, etc.) come out here.
        raise FileOpError(code, f"sh exit {code} writing {dest_path}")
    return total


# Cap on inline read (the /api/files/read text editor path) — anything
# larger surfaces as 413 rather than dragging the editor down. Streaming
# binary playback (video, audio) goes through stream_download which has
# no cap by design.
MAX_READ_BYTES = 8 * 1024 * 1024  # 8 MiB


def read_file(
    client: Any, container: str, path: str, max_bytes: int = MAX_READ_BYTES,
) -> tuple[bytes, bool]:
    """Read up to ``max_bytes`` of ``path`` into memory.

    Returns ``(bytes, truncated)``: ``truncated`` is True if the file is
    larger than ``max_bytes`` (we read the first ``max_bytes`` only).
    Caller decides whether to surface a warning or just render what came
    back. Raises FileOpError on missing file / perm denied.
    """
    # head -c <N+1> tells us "is this larger than N?". We then truncate
    # to N for the actual return. One extra byte is cheap and lets the
    # caller distinguish "exactly N bytes" from "more than N bytes".
    argv = ["head", "-c", str(max_bytes + 1), "--", path]
    code, out, err = _exec_collect(client, container, argv)
    if code != 0:
        raise FileOpError(code, err.decode("utf-8", errors="replace"))
    truncated = len(out) > max_bytes
    if truncated:
        out = out[:max_bytes]
    return out, truncated


def write_file(
    client: Any, container: str, path: str, content: bytes,
) -> int:
    """Overwrite ``path`` with ``content``. Returns bytes written.

    Reuses the upload pipeline (single-chunk iterator) so the
    shell-quoting + uid 1000 + MAX_UPLOAD_BYTES protections all apply
    uniformly — no second `cat > path` invocation to audit.
    """
    return stream_upload(client, container, path, iter([content]))


def mkdir(client: Any, container: str, path: str) -> None:
    code, _, err = _exec_collect(
        client, container, ["mkdir", "-p", "--", path]
    )
    if code != 0:
        raise FileOpError(code, err.decode("utf-8", errors="replace"))


def search_files(
    client: Any,
    container: str,
    query: str,
    *,
    root: str = "/",
    max_results: int = 500,
    max_depth: int = 12,
) -> list[dict]:
    """Substring filename search via `find`, case-insensitive.

    Returns up to `max_results` matches as dicts with {path, name, type}.
    Excludes common noise paths (/proc, /sys, /dev, /nix/store, .git
    object dirs, __pycache__, node_modules, *.pyc) so root-rooted
    searches don't return 200k system files. `find -maxdepth` bounds
    runtime; the `head` after caps output size at the source so we
    never buffer a million paths in Python.
    """
    q = (query or "").strip()
    if not q:
        return []
    # POSIX shell quote — q can contain spaces, dots, etc. Forbid quote
    # and shell metachars in the query (a search box shouldn't need them)
    # to keep the shell-string safe-by-construction.
    safe_q = "".join(ch for ch in q if ch.isalnum() or ch in "._-+ ")
    if not safe_q:
        return []
    # Default exclusions — a balance between "user sees real /workspace
    # results first" and "doesn't return half of nixpkgs". Operator can
    # turn this off via `?include_system=1` on the API.
    exclude_paths = [
        "/proc", "/sys", "/dev", "/run", "/tmp", "/var/cache",
        "/var/lib/docker", "/nix/store",
    ]
    exclude_names = [
        "__pycache__", "node_modules", ".git/objects", ".cache",
        "site-packages",
    ]
    prune_clauses = " -o ".join(
        [f"-path {shlex_quote(p)}" for p in exclude_paths]
        + [f"-name {shlex_quote(n)}" for n in exclude_names]
    )
    # -iname for case-insensitive substring (wrapped in `*<q>*`).
    # -printf '%y\\t%p\\n' so we can tag each row with its type cheaply.
    pattern = f"*{safe_q}*"
    cmd = (
        f"find {shlex_quote(root)} -maxdepth {max_depth} "
        f"\\( {prune_clauses} \\) -prune -o "
        f"-iname {shlex_quote(pattern)} "
        f"-printf '%y\\t%p\\n' 2>/dev/null | "
        f"head -n {int(max_results)}"
    )
    code, stdout, _ = _exec_collect(client, container, ["sh", "-c", cmd])
    # `find` exits 1 when a subtree is unreadable; that's expected when
    # uid 1000 walks system paths. Ignore code, parse what we got.
    results: list[dict] = []
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        if not line or "\t" not in line:
            continue
        tag, path = line.split("\t", 1)
        if tag == "f":
            t = "f"
        elif tag == "d":
            t = "d"
        elif tag == "l":
            t = "l"
        else:
            continue
        if path == root:
            continue
        results.append({
            "path": path,
            "name": path.rsplit("/", 1)[-1] or path,
            "type": t,
        })
    return results


def shlex_quote(s: str) -> str:
    """Minimal POSIX shell quote — avoids importing shlex everywhere."""
    if all(c.isalnum() or c in "/._-+=@:" for c in s) and s:
        return s
    return "'" + s.replace("'", "'\\''") + "'"


def delete(client: Any, container: str, path: str) -> None:
    """rm -rf — recursive on dirs, also removes single files. The user
    runs as uid 1000 so root-owned files (e.g. /var) are safely refused
    by the kernel; we propagate that as a 403/500 from the caller."""
    code, _, err = _exec_collect(
        client, container, ["rm", "-rf", "--", path]
    )
    if code != 0:
        raise FileOpError(code, err.decode("utf-8", errors="replace"))


__all__ = [
    "EXEC_USER",
    "MAX_READ_BYTES",
    "MAX_UPLOAD_BYTES",
    "Entry",
    "FileOpError",
    "delete",
    "list_dir",
    "mkdir",
    "read_file",
    "stat_path",
    "stream_download",
    "stream_upload",
    "write_file",
]
