"""Tests for the module-import guard added in [item 2].

The guard lives at module top level in bridge_api/auth.py and refuses to
let the module load when BRIDGE_API_DEV_BYPASS is truthy unless
BRIDGE_API_ENV is explicitly set to "dev".
"""

from __future__ import annotations

import importlib
import sys

import pytest


MODULE_NAME = "bridge_api.auth"


def _reload_auth():
    """Force a fresh import of bridge_api.auth under current os.environ."""
    sys.modules.pop(MODULE_NAME, None)
    return importlib.import_module(MODULE_NAME)


def test_bypass_without_dev_env_raises(monkeypatch):
    monkeypatch.setenv("BRIDGE_API_DEV_BYPASS", "1")
    monkeypatch.delenv("BRIDGE_API_ENV", raising=False)
    sys.modules.pop(MODULE_NAME, None)
    with pytest.raises(RuntimeError) as excinfo:
        importlib.import_module(MODULE_NAME)
    msg = str(excinfo.value)
    assert "BRIDGE_API_DEV_BYPASS" in msg
    assert "BRIDGE_API_ENV" in msg
    # Drop the half-imported module so later tests get a clean import.
    sys.modules.pop(MODULE_NAME, None)


def test_bypass_with_non_dev_env_raises(monkeypatch):
    monkeypatch.setenv("BRIDGE_API_DEV_BYPASS", "true")
    monkeypatch.setenv("BRIDGE_API_ENV", "prod")
    sys.modules.pop(MODULE_NAME, None)
    with pytest.raises(RuntimeError) as excinfo:
        importlib.import_module(MODULE_NAME)
    assert "BRIDGE_API_DEV_BYPASS" in str(excinfo.value)
    assert "BRIDGE_API_ENV" in str(excinfo.value)
    sys.modules.pop(MODULE_NAME, None)


def test_bypass_unset_imports_cleanly(monkeypatch):
    monkeypatch.delenv("BRIDGE_API_DEV_BYPASS", raising=False)
    monkeypatch.delenv("BRIDGE_API_ENV", raising=False)
    mod = _reload_auth()
    assert hasattr(mod, "require_admin")


def test_bypass_falsy_imports_cleanly(monkeypatch):
    monkeypatch.setenv("BRIDGE_API_DEV_BYPASS", "0")
    monkeypatch.delenv("BRIDGE_API_ENV", raising=False)
    mod = _reload_auth()
    assert hasattr(mod, "require_admin")


def test_bypass_with_dev_env_imports_cleanly(monkeypatch):
    monkeypatch.setenv("BRIDGE_API_DEV_BYPASS", "1")
    monkeypatch.setenv("BRIDGE_API_ENV", "dev")
    mod = _reload_auth()
    assert hasattr(mod, "require_admin")
