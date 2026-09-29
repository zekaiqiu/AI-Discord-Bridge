import os
import sys

# make workspace/ importable when running `pytest workspace/tests/...`
HERE = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.dirname(HERE)
for p in (WORKSPACE, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import pytest


@pytest.fixture(autouse=True)
def _hermetic_token_ledger(tmp_path_factory, monkeypatch):
    """Keep test turns out of the real token ledger."""
    d = tmp_path_factory.mktemp("ledger")
    monkeypatch.setenv("TOKEN_LEDGER_DB", str(d / "ledger.db"))
    monkeypatch.setenv("TOKEN_LEDGER_PRICES", str(d / "prices.json"))
    yield
