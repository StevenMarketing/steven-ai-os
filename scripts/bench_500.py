#!/usr/bin/env python3
"""Offline scale benchmark: 500 locations x ~4 reviews through the sync queue.

Group 5B. Standard library only. NO network, NO Google, NO AI/model calls.

What it exercises (all in-process, all offline):

* a generated dataset of ``--locations`` GBP locations (default 500), each with
  ~4 reviews (=> >=2000 reviews), mixed EN/VI, injected transient 429/5xx,
  a review duplicated across two pages, and ~10 persistently-failing locations;
* a concurrency-limited queue built from :mod:`threading` (cap ``--concurrency``,
  default 2) that syncs locations through :class:`~gbp.client.gbp_client.GbpClient`;
* a :class:`~gbp.client.quota.QuotaLimiter` (QPM) wired into every API call;
* retry-with-backoff on the injected 429/5xx (via ``HttpClient``);
* checkpoint/resume: one location is "killed" mid-run (after page 1) and then
  resumed from its persisted pagination checkpoint;
* idempotent re-run: a second full pass must upsert 0 rows and create 0
  duplicates.

The measured numbers are written to ``docs/BENCHMARK.md`` and printed to stdout.

Usage::

    cd /opt/data/gbp-integration
    PYTHONPATH=src python3 scripts/bench_500.py --locations 500 --concurrency 2
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

# -- make ``gbp`` importable whether run as a script or imported by a test --- #
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
for _path in (os.path.join(_ROOT, "src"), _HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from gbp.client.gbp_client import GbpClient  # noqa: E402
from gbp.client.http_client import HttpClient, Response  # noqa: E402
from gbp.client.quota import QuotaLimiter  # noqa: E402
from gbp.repo.sqlite_repo import Repository  # noqa: E402
from gbp.sync.reviews_sync import ReviewSync  # noqa: E402
from gbp.sync.state import SyncState  # noqa: E402

#: A non-routable host: the transport never actually dials it.
BASE = "https://bench.invalid"
#: Default alert threshold (matches SyncState.DEFAULT_ALERT_THRESHOLD).
ALERT_THRESHOLD = 3
BENCHMARK_PATH = os.path.join(_ROOT, "docs", "BENCHMARK.md")


class BenchKillError(RuntimeError):
    """Simulated fatal crash mid-run (NOT retryable) used to test resume."""


# --------------------------------------------------------------------------- #
# clocks / limiter
# --------------------------------------------------------------------------- #
class VirtualClock:
    """Thread-safe virtual monotonic clock whose ``sleep`` advances the time.

    Lets the QPM limiter "wait" without ever really sleeping, so the benchmark
    stays fast (no wall-clock cost) while still exercising the window logic.
    """

    def __init__(self, start: float = 0.0) -> None:
        self._t = float(start)
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self._t

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self._t += max(0.0, float(seconds))

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._t += max(0.0, float(seconds))


class LockedQuotaLimiter(QuotaLimiter):
    """A :class:`QuotaLimiter` made safe for concurrent (threaded) callers."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._acquire_lock = threading.Lock()

    def acquire(self) -> None:  # noqa: D401 - thin guard
        with self._acquire_lock:
            return super().acquire()


# --------------------------------------------------------------------------- #
# dataset
# --------------------------------------------------------------------------- #
_EN_COMMENTS = [
    "Loved my gel manicure, thank you so much!",
    "Great pedicure, though I waited about 20 minutes.",
    "The nail art design was exactly what I wanted.",
    "Clean place and friendly staff, will come back.",
]
_VI_COMMENTS = [
    "Tiệm làm móng rất đẹp, nhân viên dễ thương và chu đáo.",
    "Dịch vụ tốt nhưng mình phải chờ hơi lâu.",
    "Làm móng tay rất ưng ý, sẽ quay lại lần sau.",
    "Không gian sạch sẽ, nhân viên thân thiện.",
]


