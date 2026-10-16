"""Command-line entry point for the GBP integration (offline by default).

Usage::

    python -m gbp.cli list-locations [--account accounts/1001] [--limit N]
    python -m gbp.cli sync-reviews  [--location ID] [--since ISO]
    python -m gbp.cli draft-reply   [--location ID]

Design rules (enforced here):

* **Mock is the default.** Without ``--live`` every command reads the local
  ``fixtures/`` and never touches the network.
* ``--live`` is *refused* unless the environment carries usable credentials
  (``OPENROUTER_API_KEY`` and/or a stored OAuth token file).
* There is **no publish command at all** -- this CLI cannot publish a reply.
  ``--publish``-style commands are hard-refused even if someone tries.
* ``draft-reply`` uses the offline :class:`MockModelClient` unless
  ``--live-model`` is passed.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional, Sequence

from gbp import fixtures
from gbp.brand import BrandLoader
from gbp.client.gbp_client import DEFAULT_BASE_URL, GbpClient
from gbp.auth.oauth import (  # noqa: E402
    google_connection_status,
    guard_transport,
)
from gbp.client.http_client import HttpClient, MockTransport
from gbp.reply.duplicate import DuplicateChecker
from gbp.reply.engine import ReviewReplyEngine
from gbp.reply.generator import ReplyGenerator
from gbp.reply.model_client import MockModelClient, OpenRouterModelClient
from gbp.repo.sqlite_repo import Repository
from gbp.sync.reviews_sync import ReviewSync, PAGE_LIMIT  # noqa: F401  (PAGE_LIMIT re-exported for callers)

__all__ = ["main", "build_parser"]

#: Commands that would publish something -- they do not exist and are refused.
FORBIDDEN_COMMANDS = ("publish", "publish-reply", "reply-publish", "approve-publish")

#: Default location when the caller does not pass ``--location``.
DEFAULT_LOCATION = "accounts/1001/locations/loc-001"


# --------------------------------------------------------------------------- #
# credentials / live gate
# --------------------------------------------------------------------------- #
def _token_path() -> str:
    return os.environ.get("GBP_TOKEN_PATH", os.path.expanduser("~/.gbp/token.json"))


def has_live_credentials() -> bool:
    """True only when a stored GOOGLE OAuth token is present and usable.

    An OpenRouter (AI model) key has nothing to do with Business Profile access
    and must never be read as "Google credentials are available" -- that was a
    real bug: with an AI key exported, ``--live`` would happily build a real
    Google client.
    """
    return bool(google_connection_status(_token_path()).get("connected"))


# --------------------------------------------------------------------------- #
# offline fixture transport
# --------------------------------------------------------------------------- #
def build_fixture_transport(base: str = DEFAULT_BASE_URL) -> MockTransport:
    """A :class:`MockTransport` serving the local GBP fixtures.

    Review pages are registered token-specific-first so the parameterised route
    wins (MockTransport returns the first matching route).
    """
    transport = MockTransport()
    transport.register("GET", f"{base}/v1/accounts", fixtures.load_fixture("accounts.json"))
    transport.register(
        "GET",
        f"{base}/v1/accounts/1001/locations",
        {"locations": fixtures.list_locations(), "totalSize": len(fixtures.list_locations())},
        params={"pageSize": 100},
    )

    known = set(fixtures.LOCATION_REVIEW_PAGES)
    for location in [loc["name"] for loc in fixtures.list_locations()]:
        url = f"{base}/v4/{location}/reviews"
        if location not in known:
            transport.register("GET", url, {"reviews": []})
            continue
        pages = [fixtures.load_fixture(f) for f in fixtures.LOCATION_REVIEW_PAGES[location]]
        entries: List[Dict[str, Any]] = []
        token: Optional[str] = None
        for page in pages:
            params: Dict[str, Any] = {"pageSize": 50}
            if token:
                params["pageToken"] = token
            entries.append({"params": params, "page": page})
            token = page.get("nextPageToken")
        for entry in reversed(entries):  # token-specific route first
            transport.register("GET", url, entry["page"], params=entry["params"])
    return transport


def _mock_client(base: str = DEFAULT_BASE_URL) -> GbpClient:
    return GbpClient(build_fixture_transport(base), base=base)


def _live_client(base: str = DEFAULT_BASE_URL) -> GbpClient:
    """Build a real (socket) client. Refuses unless the operator opted in.

    The repository previously had no live transport at all (only
    ``MockTransport`` existed), and this function passed no transport to
    ``HttpClient`` -- so the live path could never have worked. It now uses
    :class:`UrllibTransport`, which is itself guarded, so a live call needs a
    deliberate ``GBP_ALLOW_LIVE_GOOGLE=1``.
    """
    from gbp.client.quota import QuotaLimiter
    from gbp.client.urllib_transport import UrllibTransport

    http = HttpClient(UrllibTransport(), timeout=15.0)
    guard_transport(http)
    return GbpClient(http, quota=QuotaLimiter(), base=base)


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def _cmd_list_locations(args: argparse.Namespace) -> int:
    client = _live_client() if args.live else _mock_client()
    account = args.account or "accounts/1001"
    locations = client.list_locations(account)
    if args.limit is not None:
        locations = locations[: max(0, int(args.limit))]
    print(f"{len(locations)} locations")
    for location in locations:
        name = location.get("name") if isinstance(location, dict) else location
        title = location.get("title", "") if isinstance(location, dict) else ""
        print(f"  {name}\t{title}")
    return 0


def _selected_locations(args: argparse.Namespace) -> List[str]:
    if args.location:
        return [args.location]
    account = args.account or "accounts/1001"
    locations = [loc["name"] for loc in fixtures.list_locations()]
    if account:
        locations = [loc for loc in locations if loc.startswith(f"{account}/") or loc.startswith(account)]
    if args.limit is not None:
        locations = locations[: max(0, int(args.limit))]
    return locations


def _cmd_sync_reviews(args: argparse.Namespace) -> int:
    repo = Repository(args.db or ":memory:")
    repo.init_db()
    try:
        client = _live_client() if args.live else _mock_client()
        sync = ReviewSync(repo, client)
        locations = _selected_locations(args)
        if args.since:
            for location in locations:
                result = sync.incremental_sync(location, since_update_time=args.since)
                print(json.dumps(result))
        else:
            results = sync.sync_all(locations)
            for location in locations:
                print(json.dumps(results[location]))
        total = repo.conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
        runs = repo.conn.execute("SELECT COUNT(*) FROM sync_run_log").fetchone()[0]
        print(f"stored {total} reviews across {len(locations)} locations ({runs} run records)")
        return 0
    finally:
        repo.close()


def _first_unanswered_review(repo: Repository, location: str):
    row = repo.conn.execute(
        """
        SELECT r.*, l.location_name
        FROM reviews r JOIN business_locations l ON l.id = r.location_id
        WHERE l.location_name = ? AND (r.reply_text IS NULL OR TRIM(r.reply_text) = '')
        ORDER BY r.id
        LIMIT 1
        """,
        (location,),
    ).fetchone()
    if row is not None:
        return row
    return repo.conn.execute(
        """
        SELECT r.*, l.location_name
        FROM reviews r JOIN business_locations l ON l.id = r.location_id
        WHERE l.location_name = ?
        ORDER BY r.id LIMIT 1
        """,
        (location,),
    ).fetchone()


def _cmd_draft_reply(args: argparse.Namespace) -> int:
    repo = Repository(args.db or ":memory:")
    repo.init_db()
    try:
        location = args.location or DEFAULT_LOCATION
        client = _live_client() if args.live else _mock_client()
        ReviewSync(repo, client).sync_location(location)

        review = _first_unanswered_review(repo, location)
        if review is None:
            print(f"no reviews for {location}")
            return 1

        brand_loader = BrandLoader.from_file(fixtures.FIXTURES_DIR / "brand_profiles.json")
        dup_checker = DuplicateChecker()
        if args.live_model:
            model_client = OpenRouterModelClient()
        else:
            model_client = MockModelClient()
        generator = ReplyGenerator(model_client, brand_loader, dup_checker)
        engine = ReviewReplyEngine(repo, generator, dup_checker, brand_loader)

        result = engine.process(review)
        print(json.dumps({
            "location": location,
            "review_id": result["review_id"],
            "status": result["status"],
            "draft_id": result["draft_id"],
            "model": type(model_client).__name__,
            "draft": result["draft_text"],
        }, ensure_ascii=False))
        return 0
    finally:
        repo.close()


# --------------------------------------------------------------------------- #
# parser / entry point
# --------------------------------------------------------------------------- #
def _add_common_flags(parser: argparse.ArgumentParser) -> None:
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--mock", action="store_true", help="offline fixtures (default)")
    mode.add_argument("--live", action="store_true", help="use the real API (needs credentials)")
    parser.add_argument("--location", help="target location resource id")
    parser.add_argument("--account", help="account resource id (e.g. accounts/1001)")
    parser.add_argument("--limit", type=int, help="max locations to process")
    parser.add_argument("--since", help="ISO updateTime watermark for an incremental sync")
    parser.add_argument("--db", help="SQLite path (default: in-memory)")
    parser.add_argument("--live-model", action="store_true",
                        help="draft-reply: use the real OpenRouter model (needs a key)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gbp", description="GBP integration CLI (mock by default)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list-locations", help="list GBP locations")
    _add_common_flags(p_list)
    p_list.set_defaults(func=_cmd_list_locations)

    p_sync = sub.add_parser("sync-reviews", help="sync reviews into the local DB")
    _add_common_flags(p_sync)
    p_sync.set_defaults(func=_cmd_sync_reviews)

    p_draft = sub.add_parser("draft-reply", help="draft one reply (never publishes)")
    _add_common_flags(p_draft)
    p_draft.set_defaults(func=_cmd_draft_reply)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the CLI. Returns a process exit code (0 ok, 2 refused, 1 other)."""
    argv = list(sys.argv[1:] if argv is None else argv)

    # Hard-refuse any publish-shaped command: the product never publishes.
    for token in argv:
        if token in FORBIDDEN_COMMANDS:
            print(f"refused: '{token}' is not available -- this CLI never publishes replies",
                  file=sys.stderr)
            return 2

    args = build_parser().parse_args(argv)

    if args.live and not has_live_credentials():
        print(
            "refused: --live requires a credential (OPENROUTER_API_KEY or a stored "
            f"OAuth token at {_token_path()}). Re-run in mock mode (the default).",
            file=sys.stderr,
        )
        return 2

    if getattr(args, "live_model", False) and not os.environ.get("OPENROUTER_API_KEY", "").strip():
        print("refused: --live-model requires OPENROUTER_API_KEY", file=sys.stderr)
        return 2

    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
