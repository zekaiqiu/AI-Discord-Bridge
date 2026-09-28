"""File operations against the user's per-user container /workspace tree.

Every operation runs as uid 1000 inside the container (the `app` user that
owns /workspace setgid 2775). Paths are validated against /workspace as a
prefix so a tree-traversal exploit can't reach /var/claude-runner or other
sensitive paths inside the container.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import posixpath
import re
import time
import uuid
from typing import Optional

from docker_exec import DockerExecError, run_exec

logger = logging.getLogger(__name__)


WORKSPACE_ROOT = "/workspace"

# Names we never display in the tree even if they exist (uid 1000 sometimes
# can't read them, and listing them just adds noise). /workspace/.claude is
# the dummy-credential bind that user_container.py seeds; treat it as system.
HIDDEN_TOP_LEVEL = frozenset({".claude", ".cache", ".drive-meta", ".drive-trash"})

# Per-user drive metadata + soft-delete locations. Kept under /workspace so
# they participate in the same container/uid model as the user's real files
# (uid 1000 owns both), but hidden from the IDE + drive's main listings via
# HIDDEN_TOP_LEVEL.
DRIVE_META_DIR = ".drive-meta"
DRIVE_STARRED_FILE = ".drive-meta/starred.json"
DRIVE_TRASH_DIR = ".drive-trash"

# Hard cap on a single read — refuse to load anything we can't render. The
# editor stays usable on a slow tab if we never hand it a 100 MB blob.
MAX_READ_BYTES = 5 * 1024 * 1024

# Same cap on writes — frontend should chunk anything bigger.
MAX_WRITE_BYTES = 5 * 1024 * 1024

# Multipart upload cap — separate from MAX_WRITE_BYTES so editor saves (which
# JSON-encode the whole body) stay light while drag-from-OS uploads can carry
# real datasets. 100 MB matches Cloudflare's free-tier tunnel default; for
# larger files use the terminal (`scp`/`curl --upload-file` into the user
# container).
MAX_UPLOAD_BYTES = 100 * 1024 * 1024


class FileOpError(Exception):
    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Directory listing cache.
#
# Every file-tree expand cost ~150 ms of pure list-dir work on top of the
# ~700 ms `_ensure_container_for` check. We cache list_dir() results in
# memory with a short TTL so repeated expands are ~free, and add explicit
# invalidation hooks so our own write/rename/delete/copy/upload endpoints
# never serve stale results for paths the user just changed in the UI.
#
# The TTL bounds staleness from terminal-side mutations the cache doesn't
# observe (`mv`, `cp`, `rm` etc.); 5 s is a balance between "cheap repeat
# expands" and "fresh enough that the user doesn't notice".
# ---------------------------------------------------------------------------

_LIST_CACHE: dict[tuple[str, str], tuple[dict, float]] = {}
_LIST_TTL_S = 5.0


def _cached_list_get(container: str, rel: str) -> Optional[dict]:
    """Return a fresh-enough cached listing for (container, rel) or None."""
    key = (container, rel)
    cached = _LIST_CACHE.get(key)
    if not cached:
        return None
    payload, ts = cached
    if time.monotonic() - ts >= _LIST_TTL_S:
        _LIST_CACHE.pop(key, None)
        return None
    return payload


def _cached_list_put(container: str, rel: str, payload: dict) -> None:
    _LIST_CACHE[(container, rel)] = (payload, time.monotonic())


def _try_channel(container: str, op: str, **kwargs) -> Optional[dict]:
    """Try to run `op` on the persistent exec channel. Returns the response
    dict on success or None on any channel error (caller falls back to the
    one-shot `run_exec` path).
    """
    try:
        import exec_channel  # local import to avoid bootstrap cycle in cold tests
        ch = exec_channel.get_channel(container)
        return ch.call(op, **kwargs)
    except Exception:  # noqa: BLE001
        return None


# Error-code map from helper's `{"error": "<code>"}` shape to FileOpError
# status_code. Codes not listed fall through to a generic 500 so the
# caller still surfaces a useful message.
_HELPER_ERROR_STATUS: dict[str, int] = {
    "not_found": 404,
    "permission_denied": 403,
    "not_a_file": 400,
    "too_large": 413,
    "exists": 409,
    "path_escapes_workspace": 400,
}


def _raise_for_helper_error(res: dict, default_msg: str) -> None:
    """If `res` is a helper error dict, translate it into a FileOpError."""
    err = res.get("error")
    if not err:
        return
    code = err.split(":", 1)[0].strip()
    status = _HELPER_ERROR_STATUS.get(code, 500)
    msg = {
        "not_found": "not found",
        "permission_denied": "permission denied",
        "not_a_file": "not a regular file",
        "too_large": f"{default_msg} exceeds size cap",
        "exists": "destination already exists",
        "path_escapes_workspace": "path escapes /workspace via symlink",
    }.get(code, f"{default_msg}: {err[:200]}")
    raise FileOpError(msg, status_code=status)


def invalidate_listing(container: str, path: str) -> None:
    """Drop the cached listing for `path`'s parent dir (and the path itself if dir).

    Callers: every mutating endpoint passes the *workspace-relative* path of
    the file/dir that was created/renamed/deleted. We drop both the parent
    (which contained the entry) and the path itself (in case the path was a
    directory whose contents may now be stale).
    """
    if path is None:
        return
    rel = path.strip().lstrip("/")
    parent = posixpath.dirname(rel) if rel else ""
    _LIST_CACHE.pop((container, parent), None)
    if rel:
        _LIST_CACHE.pop((container, rel), None)


# ---------------------------------------------------------------------------
# Path validation.
# ---------------------------------------------------------------------------

def _normalize(path: str) -> str:
    """Reject path traversal; return a clean absolute path under /workspace.

    Empty / "/" / "." all map to /workspace itself. Anything that escapes
    /workspace (whether by leading slash, ../, or weird symlink-ish input)
    raises FileOpError.

    Note: this is STRING-LEVEL only. A symlink under /workspace can still
    point outside (e.g. /workspace/foo -> /etc). Public ops that read or
    mutate files should additionally call _guard_path(container, abs_path)
    so symlinks are resolved inside the container before the op runs.
    """
    if path is None:
        raise FileOpError("missing path")
    if not isinstance(path, str):
        raise FileOpError("path must be a string")
    # Strip leading slash so it composes cleanly under WORKSPACE_ROOT.
    cleaned = path.strip()
    if cleaned in ("", "/", "."):
        return WORKSPACE_ROOT
    # Forbid absolute paths (the API is workspace-rooted by contract).
    if cleaned.startswith("/"):
        raise FileOpError("path must be relative to /workspace")
    # Forbid NULs (would split shell args).
    if "\x00" in cleaned:
        raise FileOpError("path contains NUL byte")
    abs_path = posixpath.normpath(posixpath.join(WORKSPACE_ROOT, cleaned))
    if abs_path != WORKSPACE_ROOT and not abs_path.startswith(WORKSPACE_ROOT + "/"):
        raise FileOpError("path escapes /workspace")
    return abs_path


def _guard_path(container: str, abs_path: str) -> str:
    """Resolve symlinks inside the container and verify the realpath stays
    under /workspace. Returns the resolved path on success.

    The host-side _normalize is string-only; without this guard a symlink
    under /workspace (e.g. /workspace/bad -> /etc) lets read/write/delete
    operate on a target outside the user's intended sandbox. Calls one
    short docker exec.

    For the workspace root itself nothing can be resolved away — return
    early. For not-yet-existing paths (write/mkdir target), os.path.realpath
    resolves the parent symlinks and appends the basename, so the check
    still catches a symlinked parent.
    """
    if abs_path == WORKSPACE_ROOT:
        return WORKSPACE_ROOT
    script = (
        "import os, sys\n"
        f"p = {json.dumps(abs_path)}\n"
        f"ws = {json.dumps(WORKSPACE_ROOT)}\n"
        "rp = os.path.realpath(p)\n"
        "if rp == ws or rp.startswith(ws + '/'):\n"
        "    print(rp)\n"
        "else:\n"
        "    sys.exit(7)\n"
    )
    try:
        out = run_exec(container, ["/usr/local/bin/python3", "-c", script])
    except DockerExecError as exc:
        if exc.exit_code == 7:
            raise FileOpError("path escapes /workspace via symlink", status_code=400) from exc
        raise FileOpError(f"path guard failed: {exc.stderr[:200]}", status_code=500) from exc
    resolved = out.decode("utf-8").strip()
    if not resolved or (resolved != WORKSPACE_ROOT and not resolved.startswith(WORKSPACE_ROOT + "/")):
        # Defense in depth: refuse anything that didn't come back clean.
        raise FileOpError("path escapes /workspace via symlink", status_code=400)
    return resolved


def _relative(abs_path: str) -> str:
    """Return the workspace-relative form for client display."""
    if abs_path == WORKSPACE_ROOT:
        return ""
    return abs_path[len(WORKSPACE_ROOT) + 1:]


# ---------------------------------------------------------------------------
# Operations.
# ---------------------------------------------------------------------------

# Output format for `ls --quoting-style=literal -1aA -p`:
# - `-A` skip . and ..
# - `-1` one per line
# - `-p` append / to dirs (cheap "is it a dir" signal without stat)
# - `--quoting-style=literal` raw filenames (no quoting/escaping)
# A separate stat-batch enriches with size + mtime in one shot.
def list_dir(container: str, path: str) -> dict:
    """Return {path, entries:[{name, kind, size, mtime}]}.

    Cached for `_LIST_TTL_S` per (container, rel) — mutating endpoints
    invalidate via `invalidate_listing` so the UI's own changes are
    reflected immediately; terminal-side mutations are bounded by the TTL.

    Cold-miss path uses the persistent exec channel (no docker exec
    startup, no python interpreter boot per call). On any channel error,
    falls back to the one-shot `run_exec` path below.
    """
    abs_path = _normalize(path)
    rel = _relative(abs_path)
    cached = _cached_list_get(container, rel)
    if cached is not None:
        return cached

    # Fast path: ask the persistent helper.
    try:
        import exec_channel  # local import to avoid bootstrap cycle
        ch = exec_channel.get_channel(container)
        res = ch.call("list_dir", root=abs_path, hidden_top=sorted(HIDDEN_TOP_LEVEL))
        if res.get("error") == "not_found":
            raise FileOpError("directory not found", status_code=404)
        if res.get("error") == "permission_denied":
            raise FileOpError("permission denied", status_code=403)
        if res.get("error") == "path_escapes_workspace":
            raise FileOpError("path escapes /workspace via symlink", status_code=400)
        if "entries" in res:
            payload = {"path": rel, "entries": res["entries"]}
            _cached_list_put(container, rel, payload)
            return payload
        # Helper returned an unexpected shape — fall through to the
        # one-shot path so the user gets a real listing, not a 500.
    except FileOpError:
        raise
    except Exception:  # noqa: BLE001  (broad fallback — channel errors are intentionally diverse)
        pass  # fall through to one-shot exec

    # Use a small python -c so we get a structured result in one round-trip
    # rather than parsing ls + stat. Inside the container, /usr/local/bin/python3
    # exists (baked into the chat image which the per-user container reuses).
    script = (
        "import json, os, sys\n"
        f"root = {json.dumps(abs_path)}\n"
        f"ws = {json.dumps(WORKSPACE_ROOT)}\n"
        # Symlink guard (Fix 6) inline: resolve realpath and reject escape.
        "root = os.path.realpath(root)\n"
        "if root != ws and not root.startswith(ws + '/'):\n"
        "    print(json.dumps({'error': 'path_escapes_workspace'}))\n"
        "    sys.exit(0)\n"
        "hidden_top = " + json.dumps(sorted(HIDDEN_TOP_LEVEL)) + "\n"
        "entries = []\n"
        "try:\n"
        "    names = sorted(os.listdir(root))\n"
        "except FileNotFoundError:\n"
        "    print(json.dumps({'error': 'not_found'}))\n"
        "    sys.exit(0)\n"
        "except PermissionError:\n"
        "    print(json.dumps({'error': 'permission_denied'}))\n"
        "    sys.exit(0)\n"
        "for name in names:\n"
        f"    if root == {json.dumps(WORKSPACE_ROOT)} and name in hidden_top:\n"
        "        continue\n"
        "    full = os.path.join(root, name)\n"
        "    try:\n"
        "        st = os.stat(full, follow_symlinks=False)\n"
        "    except OSError:\n"
        "        continue\n"
        "    import stat as _stat\n"
        "    if _stat.S_ISLNK(st.st_mode):\n"
        "        kind = 'link'\n"
        "    elif _stat.S_ISDIR(st.st_mode):\n"
        "        kind = 'dir'\n"
        "    elif _stat.S_ISREG(st.st_mode):\n"
        "        kind = 'file'\n"
        "    else:\n"
        "        kind = 'other'\n"
        "    entries.append({\n"
        "        'name': name,\n"
        "        'kind': kind,\n"
        "        'size': st.st_size,\n"
        "        'mtime': st.st_mtime,\n"
        "    })\n"
        "print(json.dumps({'entries': entries}))\n"
    )
    try:
        out = run_exec(container, ["/usr/local/bin/python3", "-c", script])
    except DockerExecError as exc:
        raise FileOpError(f"list failed: {exc.stderr[:200]}", status_code=500) from exc
    try:
        result = json.loads(out.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise FileOpError(f"list returned invalid JSON: {exc}", status_code=500) from exc
    if result.get("error") == "path_escapes_workspace":
        raise FileOpError("path escapes /workspace via symlink", status_code=400)
    if result.get("error") == "not_found":
        raise FileOpError("directory not found", status_code=404)
    if result.get("error") == "permission_denied":
        raise FileOpError("permission denied", status_code=403)
    payload = {"path": rel, "entries": result["entries"]}
    _cached_list_put(container, rel, payload)
    return payload


def read_file(container: str, path: str) -> dict:
    """Return {path, content, size, truncated}.

    Truncated=True if the file exceeds MAX_READ_BYTES. The frontend should
    surface a banner; we never silently lose data.
    """
    abs_path = _normalize(path)
    if abs_path == WORKSPACE_ROOT:
        raise FileOpError("cannot read the workspace root as a file", status_code=400)

    # Fast path: persistent exec channel.
    res = _try_channel(container, "read_file", path=abs_path, max_bytes=MAX_READ_BYTES)
    if res is not None:
        _raise_for_helper_error(res, "read")
        return {"path": _relative(abs_path), **res}

    # Stat first so we can refuse oversize cleanly with a precise error,
    # then cat. Two execs is fine for the cold-open case; for typical
    # editor reuse the file is small and cheap.
    # Symlink guard (Fix 6): resolve realpath inside the container and
    # verify containment before stat. Return the resolved path so the
    # subsequent head -c reads the verified target, not the symlink.
    stat_script = (
        "import json, os, stat, sys\n"
        f"p = {json.dumps(abs_path)}\n"
        f"ws = {json.dumps(WORKSPACE_ROOT)}\n"
        "rp = os.path.realpath(p)\n"
        "if rp != ws and not rp.startswith(ws + '/'):\n"
        "    print(json.dumps({'error': 'path_escapes_workspace'}))\n"
        "    sys.exit(0)\n"
        "try:\n"
        "    st = os.stat(rp, follow_symlinks=False)\n"
        "except FileNotFoundError:\n"
        "    print(json.dumps({'error': 'not_found'}))\n"
        "    sys.exit(0)\n"
        "except PermissionError:\n"
        "    print(json.dumps({'error': 'permission_denied'}))\n"
        "    sys.exit(0)\n"
        "if not stat.S_ISREG(st.st_mode):\n"
        "    print(json.dumps({'error': 'not_a_file'}))\n"
        "    sys.exit(0)\n"
        "print(json.dumps({'size': st.st_size, 'rp': rp}))\n"
    )
    try:
        stat_out = run_exec(container, ["/usr/local/bin/python3", "-c", stat_script])
    except DockerExecError as exc:
        raise FileOpError(f"stat failed: {exc.stderr[:200]}", status_code=500) from exc
    stat_res = json.loads(stat_out.decode("utf-8"))
    if stat_res.get("error") == "path_escapes_workspace":
        raise FileOpError("path escapes /workspace via symlink", status_code=400)
    if stat_res.get("error") == "not_found":
        raise FileOpError("file not found", status_code=404)
    if stat_res.get("error") == "permission_denied":
        raise FileOpError("permission denied", status_code=403)
    if stat_res.get("error") == "not_a_file":
        raise FileOpError("not a regular file", status_code=400)
    size = int(stat_res["size"])
    safe_path = stat_res.get("rp") or abs_path
    truncated = size > MAX_READ_BYTES
    read_bytes = min(size, MAX_READ_BYTES)

    if read_bytes == 0:
        return {"path": _relative(abs_path), "content": "", "size": size, "truncated": False}

    # head -c reads only the bytes we want — no need to load the full file
    # into the python helper, which would defeat the cap. Use the guarded
    # realpath so a symlinked path can't redirect the read.
    try:
        out = run_exec(container, ["head", "-c", str(read_bytes), safe_path])
    except DockerExecError as exc:
        raise FileOpError(f"read failed: {exc.stderr[:200]}", status_code=500) from exc

    # Decode as utf-8 with replacement so the editor can at least open binary
    # files; the frontend can warn the user.
    text = out.decode("utf-8", errors="replace")
    return {"path": _relative(abs_path), "content": text, "size": size, "truncated": truncated}


def read_file_bytes(container: str, path: str, max_bytes: int = MAX_UPLOAD_BYTES) -> tuple[bytes, int]:
    """Return (bytes, total_size) for binary viewers (image/pdf/xlsx/media).

    Caps at `max_bytes`; a file larger than the cap raises FileOpError(413).
    Mirrors read_file's stat-then-head shape but skips utf-8 decode.
    """
    abs_path = _normalize(path)
    if abs_path == WORKSPACE_ROOT:
        raise FileOpError("cannot read the workspace root as a file", status_code=400)
    # Symlink guard (Fix 6) inlined with the stat — same shape as read_file.
    stat_script = (
        "import json, os, stat, sys\n"
        f"p = {json.dumps(abs_path)}\n"
        f"ws = {json.dumps(WORKSPACE_ROOT)}\n"
        "rp = os.path.realpath(p)\n"
        "if rp != ws and not rp.startswith(ws + '/'):\n"
        "    print(json.dumps({'error': 'path_escapes_workspace'}))\n"
        "    sys.exit(0)\n"
        "try:\n"
        "    st = os.stat(rp, follow_symlinks=False)\n"
        "except FileNotFoundError:\n"
        "    print(json.dumps({'error': 'not_found'}))\n"
        "    sys.exit(0)\n"
        "except PermissionError:\n"
        "    print(json.dumps({'error': 'permission_denied'}))\n"
        "    sys.exit(0)\n"
        "if not stat.S_ISREG(st.st_mode):\n"
        "    print(json.dumps({'error': 'not_a_file'}))\n"
        "    sys.exit(0)\n"
        "print(json.dumps({'size': st.st_size, 'rp': rp}))\n"
    )
    try:
        stat_out = run_exec(container, ["/usr/local/bin/python3", "-c", stat_script])
    except DockerExecError as exc:
        raise FileOpError(f"stat failed: {exc.stderr[:200]}", status_code=500) from exc
    stat_res = json.loads(stat_out.decode("utf-8"))
    if stat_res.get("error") == "path_escapes_workspace":
        raise FileOpError("path escapes /workspace via symlink", status_code=400)
    if stat_res.get("error") == "not_found":
        raise FileOpError("file not found", status_code=404)
    if stat_res.get("error") == "permission_denied":
        raise FileOpError("permission denied", status_code=403)
    if stat_res.get("error") == "not_a_file":
        raise FileOpError("not a regular file", status_code=400)
    size = int(stat_res["size"])
    safe_path = stat_res.get("rp") or abs_path
    if size > max_bytes:
        raise FileOpError(
            f"file exceeds max view size of {max_bytes} bytes",
            status_code=413,
        )
    if size == 0:
        return b"", 0
    try:
        out = run_exec(container, ["head", "-c", str(size), safe_path])
    except DockerExecError as exc:
        raise FileOpError(f"read failed: {exc.stderr[:200]}", status_code=500) from exc
    return out, size


def write_file(container: str, path: str, content: str) -> dict:
    """Atomically replace the file at `path` with `content`.

    Writes to `<path>.tmp-<rand>` then renames — so a torn write never leaves
    a partial file under the canonical name (matters for `python file.py`
    that may be reading a half-written script).
    """
    abs_path = _normalize(path)
    if abs_path == WORKSPACE_ROOT:
        raise FileOpError("cannot write to the workspace root", status_code=400)
    payload = content.encode("utf-8")
    if len(payload) > MAX_WRITE_BYTES:
        raise FileOpError(
            f"file exceeds max write size of {MAX_WRITE_BYTES} bytes",
            status_code=413,
        )

    # Fast path: persistent exec channel. Helper does the same
    # mkdir-parent + tempfile + atomic rename dance.
    res = _try_channel(container, "write_file", path=abs_path, content=content, max_bytes=MAX_WRITE_BYTES)
    if res is not None:
        _raise_for_helper_error(res, "write")
        return {"path": _relative(abs_path), "size": int(res.get("size", len(payload)))}

    # Slow-path: explicit symlink guard (Fix 6) since the sh script below
    # doesn't validate. Adds one docker exec but only when the persistent
    # channel is unavailable.
    safe_path = _guard_path(container, abs_path)
    # mkdir -p the parent then atomic write. `cat > $tmp && mv` keeps it
    # one round-trip (vs python -c which would pull the payload onto argv).
    parent = posixpath.dirname(safe_path) or WORKSPACE_ROOT
    tmp_path = f"{safe_path}.tmp.dev-wizerith"
    script = (
        f"set -e\n"
        f"mkdir -p {shquote(parent)}\n"
        f"cat > {shquote(tmp_path)}\n"
        f"mv {shquote(tmp_path)} {shquote(safe_path)}\n"
    )
    try:
        run_exec(
            container,
            ["sh", "-c", script],
            stdin_bytes=payload,
            timeout=60.0,
        )
    except DockerExecError as exc:
        raise FileOpError(f"write failed: {exc.stderr[:200]}", status_code=500) from exc
    return {"path": _relative(abs_path), "size": len(payload)}


def mkdir(container: str, path: str) -> dict:
    abs_path = _normalize(path)
    if abs_path == WORKSPACE_ROOT:
        raise FileOpError("workspace root already exists", status_code=400)
    res = _try_channel(container, "mkdir", path=abs_path)
    if res is not None:
        _raise_for_helper_error(res, "mkdir")
        return {"path": _relative(abs_path)}
    # Slow-path symlink guard (Fix 6).
    safe_path = _guard_path(container, abs_path)
    try:
        run_exec(container, ["mkdir", "-p", safe_path])
    except DockerExecError as exc:
        raise FileOpError(f"mkdir failed: {exc.stderr[:200]}", status_code=500) from exc
    return {"path": _relative(abs_path)}


def search(container: str, query: str, root: str = "/", max_results: int = 500, max_depth: int = 12) -> dict:
    """Filename substring search rooted at `root`.

    Same shape as term-router's /api/files/search: case-insensitive
    `find -iname '*q*'` with prune clauses for common noise paths
    (/proc, /sys, /nix/store, __pycache__, node_modules, .git/objects,
    site-packages). Caps depth and result count so a search for 'a'
    doesn't enumerate 200k files.
    """
    q = (query or "").strip()
    if not q:
        return {"query": q, "root": root, "results": []}
    safe_q = "".join(ch for ch in q if ch.isalnum() or ch in "._-+ ")
    if not safe_q:
        return {"query": q, "root": root, "results": []}
    # Validate root the same way file ops do — but allow `/` since we
    # explicitly want system-wide search.
    safe_root = root if root in ("/", "") else _normalize(root[len(WORKSPACE_ROOT) + 1:] if root.startswith(WORKSPACE_ROOT) else "")
    if root == "/" or not root:
        safe_root = "/"
    exclude_paths = [
        "/proc", "/sys", "/dev", "/run", "/tmp", "/var/cache",
        "/var/lib/docker", "/nix/store",
    ]
    exclude_names = ["__pycache__", "node_modules", ".git/objects", ".cache", "site-packages"]
    prune_clauses = " -o ".join(
        [f"-path {shquote(p)}" for p in exclude_paths]
        + [f"-name {shquote(n)}" for n in exclude_names]
    )
    pattern = f"*{safe_q}*"
    cmd = (
        f"find {shquote(safe_root)} -maxdepth {max_depth} "
        f"\\( {prune_clauses} \\) -prune -o "
        f"-iname {shquote(pattern)} "
        f"-printf '%y\\t%p\\n' 2>/dev/null | "
        f"head -n {int(max_results)}"
    )
    try:
        out = run_exec(container, ["sh", "-c", cmd], timeout=20.0)
    except DockerExecError as exc:
        # `find` exits non-zero when subtrees are unreadable — that's
        # expected for uid 1000 walking /root etc. Surface only if
        # nothing came back at all.
        if not exc.stderr or exc.exit_code in (1,):
            return {"query": q, "root": safe_root, "results": []}
        raise FileOpError(f"search failed: {exc.stderr[:200]}", status_code=500) from exc
    results = []
    for line in out.decode("utf-8", errors="replace").splitlines():
        if not line or "\t" not in line:
            continue
        tag, path = line.split("\t", 1)
        if tag not in ("f", "d", "l"):
            continue
        if path == safe_root:
            continue
        results.append({
            "path": path,
            "name": path.rsplit("/", 1)[-1] or path,
            "type": tag,
        })
    return {"query": q, "root": safe_root, "results": results}


def delete(container: str, path: str) -> dict:
    abs_path = _normalize(path)
    if abs_path == WORKSPACE_ROOT:
        raise FileOpError("cannot delete the workspace root", status_code=400)
    res = _try_channel(container, "delete", path=abs_path)
    if res is not None:
        # `not_found` for delete is a no-op success in the legacy path
        # (rm -rf doesn't error on missing). Match that here.
        if res.get("error") == "not_found":
            return {"path": _relative(abs_path)}
        _raise_for_helper_error(res, "delete")
        return {"path": _relative(abs_path)}
    # Slow-path symlink guard (Fix 6): refuse to rm -rf a path whose
    # realpath escapes /workspace.
    safe_path = _guard_path(container, abs_path)
    try:
        run_exec(container, ["rm", "-rf", "--", safe_path])
    except DockerExecError as exc:
        raise FileOpError(f"delete failed: {exc.stderr[:200]}", status_code=500) from exc
    return {"path": _relative(abs_path)}


def copy(container: str, src: str, dst: str) -> dict:
    """Recursive copy of src to dst (both workspace-relative).

    Refuses to overwrite — caller resolves a unique destination first
    (used by the file tree's "Duplicate" context-menu action). For dirs,
    copies the whole subtree.
    """
    src_abs = _normalize(src)
    dst_abs = _normalize(dst)
    if src_abs == WORKSPACE_ROOT or dst_abs == WORKSPACE_ROOT:
        raise FileOpError("cannot copy the workspace root", status_code=400)
    if src_abs == dst_abs:
        raise FileOpError("source and destination are the same", status_code=400)
    if dst_abs.startswith(src_abs + "/"):
        raise FileOpError("destination is inside source", status_code=400)

    res = _try_channel(container, "copy", src=src_abs, dst=dst_abs)
    if res is not None:
        _raise_for_helper_error(res, "copy")
        return {"src": _relative(src_abs), "dst": _relative(dst_abs)}

    # Slow-path symlink guard (Fix 6): both src and dst.
    src_safe = _guard_path(container, src_abs)
    dst_safe = _guard_path(container, dst_abs)
    parent = posixpath.dirname(dst_safe) or WORKSPACE_ROOT
    # -n / --no-clobber so a pre-existing dst is not silently overwritten;
    # the client picks a unique name. -R for directory recursion.
    script = (
        f"set -e\n"
        f"mkdir -p {shquote(parent)}\n"
        f"if [ -e {shquote(dst_safe)} ]; then echo exists >&2; exit 17; fi\n"
        f"cp -R -- {shquote(src_safe)} {shquote(dst_safe)}\n"
    )
    try:
        run_exec(container, ["sh", "-c", script])
    except DockerExecError as exc:
        if "exists" in (exc.stderr or ""):
            raise FileOpError("destination already exists", status_code=409) from exc
        raise FileOpError(f"copy failed: {exc.stderr[:200]}", status_code=500) from exc
    return {"src": _relative(src_abs), "dst": _relative(dst_abs)}


# ---------------------------------------------------------------------------
# Compress (streaming).
#
# Compress is the slow op (seconds to minutes on big dirs). To keep the UI
# from looking frozen, we run the work in a background asyncio task and
# expose progress via SSE — the same shape as `/api/jobs/<id>/stream` but
# tailored to compress events (`scanned` / `progress` / `done` / `error`).
#
# The in-container helper emits NDJSON lines on stdout; the supervisor here
# parses them, fans events out to subscribers, and stamps the final state
# on the CompressJob so a late-arriving SSE reconnect can replay buffered
# events without losing anything.
# ---------------------------------------------------------------------------

# Cap on the uncompressed footprint we're willing to zip. Refused cleanly
# (status 413) before any work starts; for bigger archives, use the terminal.
MAX_COMPRESS_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB

# Hard wall-clock cap on a single compress job — the subprocess is killed
# past this. 30 min matches "I left this running while I went to lunch."
COMPRESS_TIMEOUT_S = 30 * 60.0


@dataclasses.dataclass
class CompressJob:
    """One compress job: 1 source dir → 1 zip file in the parent.

    Subscribers attach via `subscribe_compress` and read events from their
    queue. Buffered events are replayed on reconnect (drop nothing).
    """
    id: str
    email: str
    container: str
    src: str        # workspace-relative form for client display
    src_abs: str    # /workspace/<...>
    started_at: float
    status: str = "running"           # running | done | error | cancelled
    total_bytes: int = 0
    total_files: int = 0
    bytes_done: int = 0
    files_done: int = 0
    dst: Optional[str] = None         # workspace-relative dst, set on `done`
    dst_abs: Optional[str] = None     # absolute dst inside container
    size: Optional[int] = None        # output zip size
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    events: list[dict] = dataclasses.field(default_factory=list)
    events_cap: int = 500
    subscribers: list[asyncio.Queue] = dataclasses.field(default_factory=list)
    _proc: Optional[asyncio.subprocess.Process] = None
    _task: Optional[asyncio.Task] = None

    def append_event(self, evt: dict) -> None:
        self.events.append(evt)
        if len(self.events) > self.events_cap:
            self.events = self.events[-self.events_cap:]
        for q in list(self.subscribers):
            try:
                q.put_nowait(evt)
            except asyncio.QueueFull:
                pass


class _CompressRegistry:
    """In-memory registry of compress jobs, keyed by id."""

    # Grace window after completion: SSE reconnects can still get the buffer.
    DONE_GRACE_S = 10 * 60.0

    def __init__(self) -> None:
        self._jobs: dict[str, CompressJob] = {}
        self._lock = asyncio.Lock()

    def create(self, *, email: str, container: str, src_rel: str, src_abs: str) -> CompressJob:
        job = CompressJob(
            id=uuid.uuid4().hex,
            email=email,
            container=container,
            src=src_rel,
            src_abs=src_abs,
            started_at=time.time(),
        )
        self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Optional[CompressJob]:
        return self._jobs.get(job_id)

    async def gc(self) -> int:
        now = time.time()
        removed = 0
        for jid, j in list(self._jobs.items()):
            if j.status != "running" and (now - j.started_at) > self.DONE_GRACE_S:
                self._jobs.pop(jid, None)
                removed += 1
        return removed


compress_registry = _CompressRegistry()


def _compress_helper_script(src_abs: str, max_bytes: int) -> str:
    """Build the in-container python helper script for one compress job.

    Emits NDJSON events on stdout (one per line, flushed):
      {"event": "scanned",  "total_bytes": N, "total_files": M}
      {"event": "progress", "bytes_done": N, "files_done": M}     (throttled)
      {"event": "done",     "dst": "...",     "size": N}
      {"event": "error",    "code": "...",    "message": "..."}

    Same arcname sanitization as the previous synchronous compress: colons,
    `*?<>|"\\`, and control chars are replaced with `_`; trailing dots /
    spaces stripped (Windows compat).
    """
    return (
        "import json, os, stat, sys, time, zipfile\n"
        f"src = {json.dumps(src_abs)}\n"
        f"max_bytes = {max_bytes}\n"
        f"WORKSPACE_ROOT = {json.dumps(WORKSPACE_ROOT)}\n"
        "BAD_CHARS = ':*?<>|\"\\\\'\n"
        "PROGRESS_INTERVAL = 0.25  # seconds\n"
        "def emit(event, **kw):\n"
        "    sys.stdout.write(json.dumps({'event': event, **kw}, separators=(',',':')) + '\\n')\n"
        "    sys.stdout.flush()\n"
        "def sanitize_segment(name):\n"
        "    out = ''.join('_' if c in BAD_CHARS or ord(c) < 32 else c for c in name)\n"
        "    out = out.rstrip(' .')\n"
        "    return out or '_'\n"
        "def sanitize_arc(arc):\n"
        "    return '/'.join(sanitize_segment(s) for s in arc.split('/'))\n"
        "# Symlink guard (Fix 6): resolve src and refuse if it escapes workspace.\n"
        "src = os.path.realpath(src)\n"
        "if src != WORKSPACE_ROOT and not src.startswith(WORKSPACE_ROOT + '/'):\n"
        "    emit('error', code='path_escapes_workspace', message='source escapes /workspace'); sys.exit(0)\n"
        "try:\n"
        "    st = os.stat(src, follow_symlinks=False)\n"
        "except FileNotFoundError:\n"
        "    emit('error', code='not_found', message='source not found'); sys.exit(0)\n"
        "except PermissionError:\n"
        "    emit('error', code='permission_denied', message='permission denied'); sys.exit(0)\n"
        "if not stat.S_ISDIR(st.st_mode):\n"
        "    emit('error', code='not_a_directory', message='source is not a directory'); sys.exit(0)\n"
        "parent = os.path.dirname(src) or '/'\n"
        "base = os.path.basename(src)\n"
        "safe_base = sanitize_segment(base)\n"
        "# Phase 1: walk + size. Collect (full, arc, is_dir, size) to avoid\n"
        "# walking twice (saves ~25%% wall-time on big trees).\n"
        "items = []\n"
        "total_bytes = 0\n"
        "total_files = 0\n"
        "for r, ds, fs in os.walk(src, followlinks=False):\n"
        "    for d in ds:\n"
        "        full = os.path.join(r, d)\n"
        "        rel = os.path.relpath(full, src).replace(os.sep, '/')\n"
        "        items.append((full, sanitize_arc(safe_base + '/' + rel), True, 0))\n"
        "    for f in fs:\n"
        "        full = os.path.join(r, f)\n"
        "        rel = os.path.relpath(full, src).replace(os.sep, '/')\n"
        "        try: sz = os.path.getsize(full)\n"
        "        except OSError: sz = 0\n"
        "        total_bytes += sz\n"
        "        total_files += 1\n"
        "        items.append((full, sanitize_arc(safe_base + '/' + rel), False, sz))\n"
        "    if total_bytes > max_bytes:\n"
        "        emit('error', code='too_large', message=f'exceeds {max_bytes} bytes', size=total_bytes); sys.exit(0)\n"
        "emit('scanned', total_bytes=total_bytes, total_files=total_files)\n"
        "# Pick a unique dst name.\n"
        "candidate = safe_base + '.zip'\n"
        "i = 2\n"
        "while os.path.lexists(os.path.join(parent, candidate)):\n"
        "    candidate = safe_base + ' ' + str(i) + '.zip'\n"
        "    i += 1\n"
        "dst = os.path.join(parent, candidate)\n"
        "tmp = dst + '.partial'\n"
        "bytes_done = 0\n"
        "files_done = 0\n"
        "last_emit = time.monotonic()\n"
        "try:\n"
        "    with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED, allowZip64=True) as zf:\n"
        "        zf.write(src, arcname=safe_base)\n"
        "        for full, arc, is_dir, sz in items:\n"
        "            try:\n"
        "                zf.write(full, arcname=arc)\n"
        "                if not is_dir:\n"
        "                    bytes_done += sz\n"
        "                    files_done += 1\n"
        "            except OSError:\n"
        "                pass\n"
        "            now = time.monotonic()\n"
        "            if now - last_emit >= PROGRESS_INTERVAL:\n"
        "                emit('progress', bytes_done=bytes_done, files_done=files_done)\n"
        "                last_emit = now\n"
        "    os.rename(tmp, dst)\n"
        "except PermissionError:\n"
        "    try: os.unlink(tmp)\n"
        "    except OSError: pass\n"
        "    emit('error', code='permission_denied', message='permission denied'); sys.exit(0)\n"
        "except OSError as e:\n"
        "    try: os.unlink(tmp)\n"
        "    except OSError: pass\n"
        "    emit('error', code='compress_failed', message=str(e)); sys.exit(0)\n"
        "# Final progress = total (paper over throttling).\n"
        "emit('progress', bytes_done=bytes_done, files_done=files_done)\n"
        "try: out_size = os.path.getsize(dst)\n"
        "except OSError: out_size = 0\n"
        "emit('done', dst=dst, size=out_size)\n"
    )


def start_compress_job(*, email: str, container: str, src: str) -> CompressJob:
    """Validate input, create a CompressJob, spawn the supervisor task.

    Synchronous return — the actual zipping happens in the background;
    progress + completion flow through the SSE stream endpoint.
    """
    src_abs = _normalize(src)
    if src_abs == WORKSPACE_ROOT:
        raise FileOpError("cannot compress the workspace root", status_code=400)
    job = compress_registry.create(
        email=email, container=container, src_rel=_relative(src_abs), src_abs=src_abs,
    )
    job._task = asyncio.create_task(_supervise_compress(job))
    return job


async def _supervise_compress(job: CompressJob) -> None:
    """Spawn the docker exec helper, parse NDJSON from stdout, fan out.

    Wraps a wall-clock timeout (`COMPRESS_TIMEOUT_S`) so a runaway compress
    eventually frees its slot. Stderr is captured for diagnostics on the
    `error` path (e.g. helper crashed before emitting `error`).
    """
    script = _compress_helper_script(job.src_abs, MAX_COMPRESS_BYTES)
    argv = [
        "docker", "exec", "-i", "--user", "1000:1000", job.container,
        "/usr/local/bin/python3", "-u", "-c", script,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )
    except Exception as exc:  # noqa: BLE001
        job.status = "error"
        job.error_code = "spawn_failed"
        job.error_message = str(exc)
        job.append_event({"event": "error", "code": "spawn_failed", "message": str(exc)})
        _wake_subscribers(job)
        return
    job._proc = proc
    deadline = time.monotonic() + COMPRESS_TIMEOUT_S
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                proc.kill()
                if job.status == "running":
                    job.status = "error"
                    job.error_code = "timeout"
                    job.error_message = f"compress timed out after {COMPRESS_TIMEOUT_S:.0f}s"
                    job.append_event({"event": "error", "code": "timeout",
                                       "message": job.error_message})
                break
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
            except asyncio.TimeoutError:
                continue  # loop re-evaluates the deadline above
            if not line:
                break
            try:
                evt = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                logger.warning("compress: unparseable line: %r", line[:200])
                continue
            _apply_compress_event(job, evt)
        # Drain process.
        try:
            await proc.wait()
        except Exception:  # noqa: BLE001
            pass
        # If we reached here without a terminal event, classify as error.
        if job.status == "running":
            stderr = b""
            try:
                if proc.stderr is not None:
                    stderr = (await proc.stderr.read()) or b""
            except Exception:  # noqa: BLE001
                pass
            job.status = "error"
            job.error_code = "stream_truncated"
            job.error_message = (
                f"compress stream ended unexpectedly (exit={proc.returncode}); "
                f"stderr={stderr.decode('utf-8', 'replace')[:300]!r}"
            )
            job.append_event({"event": "error", "code": job.error_code,
                              "message": job.error_message})
    finally:
        _wake_subscribers(job)


def _apply_compress_event(job: CompressJob, evt: dict) -> None:
    """Update job state from an NDJSON event, then fan out to subscribers."""
    et = evt.get("event")
    if et == "scanned":
        job.total_bytes = int(evt.get("total_bytes", 0))
        job.total_files = int(evt.get("total_files", 0))
    elif et == "progress":
        job.bytes_done = int(evt.get("bytes_done", 0))
        job.files_done = int(evt.get("files_done", 0))
    elif et == "done":
        job.status = "done"
        dst_abs = str(evt.get("dst", ""))
        job.dst_abs = dst_abs
        job.dst = _relative(dst_abs) if dst_abs else None
        job.size = int(evt.get("size", 0))
        # Paper over progress-emission throttling so the final % is 100.
        if job.total_bytes:
            job.bytes_done = job.total_bytes
        if job.total_files:
            job.files_done = job.total_files
    elif et == "error":
        job.status = "error"
        job.error_code = str(evt.get("code", "error"))
        job.error_message = str(evt.get("message", "compress failed"))
    job.append_event(evt)


def _wake_subscribers(job: CompressJob) -> None:
    for q in list(job.subscribers):
        try:
            q.put_nowait(None)
        except asyncio.QueueFull:
            pass


def subscribe_compress(job: CompressJob) -> asyncio.Queue:
    """Attach a subscriber queue. Caller is responsible for `unsubscribe`."""
    q: asyncio.Queue = asyncio.Queue(maxsize=1024)
    job.subscribers.append(q)
    return q


def unsubscribe_compress(job: CompressJob, q: asyncio.Queue) -> None:
    try:
        job.subscribers.remove(q)
    except ValueError:
        pass


def compress_result_payload(job: CompressJob) -> dict:
    """Final state of a finished compress job, in the shape the frontend wants."""
    return {
        "id": job.id,
        "status": job.status,
        "src": job.src,
        "dst": job.dst,
        "size": job.size,
        "total_bytes": job.total_bytes,
        "total_files": job.total_files,
        "bytes_done": job.bytes_done,
        "files_done": job.files_done,
        "error": (
            {"code": job.error_code, "message": job.error_message}
            if job.status == "error" else None
        ),
    }


def rename(container: str, src: str, dst: str) -> dict:
    src_abs = _normalize(src)
    dst_abs = _normalize(dst)
    if src_abs == WORKSPACE_ROOT or dst_abs == WORKSPACE_ROOT:
        raise FileOpError("cannot rename the workspace root", status_code=400)

    res = _try_channel(container, "rename", src=src_abs, dst=dst_abs)
    if res is not None:
        _raise_for_helper_error(res, "rename")
        return {"src": _relative(src_abs), "dst": _relative(dst_abs)}

    # Slow-path symlink guard (Fix 6).
    src_safe = _guard_path(container, src_abs)
    dst_safe = _guard_path(container, dst_abs)
    parent = posixpath.dirname(dst_safe) or WORKSPACE_ROOT
    script = f"set -e\nmkdir -p {shquote(parent)}\nmv {shquote(src_safe)} {shquote(dst_safe)}\n"
    try:
        run_exec(container, ["sh", "-c", script])
    except DockerExecError as exc:
        raise FileOpError(f"rename failed: {exc.stderr[:200]}", status_code=500) from exc
    return {"src": _relative(src_abs), "dst": _relative(dst_abs)}


# ---------------------------------------------------------------------------
# Tiny shell-quote helper. Python's shlex.quote works but emits empty-string
# `''` and over-quotes — these always run through `sh -c` so we want the
# minimal POSIX-safe form. We restrict to absolute paths anyway, so the
# input shape is narrow.
# ---------------------------------------------------------------------------

_SAFE = re.compile(r"^[A-Za-z0-9_./@:+=-]+$")


def shquote(s: str) -> str:
    if _SAFE.match(s):
        return s
    # Replace ' → '\'' and wrap in single quotes.
    return "'" + s.replace("'", "'\\''") + "'"


# ---------------------------------------------------------------------------
# Drive-only ops (Recent / Starred / Trash).
#
# These back the drive.wizerith.ai SPA's Recent, Starred, and Trash views.
# Implemented as one-shot `python3 -c` scripts via run_exec — none are
# hot-path enough to justify the persistent exec_channel.
#
# Storage layout per user container (all under /workspace, hidden):
#   /workspace/.drive-meta/starred.json   — {"paths": ["foo/bar.txt", ...]}
#   /workspace/.drive-trash/<trash_id>/payload   — moved file/dir
#   /workspace/.drive-trash/<trash_id>/meta.json — {original_path, deleted_at,
#                                                   name, kind, size}
#
# The trash layout uses a directory per item so the original basename can
# survive verbatim inside `payload` (no name mangling) while metadata sits
# in a sidecar — restore reads meta.json, moves payload back to
# original_path (with a numeric suffix if that path is now occupied).
# ---------------------------------------------------------------------------


def _run_helper(container: str, script: str, timeout: float = 30.0) -> dict:
    """Run a python script in the container and JSON-decode its stdout.

    Helper convention: the script either prints `{"error": "<code>"}` and
    exits 0, OR prints a result dict and exits 0. Non-zero exit + stderr is
    a host-side failure (container died, python missing, etc.) and surfaces
    as a 500.
    """
    try:
        out = run_exec(container, ["/usr/local/bin/python3", "-c", script], timeout=timeout)
    except DockerExecError as exc:
        raise FileOpError(f"helper failed: {exc.stderr[:200]}", status_code=500) from exc
    try:
        return json.loads(out.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise FileOpError(f"helper returned invalid JSON: {exc}", status_code=500) from exc


def list_recent(container: str, limit: int = 100, max_age_days: int = 30) -> dict:
    """Return up to `limit` most-recently-modified regular files anywhere
    under /workspace (excluding hidden tops + the usual noise dirs).

    Sorted by mtime descending. `max_age_days` is a soft cap to keep the
    walk cheap on workspaces with millions of stale files; 0 disables it.
    Returns the same item shape as list_dir entries plus an absolute-style
    `path` field (workspace-relative) so the frontend can render+navigate
    without rejoining.
    """
    limit = max(1, min(int(limit or 100), 500))
    max_age = max(0, int(max_age_days or 0))
    hidden = sorted(HIDDEN_TOP_LEVEL)
    script = (
        "import json, os, stat as _stat, time\n"
        f"ROOT = {json.dumps(WORKSPACE_ROOT)}\n"
        f"LIMIT = {limit}\n"
        f"MAX_AGE = {max_age * 86400}\n"
        f"HIDDEN = set({json.dumps(hidden)})\n"
        # Names we always skip while walking; cheap to maintain, keeps recent
        # from filling up with caches and node_modules churn.
        "SKIP_DIRS = {'__pycache__', 'node_modules', '.git', '.cache', 'site-packages', '.venv', 'venv', '.next', '.tox', 'dist', 'build'}\n"
        "cutoff = (time.time() - MAX_AGE) if MAX_AGE > 0 else 0\n"
        "files = []\n"
        "for dirpath, dirnames, filenames in os.walk(ROOT, followlinks=False):\n"
        "    rel = os.path.relpath(dirpath, ROOT)\n"
        # Skip the workspace's own hidden tops; everything inside is opaque.
        "    if dirpath == ROOT:\n"
        "        dirnames[:] = [d for d in dirnames if d not in HIDDEN and d not in SKIP_DIRS]\n"
        "    else:\n"
        "        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith('.')]\n"
        "    for name in filenames:\n"
        "        if name.startswith('.'):\n"
        "            continue\n"
        "        full = os.path.join(dirpath, name)\n"
        "        try:\n"
        "            st = os.stat(full, follow_symlinks=False)\n"
        "        except OSError:\n"
        "            continue\n"
        "        if not _stat.S_ISREG(st.st_mode):\n"
        "            continue\n"
        "        if cutoff and st.st_mtime < cutoff:\n"
        "            continue\n"
        "        rel_path = os.path.relpath(full, ROOT)\n"
        "        files.append({\n"
        "            'name': name,\n"
        "            'path': rel_path,\n"
        "            'kind': 'file',\n"
        "            'size': st.st_size,\n"
        "            'mtime': st.st_mtime,\n"
        "        })\n"
        "files.sort(key=lambda e: e['mtime'], reverse=True)\n"
        "print(json.dumps({'items': files[:LIMIT]}))\n"
    )
    res = _run_helper(container, script, timeout=30.0)
    return {"items": res.get("items", [])}


def _starred_helper_io(container: str, mutation: Optional[str] = None,
                       path: Optional[str] = None) -> dict:
    """Read/mutate the starred manifest atomically inside the container.

    `mutation` ∈ {None (read+enrich), 'add', 'remove'}. For mutations,
    returns the new manifest paths list. For reads, returns enriched items
    with current size/mtime/kind; missing paths are silently evicted.
    """
    if mutation not in (None, "add", "remove"):
        raise FileOpError(f"invalid starred mutation: {mutation}")
    path_arg = "" if path is None else _relative(_normalize(path))
    if mutation in ("add", "remove") and not path_arg:
        raise FileOpError("cannot star the workspace root", status_code=400)
    script = (
        "import json, os, stat as _stat, tempfile\n"
        f"ROOT = {json.dumps(WORKSPACE_ROOT)}\n"
        f"META_DIR = {json.dumps(posixpath.join(WORKSPACE_ROOT, DRIVE_META_DIR))}\n"
        f"STARRED = {json.dumps(posixpath.join(WORKSPACE_ROOT, DRIVE_STARRED_FILE))}\n"
        f"MUTATION = {json.dumps(mutation or '')}\n"
        f"REL = {json.dumps(path_arg)}\n"
        "data = {'paths': []}\n"
        "try:\n"
        "    with open(STARRED, 'r', encoding='utf-8') as f:\n"
        "        loaded = json.load(f)\n"
        "        if isinstance(loaded, dict) and isinstance(loaded.get('paths'), list):\n"
        "            data = {'paths': [p for p in loaded['paths'] if isinstance(p, str) and p]}\n"
        "except (FileNotFoundError, json.JSONDecodeError, OSError):\n"
        "    pass\n"
        "seen = set(data['paths'])\n"
        "if MUTATION == 'add':\n"
        "    if REL not in seen:\n"
        "        data['paths'].append(REL)\n"
        "elif MUTATION == 'remove':\n"
        "    data['paths'] = [p for p in data['paths'] if p != REL]\n"
        "if MUTATION:\n"
        "    try:\n"
        "        os.makedirs(META_DIR, exist_ok=True)\n"
        "    except OSError as exc:\n"
        "        print(json.dumps({'error': 'mkdir_failed: ' + str(exc)}))\n"
        "        raise SystemExit(0)\n"
        "    fd, tmp = tempfile.mkstemp(prefix='.tmp.starred.', dir=META_DIR)\n"
        "    try:\n"
        "        with os.fdopen(fd, 'w', encoding='utf-8') as f:\n"
        "            json.dump(data, f)\n"
        "        os.rename(tmp, STARRED)\n"
        "    except OSError as exc:\n"
        "        try: os.unlink(tmp)\n"
        "        except OSError: pass\n"
        "        print(json.dumps({'error': 'write_failed: ' + str(exc)}))\n"
        "        raise SystemExit(0)\n"
        "items = []\n"
        "kept_paths = []\n"
        "for p in data['paths']:\n"
        "    full = os.path.join(ROOT, p)\n"
        "    try:\n"
        "        st = os.stat(full, follow_symlinks=False)\n"
        "    except OSError:\n"
        "        continue  # silently evict missing paths from the read view\n"
        "    if _stat.S_ISDIR(st.st_mode):\n"
        "        kind = 'dir'\n"
        "    elif _stat.S_ISREG(st.st_mode):\n"
        "        kind = 'file'\n"
        "    elif _stat.S_ISLNK(st.st_mode):\n"
        "        kind = 'link'\n"
        "    else:\n"
        "        kind = 'other'\n"
        "    items.append({\n"
        "        'name': os.path.basename(p) or p,\n"
        "        'path': p,\n"
        "        'kind': kind,\n"
        "        'size': st.st_size,\n"
        "        'mtime': st.st_mtime,\n"
        "    })\n"
        "    kept_paths.append(p)\n"
        # If reading, garbage-collect missing paths from the manifest so it
        # doesn't grow unbounded.
        "if not MUTATION and kept_paths != data['paths']:\n"
        "    try:\n"
        "        os.makedirs(META_DIR, exist_ok=True)\n"
        "        fd, tmp = tempfile.mkstemp(prefix='.tmp.starred.', dir=META_DIR)\n"
        "        with os.fdopen(fd, 'w', encoding='utf-8') as f:\n"
        "            json.dump({'paths': kept_paths}, f)\n"
        "        os.rename(tmp, STARRED)\n"
        "    except OSError:\n"
        "        pass  # GC best-effort; user still gets the live view\n"
        "items.sort(key=lambda e: e['mtime'], reverse=True)\n"
        "print(json.dumps({'items': items, 'paths': kept_paths if not MUTATION else data['paths']}))\n"
    )
    res = _run_helper(container, script, timeout=15.0)
    if "error" in res:
        raise FileOpError(res["error"], status_code=500)
    return res


def list_starred(container: str) -> dict:
    res = _starred_helper_io(container, mutation=None)
    return {"items": res.get("items", [])}


def star(container: str, path: str) -> dict:
    res = _starred_helper_io(container, mutation="add", path=path)
    return {"path": _relative(_normalize(path)), "starred": True, "count": len(res.get("paths", []))}


def unstar(container: str, path: str) -> dict:
    res = _starred_helper_io(container, mutation="remove", path=path)
    return {"path": _relative(_normalize(path)), "starred": False, "count": len(res.get("paths", []))}


def trash(container: str, path: str) -> dict:
    """Soft-delete: move <path> into /workspace/.drive-trash/<trash_id>/payload
    with a meta.json sidecar recording the original path + deletion time.

    Returns {trash_id, original_path}.
    """
    abs_path = _normalize(path)
    if abs_path == WORKSPACE_ROOT:
        raise FileOpError("cannot trash the workspace root", status_code=400)
    # Refuse to trash anything *inside* the trash or meta dirs — that would
    # confuse list_trash and could be exploited to nest trash items.
    trash_abs = posixpath.join(WORKSPACE_ROOT, DRIVE_TRASH_DIR)
    meta_abs = posixpath.join(WORKSPACE_ROOT, DRIVE_META_DIR)
    if abs_path == trash_abs or abs_path.startswith(trash_abs + "/"):
        raise FileOpError("cannot trash items already inside the trash", status_code=400)
    if abs_path == meta_abs or abs_path.startswith(meta_abs + "/"):
        raise FileOpError("cannot trash drive metadata", status_code=400)

    rel = _relative(abs_path)
    trash_id = uuid.uuid4().hex[:16]
    script = (
        "import json, os, shutil, stat as _stat, time, sys\n"
        f"ROOT = {json.dumps(WORKSPACE_ROOT)}\n"
        f"TRASH_DIR = {json.dumps(trash_abs)}\n"
        f"TRASH_ID = {json.dumps(trash_id)}\n"
        f"SRC = {json.dumps(abs_path)}\n"
        f"REL = {json.dumps(rel)}\n"
        "src_real = os.path.realpath(SRC)\n"
        "if src_real != ROOT and not src_real.startswith(ROOT + '/'):\n"
        "    print(json.dumps({'error': 'path_escapes_workspace'}))\n"
        "    sys.exit(0)\n"
        "try:\n"
        "    st = os.lstat(SRC)\n"
        "except FileNotFoundError:\n"
        "    print(json.dumps({'error': 'not_found'}))\n"
        "    sys.exit(0)\n"
        "except PermissionError:\n"
        "    print(json.dumps({'error': 'permission_denied'}))\n"
        "    sys.exit(0)\n"
        "if _stat.S_ISDIR(st.st_mode) and not _stat.S_ISLNK(st.st_mode):\n"
        "    kind = 'dir'\n"
        "elif _stat.S_ISLNK(st.st_mode):\n"
        "    kind = 'link'\n"
        "else:\n"
        "    kind = 'file'\n"
        "item_dir = os.path.join(TRASH_DIR, TRASH_ID)\n"
        "try:\n"
        "    os.makedirs(item_dir, exist_ok=False)\n"
        "except FileExistsError:\n"
        "    print(json.dumps({'error': 'trash_id_collision'}))\n"
        "    sys.exit(0)\n"
        "except OSError as exc:\n"
        "    print(json.dumps({'error': 'mkdir_failed: ' + str(exc)}))\n"
        "    sys.exit(0)\n"
        "dst = os.path.join(item_dir, 'payload')\n"
        "try:\n"
        "    os.rename(SRC, dst)\n"
        "except OSError as exc:\n"
        # If rename fails (cross-device — shouldn't happen on a single
        # workspace mount, but defensive), fall back to copy+remove.
        "    try:\n"
        "        if kind == 'dir':\n"
        "            shutil.copytree(SRC, dst, symlinks=True)\n"
        "            shutil.rmtree(SRC)\n"
        "        else:\n"
        "            shutil.copy2(SRC, dst, follow_symlinks=False)\n"
        "            os.unlink(SRC)\n"
        "    except OSError as exc2:\n"
        "        try: shutil.rmtree(item_dir)\n"
        "        except OSError: pass\n"
        "        print(json.dumps({'error': 'move_failed: ' + str(exc2)}))\n"
        "        sys.exit(0)\n"
        # size: regular file = st.st_size; dir = walk for total bytes\n"
        "size = st.st_size\n"
        "if kind == 'dir':\n"
        "    size = 0\n"
        "    for dp, _dn, fn in os.walk(dst, followlinks=False):\n"
        "        for n in fn:\n"
        "            try:\n"
        "                size += os.lstat(os.path.join(dp, n)).st_size\n"
        "            except OSError:\n"
        "                pass\n"
        "meta = {\n"
        "    'trash_id': TRASH_ID,\n"
        "    'original_path': REL,\n"
        "    'name': os.path.basename(REL) or REL,\n"
        "    'kind': kind,\n"
        "    'size': size,\n"
        "    'deleted_at': time.time(),\n"
        "}\n"
        "with open(os.path.join(item_dir, 'meta.json'), 'w', encoding='utf-8') as f:\n"
        "    json.dump(meta, f)\n"
        "print(json.dumps({'ok': True, 'trash_id': TRASH_ID, 'meta': meta}))\n"
    )
    res = _run_helper(container, script, timeout=60.0)
    if res.get("error") == "not_found":
        raise FileOpError("file not found", status_code=404)
    if res.get("error") == "permission_denied":
        raise FileOpError("permission denied", status_code=403)
    if res.get("error") == "path_escapes_workspace":
        raise FileOpError("path escapes /workspace via symlink", status_code=400)
    if "error" in res:
        raise FileOpError(res["error"], status_code=500)
    return res


def list_trash(container: str) -> dict:
    """List all items currently in the trash with their original-path metadata."""
    trash_abs = posixpath.join(WORKSPACE_ROOT, DRIVE_TRASH_DIR)
    script = (
        "import json, os, stat as _stat\n"
        f"TRASH_DIR = {json.dumps(trash_abs)}\n"
        "items = []\n"
        "try:\n"
        "    entries = os.scandir(TRASH_DIR)\n"
        "except FileNotFoundError:\n"
        "    print(json.dumps({'items': []}))\n"
        "    raise SystemExit(0)\n"
        "except PermissionError:\n"
        "    print(json.dumps({'error': 'permission_denied'}))\n"
        "    raise SystemExit(0)\n"
        "for de in entries:\n"
        "    if not de.is_dir(follow_symlinks=False):\n"
        "        continue\n"
        "    meta_path = os.path.join(de.path, 'meta.json')\n"
        "    try:\n"
        "        with open(meta_path, 'r', encoding='utf-8') as f:\n"
        "            meta = json.load(f)\n"
        "    except (OSError, json.JSONDecodeError):\n"
        # Orphaned trash item (no meta) — surface it minimally so the user
        # can purge it from the UI; otherwise it'd be invisible junk.
        "        meta = {'trash_id': de.name, 'original_path': '(unknown)', 'name': de.name, 'kind': 'file', 'size': 0, 'deleted_at': 0}\n"
        "    items.append(meta)\n"
        "items.sort(key=lambda e: e.get('deleted_at', 0), reverse=True)\n"
        "print(json.dumps({'items': items}))\n"
    )
    res = _run_helper(container, script, timeout=15.0)
    if res.get("error") == "permission_denied":
        raise FileOpError("permission denied", status_code=403)
    return {"items": res.get("items", [])}


def restore_from_trash(container: str, trash_id: str) -> dict:
    """Move a trashed item's payload back to its original_path. If that
    path is now occupied, append a numeric suffix ` (restored N)` to the
    basename so the restore always succeeds.
    """
    if not trash_id or not re.match(r"^[a-f0-9]{4,32}$", trash_id):
        raise FileOpError("invalid trash_id", status_code=400)
    trash_abs = posixpath.join(WORKSPACE_ROOT, DRIVE_TRASH_DIR)
    script = (
        "import json, os, shutil, sys\n"
        f"ROOT = {json.dumps(WORKSPACE_ROOT)}\n"
        f"TRASH_DIR = {json.dumps(trash_abs)}\n"
        f"TRASH_ID = {json.dumps(trash_id)}\n"
        "item_dir = os.path.join(TRASH_DIR, TRASH_ID)\n"
        "meta_path = os.path.join(item_dir, 'meta.json')\n"
        "payload = os.path.join(item_dir, 'payload')\n"
        "if not os.path.isdir(item_dir):\n"
        "    print(json.dumps({'error': 'not_found'}))\n"
        "    sys.exit(0)\n"
        "try:\n"
        "    with open(meta_path, 'r', encoding='utf-8') as f:\n"
        "        meta = json.load(f)\n"
        "except (OSError, json.JSONDecodeError) as exc:\n"
        "    print(json.dumps({'error': 'meta_read_failed: ' + str(exc)}))\n"
        "    sys.exit(0)\n"
        "if not os.path.lexists(payload):\n"
        "    print(json.dumps({'error': 'payload_missing'}))\n"
        "    sys.exit(0)\n"
        "orig_rel = meta.get('original_path') or ''\n"
        "if not isinstance(orig_rel, str) or orig_rel in ('', '.', '/') or orig_rel.startswith('/') or '..' in orig_rel.split('/'):\n"
        "    print(json.dumps({'error': 'meta_invalid_original_path'}))\n"
        "    sys.exit(0)\n"
        "dst = os.path.join(ROOT, orig_rel)\n"
        "dst_real = os.path.realpath(os.path.dirname(dst) or ROOT)\n"
        "if dst_real != ROOT and not dst_real.startswith(ROOT + '/'):\n"
        "    print(json.dumps({'error': 'destination_escapes_workspace'}))\n"
        "    sys.exit(0)\n"
        # Resolve collision: append ' (restored N)' before extension.\n"
        "final = dst\n"
        "if os.path.lexists(dst):\n"
        "    base = os.path.basename(dst)\n"
        "    parent = os.path.dirname(dst)\n"
        "    stem, ext = (base.rsplit('.', 1) + [''])[:2] if '.' in base and not base.startswith('.') else (base, '')\n"
        "    suffix_ext = ('.' + ext) if ext else ''\n"
        "    for n in range(1, 1000):\n"
        "        candidate = os.path.join(parent, f'{stem} (restored {n}){suffix_ext}')\n"
        "        if not os.path.lexists(candidate):\n"
        "            final = candidate\n"
        "            break\n"
        "    else:\n"
        "        print(json.dumps({'error': 'too_many_collisions'}))\n"
        "        sys.exit(0)\n"
        "try:\n"
        "    os.makedirs(os.path.dirname(final) or ROOT, exist_ok=True)\n"
        "    os.rename(payload, final)\n"
        "except OSError as exc:\n"
        "    try:\n"
        "        if os.path.isdir(payload) and not os.path.islink(payload):\n"
        "            shutil.copytree(payload, final, symlinks=True)\n"
        "            shutil.rmtree(payload)\n"
        "        else:\n"
        "            shutil.copy2(payload, final, follow_symlinks=False)\n"
        "            os.unlink(payload)\n"
        "    except OSError as exc2:\n"
        "        print(json.dumps({'error': 'restore_failed: ' + str(exc2)}))\n"
        "        sys.exit(0)\n"
        "try:\n"
        "    shutil.rmtree(item_dir)\n"
        "except OSError:\n"
        "    pass\n"
        "restored_rel = os.path.relpath(final, ROOT)\n"
        "print(json.dumps({'ok': True, 'restored_path': restored_rel, 'requested_path': orig_rel}))\n"
    )
    res = _run_helper(container, script, timeout=60.0)
    if res.get("error") == "not_found":
        raise FileOpError("trash item not found", status_code=404)
    if "error" in res:
        raise FileOpError(res["error"], status_code=500)
    return res


def purge_trash_item(container: str, trash_id: str) -> dict:
    """Permanently delete a single trash item."""
    if not trash_id or not re.match(r"^[a-f0-9]{4,32}$", trash_id):
        raise FileOpError("invalid trash_id", status_code=400)
    trash_abs = posixpath.join(WORKSPACE_ROOT, DRIVE_TRASH_DIR)
    item_dir = posixpath.join(trash_abs, trash_id)
    script = (
        "import json, os, shutil, sys\n"
        f"ITEM = {json.dumps(item_dir)}\n"
        f"TRASH_DIR = {json.dumps(trash_abs)}\n"
        # Sanity: item must be a direct child of TRASH_DIR.\n"
        "if os.path.dirname(ITEM) != TRASH_DIR:\n"
        "    print(json.dumps({'error': 'invalid_trash_path'}))\n"
        "    sys.exit(0)\n"
        "if not os.path.isdir(ITEM):\n"
        "    print(json.dumps({'error': 'not_found'}))\n"
        "    sys.exit(0)\n"
        "try:\n"
        "    shutil.rmtree(ITEM)\n"
        "except OSError as exc:\n"
        "    print(json.dumps({'error': 'purge_failed: ' + str(exc)}))\n"
        "    sys.exit(0)\n"
        "print(json.dumps({'ok': True}))\n"
    )
    res = _run_helper(container, script, timeout=30.0)
    if res.get("error") == "not_found":
        raise FileOpError("trash item not found", status_code=404)
    if "error" in res:
        raise FileOpError(res["error"], status_code=500)
    return res


def empty_trash(container: str) -> dict:
    """Permanently delete every item in the trash. Returns {removed: N}."""
    trash_abs = posixpath.join(WORKSPACE_ROOT, DRIVE_TRASH_DIR)
    script = (
        "import json, os, shutil\n"
        f"TRASH_DIR = {json.dumps(trash_abs)}\n"
        "removed = 0\n"
        "try:\n"
        "    entries = list(os.scandir(TRASH_DIR))\n"
        "except FileNotFoundError:\n"
        "    print(json.dumps({'ok': True, 'removed': 0}))\n"
        "    raise SystemExit(0)\n"
        "for de in entries:\n"
        "    try:\n"
        "        if de.is_dir(follow_symlinks=False):\n"
        "            shutil.rmtree(de.path)\n"
        "        else:\n"
        "            os.unlink(de.path)\n"
        "        removed += 1\n"
        "    except OSError:\n"
        "        pass\n"
        "print(json.dumps({'ok': True, 'removed': removed}))\n"
    )
    res = _run_helper(container, script, timeout=60.0)
    return res
