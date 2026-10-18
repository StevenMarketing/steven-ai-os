"""Offline Phase-1 command bridge for ChatGPT -> Hermes control.

This package is **design + validation only**: it parses and authorizes
structured commands that arrive through GitHub issues/comments, and it emits a
bounded, redacted machine-readable status document. It never executes shell
commands, never touches Google, never deploys, and never publishes anything.

Modules
-------
* :mod:`gbp.bridge.commands`  -- strict command schema, authorization,
  idempotency/replay/TTL enforcement, injection rejection, budget-guard hook.
* :mod:`gbp.bridge.status`    -- GitHub-visible status document builder
  (``healthy_idle`` / ``healthy_busy`` / ``degraded`` / ``stopped_or_unreachable``
  / ``unknown``) that separates observed evidence from unknown/stale evidence.
* :mod:`gbp.bridge.watchdog`  -- independent watchdog: detects missing
  scheduler/worker heartbeats, stuck queues, repeated failures and budget-guard
  pauses, including when Hermes itself cannot report.
"""

from __future__ import annotations

from . import commands, status, watchdog  # noqa: F401

__all__ = ["commands", "status", "watchdog"]