@dataclass
class Dataset:
    """The generated offline dataset plus the injected faults."""

    locations: List[str]
    pages: Dict[str, List[Dict[str, Any]]]
    persistent: Set[str]
    persistent_status: Dict[str, int]
    transient: Dict[str, int]
    transient_status: Dict[str, int]
    kill_location: str
    kill_token: str
    reviews_per_location: int
    #: unique reviews generated (excludes the within-walk duplicate).
    unique_reviews: int
    #: raw reviews served, including the across-page duplicate.
    raw_reviews: int

    def unique_names(self) -> Set[str]:
        names: Set[str] = set()
        for pages in self.pages.values():
            for page in pages:
                for review in page.get("reviews") or []:
                    names.add(review["name"])
        return names


def _review(location: str, index: int, rating: int, comment: str, day: int) -> Dict[str, Any]:
    stamp = f"2026-01-{day:02d}T08:00:00Z"
    return {
        "name": f"{location}/reviews/r-{index}",
        "reviewId": f"r-{index}",
        "rating": rating,
        "comment": comment,
        "createTime": stamp,
        "updateTime": stamp,
        "reviewer": {"displayName": f"user-{index}"},
    }


def build_dataset(
    locations: int = 500,
    reviews_per_location: int = 4,
    persistent_count: int = 10,
    transient_count: int = 30,
) -> Dataset:
    """Generate ``locations`` locations with ~4 reviews each + injected faults."""
    names = [f"accounts/bench/locations/loc-{i:04d}" for i in range(locations)]
    pages: Dict[str, List[Dict[str, Any]]] = {}
    unique_reviews = 0
    raw_reviews = 0

    for idx, loc in enumerate(names):
        vi = (idx % 2 == 1)
        comments = _VI_COMMENTS if vi else _EN_COMMENTS
        reviews = [
            _review(loc, j, ((idx + j) % 5) + 1, comments[j % len(comments)], j + 1)
            for j in range(reviews_per_location)
        ]
        unique_reviews += reviews_per_location
        mid = max(1, reviews_per_location // 2)
        page1 = {"reviews": reviews[:mid], "nextPageToken": f"{loc}/tok-p2"}
        # page 2 repeats reviews[0] (a duplicate across pages) + the rest.
        page2_reviews = list(reviews[mid:])
        if idx % 4 != 0:  # skip the duplicate for 1/4 of locations
            page2_reviews.append(reviews[0])
        pages[loc] = [page1, {"reviews": page2_reviews, "totalSize": len(reviews)}]
        raw_reviews += len(page1["reviews"]) + len(page2_reviews)

    # One location is killed after page 1 and resumed from its checkpoint.
    kill_location = names[len(names) // 2]
    kill_reviews = [
        _review(kill_location, j, 5 - (j % 3), _EN_COMMENTS[j % len(_EN_COMMENTS)], j + 1)
        for j in range(reviews_per_location)
    ]
    kill_token = f"{kill_location}/kill-p2"
    pages[kill_location] = [
        {"reviews": [], "nextPageToken": kill_token},
        {"reviews": kill_reviews, "totalSize": len(kill_reviews)},
    ]

    # Persistent failures: spread ~10 of them across the range.
    persistent: Set[str] = set()
    persistent_status: Dict[str, int] = {}
    step = max(1, locations // max(1, persistent_count))
    for k in range(persistent_count):
        loc = names[(k * step + 1) % locations]
        if loc == kill_location:
            loc = names[(k * step + 2) % locations]
        persistent.add(loc)
        persistent_status[loc] = 429 if k % 2 == 0 else 500

    # Transient failures (retried once/twice then succeeding).
    transient: Dict[str, int] = {}
    transient_status: Dict[str, int] = {}
    for k in range(transient_count):
        loc = names[(k * 7 + 3) % locations]
        if loc == kill_location or loc in persistent:
            continue
        transient[loc] = 1 if k % 5 else 2
        transient_status[loc] = 429 if k % 3 else 500

    return Dataset(
        locations=names,
        pages=pages,
        persistent=persistent,
        persistent_status=persistent_status,
        transient=transient,
        transient_status=transient_status,
        kill_location=kill_location,
        kill_token=kill_token,
        reviews_per_location=reviews_per_location,
        unique_reviews=unique_reviews,
        raw_reviews=raw_reviews,
    )


# --------------------------------------------------------------------------- #
# transport
# --------------------------------------------------------------------------- #
class BenchTransport:
    """In-memory reviews transport with fault injection + concurrency metrics.

    Implements the :class:`~gbp.client.http_client.Transport` protocol (``get``)
    so it can be wrapped by :class:`~gbp.client.http_client.HttpClient` for
    retry/backoff. No sockets, no network.
    """

    def __init__(self, dataset: Dataset) -> None:
        self.dataset = dataset
        self.pages = dataset.pages
        self.persistent = dataset.persistent
        self.persistent_status = dataset.persistent_status
        self.transient = dict(dataset.transient)
        self.transient_status = dataset.transient_status
        self.kill_at = {dataset.kill_location: dataset.kill_token}
        self.killed: Set[str] = set()
        #: transient injection is only active during the first pass.
        self.allow_transient = True

        # token -> page, computed once per location.
        self._by_token: Dict[str, Dict[Optional[str], Dict[str, Any]]] = {}
        for loc, pages in self.pages.items():
            mapping: Dict[Optional[str], Dict[str, Any]] = {None: pages[0]}
            token = pages[0].get("nextPageToken")
            i = 1
            while token and i < len(pages):
                mapping[token] = pages[i]
                token = pages[i].get("nextPageToken")
                i += 1
            self._by_token[loc] = mapping

        self.lock = threading.Lock()
        self.calls = 0
        self.pages_served = 0
        self.retry_responses = 0
        self.kill_events = 0
        self.in_flight = 0
        self.peak_in_flight = 0

    @staticmethod
    def _location_of(url: str) -> str:
        marker = "/v4/"
        if marker not in url:
            return url
        rest = url.split(marker, 1)[1]
        if rest.endswith("/reviews"):
            rest = rest[: -len("/reviews")]
        return rest

    def get(self, url: str, params: Optional[Dict[str, Any]] = None,
            headers: Optional[Dict[str, str]] = None, timeout: float = 15.0) -> Response:
        with self.lock:
            self.calls += 1
            self.in_flight += 1
            if self.in_flight > self.peak_in_flight:
                self.peak_in_flight = self.in_flight
        try:
            return self._handle(url, dict(params or {}))
        finally:
            with self.lock:
                self.in_flight -= 1

    # The reviews endpoints are all GETs; keep a POST for protocol completeness.
    def post(self, url: str, data: Optional[Dict[str, Any]] = None,
             headers: Optional[Dict[str, str]] = None, timeout: float = 15.0) -> Response:
        with self.lock:
            self.calls += 1
        return self._handle(url, dict(data or {}))

    def _handle(self, url: str, params: Dict[str, Any]) -> Response:
        loc = self._location_of(url)
        if loc not in self.pages:
            return Response(404, {"error": {"code": 404, "message": "unknown location"}})

        token = params.get("pageToken")
        if loc in self.kill_at and self.kill_at[loc] == token and loc not in self.killed:
            with self.lock:
                self.killed.add(loc)
                self.kill_events += 1
            raise BenchKillError(f"simulated crash for {loc} at token {token!r}")

        if loc in self.persistent:
            code = self.persistent_status.get(loc, 429)
            with self.lock:
                self.retry_responses += 1
            return Response(code, {"error": {"code": code, "message": "persistent failure"}})

        if self.allow_transient:
            remaining = self.transient.get(loc, 0)
            if remaining > 0:
                self.transient[loc] = remaining - 1
                code = self.transient_status.get(loc, 429)
                with self.lock:
                    self.retry_responses += 1
                return Response(code, {"error": {"code": code, "message": "temporary failure"}})

        page = self._by_token.get(loc, {}).get(token)
        if page is None:
            return Response(404, {"error": {"code": 404, "message": "no page for token"}})
        with self.lock:
            self.pages_served += 1
        return Response(200, page)


# --------------------------------------------------------------------------- #
# queue
# --------------------------------------------------------------------------- #
@dataclass
class PassResult:
    results: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    peak_in_flight: int = 0
    wall_seconds: float = 0.0
    upserted: int = 0
    fetched: int = 0
    failed: int = 0


def run_queue(
    locations: List[str],
    sync: ReviewSync,
    state: SyncState,
    concurrency: int,
    run_key_prefix: str,
    db_lock: Optional[threading.RLock] = None,
) -> PassResult:
    """Sync ``locations`` through a concurrency-limited (cap ``concurrency``) queue.

    Returns a :class:`PassResult`; each location's outcome is also recorded in
    the :class:`~gbp.sync.state.SyncState` run ledger (ok / failed).

    ``db_lock`` serializes access to the single shared in-memory SQLite
    connection: the queue admits up to ``concurrency`` locations, while the one
    connection's writes are executed one at a time (SQLite requires it).
    """
    sem = threading.BoundedSemaphore(int(concurrency))
    lock = threading.Lock()
    in_flight = 0
    peak = 0
    results: Dict[str, Dict[str, Any]] = {}
    upserted = fetched = failed = 0
    db_lock = db_lock or threading.RLock()

    def worker(loc: str) -> None:
        nonlocal in_flight, peak, upserted, fetched, failed
        sem.acquire()
        try:
            with lock:
                in_flight += 1
                peak = max(peak, in_flight)
            with db_lock:
                status = "ok"
                try:
                    res = sync.incremental_sync(loc)
                    row_fetched = int(res.get("fetched") or 0)
                    row_upserted = int(res.get("upserted") or 0)
                    row_errors = int(res.get("errors") or 0)
                except Exception as exc:  # noqa: BLE001 - a failure never aborts the queue
                    status = "failed"
                    row_fetched = row_upserted = 0
                    row_errors = 1
                    res = {
                        "location": loc, "fetched": 0, "upserted": 0, "errors": 1,
                        "status": "failed", "error": type(exc).__name__,
                    }
                # Ledger write drives consecutive-failure alerting.
                state.record_run(
                    f"{run_key_prefix}:{loc}", loc, status,
                    row_fetched, row_upserted, row_errors, None, None,
                )
            with lock:
                in_flight -= 1
                results[loc] = res
                upserted += row_upserted
                fetched += row_fetched
                if status == "failed":
                    failed += 1
        finally:
            sem.release()

    threads = [threading.Thread(target=worker, args=(loc,), daemon=True) for loc in locations]
    start = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - start
    return PassResult(
        results=results, peak_in_flight=peak, wall_seconds=wall,
        upserted=upserted, fetched=fetched, failed=failed,
    )


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #
def _db_stats(repo: Repository) -> Dict[str, int]:
    row = repo.conn.execute("SELECT COUNT(*) AS n FROM reviews").fetchone()
    total = int(row["n"])
    row = repo.conn.execute(
        "SELECT COUNT(*) AS n FROM (SELECT review_name FROM reviews GROUP BY review_name)"
    ).fetchone()
    distinct = int(row["n"])
    return {"rows": total, "distinct": distinct, "duplicates": total - distinct}


def run_benchmark(
    locations: int = 500,
    concurrency: int = 2,
    qpm: int = 300,
    reviews_per_location: int = 4,
) -> Dict[str, Any]:
    """Run the full offline benchmark and return the measured metrics."""
    dataset = build_dataset(locations=locations, reviews_per_location=reviews_per_location)

    repo = Repository(":memory:")
    repo.init_db()
    state = SyncState(repo)

    transport = BenchTransport(dataset)
    clock = VirtualClock()
    quota = LockedQuotaLimiter(qpm=qpm, clock=clock, sleep=clock.sleep)
    http = HttpClient(transport, max_attempts=3, base_delay=0.01, sleep=lambda s: None)
    client = GbpClient(http, quota=quota, base=BASE)
    sync = ReviewSync(repo, client, state=state)
    db_lock = threading.RLock()

    # -- pass 1: the primary full run -------------------------------------- #
    run1 = run_queue(dataset.locations, sync, state, concurrency, "bench-p1", db_lock)
    db1 = _db_stats(repo)

    # -- resume: the killed location continues from its checkpoint --------- #
    with db_lock:
        resume_res = sync.incremental_sync(dataset.kill_location)
        state.record_run(
            f"bench-resume:{dataset.kill_location}", dataset.kill_location, "ok",
            int(resume_res.get("fetched") or 0), int(resume_res.get("upserted") or 0), 0,
            None, None,
        )
    resumed_from = resume_res.get("resumed_from")

    # transient injection is a first-pass phenomenon only.
    transport.allow_transient = False

    # -- pass 2: idempotency (must upsert 0, duplicate 0) ------------------ #
    run2 = run_queue(dataset.locations, sync, state, concurrency, "bench-p2", db_lock)
    db2 = _db_stats(repo)

    # -- pass 3: drives the persistent failures to the alert threshold ----- #
    run3 = run_queue(dataset.locations, sync, state, concurrency, "bench-p3", db_lock)
    db3 = _db_stats(repo)

    alerts = sorted(loc for loc in dataset.locations if state.should_alert(loc, ALERT_THRESHOLD))

    run1_secs = run1.wall_seconds or 0.0
    reviews_per_sec = (dataset.unique_reviews / run1_secs) if run1_secs > 0 else 0.0

    metrics: Dict[str, Any] = {
        "generated_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "locations": locations,
        "concurrency": concurrency,
        "qpm": qpm,
        "reviews_per_location": reviews_per_location,
        "unique_reviews": dataset.unique_reviews,
        "raw_reviews": dataset.raw_reviews,
        "run1": {
            "upserted": run1.upserted,
            "fetched": run1.fetched,
            "failed_locations": run1.failed,
            "duplicates": db1["duplicates"],
            "rows": db1["rows"],
            "wall_seconds": run1_secs,
            "reviews_per_sec": reviews_per_sec,
            "peak_queue": run1.peak_in_flight,
        },
        "resume": {
            "location": dataset.kill_location,
            "resumed_from": resumed_from,
            "upserted": int(resume_res.get("upserted") or 0),
        },
        "run2_idempotent": {
            "upserted": run2.upserted,
            "duplicates": db2["duplicates"],
            "rows": db2["rows"],
            "failed_locations": run2.failed,
        },
        "run3": {"upserted": run3.upserted, "failed_locations": run3.failed},
        "alert_count": len(alerts),
        "alert_locations": alerts,
        "db_rows": db3["rows"],
        "db_duplicates": db3["duplicates"],
        "transport": {
            "calls": transport.calls,
            "pages_served": transport.pages_served,
            "retry_responses": transport.retry_responses,
            "kill_events": transport.kill_events,
            "peak_concurrent_requests": transport.peak_in_flight,
        },
        "quota": {"calls": quota.calls, "qpm": qpm, "virtual_clock": clock()},
    }
    repo.close()
    return metrics


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #
def render_markdown(m: Dict[str, Any], command: str) -> str:
    run1 = m["run1"]
    lines = [
        "# Offline scale benchmark (Group 5B)",
        "",
        f"Generated: {m['generated_at']}",
        "",
        "Fully offline: in-memory SQLite, an in-memory reviews transport with",
        "fault injection, a virtual (no-wait) clock, and NO AI/model calls.",
        "",
        "## Command",
        "",
        "```",
        command,
        "```",
        "",
        "## Configuration",
        "",
        "| Setting | Value |",
        "| --- | --- |",
        f"| Locations | {m['locations']} |",
        f"| Reviews / location | {m['reviews_per_location']} |",
        f"| Unique reviews generated | {m['unique_reviews']} |",
        f"| Raw reviews served (incl. across-page duplicate) | {m['raw_reviews']} |",
        f"| Queue concurrency cap | {m['concurrency']} |",
        f"| QuotaLimiter QPM | {m['qpm']} |",
        f"| Persistent-failure locations | {m['run1']['failed_locations'] - 1} |",
        "",
        "## Pass 1 (primary run)",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Reviews upserted | {run1['upserted']} |",
        f"| Reviews fetched | {run1['fetched']} |",
        f"| Duplicates stored | {run1['duplicates']} |",
        f"| DB review rows | {run1['rows']} |",
        f"| Failed locations (incl. the killed one) | {run1['failed_locations']} |",
        f"| Wall-clock seconds | {run1['wall_seconds']:.3f} |",
        f"| Reviews / second | {run1['reviews_per_sec']:.1f} |",
        f"| Peak queue usage (locations in flight) | {run1['peak_queue']} |",
        "",
        "## Checkpoint / resume",
        "",
        f"- Killed location: `{m['resume']['location']}`",
        f"- Resumed from checkpoint token: `{m['resume']['resumed_from']}`",
        f"- Reviews upserted on resume: {m['resume']['upserted']}",
        "",
        "## Pass 2 (idempotent re-run)",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Reviews upserted | {m['run2_idempotent']['upserted']} |",
        f"| Duplicates stored | {m['run2_idempotent']['duplicates']} |",
        f"| DB review rows | {m['run2_idempotent']['rows']} |",
        f"| Failed locations | {m['run2_idempotent']['failed_locations']} |",
        "",
        "## Alerts",
        "",
        f"- `should_alert(threshold={ALERT_THRESHOLD})` fired for {m['alert_count']} locations.",
        f"- Alerting locations: {', '.join(m['alert_locations']) or '(none)'}",
        "",
        "## Transport / quota instrumentation",
        "",
        f"- Transport calls: {m['transport']['calls']}",
        f"- Pages served: {m['transport']['pages_served']}",
        f"- Injected 429/5xx responses (retried with backoff): {m['transport']['retry_responses']}",
        f"- Simulated kill events: {m['transport']['kill_events']}",
        f"- Peak concurrent transport requests: {m['transport']['peak_concurrent_requests']}",
        f"- QuotaLimiter calls admitted: {m['quota']['calls']} (QPM {m['quota']['qpm']})",
        "",
        "## Notes",
        "",
        "- `incremental_sync` persists a per-location watermark, so pass 2 and 3",
        "  fetch every page but skip every unchanged review: `upserted == 0`.",
        "- The within-walk duplicate (a review repeated on page 2) is de-duplicated",
        "  by `review_name`, so duplicates stored is always 0.",
        "- The killed location is resumed from its stored pagination checkpoint",
        "  (`resumed_from`) with no data loss.",
        "",
    ]
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--locations", type=int, default=500)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--qpm", type=int, default=300)
    parser.add_argument("--reviews-per-location", type=int, default=4)
    parser.add_argument("--write-doc", action="store_true", default=True,
                        help="write docs/BENCHMARK.md (default: true)")
    parser.add_argument("--no-doc", dest="write_doc", action="store_false")
    args = parser.parse_args(argv)

    metrics = run_benchmark(
        locations=args.locations,
        concurrency=args.concurrency,
        qpm=args.qpm,
        reviews_per_location=args.reviews_per_location,
    )

    command = (
        f"PYTHONPATH=src python3 scripts/bench_500.py "
        f"--locations {args.locations} --concurrency {args.concurrency}"
    )
    markdown = render_markdown(metrics, command)

    print("=" * 68)
    print("OFFLINE BENCHMARK RESULTS")
    print("=" * 68)
    print(f"locations={metrics['locations']} concurrency={metrics['concurrency']} "
          f"unique_reviews={metrics['unique_reviews']}")
    r1 = metrics["run1"]
    print(f"PASS1 upserts={r1['upserted']} fetched={r1['fetched']} "
          f"duplicates={r1['duplicates']} failed={r1['failed_locations']} "
          f"wall={r1['wall_seconds']:.3f}s reviews/sec={r1['reviews_per_sec']:.1f} "
          f"peak_queue={r1['peak_queue']}")
    print(f"RESUME location={metrics['resume']['location']} "
          f"resumed_from={metrics['resume']['resumed_from']} "
          f"upserts={metrics['resume']['upserted']}")
    r2 = metrics["run2_idempotent"]
    print(f"PASS2 upserts={r2['upserted']} duplicates={r2['duplicates']} "
          f"rows={r2['rows']} failed={r2['failed_locations']} (idempotent)")
    print(f"ALERTS threshold={ALERT_THRESHOLD} count={metrics['alert_count']}")
    print(f"TRANSPORT calls={metrics['transport']['calls']} "
          f"retries={metrics['transport']['retry_responses']} "
          f"quota_calls={metrics['quota']['calls']}")
    print("=" * 68)

    if args.write_doc:
        os.makedirs(os.path.dirname(BENCHMARK_PATH), exist_ok=True)
        with open(BENCHMARK_PATH, "w", encoding="utf-8") as fh:
            fh.write(markdown)
        print(f"wrote {BENCHMARK_PATH}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
