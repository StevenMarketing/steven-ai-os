#!/usr/bin/env python3
"""Launch the GBP review-reply management UI locally (offline, stdlib only).

Seeds a local SQLite database from the ``fixtures/`` files, then serves the
static UI (``web/``) plus the JSON API on ``127.0.0.1`` only. Nothing is
published to Google -- the whole app is DRAFT MODE.

Usage:
    cd /opt/data/gbp-integration
    PYTHONPATH=src python3 scripts/serve_ui.py            # http://127.0.0.1:8000/
    PYTHONPATH=src python3 scripts/serve_ui.py --port 9000
    PYTHONPATH=src python3 scripts/serve_ui.py --db /tmp/gbp_ui.db --reset

Stop it with Ctrl-C. This is a local dev tool; it is never deployed.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sqlite3
import sys
from typing import Any, Dict, List, Optional

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from gbp.api.server import create_server  # noqa: E402
from gbp.authz import AuthService  # noqa: E402
from gbp.authz.models import hash_password  # noqa: E402
from gbp.authz.store import UserStore  # noqa: E402
from gbp.security import SessionSecretError, redact  # noqa: E402
from gbp.brand import BrandLoader  # noqa: E402
from gbp.fixtures import (  # noqa: E402
    list_accounts,
    list_locations,
    load_brand_profiles,
    load_fixture,
    load_location_groups,
    review_page_files,
)
from gbp.oauth.config import live_enabled  # noqa: E402
from gbp.reply.classify import parse_stars  # noqa: E402
from gbp.repo import Repository  # noqa: E402
from gbp.workflow.service import ApprovalService  # noqa: E402

SCHEMA_PATH = os.path.join(_ROOT, "sql", "schema.sql")
BRAND_FIXTURE = os.path.join(_ROOT, "fixtures", "brand_profiles.json")

from gbp.security.policy import PasswordPolicyError, validate_password  # noqa: E402

# NOTE: earlier builds wrote a non-AI placeholder ("Draft reply pending review.")
# as a seeded draft_text, which the UI could render as if it were a real AI
# reply. That is a lie: seeded drafts are NOT AI-generated, so we now seed
# draft_text=None and the UI shows an explicit "Reply not generated" state.
# The constant is intentionally gone -- nothing may render it as an answer.


def _seed(repo: Repository) -> int:
    """Load fixture accounts/locations/reviews and stage a draft per review."""
    # Account metadata (display name + type) so the business-group projection
    # can tell a real group from the primary "Ungrouped" account (Issue #112).
    primary = (list_accounts() or [{}])[0]
    acct = repo.upsert_account(
        primary.get("name") or "accounts/1001",
        account_name=primary.get("accountName"),
        account_type=primary.get("type"),
    )

    location_ids: Dict[str, int] = {}
    for loc in list_locations():
        name = loc["name"]
        location_ids[name] = repo.upsert_location(
            acct, name,
            title=loc.get("title"), city=loc.get("city"),
            state=loc.get("state"), language=loc.get("language"),
            # Zip/postal code: only ever taken from a real data source. The
            # bundled fixtures carry no postal code, so demo rows stay NULL and
            # the UI shows an em dash -- nothing is invented.
            postal_code=loc.get("postalCode") or loc.get("postal_code"),
        )

    # Extra business groups (Issue #112): each is a real LOCATION_GROUP account
    # with its own locations, so the dashboard shows more than one group. These
    # carry no reviews (there are no review fixtures for them), so the seeded
    # review counts are unchanged.
    for group in load_location_groups():
        gname = group.get("name")
        if not gname:
            continue
        g_acct = repo.upsert_account(
            gname, account_name=group.get("accountName"),
            account_type=group.get("type"))
        for loc in group.get("locations", []):
            lname = loc.get("name")
            if not lname:
                continue
            location_ids[lname] = repo.upsert_location(
                g_acct, lname,
                title=loc.get("title"), city=loc.get("city"),
                state=loc.get("state"), language=loc.get("language"),
                postal_code=loc.get("postalCode") or loc.get("postal_code"),
            )

    # Brand profiles: keyed by the integer location id so the Locations view and
    # the service's DB fallback both see them.
    for prof in load_brand_profiles():
        name = prof.get("location_id")
        loc_id = location_ids.get(name)
        if loc_id is None:
            continue
        repo.conn.execute(
            "INSERT OR REPLACE INTO brand_profiles"
            "(location_id, business_name, city, services_json, language, tone,"
            " seo_keywords_json, rules_json, auto_publish_allowed)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                loc_id, prof.get("business_name"), prof.get("city"),
                json.dumps(prof.get("services_json"), ensure_ascii=False),
                prof.get("language"), prof.get("tone"),
                json.dumps(prof.get("seo_keywords_json"), ensure_ascii=False),
                json.dumps(prof.get("rules_json"), ensure_ascii=False),
                int(prof.get("auto_publish_allowed", 0)),
            ),
        )

    # Reviews from every per-location fixture page.
    staged = 0
    # Drafts that already exist (by review id) so re-runs are non-destructive:
    # ``insert_reply_draft`` is idempotent, and this set also keeps the staged
    # COUNT correct on a second/third run (0 new drafts, no duplicates).
    already_has_draft = {
        int(r["review_id"]) for r in repo.get_reply_drafts()
    }
    for name, loc_id in location_ids.items():
        for page_name in review_page_files(name):
            page = load_fixture(page_name)
            for rev in page.get("reviews", []):
                reply = rev.get("reviewReply") or {}
                reply_text = reply.get("comment")
                review_id = repo.upsert_review(
                    location_id=loc_id,
                    review_name=rev["name"],
                    review_id=rev.get("reviewId"),
                    rating=parse_stars(rev),
                    comment=rev.get("comment"),
                    reviewer_name=(rev.get("reviewer") or {}).get("displayName"),
                    create_time=rev.get("createTime"),
                    update_time=rev.get("updateTime"),
                    reply_text=reply_text,
                    reply_update_time=reply.get("updateTime"),
                    status="replied" if reply_text else "unanswered",
                )
                if not reply_text and review_id not in already_has_draft:
                    # draft_text stays NULL: a seeded draft is genuinely
                    # "not generated" and must never look like an AI answer.
                    repo.insert_reply_draft(
                        review_id, draft_text=None, status="needs_review"
                    )
                    already_has_draft.add(review_id)
                    staged += 1
    repo.conn.commit()
    return staged


def build_service(db_path: str, reset: bool = False) -> ApprovalService:
    if reset and os.path.exists(db_path):
        os.remove(db_path)
    repo = Repository(db_path)
    repo.init_db(SCHEMA_PATH)
    # Live mode (GBP_LIVE=1 + the OAuth client configured): this DB is a real
    # production store, so NEVER resurrect the bundled demo fixtures into it.
    # The same predicate the rest of the app uses is reused here -- no new env
    # var, and nothing is keyed off the DB contents. Non-live behaviour is
    # unchanged: the seed still runs exactly as before.
    if live_enabled():
        staged = 0
        print("demo seed skipped (live mode)")
    else:
        staged = _seed(repo)
    loader = BrandLoader.from_file(BRAND_FIXTURE) if os.path.exists(BRAND_FIXTURE) else None
    service = ApprovalService(repo, brand_loader=loader)
    if staged or not live_enabled():
        print(f"seeded {staged} draft(s) into {db_path}")
    return service


def _resolve_demo_password() -> Optional[str]:
    """Read the demo admin password WITHOUT ever printing or storing it.

    Preference order: the ``GBP_DEMO_PASSWORD`` environment variable, then an
    interactive (no-echo) prompt. Returns ``None`` when neither is available --
    the caller then starts in mock mode rather than inventing a default.
    """
    env_pw = os.environ.get("GBP_DEMO_PASSWORD")
    if env_pw:
        return env_pw
    if not sys.stdin.isatty():
        return None
    try:
        first = getpass.getpass("Password for demo admin user 'admin': ")
        if not first:
            return None
        second = getpass.getpass("Confirm password: ")
    except (EOFError, KeyboardInterrupt):
        print("", file=sys.stderr)
        return None
    if first != second:
        print("passwords did not match; not enabling auth", file=sys.stderr)
        return None
    return first


def build_auth(repo: Repository, *, password: str, allow_ephemeral: bool = False) -> AuthService:
    """Enable real auth on the demo DB, seeding a demo ``admin`` user.

    The password is hashed with the store's PBKDF2 helper and never logged,
    echoed, or written anywhere in plaintext.

    Real auth REQUIRES a ``GBP_SESSION_SECRET`` (>= 32 chars); a missing or weak
    secret raises :class:`SessionSecretError` unless the operator explicitly
    opts into an ephemeral secret for throwaway local use.
    """
    store = UserStore(repo.conn)
    live = live_enabled()
    if store.get("admin") is None:
        if live:
            # Live mode: never mint a NEW demo admin into a real store. No
            # user row is created here; provisioning real users is out of this
            # local tool's scope. Existing users are untouched either way.
            print("demo admin seeding skipped (live mode)")
        else:
            # The demo admin obeys the real password policy: no weak password can
            # sneak in through the demo path either.
            try:
                validate_password(password, username="admin")
            except PasswordPolicyError as exc:
                raise SystemExit(f"weak GBP_DEMO_PASSWORD: {exc}")
            store.create_user("admin", hash_password(password), ["admin"], [])
            print("created demo user 'admin' (roles: admin)")
    elif live:
        # Live mode never mutates an existing user row (no password reset).
        print("demo user 'admin' already exists (roles: admin)")
    else:
        # An existing admin keeps its password unless GBP_DEMO_PASSWORD was set,
        # in which case the operator clearly wants it rotated.
        if os.environ.get("GBP_DEMO_PASSWORD"):
            store.create_user("admin", hash_password(password), ["admin"], [])
            print("reset demo user 'admin' password (roles: admin)")
        else:
            print("demo user 'admin' already exists (roles: admin)")
    auth = AuthService(repo.conn, require_secret=True, allow_ephemeral=allow_ephemeral)
    if auth.ephemeral_secret:
        # Loud and explicit: this is the opt-in escape hatch, never the default.
        print(
            "WARNING: --allow-ephemeral-secret is active -- an EPHEMERAL signing "
            "secret is in use. Every session will die when this process stops and "
            "any restart logs everyone out. LOCAL EXPERIMENTATION ONLY.",
            file=sys.stderr,
        )
    return auth


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve the GBP review-reply UI (offline).")
    parser.add_argument("--db", default=os.path.join(_ROOT, "gbp_ui.db"),
                        help="SQLite file for the local dev database")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reset", action="store_true",
                        help="delete the DB file first and re-seed from fixtures")
    parser.add_argument("--auth", action="store_true",
                        help="enable real login (opt-in); otherwise the server "
                             "runs in the unauthenticated mock mode")
    parser.add_argument("--allow-ephemeral-secret", action="store_true",
                        help="LOCAL EXPERIMENTATION ONLY: with --auth, permit an "
                             "ephemeral per-process signing secret when "
                             "GBP_SESSION_SECRET is unset (sessions die with the "
                             "process). Never enable this for real use.")
    args = parser.parse_args(argv)

    if args.host not in ("127.0.0.1", "localhost"):
        print("refusing non-loopback host; this is a local dev tool", file=sys.stderr)
        return 2

    service = build_service(args.db, reset=args.reset)

    auth = None
    if args.auth:
        password = _resolve_demo_password()
        if password is None:
            print(
                "auth requested but no password was provided: set GBP_DEMO_PASSWORD "
                "or run interactively. Starting WITHOUT --auth (mock mode) instead.",
                file=sys.stderr,
            )
        else:
            try:
                auth = build_auth(
                    service.repo,
                    password=password,
                    allow_ephemeral=args.allow_ephemeral_secret,
                )
            except SessionSecretError as exc:
                # Fail closed: never invent a signing secret behind the user's back.
                print(redact(f"refusing to start with --auth: {exc}"), file=sys.stderr)
                return 3

    server = create_server(service, host=args.host, port=args.port, auth=auth)
    host, port = server.server_address[:2]
    print(f"DRAFT MODE -- nothing is published to Google.")
    mode = "auth (login required)" if auth is not None else "mock (no auth)"
    print(f"mode: {mode}")
    print(f"Management UI: http://{host}:{port}/")
    print("Press Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping...")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
