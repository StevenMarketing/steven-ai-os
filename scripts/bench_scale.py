#!/usr/bin/env python3
"""Offline scale harness (Group 8B / T27).

Seeds a DISPOSABLE database with 500 locations + 2000 reviews, starts the real
API server in-process (loopback only) and measures endpoint latency percentiles
plus a full 500-location sync through the concurrency-limited queue.

Nothing here touches Google, the network, or any production database.

Usage
-----
    cd /opt/data/gbp-integration
    PYTHONPATH=src python3 scripts/bench_scale.py                 # 500 x 4
    PYTHONPATH=src python3 scripts/bench_scale.py --locations 200 --reviews 4
    PYTHONPATH=src python3 scripts/bench_scale.py --json          # machine readable
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import statistics
import sys
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from gbp.api.server import create_server  # noqa: E402
from gbp.repo import Repository  # noqa: E402
from gbp.sync.queue import LocationSyncQueue  # noqa: E402
from gbp.sync.reviews_sync import ReviewSync  # noqa: E402
from gbp.workflow.service import ApprovalService  # noqa: E402

SCHEMA_PATH = os.path.join(_ROOT, "sql", "schema.sql")
STATES = ("CA", "TX", "NV", "GA", "FL")


def _review(location: str, index: int, rating: int, day: int) -> Dict[str, Any]:
    stamp = f"2026-03-{day:02d}T08:00:00Z"
    return {
        "name": f"{location}/reviews/r-{index}",
        "reviewId": f"r-{index}",
        "rating": rating,
        "comment": f"scale benchmark review {index}",
        "createTime": stamp,
        "updateTime": stamp,
        "reviewer": {"displayName": f"bench-{index}"},
    }


class _PageClient:
    """Minimal offline stand-in for ``GbpClient`` (two pages per location)."""

    def __init__(self, pages: Dict[str, List[Dict[str, Any]]]) -> None:
        self.pages = pages
        self.calls = 0

    def list_reviews(self, location: str, page_token: Optional[str] = None):
        pages = self.pages.get(location, [])
        index = 0 if not page_token else int(str(page_token).rsplit(":", 1)[-1])
        self.calls += 1
        if index >= len(pages):
            return {"reviews": [], "nextPageToken": None}
        page = dict(pages[index])
        page["nextPageToken"] = (
            f"{location}:{index + 1}" if index + 1 < len(pages) else None)
        return page


def seed(db_path: str, locations: int, reviews_per_location: int):
    """Create the disposable DB and return (repo, locations, pages, names)."""
    repo = Repository(db_path)
    repo.init_db(SCHEMA_PATH)
    acct = repo.upsert_account("accounts/1001")
    names: List[str] = []
    pages: Dict[str, List[Dict[str, Any]]] = {}
    half = max(1, reviews_per_location // 2)
    for i in range(1, locations + 1):
        loc = f"accounts/1001/locations/loc-{i:04d}"
        names.append(loc)
        lid = repo.upsert_location(
            acct, loc, title=f"Bench Nails {i}", city=f"City {i}",
            state=STATES[i % len(STATES)], language="en" if i % 2 else "vi",
            postal_code=f"{90000 + (i % 999):05d}",
        )
        repo.conn.execute(
            "INSERT INTO brand_profiles(location_id, business_name, city,"
            " language, auto_publish_allowed) VALUES (?, ?, ?, 'en', 0)",
            (lid, f"Bench Nails {i}", f"City {i}"))
        first = [_review(loc, j, (j % 5) + 1, j + 1) for j in range(half)]
        second = [_review(loc, half + j, ((half + j) % 5) + 1, half + j + 1)
                  for j in range(reviews_per_location - half)]
        pages[loc] = [{"reviews": first}, {"reviews": second}]
    repo.conn.commit()
    return repo, names, pages


def _pct(values: List[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    idx = min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))
    return ordered[idx]


def measure(port: int, path: str, iterations: int) -> Dict[str, float]:
    timings: List[float] = []
    size = 0
    for _ in range(iterations):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        start = time.perf_counter()
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            body = resp.read()
            assert resp.status == 200, f"{path} -> {resp.status}"
            size = len(body)
        finally:
            conn.close()
        timings.append((time.perf_counter() - start) * 1000.0)
    return {
        "path": path,
        "n": len(timings),
        "bytes": size,
        "p50_ms": round(_pct(timings, 0.50), 2),
        "p95_ms": round(_pct(timings, 0.95), 2),
        "p99_ms": round(_pct(timings, 0.99), 2),
        "max_ms": round(max(timings), 2),
        "mean_ms": round(statistics.fmean(timings), 2),
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Offline 500-location scale harness.")
    parser.add_argument("--locations", type=int, default=500)
    parser.add_argument("--reviews", type=int, default=4,
                        help="reviews per location (4 x 500 = 2000)")
    parser.add_argument("--iterations", type=int, default=15)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "scale.db")
        t0 = time.perf_counter()
        repo, locations, pages = seed(db, args.locations, args.reviews)
        seed_seconds = time.perf_counter() - t0
        review_rows = repo.conn.execute(
            "SELECT COUNT(*) AS n FROM reviews").fetchone()["n"]

        service = ApprovalService(repo)
        server = create_server(service, port=0)
        host, port = server.server_address[:2]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        results: Dict[str, Any] = {
            "locations": args.locations,
            "reviews": review_rows,
            "db": "sqlite (file, disposable)",
            "seed_seconds": round(seed_seconds, 3),
            "endpoints": [],
        }
        for path in (
            "/api/locations?page=1&page_size=25",
            "/api/locations?page=20&page_size=25",
            "/api/locations?q=Bench%20Nails%20250",
            "/api/locations?state=CA",
            "/api/locations?sort=unanswered&order=desc",
            f"/api/locations?q={90000 + (250 % 999):05d}",
            "/api/reviews?page=1&page_size=25",
            "/api/dashboard",
            "/api/locations/1/sync-runs",
        ):
            results["endpoints"].append(measure(port, path, args.iterations))

        # full sync of every location through the bounded queue
        sync = ReviewSync(repo, _PageClient(pages))
        queue = LocationSyncQueue(sync, concurrency=args.concurrency,
                                  sleep=lambda _s: None)
        t1 = time.perf_counter()
        report = queue.run(locations)
        sync_seconds = time.perf_counter() - t1
        rows = repo.conn.execute("SELECT COUNT(*) AS n FROM reviews").fetchone()["n"]
        distinct = repo.conn.execute(
            "SELECT COUNT(DISTINCT review_name) AS n FROM reviews").fetchone()["n"]
        results["sync"] = {
            "seconds": round(sync_seconds, 3),
            "locations": report["locations"],
            "ok": report["ok"],
            "failed": report["failed"],
            "fetched": report["fetched"],
            "upserted": report["upserted"],
            "errors": report["errors"],
            "peak_in_flight": report["peak_in_flight"],
            "concurrency": report["concurrency"],
            "reviews_per_second": round(report["fetched"] / sync_seconds, 1)
            if sync_seconds else 0.0,
            "rows": rows,
            "distinct": distinct,
            "lost": rows != distinct,
        }
        results["ledger"] = {
            "sync_runs": repo.conn.execute(
                "SELECT COUNT(*) AS n FROM sync_runs").fetchone()["n"],
            "sync_run_log": repo.conn.execute(
                "SELECT COUNT(*) AS n FROM sync_run_log").fetchone()["n"],
        }
        results["db_size_bytes"] = os.path.getsize(db)

        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        repo.close()

    if args.json:
        print(json.dumps(results, indent=2))
        return 0

    print(f"scale harness — {results['locations']} locations, "
          f"{results['reviews']} reviews (db {results['db_size_bytes']} bytes, "
          f"seed {results['seed_seconds']}s)")
    print(f"{'endpoint':46} {'p50':>8} {'p95':>8} {'p99':>8} {'max':>8}  bytes")
    for row in results["endpoints"]:
        print(f"{row['path']:46} {row['p50_ms']:8.2f} {row['p95_ms']:8.2f} "
              f"{row['p99_ms']:8.2f} {row['max_ms']:8.2f}  {row['bytes']}")
    sync = results["sync"]
    print(f"\nsync: {sync['seconds']}s · ok={sync['ok']} failed={sync['failed']} "
          f"fetched={sync['fetched']} upserted={sync['upserted']} "
          f"peak={sync['peak_in_flight']}/{sync['concurrency']} "
          f"({sync['reviews_per_second']}/s) rows={sync['rows']} "
          f"distinct={sync['distinct']} lost={sync['lost']}")
    print(f"ledger: sync_runs={results['ledger']['sync_runs']} "
          f"sync_run_log={results['ledger']['sync_run_log']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
