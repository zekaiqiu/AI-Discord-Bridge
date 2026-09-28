import os
import sys

# make workspace/ importable when running `pytest workspace/tests/...`
HERE = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.dirname(HERE)
for p in (WORKSPACE, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)
