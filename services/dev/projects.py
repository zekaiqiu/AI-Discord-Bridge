"""Project + interpreter helpers for dev-wizerith.

Three responsibilities:
  1. Discover available Python interpreters inside a user's container
     (system pythons, linuxbrew quant-venv, every project's `.venv`).
  2. Discover existing projects (`pyproject.toml` markers under /workspace,
     depth-capped).
  3. Read/write the per-project `.wizerith/project.json` settings file
     that holds the chosen interpreter override + future per-project
     state (run configs, etc.).

Everything runs as uid 1000 via docker_exec.run_exec.
"""

from __future__ import annotations

import json
import posixpath
import re
from typing import Any

from docker_exec import DockerExecError, run_exec
from file_ops import _normalize, shquote


WORKSPACE_ROOT = "/workspace"
PROJECT_CONFIG_DIR = ".wizerith"
PROJECT_CONFIG_FILE = "project.json"

# Discovery depth caps. find -maxdepth N — keep tight to avoid walking
# .git or node_modules for hours on a fat project.
PROJECT_DISCOVER_DEPTH = 5
VENV_DISCOVER_DEPTH = 6

# Prune list applied to every find: skip the usual binary-tree noise so
# we don't surface a venv's site-packages or a build dir as a project.
_PRUNE_NAMES = (
    "node_modules",
    "__pycache__",
    ".git",
    ".tox",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "build",
    "dist",
    ".idea",
    ".vscode",
)


def _prune_clause() -> str:
    """Shell fragment for find's `( -name X -o -name Y ) -prune -o`."""
    inner = " -o ".join(f"-name {shquote(n)}" for n in _PRUNE_NAMES)
    return f"\\( {inner} \\) -prune -o"


# ---------------------------------------------------------------------------
# Interpreter discovery.
# ---------------------------------------------------------------------------

def _python_version(container: str, path: str) -> str:
    """Best-effort `python --version` → '3.12.3'. Empty string on failure."""
    try:
        out = run_exec(
            container,
            ["sh", "-c", f"{shquote(path)} --version 2>&1 | awk '{{print $2}}'"],
            timeout=8.0,
        )
    except DockerExecError:
        return ""
    return out.decode("utf-8", errors="replace").strip()


def discover_interpreters(container: str) -> list[dict[str, Any]]:
    """Return a flat list of `{path, version, label, kind}` entries.

    Sources, in roughly the order a user expects to pick from:
      - workspace venvs (`/workspace/**/.venv/bin/python`, depth-capped)
      - linuxbrew interpreters (`/home/linuxbrew/.linuxbrew/bin/python3*`)
      - system pythons (`/usr/local/bin/python3*`, `/usr/bin/python3*`)

    Deduplicates by realpath so a venv shimming a system python only
    appears once. Each entry's `kind` is one of `venv`, `linuxbrew`,
    `system`.
    """
    prune = _prune_clause()
    script = (
        f"set -e\n"
        # Workspace venvs first — most user-facing.
        f"find {shquote(WORKSPACE_ROOT)} -maxdepth {VENV_DISCOVER_DEPTH} "
        f"{prune} -type f -name python "
        f"\\( -path '*/.venv/bin/python' -o -path '*/venv/bin/python' \\) "
        f"-print 2>/dev/null | sort\n"
        f"echo '---LINUXBREW---'\n"
        # Restrict to real interpreters — exclude *-config, *-build, etc.
        # python / python3 / python3.N where N is digits.
        f"for f in /home/linuxbrew/.linuxbrew/bin/python "
        f"/home/linuxbrew/.linuxbrew/bin/python3 "
        f"/home/linuxbrew/.linuxbrew/bin/python3.*; do "
        f"[ -x \"$f\" ] && case \"$f\" in *-config|*-build|*-gdb*|*-config) ;; *) echo \"$f\";; esac; "
        f"done | grep -E '/python(3(\\.[0-9]+)?)?$' | sort -u\n"
        f"echo '---SYSTEM---'\n"
        f"for f in /usr/local/bin/python /usr/local/bin/python3 "
        f"/usr/local/bin/python3.* /usr/bin/python /usr/bin/python3 "
        f"/usr/bin/python3.*; do "
        f"[ -x \"$f\" ] && echo \"$f\"; "
        f"done | grep -E '/python(3(\\.[0-9]+)?)?$' | sort -u\n"
    )
    try:
        out = run_exec(container, ["sh", "-c", script], timeout=30.0)
    except DockerExecError:
        return []
    text = out.decode("utf-8", errors="replace")

    sections = {"venv": [], "linuxbrew": [], "system": []}
    current = "venv"
    for line in text.splitlines():
        s = line.strip()
        if s == "---LINUXBREW---":
            current = "linuxbrew"
            continue
        if s == "---SYSTEM---":
            current = "system"
            continue
        if not s:
            continue
        # find may surface symlinks pointing at the same realpath as a
        # later entry — dedupe on the path string itself, then on
        # realpath (cheap docker exec) only if we keep getting noise.
        sections[current].append(s)

    # Dedupe via realpath in one batched shell call (saves N exec round-trips).
    all_paths = sections["venv"] + sections["linuxbrew"] + sections["system"]
    if not all_paths:
        return []
    realpath_script = " ".join(
        f"echo \"$(readlink -f {shquote(p)})|{shquote(p)}\";" for p in all_paths
    )
    try:
        realpath_out = run_exec(container, ["sh", "-c", realpath_script], timeout=15.0)
    except DockerExecError:
        # Fall back to no dedupe — better to surface extras than fail.
        realpath_out = b""
    seen: dict[str, str] = {}     # realpath -> chosen display path
    if realpath_out:
        for line in realpath_out.decode("utf-8", errors="replace").splitlines():
            if "|" not in line:
                continue
            real, display = line.split("|", 1)
            if real and real not in seen:
                seen[real] = display

    keep: set[str] = set(seen.values()) if seen else set(all_paths)

    results: list[dict[str, Any]] = []
    for kind, paths in (("venv", sections["venv"]),
                        ("linuxbrew", sections["linuxbrew"]),
                        ("system", sections["system"])):
        for p in paths:
            if p not in keep:
                continue
            version = _python_version(container, p)
            label = _interpreter_label(p, kind, version)
            results.append({"path": p, "version": version, "label": label, "kind": kind})
    return results


