"""Value objects for the offline reply workflow.

Only synthetic-fixture data is ever held here: a reply id, a review id, a
location id, the reply text and the approval/audit metadata. Nothing in this
module touches the network or a credential store.
"""

from __future__ import annotations

import dataclasses
import hashlib
from typing import Any, Dict, Optional

__all__ = ["Reply", "Approval", "AuditEntry", "text_hash"]


def text_hash(text: str) -> str:
    """Stable SHA-256 hex digest of the exact reply text.

    The approval is bound to this hash, so any change to the approved text is
    detectable even if the revision counter were reset.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclasses.dataclass
class Approval:
    """A human approval bound to one exact revision of one exact text."""

    revision: int
    approver: str
    approved_at: str
    text_hash: str

    def authorises(self, revision: int, text: str) -> bool:
        """True only when this approval covers this revision and this text."""
        return self.revision == revision and self.text_hash == text_hash(text)


@dataclasses.dataclass
class AuditEntry:
    """One append-only record of a transition. Never carries credentials."""

    reply_id: str
    review_id: str
    location_id: str
    from_state: str
    to_state: str
    actor: str
    at: str
    revision: int
    text_hash: str

    def as_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class Reply:
    """A reply and its position in the lifecycle."""

    reply_id: str
    review_id: str
    location_id: str
    text: str
    state: str = "draft"
    revision: int = 1
    approval: Optional[Approval] = None
    publish_intent: bool = False
