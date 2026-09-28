"""Test setup for services/term-router.

The term-router source files (`app.py`, `pty_bridge.py`) live in
`services/term-router/`, which has a hyphen — Python's package machinery
forbids hyphens in dotted names, so we cannot say `import services.term_router.app`.
At runtime the Dockerfile sidesteps this by setting `WORKDIR /app` and
copying the files to `/app/{app,pty_bridge}.py`, then importing them as
top-level modules. Tests mirror that with `importlib.util.spec_from_file_location`
in test_app.py.

This conftest also wires the chat package onto sys.path so the term-router
import `from services.chat import auth, user_container` resolves. We add
the repo root (parent of `services/`) — services/ is a namespace package,
no `__init__.py` required.
"""

from __future__ import annotations

import pathlib
import sys

# Two parents up from this file: services/term-router/tests/conftest.py
# -> services/term-router/  ->  services/  ->  <repo-root>
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Also add services/chat so the bare-import convention used by chat tests
# (e.g. `import auth`) keeps working — auth.py imports nothing from the
# chat package itself, so it does not strictly need this, but it matches
# the convention exercised by services/chat/conftest.py.
_CHAT_DIR = _REPO_ROOT / "services" / "chat"
if str(_CHAT_DIR) not in sys.path:
    sys.path.insert(0, str(_CHAT_DIR))
