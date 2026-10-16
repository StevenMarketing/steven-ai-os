"""JSON API for the approval workflow (stdlib ``http.server`` only)."""

from __future__ import annotations

from .server import create_server, make_handler  # noqa: F401

__all__ = ["create_server", "make_handler"]
