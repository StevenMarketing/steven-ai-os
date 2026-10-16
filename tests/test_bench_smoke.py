"""Fast offline smoke test for the 500-location benchmark harness (T12/T17).

This is the missing smoke test from T12, kept deliberately small and fast
(< ~1 s of real work: 25 locations, a virtual no-wait clock, no real sleeps)
so it can never blow a task's runtime cap the way the bundled T12 card did.

Standard library only, NO network, NO Google, NO AI calls. It REUSES the harness
in ``scripts/bench_500.py`` (importing it) instead of duplicating logic:

* 25 locations through the concurrency-limited queue (cap C=2); assert that
  NEVER more than 2 locations are in flight at once;
* checkpoint/resume: the injected mid-run kill of one location, resumed from its
  persisted pagination checkpoint with no data loss;
* idempotency: 0 duplicates stored, and a second identical pass upserts 0 rows.

Run with::

    cd /opt/data/gbp-integration && PYTHONPATH=src python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib.util
import os
import sys
import threading
import unittest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_SCRIPTS = os.path.join(_ROOT, "scripts")
sys.path.insert(0, os.path.join(_ROOT, "src"))

#: ``scripts/`` is not a package, so load the benchmark module by path.
_SPEC = importlib.util.spec_from_file_location("bench_500", os.path.join(_SCRIPTS, "bench_500.py"))
assert _SPEC is not None and _SPEC.loader is not None
bench = importlib.util.module_from_spec(_SPEC)
# dataclasses resolves string annotations via sys.modules[cls.__module__].
sys.modules[_SPEC.name] = bench
_SPEC.loader.exec_module(bench)

from gbp.client.gbp_client import GbpClient  # noqa: E402
from gbp.client.http_client import HttpClient  # noqa: E402
from gbp.repo.sqlite_repo import Repository  # noqa: E402
from gbp.sync.reviews_sync import ReviewSync  # noqa: E402
from gbp.sync.state import SyncState  # noqa: E402

LOCATIONS = 25
CONCURRENCY = 2


class BenchSmokeTestCase(unittest.TestCase):
    """25-location smoke run of the benchmark harness (concurrency/resume/idempotency)."""

    def setUp(self):
        self.dataset = bench.build_dataset(locations=LOCATIONS)
        self.repo = Repository(":memory:")
        self.repo.init_db()
        self.state = SyncState(self.repo)

        self.transport = bench.BenchTransport(self.dataset)
        clock = bench.VirtualClock()
        quota = bench.LockedQuotaLimiter(qpm=300, clock=clock, sleep=clock.sleep)
        # max_attempts=3 + retries that never actually sleep (no-op).
        http = HttpClient(self.transport, max_attempts=3, base_delay=0.01, sleep=lambda s: None)
        client = GbpClient(http, quota=quota, base=bench.BASE)
        self.sync = ReviewSync(self.repo, client, state=self.state)
        self.db_lock = threading.RLock()

    def tearDown(self):
        self.repo.close()

    def _reviews_stored(self, location):
        row = self.repo.conn.execute(
            "SELECT COUNT(*) AS n FROM reviews WHERE review_name LIKE ?",
            (f"{location}/%",),
        ).fetchone()
        return int(row["n"])

    # ------------------------------------------------------------------ #
    def test_queue_cap_resume_and_idempotency(self):
        # -- pass 1 -------------------------------------------------------- #
        run1 = bench.run_queue(
            self.dataset.locations, self.sync, self.state, CONCURRENCY,
            "smoke-p1", self.db_lock,
        )

        # Concurrency cap: the queue never admits more than C locations at once.
        self.assertLessEqual(run1.peak_in_flight, CONCURRENCY)
        self.assertLessEqual(self.transport.peak_in_flight, CONCURRENCY)
        self.assertGreaterEqual(run1.peak_in_flight, 1)

        # The 10 injected persistent failures + the 1 simulated kill failed.
        self.assertEqual(run1.failed, len(self.dataset.persistent) + 1)
        self.assertIn(self.dataset.kill_location, run1.results)
        self.assertEqual(run1.results[self.dataset.kill_location]["status"], "failed")

        # No duplicate rows: the within-walk duplicate is de-duped by review_name.
        self.assertEqual(bench._db_stats(self.repo)["duplicates"], 0)

        # -- resume from the killed location's checkpoint ------------------ #
        with self.db_lock:
            resume = self.sync.incremental_sync(self.dataset.kill_location)
        self.assertEqual(resume["resumed_from"], self.dataset.kill_token)
        self.assertEqual(resume["upserted"], self.dataset.reviews_per_location)
        # No data loss: every review of the killed location is now stored.
        self.assertEqual(self._reviews_stored(self.dataset.kill_location),
                         self.dataset.reviews_per_location)

        # -- pass 2: identical pass must be a no-op (idempotent) ----------- #
        self.transport.allow_transient = False
        run2 = bench.run_queue(
            self.dataset.locations, self.sync, self.state, CONCURRENCY,
            "smoke-p2", self.db_lock,
        )
        self.assertEqual(run2.upserted, 0)
        self.assertLessEqual(run2.peak_in_flight, CONCURRENCY)
        self.assertEqual(bench._db_stats(self.repo)["duplicates"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