def _interpreter_label(path: str, kind: str, version: str) -> str:
    """Human-readable label: 'Python 3.12.3 — /workspace/foo/.venv/bin/python'.

    Truncate long workspace paths to `<project>/.venv` for readability.
    """
    ver = f"Python {version}" if version else "Python"
    if kind == "venv":
        # /workspace/<project-or-deeper>/.venv/bin/python
        if path.endswith("/.venv/bin/python"):
            parent = path[: -len("/.venv/bin/python")]
            rel = parent[len(WORKSPACE_ROOT) + 1:] if parent.startswith(WORKSPACE_ROOT + "/") else parent
            return f"{ver} · {rel}/.venv"
        if path.endswith("/venv/bin/python"):
            parent = path[: -len("/venv/bin/python")]
            rel = parent[len(WORKSPACE_ROOT) + 1:] if parent.startswith(WORKSPACE_ROOT + "/") else parent
            return f"{ver} · {rel}/venv"
        return f"{ver} · {path}"
    if kind == "linuxbrew":
        return f"{ver} · linuxbrew ({path.split('/')[-1]})"
    return f"{ver} · system ({path.split('/')[-1]})"


# ---------------------------------------------------------------------------
# Project discovery.
# ---------------------------------------------------------------------------


def discover_projects(container: str) -> list[dict[str, Any]]:
    """Return `[{path, name, has_venv}]` for every pyproject.toml under
    /workspace (depth-capped, prune dirs ignored).
    """
    prune = _prune_clause()
    script = (
        f"set -e\n"
        f"find {shquote(WORKSPACE_ROOT)} -maxdepth {PROJECT_DISCOVER_DEPTH} "
        f"{prune} -type f -name pyproject.toml -print 2>/dev/null | sort\n"
    )
    try:
        out = run_exec(container, ["sh", "-c", script], timeout=20.0)
    except DockerExecError:
        return []
    paths = [
        line.strip() for line in out.decode("utf-8", errors="replace").splitlines()
        if line.strip()
    ]
    results: list[dict[str, Any]] = []
    for p in paths:
        # /workspace/<...>/pyproject.toml → project dir
        if not p.endswith("/pyproject.toml"):
            continue
        proj_abs = p[: -len("/pyproject.toml")]
        if not proj_abs.startswith(WORKSPACE_ROOT + "/") and proj_abs != WORKSPACE_ROOT:
            continue
        rel = "" if proj_abs == WORKSPACE_ROOT else proj_abs[len(WORKSPACE_ROOT) + 1:]
        name = posixpath.basename(proj_abs) or "(workspace root)"
        # Heuristic: project has a venv if `.venv` or `venv` exists.
        # Single shell test, batched would be nice but the project count
        # is small (< 50) so per-project cost is acceptable.
        has_venv = _dir_exists(container, proj_abs + "/.venv") or _dir_exists(container, proj_abs + "/venv")
        results.append({"path": rel, "name": name, "has_venv": has_venv})
    return results


def _dir_exists(container: str, abs_path: str) -> bool:
    try:
        run_exec(container, ["test", "-d", abs_path], timeout=5.0)
        return True
    except DockerExecError:
        return False


# ---------------------------------------------------------------------------
# Per-project config (.wizerith/project.json).
# ---------------------------------------------------------------------------


DEFAULT_PROJECT_CONFIG: dict[str, Any] = {
    "version": 1,
    "interpreter": None,    # absolute path or null → fall back to global
    # Future fields: run_configs, env_vars, formatter, etc.
}


def _config_path(proj_rel: str) -> str:
    proj_abs = _normalize(proj_rel)
    return posixpath.join(proj_abs, PROJECT_CONFIG_DIR, PROJECT_CONFIG_FILE)


