"""Make ``from helpers import ...`` resolve to ``tests/helpers.py``.

The root conftest puts the service dir on sys.path (so ``import app`` works);
the tests dir itself is a package and was never importable as a top-level
module root, so the shared helpers module must be reachable explicitly.
"""
import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))
