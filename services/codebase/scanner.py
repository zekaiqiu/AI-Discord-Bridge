"""Read-only filesystem walker that produces a JSON model of three repos.

Output shape (consumed by frontend three.js renderer):

    {
      "scanned_at": <epoch_seconds>,
      "now": <epoch_seconds>,
      "repos": [
        {"name": "portfolio-tool", "root": "/...", "file_count": 412,
         "files": [{"path": "services/spend/app.py", "loc": 329,
                    "mtime": 1730000000.0, "ext": ".py", "size": 12345}, ...]},
        ...
      ]
    }

Skips VCS / build / virtualenv / cache directories. Counts non-blank lines
for files matching a code-extension allowlist; non-code files (images, lock
files, binaries) get loc=0 but are still emitted so the city has texture.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

IGNORE_DIRS = frozenset({
    ".git", ".hg", ".svn",
    "node_modules", "bower_components", ".pnpm-store",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".venv", "venv", "env", ".env",
    "dist", "build", ".next", ".nuxt", ".turbo", ".cache",
    "target", ".gradle",
    ".tox", ".coverage", "htmlcov",
    ".idea", ".vscode",
    "site-packages",
    # repo-specific bulky dirs we don't want represented as code
    "projects",  # multi-agent-pipeline run state
    "phases",    # phase artifacts under multi-agent-framework projects
    "sessions",  # transcript dumps
})

# Files we care about for LOC counting. Other extensions still appear as
# tiny buildings (texture) but with loc=0.
CODE_EXTS = frozenset({
    ".py", ".pyi",
    ".js", ".mjs", ".cjs", ".jsx",
    ".ts", ".tsx",
    ".go", ".rs", ".rb",
    ".java", ".kt", ".scala", ".swift",
    ".c", ".h", ".cc", ".cpp", ".hpp", ".hh",
    ".cs",
    ".sh", ".bash", ".zsh", ".fish",
    ".css", ".scss", ".less",
    ".html", ".htm", ".vue", ".svelte",
    ".md", ".rst",
    ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".json",
    ".sql",
    ".lua",
    ".dockerfile", "Dockerfile",
})

MAX_FILE_SIZE = 1_000_000  # 1 MiB; bigger is almost certainly generated/binary
MAX_FILES_PER_REPO = 4_000  # keep three.js comfortable on the client side
MAX_AGE_SECONDS = 5 * 365 * 24 * 3600  # 5 years; older = vendored/abandoned


def _ext_of(path: Path) -> str:
    name = path.name
    if name in {"Dockerfile", "Containerfile", "Makefile"}:
        return name
    return path.suffix.lower()


def _count_loc(path: Path, size: int) -> int:
    """Non-blank, non-comment-only line count. Cheap heuristic — we don't try
    to be language-aware; just skip whitespace-only lines. For visualization
    purposes the relative magnitudes are what matter, not exactness."""
    if size == 0:
        return 0
    try:
        with path.open("rb") as f:
            data = f.read()
    except OSError:
        return 0
    # Quick binary sniff: NUL byte in the first 8 KiB ⇒ skip.
    if b"\x00" in data[:8192]:
        return 0
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        return 0
    n = 0
    for line in text.splitlines():
        if line.strip():
            n += 1
    return n


def scan_repo(root: Path, repo_name: str, now_ts: float) -> dict[str, Any]:
    """Walk one repo, return the structured payload described in the module
    docstring. Errors on individual files are swallowed — the visualization
    shouldn't blow up because one file in one repo is unreadable."""
    files: list[dict[str, Any]] = []
    if not root.exists() or not root.is_dir():
        return {"name": repo_name, "root": str(root), "file_count": 0,
                "files": [], "missing": True}

    cutoff_mtime = now_ts - MAX_AGE_SECONDS

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # Mutate dirnames in-place to prune walks. Skip ignore-listed dirs
        # and dotfiles (.git, .pytest_cache, etc) but keep .github.
        dirnames[:] = [
            d for d in dirnames
            if d not in IGNORE_DIRS
            and (not d.startswith(".") or d == ".github")
        ]
        # Bound on file count per repo — once we hit the cap, stop walking.
        if len(files) >= MAX_FILES_PER_REPO:
            break

        for fname in filenames:
            if len(files) >= MAX_FILES_PER_REPO:
                break
            full = Path(dirpath) / fname
            try:
                st = full.lstat()
            except OSError:
                continue
            if not (st.st_mode & 0o170000) == 0o100000:  # regular file
                # Symlinks, sockets, etc. — skip silently.
                continue
            size = st.st_size
            if size > MAX_FILE_SIZE:
                continue
            mtime = st.st_mtime
            if mtime < cutoff_mtime:
                continue
            ext = _ext_of(full)
            loc = _count_loc(full, size) if ext in CODE_EXTS else 0
            try:
                rel = full.relative_to(root).as_posix()
            except ValueError:
                rel = full.as_posix()
            files.append({
                "path": rel,
                "loc": loc,
                "mtime": mtime,
                "ext": ext,
                "size": size,
            })

    # Largest LOC first — stable input for treemap layouts on the client.
    files.sort(key=lambda f: (-f["loc"], f["path"]))
    return {
        "name": repo_name,
        "root": str(root),
        "file_count": len(files),
        "files": files,
        "missing": False,
    }


def scan_all(repos: list[tuple[str, Path]]) -> dict[str, Any]:
    now_ts = time.time()
    return {
        "scanned_at": now_ts,
        "now": now_ts,
        "repos": [scan_repo(root, name, now_ts) for name, root in repos],
    }


# Default repo list when running inside the codebase container — these paths
# are the bind-mount targets configured in docker-compose.yml.
DEFAULT_REPOS: list[tuple[str, Path]] = [
    ("portfolio-tool", Path("/repos/portfolio-tool")),
    ("claude-bridge", Path("/repos/claude-bridge")),
    ("Multi-Agent-Framework", Path("/repos/Multi-Agent-Framework")),
]
