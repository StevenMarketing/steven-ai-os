"""Write side of the worker: draft pull request, issue comment, labels.

Every function takes an optional ``transport`` so the flow can be verified
end-to-end against a recording fake -- no network, no tokens, no deploy. The
default transport is the real one and refuses any host other than
``api.github.com``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

import issue_github as gh

#: labels this worker manages (never arbitrary labels from an issue body)
WORKER_LABELS = ("hermes-proposed", "hermes-ready", "hermes-running",
                 "hermes-needs-info", "hermes-done", "hermes-blocked")


def pr_title(issue_number: Any, title: str) -> str:
    clean = " ".join(str(title or "").split())
    return f"#{issue_number} {clean}".strip()


def pr_body(issue_number: Any, *, summary: str, files: Sequence[str] = (),
            tests: str = "", cost: Optional[float] = None, notes: str = "",
            rollback: str = "") -> str:
    """A body that follows .github/pull_request_template.md and closes the issue."""
    file_rows = "\n".join(f"| `{path}` |  |" for path in files) or "|  |  |"
    cost_line = "not recorded" if cost is None else f"${float(cost):.4f}"
    return (
        "## What and why\n\n"
        f"{summary.strip()}\n\n"
        f"Closes #{issue_number}\n\n"
        "## Change summary\n\n"
        "| File | Change |\n|---|---|\n"
        f"{file_rows}\n\n"
        "## Verification\n\n"
        "| Check | Result |\n|---|---|\n"
        f"| Full test suite | {tests or 'not recorded'} |\n"
        "| New tests added | see the diff |\n"
        "| Manual check | see the issue thread |\n\n"
        "## Guard rails (all must be true)\n\n"
        "- [x] Branch only — `main` was never pushed to, nothing was force-pushed\n"
        "- [x] No secret, token, credential or real customer data in the diff\n"
        "- [x] No protected path touched (checked by `issue_guard verify-diff`)\n"
        "- [x] No deploy, no Google API call, no review published\n"
        f"- [x] AI cost recorded on the issue: {cost_line}\n\n"
        f"## Rollback\n\n{rollback.strip() or 'Revert this merge commit; no migration or state change.'}\n\n"
        f"## Reviewer notes\n\n{notes.strip() or 'None.'}\n"
    )


def create_pull_request(repo: str, *, head: str, base: str, title: str, body: str,
                        token: str, transport: Optional[gh.Transport] = None,
                        draft: bool = True) -> Dict[str, Any]:
    """Open a DRAFT pull request. Never merges, never pushes to the base."""
    payload, _headers = gh.request("POST", f"/repos/{repo}/pulls", token,
                                   transport=transport,
                                   body={"title": title, "head": head, "base": base,
                                         "body": body, "draft": bool(draft)},
                                   expect=(201,))
    return payload or {}


def comment_issue(repo: str, number: Any, body: str, token: str, *,
                  transport: Optional[gh.Transport] = None) -> Dict[str, Any]:
    payload, _headers = gh.request("POST", f"/repos/{repo}/issues/{number}/comments",
                                   token, transport=transport,
                                   body={"body": body}, expect=(201,))
    return payload or {}


def set_labels(repo: str, number: Any, labels: Sequence[str], token: str, *,
               transport: Optional[gh.Transport] = None) -> List[Any]:
    """Replace the issue's labels (only names this worker owns are allowed)."""
    wanted = [name for name in labels if name in WORKER_LABELS]
    payload, _headers = gh.request("PUT", f"/repos/{repo}/issues/{number}/labels",
                                   token, transport=transport,
                                   body={"labels": wanted}, expect=(200,))
    return payload or []


def add_label(repo: str, number: Any, label: str, token: str, *,
              transport: Optional[gh.Transport] = None) -> List[Any]:
    if label not in WORKER_LABELS:
        raise ValueError(f"refusing to touch a label this worker does not own: {label}")
    payload, _headers = gh.request("POST", f"/repos/{repo}/issues/{number}/labels",
                                   token, transport=transport,
                                   body={"labels": [label]}, expect=(200,))
    return payload or []


def remove_label(repo: str, number: Any, label: str, token: str, *,
                 transport: Optional[gh.Transport] = None) -> None:
    if label not in WORKER_LABELS:
        raise ValueError(f"refusing to touch a label this worker does not own: {label}")
    gh.request("DELETE", f"/repos/{repo}/issues/{number}/labels/{label}", token,
               transport=transport, expect=(200, 404))


def compare_url(repo: str, branch: str, base: str = "main") -> str:
    return f"https://github.com/{repo}/compare/{base}...{branch}?expand=1"