def load_project_config(container: str, proj_rel: str) -> dict[str, Any]:
    """Read .wizerith/project.json for a project; return defaults if missing."""
    cfg_path = _config_path(proj_rel)
    try:
        out = run_exec(
            container,
            ["sh", "-c", f"cat {shquote(cfg_path)} 2>/dev/null || true"],
            timeout=5.0,
        )
    except DockerExecError:
        return dict(DEFAULT_PROJECT_CONFIG)
    raw = out.decode("utf-8", errors="replace").strip()
    if not raw:
        return dict(DEFAULT_PROJECT_CONFIG)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return dict(DEFAULT_PROJECT_CONFIG)
    if not isinstance(parsed, dict):
        return dict(DEFAULT_PROJECT_CONFIG)
    merged = dict(DEFAULT_PROJECT_CONFIG)
    merged.update(parsed)
    return merged


def save_project_config(container: str, proj_rel: str, cfg: dict[str, Any]) -> None:
    proj_abs = _normalize(proj_rel)
    dir_abs = posixpath.join(proj_abs, PROJECT_CONFIG_DIR)
    file_abs = posixpath.join(dir_abs, PROJECT_CONFIG_FILE)
    payload = json.dumps(cfg, separators=(",", ":")).encode("utf-8")
    script = (
        f"set -e\n"
        f"mkdir -p {shquote(dir_abs)}\n"
        f"cat > {shquote(file_abs + '.tmp')}\n"
        f"mv {shquote(file_abs + '.tmp')} {shquote(file_abs)}\n"
    )
    run_exec(container, ["sh", "-c", script], stdin_bytes=payload, timeout=10.0)


# ---------------------------------------------------------------------------
# Project creation — templates + post-create scripts.
# ---------------------------------------------------------------------------


# A project template is a dict { "files": { rel_path: content }, "packages":
# [...], "main_file": "main.py" }. Templates are simple — anything beyond
# Hello World belongs in the user's hands.
TEMPLATES: dict[str, dict[str, Any]] = {
    "empty": {
        "files": {},
        "packages": [],
        "main_file": None,
    },
    "script": {
        "files": {
            "main.py": (
                'def main() -> None:\n'
                '    print("Hello from {name}!")\n\n\n'
                'if __name__ == "__main__":\n'
                '    main()\n'
            ),
        },
        "packages": [],
        "main_file": "main.py",
    },
    "module": {
        "files": {
            "{slug}/__init__.py": '"""Top-level package."""\n',
            "{slug}/__main__.py": (
                'def main() -> None:\n'
                '    print("Hello from {name}!")\n\n\n'
                'if __name__ == "__main__":\n'
                '    main()\n'
            ),
        },
        "packages": [],
        "main_file": "{slug}/__main__.py",
    },
    "quant": {
        "files": {
            "main.py": (
                '"""Quant research starter — pandas + numpy preinstalled."""\n'
                'from __future__ import annotations\n\n'
                'import numpy as np\n'
                'import pandas as pd\n\n\n'
                'def main() -> None:\n'
                '    s = pd.Series(np.random.randn(10))\n'
                '    print(s.describe())\n\n\n'
                'if __name__ == "__main__":\n'
                '    main()\n'
            ),
        },
        "packages": ["numpy", "pandas", "yfinance", "matplotlib"],
        "main_file": "main.py",
    },
    "fastapi": {
        "files": {
            "main.py": (
                'from fastapi import FastAPI\n\n'
                'app = FastAPI(title="{name}")\n\n\n'
                '@app.get("/")\n'
                'async def root() -> dict:\n'
                '    return {{"hello": "{name}"}}\n'
            ),
        },
        "packages": ["fastapi", "uvicorn[standard]"],
        "main_file": "main.py",
    },
}


PYTHON_GITIGNORE = (
    "# Byte-compiled / optimized\n"
    "__pycache__/\n"
    "*.py[cod]\n"
    "*$py.class\n\n"
    "# Distribution / packaging\n"
    ".Python\n"
    "build/\n"
    "dist/\n"
    "*.egg-info/\n"
    ".eggs/\n\n"
    "# Virtual environments\n"
    ".venv/\n"
    "venv/\n"
    "env/\n\n"
    "# Tooling caches\n"
    ".pytest_cache/\n"
    ".mypy_cache/\n"
    ".ruff_cache/\n"
    ".tox/\n\n"
    "# Editor\n"
    ".idea/\n"
    ".vscode/\n"
    "*.swp\n"
)


_SLUG_RE = re.compile(r"[^a-z0-9_]")


def slugify(name: str) -> str:
    """`My Awesome Project` → `my_awesome_project` (snake_case, ASCII-only).

    Used as the package directory name in the "module" template and as a
    safe import-name for the pyproject `[project] name` field.
    """
    s = name.strip().lower().replace("-", "_").replace(" ", "_")
    s = _SLUG_RE.sub("_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "project"
