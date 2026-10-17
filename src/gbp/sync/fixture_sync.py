"""Offline, fixture-driven review synchronisation (read-only, stdlib only).

:class:`FixtureSync` mirrors a *synthetic* review dataset -- a plain JSON
object with a ``locations`` list, each carrying an inline ``reviews`` list --
into the local :class:`~gbp.repo.sqlite_repo.Repository`. It is the deterministic
counterpart of :class:`~gbp.sync.reviews_sync.ReviewSync`: it never talks to
Google, never paginates, never sleeps and never publishes anything.

Guarantees
----------
* **No network.** The module imports no HTTP client and drives no transport. The
  optional ``client`` argument exists only so a caller can hand in a guard
  client whose transport *raises on any request*; a test proves the run never
  touches it.
* **Deduplicated.** Within one dataset the same review id for the same location
  collapses to one record (the newest ``updateTime`` wins); the extra copies are
  counted in ``duplicates``.
* **Idempotent upsert.** Replaying the identical dataset is a no-op: no new
  rows, no duplicate rows, no field churn (``created``/``updated`` are both 0
  on the second run).
* **Multi-location, no leakage.** Two locations that carry the same review id
  remain two separate records because the internal key is the full resource
  name ``<location>/reviews/<id>``.
* **Never partial.** A malformed review is skipped and reported in ``errors``;
  it is never written and never aborts the run.
* **Read-only surface.** Only ``sync_dataset``/``load_dataset`` are public. No
  method can publish, edit or delete anything on the Google side; review state
  (``published``/``pending``/...) is stored as data only.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

__all__ = ["FixtureError", "FixtureSync", "normalize_review"]

#: Fields compared against the stored row to decide whether a write is needed.
#: Everything else (``first_seen_at``, ``last_seen_at``, raw payload) is
#: bookkeeping and must not by itself make a replay look like a change.
_COMPARE_FIELDS = ("rating", "comment", "reviewer_name", "create_time",
                   "update_time", "status")

_COUNT_KEYS = ("created", "updated", "unchanged", "duplicates", "errors", "stored")


class FixtureError(ValueError):
    """A fixture entry is unusable. It is reported, never written."""


def _opt_str(value: Any, field: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise FixtureError(f"{field} must be a string or null")
    return value


def normalize_review(raw: Any, location: str) -> Dict[str, Any]:
    """Map one fixture review into a stable internal record.

    Raises :class:`FixtureError` for a malformed entry so the caller can skip
    and report it instead of writing a partial record.
    """
    if not isinstance(raw, dict):
        raise FixtureError("review entry is not a JSON object")
    review_id = raw.get("reviewId", raw.get("review_id"))
    if not isinstance(review_id, str) or not review_id.strip():
        raise FixtureError("review is missing a string reviewId")
    rating = raw.get("rating")
    if isinstance(rating, bool) or not isinstance(rating, int) or not 1 <= rating <= 5:
        raise FixtureError("review rating must be an integer 1..5")
    comment = raw.get("comment")
    if comment is not None and not isinstance(comment, str):
        raise FixtureError("review comment must be a string or null")
    reviewer = raw.get("reviewer")
    if reviewer is not None and not isinstance(reviewer, dict):
        raise FixtureError("reviewer must be an object or null")
    reviewer_name = reviewer.get("displayName") if isinstance(reviewer, dict) else None
    if reviewer_name is not None and not isinstance(reviewer_name, str):
        raise FixtureError("reviewer displayName must be a string or null")
    status = raw.get("status")
    if status is not None and not isinstance(status, str):
        raise FixtureError("status must be a string or null")

    review_id = review_id.strip()
    return {
        "review_name": f"{location}/reviews/{review_id}",
        "review_id": review_id,
        "rating": rating,
        "comment": comment,
        "reviewer_name": reviewer_name,
        "create_time": _opt_str(raw.get("createTime"), "createTime"),
        "update_time": _opt_str(raw.get("updateTime"), "updateTime"),
        "status": status,
    }


class FixtureSync:
    """Mirror a synthetic review dataset into the repository, offline.

    ``client`` is never contacted. It exists so a caller can pass a guard client
    (a transport that raises on any request) and demonstrate that a full sync
    run makes no live call.
    """

    def __init__(self, repo: Any, client: Optional[Any] = None) -> None:
        self.repo = repo
        #: intentionally unused: proves the sync surface is offline by design.
        self.client = client

    # -- helpers ----------------------------------------------------------- #
    @staticmethod
    def _account_of(location: str) -> str:
        parts = str(location).split("/")
        return "/".join(parts[:2]) if len(parts) >= 2 else str(location)

    @staticmethod
    def _is_newer(candidate: Dict[str, Any], current: Dict[str, Any]) -> bool:
        """True when ``candidate`` should replace ``current`` (last wins on tie)."""
        new = candidate.get("update_time") or ""
        old = current.get("update_time") or ""
        if new and old and str(new) < str(old):
            return False
        return True

    def _existing(self, review_name: str):
        return self.repo.conn.execute(
            "SELECT * FROM reviews WHERE review_name = ?", (review_name,)
        ).fetchone()

    @staticmethod
    def _differs(existing: Any, record: Dict[str, Any]) -> bool:
        for field in _COMPARE_FIELDS:
            if (existing[field] or None) != (record.get(field) or None):
                return True
        return False

    def _write(self, location_id: int, record: Dict[str, Any]) -> None:
        self.repo.upsert_review(
            location_id=location_id,
            review_name=record["review_name"],
            review_id=record["review_id"],
            rating=record["rating"],
            comment=record["comment"],
            reviewer_name=record["reviewer_name"],
            create_time=record["create_time"],
            update_time=record["update_time"],
            status=record["status"],
            raw_json=json.dumps(record, ensure_ascii=False, sort_keys=True),
        )

    def _count_stored(self, location_id: int) -> int:
        row = self.repo.conn.execute(
            "SELECT COUNT(*) AS n FROM reviews WHERE location_id = ?", (location_id,)
        ).fetchone()
        return int(row["n"])

    # -- public API -------------------------------------------------------- #
    @staticmethod
    def load_dataset(path: str) -> Dict[str, Any]:
        """Read a synthetic dataset from a JSON file (offline, no network)."""
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def sync_dataset(self, dataset: Any) -> Dict[str, Any]:
        """Sync every location in ``dataset`` and return a machine-readable summary.

        The summary shape is stable::

            {
              "locations": {"<resource>": {created, updated, unchanged,
                                           duplicates, errors, stored}},
              "totals":    {created, updated, unchanged, duplicates, errors, stored},
              "errors":    [{"location": <resource|null>, "reason": "<text>"}],
            }
        """
        if not isinstance(dataset, dict) or not isinstance(dataset.get("locations"), list):
            raise FixtureError("dataset must be an object with a 'locations' list")

        summary: Dict[str, Any] = {
            "locations": {},
            "totals": {key: 0 for key in _COUNT_KEYS},
            "errors": [],
        }
        for entry in dataset["locations"]:
            self._sync_location(entry, summary)
        return summary

    def _sync_location(self, entry: Any, summary: Dict[str, Any]) -> None:
        location = entry.get("location") if isinstance(entry, dict) else None
        if not isinstance(location, str) or not location.strip():
            self._bump(summary, "errors", 1)
            summary["errors"].append(
                {"location": None, "reason": "location entry has no resource name"}
            )
            return

        reviews = entry.get("reviews")
        if reviews is None:
            reviews = []
        if not isinstance(reviews, list):
            self._bump(summary, "errors", 1)
            summary["errors"].append(
                {"location": location, "reason": "reviews must be a list"}
            )
            return

        account_id = self.repo.upsert_account(self._account_of(location))
        location_id = self.repo.upsert_location(account_id, location)

        counts = {key: 0 for key in _COUNT_KEYS}

        # 1) normalise + de-duplicate within this payload (newest updateTime wins)
        unique: Dict[str, Dict[str, Any]] = {}
        for raw in reviews:
            try:
                record = normalize_review(raw, location)
            except FixtureError as exc:
                counts["errors"] += 1
                summary["errors"].append({"location": location, "reason": str(exc)})
                continue
            name = record["review_name"]
            current = unique.get(name)
            if current is not None:
                counts["duplicates"] += 1
                if not self._is_newer(record, current):
                    continue  # an older duplicate: keep the newer snapshot
            unique[name] = record

        # 2) compare-then-write: never churn a row that has not changed
        for record in unique.values():
            existing = self._existing(record["review_name"])
            if existing is None:
                self._write(location_id, record)
                counts["created"] += 1
            elif self._differs(existing, record):
                self._write(location_id, record)
                counts["updated"] += 1
            else:
                counts["unchanged"] += 1

        counts["stored"] = self._count_stored(location_id)
        summary["locations"][location] = counts
        for key in _COUNT_KEYS:
            self._bump(summary, key, counts[key])

    @staticmethod
    def _bump(summary: Dict[str, Any], key: str, delta: int) -> None:
        summary["totals"][key] += delta
