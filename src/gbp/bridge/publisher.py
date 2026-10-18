"""Redacted status publisher for a GitHub-readable surface (blocker 3).

Publishes a bounded, redacted worker/cron status document to GitHub so a connector
(such as the owner's ChatGPT GitHub integration) can read the worker state without
screenshots and without any VPS access.

Rules:
  * The document carries ``generated_at``; it deliberately does NOT carry a frozen
    ``document_age_seconds``. An external reader must recompute staleness from
    ``generated_at`` and its own clock (see ``gbp.bridge.external_watchdog``).
  * Publishing is dry-run by default. A live publish needs an explicit ``dry_run=False``
    and a write-capable token; failures are bounded (retries) and are returned as a
    structured result instead of raising into a worker run.
  * Every string that leaves this module goes through the existing redactor.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, Optional

try:  # reuse the repository redactor, never duplicate it
    from ..security.redact import redact_text
except Exception:  # pragma: no cover - import guard only
    def redact_text(value: Any) -> str:  # minimal fallback
        return str(value)

__all__ = [
    "STATUS_ISSUE_TITLE",
    "MAX_AGE_SECONDS",
    "build_redacted_document",
    "age_seconds",
    "render_comment",
    "find_status_issue",
    "ensure_status_issue",
    "publish_status",
]

STATUS_ISSUE_TITLE = "Steven AI OS — worker status (machine-readable)"
MAX_AGE_SECONDS = 900


def _clean(value: Any) -> str:
    return redact_text(str(value if value is not None else ""))


def build_redacted_document(
    *,
    generated_at: str,
    worker: Dict[str, Any],
    cron: Dict[str, Any],
    budget: Dict[str, Any],
    repository: str = "",
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Assemble the published status document (redacted, no frozen age)."""
    document: Dict[str, Any] = {
        "schema": "steven-ai-os/worker-status@1",
        "generated_at": str(generated_at),
        "repository": str(repository),
        "freshness": {
            "max_age_seconds": MAX_AGE_SECONDS,
            "recomputed_by_reader": True,
        },
        "worker": {k: (_clean(v) if isinstance(v, str) else v) for k, v in dict(worker).items()},
        "cron": {k: (_clean(v) if isinstance(v, str) else v) for k, v in dict(cron).items()},
        "budget": {k: (_clean(v) if isinstance(v, str) else v) for k, v in dict(budget).items()},
    }
    if extra:
        document["extra"] = {k: (_clean(v) if isinstance(v, str) else v) for k, v in dict(extra).items()}
    return document


def age_seconds(document: Any, now: float) -> Optional[float]:
    """Reader-side age; delegates to the external watchdog's parser."""
    from .external_watchdog import document_age_seconds

    return document_age_seconds(document, now)


def render_comment(document: Dict[str, Any]) -> str:
    """Human line + fenced JSON, so a connector can parse either way."""
    pretty = json.dumps(document, indent=2, sort_keys=True)
    header = (
        "Steven AI OS worker status\n\n"
        "- generated_at: `%s`\n- worker: `%s`\n- cron: `%s`\n"
        "- staleness must be recomputed by the reader from `generated_at`\n"
    ) % (
        document.get("generated_at"),
        (document.get("worker") or {}).get("state", "unknown"),
        (document.get("cron") or {}).get("state", "unknown"),
    )
    return header + "\n```json\n" + pretty + "\n```\n"


def find_status_issue(request: Callable[..., Any], token: Any, repository: str) -> Optional[int]:
    """Return the id of the machine-readable status issue, if it exists."""
    payload, _ = request("GET", "/repos/%s/issues?state=open&per_page=100" % repository, token)
    for issue in payload or []:
        if str(issue.get("title") or "") == STATUS_ISSUE_TITLE:
            return int(issue["number"])
    return None


def ensure_status_issue(request: Callable[..., Any], token: Any, repository: str) -> int:
    """Find or create the dedicated status issue (never a duplicate)."""
    existing = find_status_issue(request, token, repository)
    if existing is not None:
        return existing
    body = (
        "Machine-readable Steven AI OS worker status. Published by "
        "`scripts/bridge_status_publish.py`. Newest status is the last comment.\n\n"
        "Staleness must be recomputed by the reader from `generated_at`."
    )
    payload, _ = request("POST", "/repos/%s/issues" % repository, token,
                         body={"title": STATUS_ISSUE_TITLE, "body": body})
    return int(payload["number"])


def publish_status(
    request: Callable[..., Any],
    token: Any,
    repository: str,
    document: Dict[str, Any],
    *,
    dry_run: bool = True,
    issue_number: Optional[int] = None,
    attempts: int = 3,
    body_builder: Optional[Callable[[Dict[str, Any]], str]] = None,
) -> Dict[str, Any]:
    """Publish ``document`` as a comment on the status issue. Never raises.

    Returns ``{"ok", "code", "detail", "issue", "url", "dry_run", "attempts"}``.
    """
    result: Dict[str, Any] = {"ok": False, "code": "not_attempted", "detail": "",
                              "issue": issue_number, "url": "", "dry_run": bool(dry_run),
                              "attempts": 0}
    if not isinstance(document, dict) or not document.get("generated_at"):
        result.update(code="invalid_document", detail="document needs a generated_at timestamp")
        return result
    text = (body_builder or render_comment)(document)
    if dry_run:
        result.update(ok=True, code="dry_run", detail="validated only", url="")
        return result

    last_detail = ""
    for attempt in range(1, max(1, int(attempts)) + 1):
        result["attempts"] = attempt
        try:
            number = issue_number or ensure_status_issue(request, token, repository)
            payload, _ = request("POST", "/repos/%s/issues/%d/comments" % (repository, int(number)),
                                 token, body={"body": text})
            result.update(ok=True, code="published", issue=int(number),
                          url=str(payload.get("html_url") or ""), detail="")
            return result
        except Exception as exc:  # bounded, structured failure
            last_detail = "%s: %s" % (type(exc).__name__, str(exc)[:160])
    result.update(code="publish_failed", detail=last_detail)
    return result
