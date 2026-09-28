"""Claude Bridge Ops HTTP API.

A thin FastAPI service that exposes the bot's existing handlers as JSON
endpoints so a web dashboard can drive the same operations Discord drives
today. Auth is Cloudflare Access JWT (mirroring services/chat/auth.py).

Submodules:
  * ``bridge_api.app``    — FastAPI app + routes (the importable ``app``)
  * ``bridge_api.auth``   — CF Access JWT verification + admin gate
  * ``bridge_api.schemas`` — Pydantic wire types

NOTE: we deliberately do NOT re-export ``app`` here as
``from .app import app`` — that would shadow the ``app`` submodule on the
``bridge_api`` package object, breaking ``import bridge_api.app`` for
test code that wants to monkeypatch module-level state. Importers should
say ``from bridge_api.app import app`` (or ``-m uvicorn bridge_api.app:app``).
"""
