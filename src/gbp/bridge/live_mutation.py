"""Issue #57 — the ONLY place a live ``queue_issue`` actually mutates GitHub.

Admission is decided by :mod:`gbp.bridge.live_queue`; an admission is **not** a
mutation. This module performs the single bounded mutation the owner approved:
it adds the ``hermes-ready`` label to exactly one target issue, and reports
``mutated: true`` **only** after the GitHub API response confirms the label is
present. Everything else stays read-only.

Fail-closed rules (enforced, not documented by convention):

* no ``request`` callable or no token -> refused, and **no network call** is made;
* the label already being present is an idempotent no-op, not a mutation
  (``mutated: false``), so a replay can never enqueue twice;
* a transport exception or a non-2xx status -> ``mutated: false`` with a bounded
  ``error``; the API is never assumed to have half-succeeded;
* a response that does not echo the label -> ``mutated: false``;
* at most one mutation attempt per call.

Standard library only; the transport is injected, so tests run offline.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

__all__ = ["MUTATION_LABEL", "perform_label_mutation", "apply_mutation_to_receipt"]

#: The one label a live ``queue_issue`` may ever add.
MUTATION_LABEL = "hermes-ready"


def label_names(payload: Any) -> List[str]:
    """Normalise a GitHub issue/label payload (or a bare list) into label names."""
    if isinstance(payload, dict):
        payload = payload.get("labels")
    out: List[str] = []
    for label in payload or []:
        if isinstance(label, dict):
            out.append(str(label.get("name") or ""))
        else:
            out.append(str(label))
    return out


def perform_label_mutation(
    request: Optional[Callable[..., Any]],
    token: str,
    repo: str,
    number: int,
    *,
    label: str = MUTATION_LABEL,
    existing_labels: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Add ``label`` to issue ``number`` exactly once. Never raises."""
    out: Dict[str, Any] = {
        "operation": "queue_issue",
        "issue": int(number),
        "label": label,
        "attempted": False,
        "mutated": False,
        "error": "",
    }
    if request is None or not token:
        out["error"] = "mutation_unavailable"
        return out
    if existing_labels is not None and label in [str(x) for x in existing_labels]:
        # Adding a label that is already there is a no-op, not an enqueue.
        out["error"] = "label_already_present"
        return out
    out["attempted"] = True
    try:
        payload, _ = request("POST", "/repos/%s/issues/%d/labels" % (repo, int(number)),
                             token, body={"labels": [label]})
    except Exception:  # noqa: BLE001 - never leak a provider/transport detail
        out["error"] = "mutation_transport_error"
        return out
    if label not in label_names(payload):
        out["error"] = "mutation_not_confirmed"
        return out
    out["mutated"] = True
    return out


def apply_mutation_to_receipt(
    receipt: Dict[str, Any],
    *,
    request: Optional[Callable[..., Any]],
    token: str,
    repo: str,
    number: int,
    existing_labels: Optional[List[str]] = None,
    label: str = MUTATION_LABEL,
) -> Dict[str, Any]:
    """Run the mutation for an ADMITTED receipt and fold the result back in.

    ``mutated`` becomes true only on confirmed API success; otherwise the receipt
    is downgraded to a refusal (``admitted: false``) so the caller can never
    report a queued issue that was never actually labelled.
    """
    if not isinstance(receipt, dict):
        return receipt
    receipt["mutated"] = bool(receipt.get("mutated"))
    if not receipt.get("admitted"):
        return receipt
    outcome = perform_label_mutation(request, token, repo, number,
                                     label=label, existing_labels=existing_labels)
    receipt["mutation"] = {k: outcome[k] for k in ("attempted", "mutated", "error", "label")}
    if outcome.get("mutated"):
        receipt["mutated"] = True
        receipt["code"] = "completed"
    else:
        receipt["admitted"] = False
        receipt["mutated"] = False
        receipt["code"] = "mutation_failed"
        reasons = list(receipt.get("reasons") or [])
        reasons.append("mutation_failed: %s" % outcome.get("error"))
        receipt["reasons"] = reasons
    return receipt
