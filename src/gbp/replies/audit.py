"""Append-only local audit trail for reply transitions.

Entries are immutable once appended: callers can read the trail but cannot edit
or delete an entry. Credential-shaped text is masked by the caller (see
:func:`gbp.replies.workflow.ReplyWorkflow._audit`), and the trail stores only
ids, state names, the actor, the revision and a text hash -- never the raw
approved text and never a credential.
"""

from __future__ import annotations

from typing import Iterable, List, Tuple

from .models import AuditEntry

__all__ = ["AuditTrail"]


class AuditTrail:
    """An append-only list of :class:`AuditEntry` objects."""

    def __init__(self, entries: Iterable[AuditEntry] = ()) -> None:
        self._entries: List[AuditEntry] = list(entries)

    def record(self, entry: AuditEntry) -> None:
        """Append one entry, unconditionally -- the trail is append-only."""
        self._entries.append(entry)

    def entries(self) -> Tuple[AuditEntry, ...]:
        """A read-only snapshot of the trail."""
        return tuple(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self):
        return iter(self._entries)
