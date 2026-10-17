"""Offline tests for the fixture-driven review sync (issue #32).

Everything runs against a synthetic JSON dataset and an in-memory SQLite store.
No network, no Google, standard library only. A guard transport that would raise
on any request proves a full sync makes no live call.
"""

from __future__ import annotations

import copy
import json
import pathlib
import unittest

from gbp.repo.sqlite_repo import Repository
from gbp.sync.fixture_sync import FixtureError, FixtureSync, normalize_review

DATASET_PATH = pathlib.Path(__file__).resolve().parent / "fixtures" / "reviews_offline_dataset.json"

LOC_A = "accounts/9001/locations/loc-101"
LOC_B = "accounts/9001/locations/loc-202"
LOC_C = "accounts/9001/locations/loc-303"


class RaisingTransport:
    """A transport that fails loudly if the sync ever tries a live call."""

    def __init__(self) -> None:
        self.calls = 0

    def request(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("a live HTTP call was attempted during an offline sync")


def load_dataset() -> dict:
    with DATASET_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def fresh_repo() -> Repository:
    repo = Repository(":memory:")
    repo.init_db()
    return repo


def rows_for(repo: Repository, location: str):
    return repo.conn.execute(
        "SELECT r.* FROM reviews r JOIN business_locations l ON l.id = r.location_id "
        "WHERE l.location_name = ? ORDER BY r.id",
        (location,),
    ).fetchall()


class OfflineFixtureSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = fresh_repo()
        self.sync = FixtureSync(self.repo)
        self.dataset = load_dataset()

    # 1 ------------------------------------------------------------------- #
    def test_first_run_creates_expected_count_per_location(self) -> None:
        summary = self.sync.sync_dataset(self.dataset)
        # loc-101: rev-1 (deduplicated) + rev-2 + rev-3 = 3
        # loc-202: rev-1 only; the four malformed entries are skipped
        # loc-303: empty
        self.assertEqual(summary["locations"][LOC_A]["created"], 3)
        self.assertEqual(summary["locations"][LOC_B]["created"], 1)
        self.assertEqual(summary["locations"][LOC_C]["created"], 0)
        self.assertEqual(summary["totals"]["created"], 4)
        self.assertEqual(summary["totals"]["errors"], 4)
        self.assertEqual(summary["totals"]["duplicates"], 1)
        self.assertEqual(len(rows_for(self.repo, LOC_A)), 3)
        self.assertEqual(len(rows_for(self.repo, LOC_B)), 1)
        self.assertEqual(len(rows_for(self.repo, LOC_C)), 0)
        # the summary is machine-readable
        self.assertIsInstance(json.dumps(summary), str)

    # 2 ------------------------------------------------------------------- #
    def test_rerun_is_a_noop_with_no_field_churn(self) -> None:
        self.sync.sync_dataset(self.dataset)
        before = [
            tuple(row) for row in self.repo.conn.execute(
                "SELECT id, review_name, rating, comment, reviewer_name, "
                "update_time, first_seen_at, last_seen_at FROM reviews ORDER BY id"
            )
        ]
        summary = self.sync.sync_dataset(self.dataset)
        after = [
            tuple(row) for row in self.repo.conn.execute(
                "SELECT id, review_name, rating, comment, reviewer_name, "
                "update_time, first_seen_at, last_seen_at FROM reviews ORDER BY id"
            )
        ]
        self.assertEqual(summary["totals"]["created"], 0)
        self.assertEqual(summary["totals"]["updated"], 0)
        self.assertEqual(summary["totals"]["unchanged"], 4)
        self.assertEqual(summary["totals"]["stored"], 4)
        self.assertEqual(before, after)  # stable counts, no churn, no duplicate rows

    # 3 ------------------------------------------------------------------- #
    def test_changed_fields_update_the_same_row_in_place(self) -> None:
        self.sync.sync_dataset(self.dataset)
        original_id = rows_for(self.repo, LOC_A)[1]["id"]  # rev-2

        changed = copy.deepcopy(self.dataset)
        for entry in changed["locations"]:
            if entry["location"] == LOC_A:
                for review in entry["reviews"]:
                    if review["reviewId"] == "rev-2":
                        review["comment"] = "Edited: nice nails."
                        review["rating"] = 2
                        review["updateTime"] = "2026-03-01T00:00:00Z"

        summary = self.sync.sync_dataset(changed)
        self.assertEqual(summary["totals"]["created"], 0)
        self.assertEqual(summary["totals"]["updated"], 1)
        self.assertEqual(len(rows_for(self.repo, LOC_A)), 3)  # no new row
        row = rows_for(self.repo, LOC_A)[1]
        self.assertEqual(row["id"], original_id)  # same row updated in place
        self.assertEqual(row["comment"], "Edited: nice nails.")
        self.assertEqual(row["rating"], 2)

    # 4 ------------------------------------------------------------------- #
    def test_same_review_id_across_two_locations_stays_two_records(self) -> None:
        self.sync.sync_dataset(self.dataset)
        a = rows_for(self.repo, LOC_A)[0]
        b = rows_for(self.repo, LOC_B)[0]
        self.assertEqual(a["review_id"], "rev-1")
        self.assertEqual(b["review_id"], "rev-1")
        self.assertNotEqual(a["location_id"], b["location_id"])
        self.assertNotEqual(a["review_name"], b["review_name"])
        self.assertEqual(a["comment"], "Great service, thanks!")  # newest dedup winner

    # 5 ------------------------------------------------------------------- #
    def test_empty_and_malformed_fixtures_never_crash_or_write_partial(self) -> None:
        empty = self.sync.sync_dataset({"locations": []})
        self.assertEqual(empty["totals"]["created"], 0)
        self.assertEqual(empty["locations"], {})

        malformed = self.sync.sync_dataset(
            {"locations": [{"location": "accounts/9001/locations/loc-bad",
                            "reviews": [{"rating": 4}, "nope"]}]}
        )
        self.assertEqual(malformed["totals"]["errors"], 2)
        self.assertEqual(malformed["totals"]["created"], 0)
        self.assertEqual(len(rows_for(self.repo, "accounts/9001/locations/loc-bad")), 0)

        # an entry with no resource name is reported, not written
        no_name = self.sync.sync_dataset({"locations": [{"reviews": []}]})
        self.assertEqual(no_name["totals"]["errors"], 1)
        self.assertEqual(no_name["errors"][0]["location"], None)

    # 6 ------------------------------------------------------------------- #
    def test_null_reviewer_name_is_stored_not_rejected(self) -> None:
        self.sync.sync_dataset(self.dataset)
        rev3 = [r for r in rows_for(self.repo, LOC_A) if r["review_id"] == "rev-3"][0]
        self.assertIsNone(rev3["reviewer_name"])
        self.assertEqual(rev3["status"], "pending")  # state stored as data only

    # 7 ------------------------------------------------------------------- #
    def test_error_details_report_location_and_reason(self) -> None:
        summary = self.sync.sync_dataset(self.dataset)
        self.assertEqual(len(summary["errors"]), 4)
        for item in summary["errors"]:
            self.assertEqual(item["location"], LOC_B)
            self.assertTrue(item["reason"])

    # 8 ------------------------------------------------------------------- #
    def test_transport_guard_proves_no_live_call(self) -> None:
        transport = RaisingTransport()
        guard = FixtureSync(self.repo, client=transport)
        summary = guard.sync_dataset(self.dataset)
        self.assertEqual(summary["totals"]["created"], 4)
        self.assertEqual(transport.calls, 0)  # the guard was never touched

    def test_normalize_review_rejects_bad_rating_range(self) -> None:
        with self.assertRaises(FixtureError):
            normalize_review({"reviewId": "x", "rating": 0}, LOC_A)


if __name__ == "__main__":
    unittest.main()
