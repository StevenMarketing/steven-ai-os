"""Worker operations: deterministic, offline diagnostics for remote monitoring.

This package holds pure, side-effect-free helpers (heartbeat classification,
problem taxonomy, bounded-retry policy, redaction, status snapshot and event
deduplication). It never calls the network, never calls Google and never
publishes anything; the command line wrapper lives in ``scripts/``.
"""

from . import heartbeat  # noqa: F401

__all__ = ["heartbeat"]
