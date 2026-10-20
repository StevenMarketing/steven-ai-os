"""Minimal JSON HTTP API over :class:`gbp.workflow.service.ApprovalService`.

Standard library only -- ``http.server`` + ``json``. No framework, no network
egress: every response is computed from the local (mock) SQLite data.

Routes (JSON in / JSON out):

    GET  /api/dashboard                   -> dashboard_counts()
    GET  /api/queue?page=&page_size=&q=&location_id=&stars=&status=&language=&since=
                                          -> {items, total, page, page_size, pages}
    GET  /api/locations                   -> list_locations() (+ brand flags)
    GET  /api/locations/status            -> per-location sync state + counters
    GET  /api/usage                       -> AI usage/cost from api_usage
    GET  /api/audit?entity=&entity_id=&actor=&limit=
                                          -> audit rows
    POST /api/drafts/<id>/edit            -> edit_draft
    POST /api/drafts/<id>/approve         -> approve
    POST /api/drafts/<id>/reject          -> reject
    POST /api/drafts/<id>/publish-guard   -> dry-run guard (no side effects)
    POST /api/drafts/bulk-approve         -> bulk_approve

Authentication (Group 5D/8A -- only active when ``auth`` is supplied):

    POST /api/login   {username,password} -> session cookie + CSRF token
    POST /api/logout                      -> clears the session cookie
    GET  /api/me                          -> current user (username/roles/locations + CSRF token)

When ``make_handler(service, auth=<AuthService>)`` is used every other
``/api/...`` route requires a valid session cookie (else 401), every
state-changing POST also requires a matching ``X-CSRF-Token`` header (else 403),
per-location authorization is enforced server-side (else 403), requests are
rate limited (else 429), and request bodies/paths are validated (else 400).
With ``auth=None`` the server keeps running in the original, unauthenticated
MOCK mode so the existing tests are unaffected.

Static assets (served from the project's top-level ``web/`` directory):
    GET  /                                -> web/index.html (text/html)
    GET  /app.js                          -> web/app.js    (application/javascript)
    GET  /styles.css                      -> web/styles.css (text/css)

Anything that is not ``/api/...`` is treated as a static request and is confined
to ``web/`` by a strict path-traversal guard (a path that escapes ``web/`` is
refused with 400 and is never served). The server keeps binding 127.0.0.1 only.

Error codes: 400 invalid input / bad transition / bad static path, 401
unauthenticated, 403 unauthorized / CSRF failure, 404 not found, 409 conflict
(stale version), 423 publish blocked, 429 rate limited, 405 wrong method.
"""

from __future__ import annotations

import hmac
import ipaddress as _ipaddress
import json
import os
import re
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

def _trusted_proxies() -> List[Any]:
    """Networks/addresses whose ``X-Forwarded-For`` we are willing to believe.

    Configured with ``GBP_TRUSTED_PROXIES`` (comma separated IPs or CIDRs).
    EMPTY BY DEFAULT: with nothing configured the header is ignored entirely, so
    a client can never choose its own identity -- which would otherwise let it
    dodge the login throttle, or make one attacker look like many (or many look
    like one, which is how a single attacker could lock a real admin out).
    """
    raw = os.environ.get("GBP_TRUSTED_PROXIES", "")
    nets: List[Any] = []
    for item in str(raw).split(","):
        item = item.strip()
        if not item:
            continue
        try:
            nets.append(_ipaddress.ip_network(item, strict=False))
        except ValueError:
            continue
    return nets


def _addr_in(nets: List[Any], addr: str) -> bool:
    try:
        parsed = _ipaddress.ip_address(addr)
    except ValueError:
        return False
    for net in nets:
        if isinstance(net, _ipaddress.IPv4Network) and parsed.version != 4:
            continue
        if isinstance(net, _ipaddress.IPv6Network) and parsed.version != 6:
            continue
        if parsed in net:
            return True
    return False


#: The project's top-level ``web/`` directory (this file lives at src/gbp/api/).
WEB_DIR = os.path.realpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "web")
)

#: Served file extension -> Content-Type.
STATIC_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
}

#: Name of the session cookie.
SESSION_COOKIE = "gbp_session"

#: Web security headers applied to EVERY response (Group 6C). The UI is
#: dependency-free (its own ``app.js`` / ``styles.css``, no third-party CDN), so
#: ``'self'`` is sufficient for script/style/connect -- no ``unsafe-inline`` and
#: no external origin is required.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'self'"
)

#: Non-CSP hardening headers, sent on every response (JSON and static alike).
SECURITY_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Permissions-Policy", "geolocation=(), camera=(), microphone=()"),
    ("Content-Security-Policy", CONTENT_SECURITY_POLICY),
)

#: HSTS is emitted ONLY when ``GBP_ENABLE_HSTS=1``. Strict-Transport-Security
#: tells the browser to require HTTPS for this host for the next year; on the
#: localhost demo the server speaks plain HTTP (``http://127.0.0.1``), so sending
#: it would break the demo. It is therefore opt-in and reserved for a production
#: deployment that terminates TLS in front of this process.
HSTS_HEADER = ("Strict-Transport-Security", "max-age=31536000; includeSubDomains")

#: Request-body / path limits (bytes / characters).
MAX_BODY_BYTES = 64 * 1024
MAX_TEXT_CHARS = 5000
MAX_NOTE_CHARS = 2000
_MAX_ID_LEN = 64

#: Allowed identifier shape (draft ids, location ids, filter values).
_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1," + str(_MAX_ID_LEN) + r"}$")

#: Default in-memory rate limit (requests per window per user+IP).
DEFAULT_RATE_LIMIT = 60
DEFAULT_RATE_WINDOW = 60.0

#: Reserved query params that must never authenticate a request.
_FORBIDDEN_AUTH_PARAMS = ("token", "session", "session_token", "csrf", "csrf_token")

from ..workflow import (
    ConflictError,
    NotFoundError,
    PublishBlocked,
    WorkflowError,
)
from ..authz.auth import DEFAULT_LOGIN_RATE_LIMIT
from ..sync.state import DEFAULT_ALERT_THRESHOLD
from ..reply.classify import classify
from ..workflow.states import InvalidTransition

__all__ = ["make_handler", "create_server"]

#: Server-side pagination limits for GET /api/queue.
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100


#: ROLE -> ACTION matrix, the single documented source of truth for which
#: authenticated actor may call which route. Keyed by ``(METHOD, pattern)``;
#: ``<id>`` marks a path segment. Every location-bearing route enforces the
#: mapped action via :meth:`AuthService.authorize(user, action, location_id)`
#: with the user resolved from the SESSION (never a client field). An action of
#: ``"admin"`` gates a route that has no per-location scope (so it is admin
#: only); ``"view"`` list routes additionally filter their rows to the caller's
#: permitted locations. Roles themselves map to actions in
#: :data:`gbp.authz.models.ROLE_PERMISSIONS` (admin: all; owner/reviewer:
#: view/edit/approve/reject, scoped to their own locations).
RBAC_ROUTE_MATRIX: Dict[Tuple[str, str], str] = {
    ("GET", "/api/locations"): "view",
    ("GET", "/api/locations/status"): "view",
    ("GET", "/api/locations/<id>"): "view",
    ("GET", "/api/reviews"): "view",
    ("GET", "/api/reviews/<id>"): "view",
    ("GET", "/api/queue"): "view",
    ("GET", "/api/dashboard"): "view",
    ("GET", "/api/audit"): "view",       # admin when no location scope given
    ("GET", "/api/usage"): "admin",      # global billing, no location column
    ("POST", "/api/drafts/<id>/edit"): "edit",
    ("POST", "/api/drafts/<id>/approve"): "approve",
    ("POST", "/api/drafts/<id>/reject"): "reject",
    ("POST", "/api/drafts/<id>/publish-guard"): "view",
    ("POST", "/api/drafts/bulk-approve"): "approve",
    # Identity/login routes carry no location and are not location-bearing.
    ("GET", "/api/me"): None,
    ("POST", "/api/login"): None,
    ("POST", "/api/logout"): None,
}


def _loc_clause(column: str, visible: Optional[set], has_where: bool = False) -> Tuple[str, List[Any]]:
    """Build a SQL fragment restricting ``column`` to the viewer's locations.

    ``visible is None`` means admin: no restriction (returns ``("", [])``). An
    EMPTY visible set yields a clause that matches nothing (fail closed) -- a
    scoped user with no locations must never fall back to "no filter".
    """
    if visible is None:
        return "", []
    ids = [int(v) for v in visible if str(v).lstrip("-").isdigit()]
    head = " AND " if has_where else " WHERE "
    if not ids:
        return head + f"{column} IN (NULL)", []
    placeholders = ",".join("?" for _ in ids)
    return head + f"{column} IN ({placeholders})", ids


def _scoped_dashboard_counts(repo: Any, visible: Optional[set]) -> Dict[str, int]:
    """Draft-status counts restricted to the viewer's permitted locations."""
    counts = {k: 0 for k in (
        "draft", "needs_review", "approved", "rejected",
        "ready_to_publish", "published", "failed",
    )}
    if visible is None:
        rows = repo.conn.execute(
            "SELECT status, COUNT(*) AS n FROM reply_drafts GROUP BY status"
        ).fetchall()
    else:
        ids = [int(v) for v in visible if str(v).lstrip("-").isdigit()]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            rows = repo.conn.execute(
                "SELECT d.status AS status, COUNT(*) AS n FROM reply_drafts d"
                " JOIN reviews r ON d.review_id = r.id"
                " WHERE r.location_id IN (" + placeholders + ")"
                " GROUP BY d.status", ids).fetchall()
        else:
            rows = []
    for row in rows:
        counts[str(row["status"])] = int(row["n"])
    counts["total"] = sum(v for k, v in counts.items() if k != "total")
    return counts


def _audit_row_location(repo: Any, row: Dict[str, Any]) -> Optional[Any]:
    """Resolve the owning location of an audit row, or ``None`` if unknown.

    ``audit_logs`` has no location column, so the location is derived from the
    entity it references: a ``reply_draft`` -> its review's location, a
    ``review`` -> its location, a ``business_location``/``location`` -> its own
    id. Anything else returns ``None`` (fail closed for a scoped viewer).
    """
    entity = row.get("entity")
    entity_id = row.get("entity_id")
    if entity == "reply_draft":
        r = repo.conn.execute(
            "SELECT r.location_id FROM reply_drafts d"
            " JOIN reviews r ON d.review_id = r.id WHERE d.id = ?",
            (entity_id,)).fetchone()
        return None if r is None else r["location_id"]
    if entity == "review":
        r = repo.conn.execute(
            "SELECT location_id FROM reviews WHERE id = ?", (entity_id,)).fetchone()
        return None if r is None else r["location_id"]
    if entity in ("business_location", "location"):
        return entity_id
    return None


def _clamp_page_size(value: Any) -> int:
    """Coerce ``value`` to a page size in ``[1, MAX_PAGE_SIZE]``."""
    n = _as_int(value)
    if n is None:
        return DEFAULT_PAGE_SIZE
    if n < 1:
        return 1
    return min(n, MAX_PAGE_SIZE)


def _queue_haystack(row: Dict[str, Any], business_names: Dict[Any, str]) -> str:
    """Lower-cased searchable text for one queue row (comment + business name)."""
    parts = [
        row.get("comment"),
        row.get("location_name"),
        row.get("title"),
        business_names.get(row.get("location_id")),
    ]
    return " ".join(str(p) for p in parts if p).lower()


def _locations_status(repo: Any) -> List[Dict[str, Any]]:
    """Per-location sync state + review/draft counters (read-only)."""
    conn = repo.conn
    business_names = {
        row["location_id"]: row["business_name"]
        for row in conn.execute(
            "SELECT location_id, business_name FROM brand_profiles"
        ).fetchall()
    }
    locations = conn.execute(
        "SELECT * FROM business_locations ORDER BY id").fetchall()

    # --- bulk aggregates (Group 8B / T27) ------------------------------ #
    # This used to run ~5 queries PER LOCATION (500 locations -> ~2500 queries
    # per request, ~70 ms). Everything is derived in a handful of grouped
    # queries now. Ledger keys are accepted as either the location RESOURCE NAME
    # (what ReviewSync writes) or the numeric row id (what older code wrote), so
    # a queue-driven sync is visible here as well.
    by_key: Dict[str, Any] = {}
    for loc in locations:
        by_key[str(loc["id"])] = loc["id"]
        by_key[str(loc["location_name"])] = loc["id"]

    latest_run: Dict[Any, Any] = {}
    streak: Dict[Any, int] = {}
    streak_closed: set = set()
    for row in conn.execute("SELECT * FROM sync_run_log ORDER BY id DESC").fetchall():
        lid = by_key.get(str(row["location_id"]))
        if lid is None:
            continue
        if lid not in latest_run:
            latest_run[lid] = row
        if lid not in streak_closed:
            if str(row["status"]) == "failed":
                streak[lid] = streak.get(lid, 0) + 1
            else:
                streak_closed.add(lid)

    def _grouped(sql: str) -> Dict[Any, int]:
        return {r["location_id"]: int(r["n"]) for r in
                conn.execute(sql).fetchall()}

    unanswered_by: Dict[Any, int] = _grouped(
        "SELECT location_id, COUNT(*) AS n FROM reviews"
        " WHERE reply_text IS NULL OR TRIM(reply_text) = ''"
        " GROUP BY location_id")
    pending_by: Dict[Any, int] = _grouped(
        "SELECT r.location_id AS location_id, COUNT(*) AS n FROM reply_drafts d"
        " JOIN reviews r ON d.review_id = r.id"
        " WHERE d.status IN ('draft', 'needs_review') GROUP BY r.location_id")
    approved_by: Dict[Any, int] = _grouped(
        "SELECT r.location_id AS location_id, COUNT(*) AS n FROM reply_drafts d"
        " JOIN reviews r ON d.review_id = r.id"
        " WHERE d.status IN ('approved', 'ready_to_publish')"
        " GROUP BY r.location_id")

    out: List[Dict[str, Any]] = []
    for loc in locations:
        lid = loc["id"]
        run = latest_run.get(lid)
        failures = streak.get(lid, 0)
        last_status = str(run["status"]) if run is not None else None
        last_at = None
        if run is not None:
            last_at = run["ended_at"] or run["started_at"]
        last_error = None
        if last_status == "failed":
            last_error = f"sync_failed (errors={int(run['errors'] or 0)})"
        unanswered = unanswered_by.get(lid, 0)
        pending = pending_by.get(lid, 0)
        approved = approved_by.get(lid, 0)
        out.append({
            "location_id": lid,
            "location_name": loc["location_name"],
            "business_name": business_names.get(lid) or loc["title"] or loc["location_name"],
            "city": loc["city"],
            "state": loc["state"],
            "postal_code": loc["postal_code"],
            "language": loc["language"],
            "last_sync_at": last_at,
            "last_sync_status": last_status,
            "last_error": last_error,
            "consecutive_failures": failures,
            "needs_alert": failures >= DEFAULT_ALERT_THRESHOLD,
            "unanswered_reviews": int(unanswered),
            "drafts_pending_review": int(pending),
            "drafts_approved": int(approved),
        })
    return out


def _usage_stats(repo: Any) -> Dict[str, Any]:
    """Aggregate AI usage/cost from ``api_usage`` (read-only)."""
    conn = repo.conn
    row = conn.execute(
        "SELECT COALESCE(SUM(calls), 0) AS calls,"
        " COALESCE(SUM(input_tokens), 0) AS it,"
        " COALESCE(SUM(output_tokens), 0) AS ot,"
        " COALESCE(SUM(est_cost_usd), 0) AS cost FROM api_usage"
    ).fetchone()
    by_day: List[Dict[str, Any]] = []
    for r in conn.execute(
        "SELECT date(ts) AS day, COALESCE(SUM(calls), 0) AS c,"
        " COALESCE(SUM(input_tokens), 0) AS it,"
        " COALESCE(SUM(output_tokens), 0) AS ot,"
        " COALESCE(SUM(est_cost_usd), 0) AS cost"
        " FROM api_usage GROUP BY date(ts) ORDER BY day"
    ).fetchall():
        by_day.append({
            "date": r["day"],
            "calls": int(r["c"]),
            "input_tokens": int(r["it"]),
            "output_tokens": int(r["ot"]),
            "estimated_cost_usd": round(float(r["cost"]), 6),
        })
    return {
        "total_calls": int(row["calls"]),
        "input_tokens": int(row["it"]),
        "output_tokens": int(row["ot"]),
        "estimated_cost_usd": round(float(row["cost"]), 6),
        "by_day": by_day,
        # api_usage has no location column, so a per-location split is not
        # derivable from the current schema -> reported as empty.
        "per_location": [],
    }


#: Query params that switch ``GET /api/locations`` from its legacy list shape
#: to the paginated envelope. A bare call (no params) keeps returning the
#: historical list so older clients -- and the legacy tests -- are unaffected.
_LOC_PAGE_PARAMS = ("page", "page_size", "q", "state", "sync_status", "sort", "order")


def _google_connected() -> bool:
    """Whether a real Google connection is configured.

    Offline/mock builds have no OAuth token and no sync client wired in, so the
    honest answer is ``False``. An operator who really has connected Google can
    assert it with ``GBP_GOOGLE_CONNECTED=1``. There is deliberately NO
    fallback that infers "connected" from the API merely responding -- an
    unconnected app must never look healthy.
    """
    return os.environ.get("GBP_GOOGLE_CONNECTED") == "1"


def _dashboard_metrics(repo: Any, visible: Optional[set] = None) -> Dict[str, Any]:
    """Overview metrics, read-only, derived ONLY from the local tables.

    Every value is a plain SQL aggregate over the (mock) SQLite DB -- no AI
    call and no Google sync is triggered by computing it. ``visible`` scopes
    every aggregate to the viewer's permitted locations (``None`` = admin: all
    locations). ``estimated_ai_cost_usd`` comes from ``api_usage`` only and is
    reported as 0.0 for a scoped viewer (the table has no location column, so a
    cross-location billing total must never leak to a non-admin).
    """
    conn = repo.conn

    loc_f, loc_p = _loc_clause("id", visible)
    total_locations = int(conn.execute(
        "SELECT COUNT(*) AS n FROM business_locations" + loc_f, loc_p).fetchone()["n"])
    rev_f, rev_p = _loc_clause("location_id", visible)
    total_reviews = int(conn.execute(
        "SELECT COUNT(*) AS n FROM reviews" + rev_f, rev_p).fetchone()["n"])
    un_f, un_p = _loc_clause("location_id", visible, has_where=True)
    unanswered = int(conn.execute(
        "SELECT COUNT(*) AS n FROM reviews"
        " WHERE (reply_text IS NULL OR TRIM(reply_text) = '')" + un_f, un_p
    ).fetchone()["n"])

    draft_ids = None
    if visible is not None:
        draft_ids = [int(v) for v in visible if str(v).lstrip("-").isdigit()]

    def _draft_count(where: str) -> int:
        if draft_ids is None:
            return int(conn.execute(
                "SELECT COUNT(*) AS n FROM reply_drafts WHERE " + where).fetchone()["n"])
        if not draft_ids:
            return 0
        placeholders = ",".join("?" for _ in draft_ids)
        return int(conn.execute(
            "SELECT COUNT(*) AS n FROM reply_drafts WHERE " + where +
            " AND review_id IN (SELECT id FROM reviews WHERE location_id IN ("
            + placeholders + "))", draft_ids).fetchone()["n"])

    needs_approval = _draft_count("status IN ('draft', 'needs_review')")
    approved = _draft_count("status IN ('approved', 'ready_to_publish')")
    published = _draft_count("status = 'published'")

    # A location counts as a sync error when its MOST RECENT ledger row failed.
    sync_errors = 0
    for loc in conn.execute(
            "SELECT id FROM business_locations" + loc_f, loc_p).fetchall():
        run = conn.execute(
            "SELECT status FROM sync_run_log WHERE location_id = ?"
            " ORDER BY id DESC LIMIT 1", (str(loc["id"]),)).fetchone()
        if run is not None and str(run["status"]) == "failed":
            sync_errors += 1

    cost = 0.0
    if visible is None:
        cost = float(conn.execute(
            "SELECT COALESCE(SUM(est_cost_usd), 0) AS c FROM api_usage").fetchone()["c"])

    dist = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    for row in conn.execute(
            "SELECT rating, COUNT(*) AS n FROM reviews" + rev_f + " GROUP BY rating",
            rev_p).fetchall():
        try:
            star = int(row["rating"])
        except (TypeError, ValueError):
            continue
        if star in dist:
            dist[star] = int(row["n"])
    rating_distribution = [{"stars": s, "count": dist[s]} for s in (1, 2, 3, 4, 5)]

    # Group by the review create/update timestamp. Rows with no timestamp are
    # not counted and an empty result is reported as "not tracked" -- the API
    # never fabricates a series.
    tr_f, tr_p = _loc_clause("location_id", visible, has_where=True)
    trend_rows = conn.execute(
        "SELECT date(COALESCE(create_time, update_time)) AS d, COUNT(*) AS n"
        " FROM reviews WHERE COALESCE(create_time, update_time) IS NOT NULL"
        + tr_f + " GROUP BY d ORDER BY d", tr_p).fetchall()
    review_trend = [
        {"date": r["d"], "count": int(r["n"])} for r in trend_rows if r["d"]
    ]

    recent: List[Dict[str, Any]] = []
    rc_f, rc_p = _loc_clause("r.location_id", visible)
    for row in conn.execute(
            "SELECT r.id, r.rating, r.comment, r.status, r.create_time, r.update_time,"
            " l.postal_code,"
            " COALESCE(b.business_name, l.title, l.location_name) AS business_name"
            " FROM reviews r"
            " LEFT JOIN business_locations l ON r.location_id = l.id"
            " LEFT JOIN brand_profiles b ON b.location_id = r.location_id"
            + rc_f +
            " ORDER BY COALESCE(r.create_time, r.update_time) DESC, r.id DESC"
            " LIMIT 10", rc_p).fetchall():
        comment = str(row["comment"] or "")
        recent.append({
            "review_id": row["id"],
            "business_name": row["business_name"],
            "postal_code": row["postal_code"],
            "stars": row["rating"],
            "date": row["create_time"] or row["update_time"],
            "status": row["status"],
            "comment": comment[:160] + ("\u2026" if len(comment) > 160 else ""),
        })

    attention = [
        l for l in _locations_status(repo)
        if (visible is None or str(l.get("location_id")) in visible)
        and (l.get("needs_alert") or int(l.get("unanswered_reviews") or 0) > 0)
    ]
    # Worst first: alerts, then most unanswered, then most consecutive failures.
    attention.sort(key=lambda l: (
        0 if l.get("needs_alert") else 1,
        -int(l.get("unanswered_reviews") or 0),
        -int(l.get("consecutive_failures") or 0),
    ))

    return {
        "kpis": {
            "total_locations": total_locations,
            "total_reviews": total_reviews,
            "unanswered_reviews": unanswered,
            "needs_approval": needs_approval,
            "approved_replies": approved,
            "published_replies": published,
            "sync_errors": sync_errors,
            "estimated_ai_cost_usd": round(cost, 6),
        },
        "rating_distribution": rating_distribution,
        "rating_distribution_available": total_reviews > 0,
        "review_trend": review_trend,
        "review_trend_available": bool(review_trend),
        "attention_locations": attention[:10],
        "recent_reviews": recent,
    }


def _location_rows(repo: Any, service: Any) -> List[Dict[str, Any]]:
    """Every location with its aggregates, derived from local tables only.

    Keeps the keys ``/api/locations`` returned before (``id``, ``title``,
    ``brand_complete``, ...) and ADDS the V2 keys the new table needs.
    """
    conn = repo.conn
    total_reviews = {
        r["location_id"]: int(r["n"])
        for r in conn.execute(
            "SELECT location_id, COUNT(*) AS n FROM reviews GROUP BY location_id"
        ).fetchall()
    }
    unanswered = {
        r["location_id"]: int(r["n"])
        for r in conn.execute(
            "SELECT location_id, COUNT(*) AS n FROM reviews"
            " WHERE reply_text IS NULL OR TRIM(reply_text) = ''"
            " GROUP BY location_id"
        ).fetchall()
    }
    pending = {
        r["location_id"]: int(r["n"])
        for r in conn.execute(
            "SELECT r.location_id AS location_id, COUNT(*) AS n FROM reply_drafts d"
            " JOIN reviews r ON d.review_id = r.id"
            " WHERE d.status IN ('draft', 'needs_review') GROUP BY r.location_id"
        ).fetchall()
    }
    brands = {
        r["location_id"]: r
        for r in conn.execute(
            "SELECT location_id, business_name, auto_publish_allowed"
            " FROM brand_profiles").fetchall()
    }
    status = {s["location_id"]: s for s in _locations_status(repo)}

    out: List[Dict[str, Any]] = []
    for loc in service.list_locations():
        lid = loc["id"]
        st = status.get(lid, {})
        brand = brands.get(lid)
        auto = bool(brand["auto_publish_allowed"]) if brand is not None else bool(
            loc.get("auto_publish_allowed"))
        brand_name = brand["business_name"] if brand is not None else None
        item = dict(loc)  # keep every previously returned key
        item.update({
            "business_name": brand_name or loc.get("title") or loc.get("location_name"),
            "postal_code": st.get("postal_code"),
            "total_reviews": total_reviews.get(lid, 0),
            "unanswered": unanswered.get(lid, 0),
            "needs_approval": pending.get(lid, 0),
            "sync_status": st.get("last_sync_status"),
            "last_sync": st.get("last_sync_at"),
            "last_error": st.get("last_error"),
            "needs_alert": bool(st.get("needs_alert", False)),
            "brand_profile_configured": bool(brand is not None
                                             or loc.get("brand_complete")),
            "auto_publish_policy": "auto" if auto else "manual",
        })
        out.append(item)
    return out


def _brand_profile_summary(repo: Any, location_id: Any) -> Dict[str, Any]:
    """Read-only summary of the brand profile for one location."""
    row = repo.conn.execute(
        "SELECT business_name, city, language, tone, services_json,"
        " seo_keywords_json, auto_publish_allowed, updated_at"
        " FROM brand_profiles WHERE location_id = ?", (location_id,)).fetchone()
    if row is None:
        return {"configured": False}
    return {
        "configured": True,
        "business_name": row["business_name"],
        "city": row["city"],
        "language": row["language"],
        "tone": row["tone"],
        "auto_publish_allowed": bool(row["auto_publish_allowed"]),
        "updated_at": row["updated_at"],
    }


def _locations_page(rows: List[Dict[str, Any]], query: Dict[str, Any]) -> Dict[str, Any]:
    """Filter + sort + paginate location rows server-side."""
    q = (_first(query, "q") or "").strip().lower()
    if q:
        rows = [r for r in rows if q in (
            str(r.get("business_name") or "") + " "
            + str(r.get("postal_code") or "")
        ).lower()]
    state = _first(query, "state")
    if state:
        rows = [r for r in rows
                if str(r.get("state") or "").lower() == state.lower()]
    sync_status = _first(query, "sync_status")
    if sync_status:
        rows = [r for r in rows if str(r.get("sync_status") or "") == sync_status]

    sort = (_first(query, "sort") or "business_name").lower()
    order = (_first(query, "order") or "asc").lower()
    if sort in ("business_name", "city", "state", "postal_code"):
        key = lambda r: str(r.get(sort) or "").lower()  # noqa: E731
    elif sort in ("total_reviews", "unanswered"):
        key = lambda r: int(r.get(sort) or 0)  # noqa: E731
    elif sort == "last_sync":
        key = lambda r: str(r.get("last_sync") or "")  # noqa: E731
    else:
        sort = "business_name"
        key = lambda r: str(r.get("business_name") or "").lower()  # noqa: E731
    rows = sorted(rows, key=key, reverse=(order == "desc"))

    total = len(rows)
    page_size = _clamp_page_size(_first(query, "page_size"))
    page = _as_int(_first(query, "page"))
    if page is None or page < 1:
        page = 1
    start = (page - 1) * page_size
    pages = (total + page_size - 1) // page_size if total else 0
    return {
        "items": rows[start:start + page_size],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
        "sort": sort,
        "order": order,
    }


#: Sort keys accepted by ``GET /api/reviews``.
REVIEW_SORT_KEYS = ("review_date", "stars", "business_name", "status")


def _review_inbox_rows(repo: Any) -> List[Dict[str, Any]]:
    """Every review as an inbox row (joined to its location + latest draft).

    Read-only: a plain SQL join, no AI call. ``business_name`` is the
    human-readable display title (brand profile -> location title -> location
    name); ``location_resource_id`` is the raw resource name and is a TECHNICAL
    field only -- the UI never shows it as a title. ``draft_text`` is the REAL
    stored draft text of the newest draft (or ``None``) so the UI can show the
    "Reply not generated" state instead of a placeholder.
    """
    rows = repo.conn.execute(
        "SELECT r.id AS review_id, r.location_id, r.rating, r.rating AS stars,"
        " r.comment, r.reviewer_name, r.status AS review_status,"
        " COALESCE(r.create_time, r.update_time) AS review_date,"
        " l.language, l.location_name AS location_resource_id, l.title,"
        " l.postal_code,"
        " COALESCE(b.business_name, l.title, l.location_name) AS business_name,"
        " d.id AS draft_id, d.draft_text, d.status AS draft_status,"
        " d.version AS draft_version, d.updated_at AS draft_updated_at"
        " FROM reviews r"
        " LEFT JOIN business_locations l ON r.location_id = l.id"
        " LEFT JOIN brand_profiles b ON b.location_id = r.location_id"
        " LEFT JOIN reply_drafts d ON d.id = ("
        "   SELECT id FROM reply_drafts WHERE review_id = r.id"
        "   ORDER BY id DESC LIMIT 1)"
        " ORDER BY r.id"
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for row in rows:
        item: Dict[str, Any] = dict(row)
        c = classify(row)
        stars = item.get("stars")
        draft_text = item.get("draft_text")
        if draft_text is not None and not str(draft_text).strip():
            draft_text = None
        item["draft_text"] = draft_text
        item["has_draft"] = item.get("draft_id") is not None
        # Effective workflow status of the review's newest draft; a review with
        # no draft is "no_draft" (never shown as a draft state).
        item["status"] = item.get("draft_status") or "no_draft"
        item["is_sensitive"] = c.is_sensitive
        item["requires_manual_approval"] = bool(
            (stars in (1, 2)) or c.is_sensitive)
        # Human-readable reason (low stars / rating-only / sensitive signals).
        item["validator_reason"] = c.reason or None
        out.append(item)
    return out


def _reviews_page(rows: List[Dict[str, Any]], query: Dict[str, Any]) -> Dict[str, Any]:
    """Filter + sort + paginate review-inbox rows server-side."""
    location_id = _first(query, "location_id")
    if location_id:
        rows = [r for r in rows
                if str(r.get("location_id")) == str(location_id)
                or str(r.get("location_resource_id")) == location_id]
    stars = _as_int(_first(query, "stars"))
    if stars is not None:
        rows = [r for r in rows if _as_int(r.get("stars")) == stars]
    status = _first(query, "status")
    if status:
        rows = [r for r in rows if str(r.get("status")) == status]
    language = _first(query, "language")
    if language:
        rows = [r for r in rows if str(r.get("language") or "") == language]
    since = _first(query, "since")
    if since:
        rows = [r for r in rows if str(r.get("review_date") or "") >= since]
    term = (_first(query, "q") or "").strip().lower()
    if term:
        rows = [r for r in rows if term in " ".join(
            str(r.get(k) or "") for k in
            ("comment", "business_name", "reviewer_name", "postal_code")).lower()]
    # Increment 1 (Issue #71): the unanswered view the salon switcher needs. A review counts as
    # answered only when its newest reply is approved or sent; a reply still in draft - or edited
    # back to draft - means the salon still owes an answer. Reuses the row fields the join already
    # provides, so no extra query is introduced.
    _raw_unanswered = (query or {}).get("unanswered")
    if isinstance(_raw_unanswered, (list, tuple)):
        _raw_unanswered = _raw_unanswered[0] if _raw_unanswered else ""
    unanswered = str(_raw_unanswered or "").strip().lower() in ("1", "true", "yes", "on")
    if unanswered:
        rows = [r for r in rows if str(r.get("status")) not in ("approved", "sent")]

    sort = (_first(query, "sort") or "review_date").lower()
    order = (_first(query, "order") or "desc").lower()
    if sort == "stars":
        key = lambda r: _as_int(r.get("stars")) or 0  # noqa: E731
    elif sort == "business_name":
        key = lambda r: str(r.get("business_name") or "").lower()  # noqa: E731
    elif sort == "status":
        key = lambda r: str(r.get("status") or "").lower()  # noqa: E731
    else:
        sort = "review_date"
        key = lambda r: str(r.get("review_date") or "")  # noqa: E731
    rows = sorted(rows, key=key, reverse=(order != "asc"))

    total = len(rows)
    page_size = _clamp_page_size(_first(query, "page_size"))
    page = _as_int(_first(query, "page"))
    if page is None or page < 1:
        page = 1
    start = (page - 1) * page_size
    pages = (total + page_size - 1) // page_size if total else 0
    return {
        "items": rows[start:start + page_size],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
        "sort": sort,
        "order": order,
    }


def _review_detail(repo: Any, review_id: int) -> Optional[Dict[str, Any]]:
    """One review + its draft row(s) + related audit entries (recent first)."""
    item = next((r for r in _review_inbox_rows(repo)
                 if r.get("review_id") == review_id), None)
    if item is None:
        return None
    drafts = repo.conn.execute(
        "SELECT id, review_id, draft_text, status, version, created_at, updated_at"
        " FROM reply_drafts WHERE review_id = ? ORDER BY id", (review_id,)
    ).fetchall()
    audit: List[Dict[str, Any]] = []
    draft_ids = [str(d["id"]) for d in drafts]
    if draft_ids:
        placeholders = ",".join("?" for _ in draft_ids)
        for row in repo.conn.execute(
            "SELECT * FROM audit_logs WHERE entity = 'reply_draft'"
            " AND entity_id IN (" + placeholders + ") ORDER BY id DESC",
            draft_ids,
        ).fetchall():
            audit.append(dict(row))
    detail = dict(item)
    detail["drafts"] = [dict(d) for d in drafts]
    detail["audit"] = audit
    return detail


def _err(code: int, error: str, **extra: Any) -> Tuple[int, Dict[str, Any]]:
    payload: Dict[str, Any] = {"error": error}
    payload.update(extra)
    return code, payload


def _as_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _valid_id(value: Any) -> bool:
    return bool(_ID_RE.match(str(value)))


def _reject_unknown(body: Dict[str, Any], allowed: Any) -> None:
    extra = sorted(set(body) - set(allowed))
    if extra:
        raise ValueError(f"unknown field(s): {', '.join(extra)}")


def make_handler(
    service: Any,
    auth: Any = None,
    rate_limit: Optional[int] = None,
    rate_window: float = DEFAULT_RATE_WINDOW,
    login_limit: Optional[int] = None,
):
    """Build a ``BaseHTTPRequestHandler`` subclass bound to ``service``.

    ``auth`` -- an optional :class:`gbp.authz.auth.AuthService`. When omitted
    the handler runs in the legacy unauthenticated MOCK mode.

    Rate limiting is delegated to the auth service's PERSISTENT
    :class:`gbp.authz.store.RateLimitStore`, so the throttle survives a restart
    and is shared across worker processes (the two buckets are ``route:`` for
    normal API calls and ``login:`` for ``/api/login``).
    """
    use_auth = auth is not None
    limit = rate_limit if rate_limit is not None else DEFAULT_RATE_LIMIT
    login_limit = login_limit if login_limit is not None else DEFAULT_LOGIN_RATE_LIMIT
    window_seconds = max(1, int(round(rate_window)))
    # Read the HSTS opt-in once, at handler-construction time, so a running
    # server's header set is deterministic (env changes don't leak mid-flight).
    hsts_enabled = os.environ.get("GBP_ENABLE_HSTS") == "1"
    # Session cookies get ``Secure`` ONLY when an operator asserts this process
    # is reachable over TLS (a TLS-terminating proxy in front, or HTTPS). The
    # localhost demo speaks plain HTTP (``http://127.0.0.1``), so a ``Secure``
    # cookie would be dropped by the browser and break login -- hence opt-in
    # (``GBP_COOKIE_SECURE=1``), read once so a running server is deterministic.
    secure_cookies = os.environ.get("GBP_COOKIE_SECURE") == "1"

    class Handler(BaseHTTPRequestHandler):
        server_version = "GBPApprovalAPI/1.0"

        # ---------------------------------------------------------------- #
        def log_message(self, *args: Any) -> None:  # noqa: D401 - silence
            """Silence the default stderr access log."""

        def _security_headers(self) -> List[Tuple[str, str]]:
            """Baseline security headers for every response (+ HSTS if opted in)."""
            headers = list(SECURITY_HEADERS)
            if hsts_enabled:
                headers.append(HSTS_HEADER)
            return headers

        # ---------------------------------------------------------------- #
        def _send(self, code: int, payload: Any, headers: Optional[List[Tuple[str, str]]] = None) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            for name, value in self._security_headers():
                self.send_header(name, value)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            # API responses are per-user/per-request data: never cache them.
            self.send_header("Cache-Control", "no-store")
            for name, value in headers or []:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _send_bytes(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            for name, value in self._security_headers():
                self.send_header(name, value)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _serve_static(self, raw_path: str) -> None:
            """Serve a file from ``WEB_DIR``, refusing any escape (traversal)."""
            rel = unquote(raw_path or "/")
            if "\x00" in rel:
                self._send(*_err(400, "bad_path", detail="invalid path"))
                return
            # Normalize into path segments and reject any traversal segment.
            segments = [seg for seg in rel.split("/") if seg not in ("", ".")]
            if any(seg == ".." for seg in segments):
                self._send(*_err(400, "bad_path", detail="path traversal refused"))
                return
            if not segments:
                segments = ["index.html"]
            candidate = os.path.realpath(os.path.join(WEB_DIR, *segments))
            # Confine to WEB_DIR (guards against symlinks/surprises too).
            if candidate != WEB_DIR and not candidate.startswith(WEB_DIR + os.sep):
                self._send(*_err(400, "bad_path", detail="path escapes web/"))
                return
            if not os.path.isfile(candidate):
                self._send(*_err(404, "not_found", detail=f"no static file {rel!r}"))
                return
            ext = os.path.splitext(candidate)[1].lower()
            ctype = STATIC_CONTENT_TYPES.get(ext, "application/octet-stream")
            with open(candidate, "rb") as fh:
                body = fh.read()
            self._send_bytes(200, body, ctype)

        def _read_json(self) -> Dict[str, Any]:
            length = _as_int(self.headers.get("Content-Length")) or 0
            if length < 0 or length > MAX_BODY_BYTES:
                raise ValueError("request body too large")
            raw = self.rfile.read(length) if length > 0 else b""
            if not raw:
                return {}
            try:
                data = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise ValueError("invalid JSON body")
            if not isinstance(data, dict):
                raise ValueError("JSON body must be an object")
            return data

        def _dispatch_error(self, exc: Exception) -> None:
            if isinstance(exc, NotFoundError):
                self._send(*_err(404, "not_found", detail=str(exc)))
            elif isinstance(exc, ConflictError):
                self._send(*_err(409, "conflict", detail=str(exc)))
            elif isinstance(exc, PublishBlocked):
                self._send(*_err(423, "publish_blocked", reason=exc.reason))
            elif isinstance(exc, (InvalidTransition, ValueError)):
                self._send(*_err(400, "invalid_request", detail=str(exc)))
            elif isinstance(exc, WorkflowError):
                self._send(*_err(400, "workflow_error", detail=str(exc)))
            else:  # pragma: no cover - unexpected
                self._send(*_err(500, "internal_error", detail=str(exc)))

        # ---------------------------------------------------------------- #
        # auth helpers
        # ---------------------------------------------------------------- #
        def _client_ip(self) -> str:
            """Peer address, or the client hop when the peer is a trusted proxy.

            ``X-Forwarded-For`` is honoured ONLY when the immediate peer is
            listed in ``GBP_TRUSTED_PROXIES``. Behind a reverse proxy that is
            not configured, every user shares one address -- so the per-source
            login lock would degenerate into an account-wide lock and one
            attacker could deny service to the real admin. Configuring the
            proxy fixes that; leaving it unset keeps the safe default (never
            trust a client-supplied header).
            """
            try:
                peer = str(self.client_address[0])
            except Exception:  # pragma: no cover
                return "unknown"
            if _addr_in(_trusted_proxies(), peer):
                raw = self.headers.get("X-Forwarded-For") or ""
                first = raw.split(",")[0].strip()
                if first:
                    return first
            return peer

        def _cookie_token(self) -> Optional[str]:
            """Read the session token ONLY from the cookie (never the query)."""
            raw = self.headers.get("Cookie")
            if not raw:
                return None
            try:
                jar = SimpleCookie()
                jar.load(raw)
            except Exception:  # pragma: no cover - malformed cookie
                return None
            morsel = jar.get(SESSION_COOKIE)
            return morsel.value if morsel is not None else None

        def _cookie_attrs(self, morsel: Any, max_age: Optional[str]) -> None:
            """Apply the shared session-cookie attributes to ``morsel``.

            ``HttpOnly`` + ``SameSite=Lax`` + ``Path=/`` always; ``SameSite=Lax``
            (not ``Strict``) so a normal top-level navigation to the app still
            carries the session, which is what a login-then-redirect flow needs.
            ``Secure`` is added only when ``GBP_COOKIE_SECURE=1``.
            """
            morsel["path"] = "/"
            morsel["httponly"] = True
            morsel["samesite"] = "Lax"
            if secure_cookies:
                morsel["secure"] = True
            if max_age is not None:
                morsel["max-age"] = max_age

        def _session_cookie(self, token: str, *, ttl: int) -> Tuple[str, str]:
            jar = SimpleCookie()
            jar[SESSION_COOKIE] = token
            self._cookie_attrs(jar[SESSION_COOKIE], str(int(ttl)))
            return ("Set-Cookie", jar.output(header="").strip())

        def _clear_cookie(self) -> Tuple[str, str]:
            jar = SimpleCookie()
            jar[SESSION_COOKIE] = ""
            self._cookie_attrs(jar[SESSION_COOKIE], "0")
            return ("Set-Cookie", jar.output(header="").strip())

        def _rate_key(self, user: Any) -> str:
            who = user.username if user is not None else "anon"
            return f"route:{who}|{self._client_ip()}"

        def _rate_check(self, bucket: str, limit_value: int) -> int:
            """Persistent fixed-window check; returns retry_after (0 = allowed).

            Uses the auth service's DB-backed :class:`RateLimitStore` so a
            restart or a second worker sees the same counters.
            """
            allowed, _remaining, retry_after = auth.hit_rate_limit(
                bucket, window_seconds, limit_value
            )
            return 0 if allowed else max(1, int(retry_after))

        def _reject_query_auth(self, query: Dict[str, Any]) -> bool:
            for param in _FORBIDDEN_AUTH_PARAMS:
                if param in query:
                    self._send(*_err(
                        400, "invalid_request",
                        detail=f"session/CSRF material must not be passed as a query param: {param}",
                    ))
                    return True
            return False

        def _guard(self, query: Dict[str, Any]) -> Tuple[Optional[Any], Optional[str]]:
            """Rate limit + require a valid (non-revoked) session.

            Returns ``(user, token)``; on any rejection it sends the response
            and returns ``(None, "\\x00rejected")``.
            """
            if self._reject_query_auth(query):
                return None, "\x00rejected"
            token = self._cookie_token()
            user = auth.validate(token) if token else None
            retry = self._rate_check(self._rate_key(user), limit)
            if retry:
                self._send(
                    *_err(429, "rate_limited", detail="too many requests"),
                    headers=[("Retry-After", str(retry))],
                )
                return None, "\x00rejected"
            if user is None:
                # Covers missing/expired/revoked/tampered tokens alike.
                self._send(*_err(401, "unauthenticated", detail="valid session required"))
                return None, "\x00rejected"
            if not getattr(user, "active", True):
                self._send(*_err(403, "inactive", detail="account is inactive"))
                return None, "\x00rejected"
            return user, token

        # ---------------------------------------------------------------- #
        def do_GET(self) -> None:  # noqa: N802
            try:
                parsed = urlparse(self.path)
                raw_path = parsed.path or "/"
                if not raw_path.startswith("/api/"):
                    # Everything that is not the JSON API is a static asset.
                    return self._serve_static(raw_path)
                path = raw_path.rstrip("/") or "/"
                query = parse_qs(parsed.query)
                if use_auth:
                    user, token = self._guard(query)
                    if token == "\x00rejected":
                        return
                    return self._send(*self._route_get(path, query, user, token))
                self._send(*self._route_get(path, query, None, None))
            except Exception as exc:  # noqa: BLE001
                self._dispatch_error(exc)

        def do_POST(self) -> None:  # noqa: N802
            try:
                parsed = urlparse(self.path)
                path = parsed.path.rstrip("/") or "/"
                query = parse_qs(parsed.query)
                body = self._read_json()

                if use_auth and path == "/api/login":
                    return self._handle_login(body)

                if use_auth:
                    user, token = self._guard(query)
                    if token == "\x00rejected":
                        return
                    # CSRF: every state-changing POST needs a matching header.
                    supplied = self.headers.get("X-CSRF-Token") or ""
                    expected = auth.csrf_token(str(token))
                    if not supplied or not hmac.compare_digest(supplied, expected):
                        return self._send(*_err(403, "csrf_failed", detail="invalid CSRF token"))
                    if path == "/api/logout":
                        # Actually revoke the persisted session, not just the
                        # cookie: the token can no longer be replayed.
                        auth.logout(token)
                        return self._send(
                            200, {"ok": True}, headers=[self._clear_cookie()]
                        )
                    return self._send(*self._route_post(path, query, body, user))

                self._send(*self._route_post(path, query, body, None))
            except Exception as exc:  # noqa: BLE001
                self._dispatch_error(exc)

        def _handle_login(self, body: Dict[str, Any]) -> None:
            try:
                _reject_unknown(body, ("username", "password"))
                username = body.get("username")
                password = body.get("password")
                if not isinstance(username, str) or not isinstance(password, str):
                    raise ValueError("username and password must be strings")
                if not _valid_id(username) or len(password) > 1024:
                    raise ValueError("invalid credentials payload")
            except ValueError as exc:
                return self._send(*_err(400, "invalid_request", detail=str(exc)))
            # Login has its own persistent bucket so a throttled client cannot
            # brute-force credentials, while normal API traffic stays separate.
            retry = self._rate_check(
                f"login:{username}|{self._client_ip()}", login_limit
            )
            if retry:
                return self._send(
                    *_err(429, "rate_limited", detail="too many login attempts"),
                    headers=[("Retry-After", str(retry))],
                )
            result = auth.login_with_status(
                username, password,
                ip=self._client_ip(),
                user_agent=self.headers.get("User-Agent"),
            )
            # T25: a temporarily locked source/account reports 429 with
            # Retry-After. The payload stays generic -- it never reveals
            # whether the username exists.
            if result.get("locked"):
                return self._send(
                    *_err(429, "rate_limited",
                          detail="login temporarily throttled"),
                    headers=[("Retry-After",
                              str(max(1, int(result.get("retry_after") or 1))))],
                )
            token = result.get("token")
            if not token:
                # Generic message: do not reveal whether the user exists.
                return self._send(*_err(401, "invalid_credentials"))
            user = auth.validate(token)
            if user is None:
                return self._send(*_err(401, "invalid_credentials"))
            if not getattr(user, "active", True):
                # Deactivated accounts cannot obtain a usable session.
                auth.logout(token)
                return self._send(
                    *_err(403, "inactive", detail="account is inactive"))
            payload = {
                "ok": True,
                "username": username,
                "roles": list(getattr(user, "roles", []) or []),
                "locations": list(getattr(user, "locations", []) or []),
                "csrf_token": auth.csrf_token(token),
                "expires_at": auth.expires_at(token),
            }
            self._send(200, payload, headers=[self._session_cookie(token, ttl=auth.ttl)])

        # ---------------------------------------------------------------- #
        # authorization helpers
        # ---------------------------------------------------------------- #
        def _visible(self, user: Any) -> Optional[set]:
            """``None`` = every location (admin); otherwise a str id set."""
            if user is None or "admin" in user.permitted_actions():
                return None
            return user.location_set()

        def _draft_location(self, draft_id: Any) -> Optional[Any]:
            row = service.repo.conn.execute(
                "SELECT r.location_id FROM reply_drafts d"
                " JOIN reviews r ON d.review_id = r.id WHERE d.id = ?",
                (draft_id,),
            ).fetchone()
            return None if row is None else row["location_id"]

        # ---------------------------------------------------------------- #
        def _route_get(
            self, path: str, query: Dict[str, Any], user: Any = None,
            token: Optional[str] = None,
        ) -> Tuple[int, Any]:
            if use_auth:
                for key in ("location_id", "status", "language", "since",
                            "entity", "entity_id", "actor"):
                    value = _first(query, key)
                    if value is not None and not _valid_id(value):
                        return _err(400, "invalid_request", detail=f"invalid {key}")

            if path == "/api/me":
                # The logged-in actor, so the UI can show who is acting instead
                # of a free-text box. NEVER returns the session token or the
                # password hash: only identity + the per-session CSRF token.
                if not use_auth:
                    # Mock mode: no authentication, no actor identity.
                    return 200, {
                        "authenticated": False,
                        "mock_mode": True,
                        "username": None,
                        "roles": [],
                        "locations": [],
                        "permissions": [],
                        "is_admin": False,
                        "scoped_locations": None,
                        "csrf_token": None,
                    }
                perms = sorted(user.permitted_actions())
                is_admin = "admin" in perms
                return 200, {
                    "authenticated": True,
                    "mock_mode": False,
                    "username": getattr(user, "username", None),
                    "roles": list(getattr(user, "roles", []) or []),
                    "locations": list(getattr(user, "locations", []) or []),
                    # Effective permissions so the UI can reflect them; the
                    # backend stays authoritative. ``scoped_locations`` is None
                    # for an admin (every location) else the explicit id list.
                    "permissions": perms,
                    "is_admin": is_admin,
                    "scoped_locations": None if is_admin
                        else sorted(user.location_set()),
                    # Rotated on every login (it is derived from the session).
                    "csrf_token": auth.csrf_token(str(token)) if token else None,
                }
            if path == "/api/dashboard":
                # Keeps every legacy key (draft/needs_review/.../total) and ADDS
                # the V2 overview metrics. Read-only: no AI call, no sync.
                # Scoped to the caller's permitted locations (admin: all).
                visible = self._visible(user) if use_auth else None
                payload: Dict[str, Any] = dict(
                    _scoped_dashboard_counts(service.repo, visible))
                metrics = _dashboard_metrics(service.repo, visible)
                payload.update(metrics)
                # Two spellings so the UI can render "Not tracked" from either.
                payload["reviews_trend_available"] = bool(
                    metrics["review_trend_available"])
                payload["reviews_distribution_available"] = bool(
                    metrics["rating_distribution_available"])
                # --- connectivity / mode signals (top-level, NOT inside kpis,
                # so the fixed 8-KPI contract is unchanged) -------------------
                # google_connected is a REAL signal only: this build ships with
                # no OAuth/sync configured, so it is False unless an operator
                # explicitly asserts GBP_GOOGLE_CONNECTED=1. It is never
                # derived from "the API answered", so the pill can never claim
                # a healthy sync when Google is not connected.
                payload["google_connected"] = _google_connected()
                # mock_mode: the server runs with auth=None -> demo/mock data.
                payload["mock_mode"] = not bool(use_auth)
                # api_health is "ok" precisely because this response IS the
                # dashboard having answered; the UI treats a failed fetch as
                # "error".
                payload["api_health"] = "ok"
                return 200, payload
            if path == "/api/queue":
                location_id = _first(query, "location_id")
                if use_auth and location_id is not None:
                    if not auth.authorize(user, "view", location_id):
                        return _err(403, "forbidden",
                                    detail=f"no access to location {location_id!r}")
                rows = service.list_queue(
                    location_id=location_id,
                    stars=_as_int(_first(query, "stars")),
                    status=_first(query, "status"),
                    language=_first(query, "language"),
                    since=_first(query, "since"),
                )
                visible = self._visible(user) if use_auth else None
                if visible is not None:
                    rows = [r for r in rows if str(r.get("location_id")) in visible]
                # Free-text search over the review comment + business name.
                term = (_first(query, "q") or "").strip().lower()
                if term:
                    business_names = {
                        r["location_id"]: r["business_name"]
                        for r in service.repo.conn.execute(
                            "SELECT location_id, business_name FROM brand_profiles"
                        ).fetchall()
                    }
                    rows = [
                        r for r in rows
                        if term in _queue_haystack(r, business_names)
                    ]
                # Server-side pagination.
                total = len(rows)
                page_size = _clamp_page_size(_first(query, "page_size"))
                page = _as_int(_first(query, "page"))
                if page is None or page < 1:
                    page = 1
                start = (page - 1) * page_size
                items = rows[start:start + page_size]
                pages = (total + page_size - 1) // page_size if total else 0
                return 200, {
                    "items": items,
                    "total": total,
                    "page": page,
                    "page_size": page_size,
                    "pages": pages,
                }
            if path == "/api/reviews":
                # Review-centric inbox (Group 7 / T20): every item carries the
                # human-readable ``business_name`` (never a raw resource id).
                # READ-ONLY -- no AI call, no draft is generated here.
                location_id = _first(query, "location_id")
                if use_auth and location_id is not None:
                    if not auth.authorize(user, "view", location_id):
                        return _err(403, "forbidden",
                                    detail=f"no access to location {location_id!r}")
                rows = _review_inbox_rows(service.repo)
                visible = self._visible(user) if use_auth else None
                if visible is not None:
                    rows = [r for r in rows
                            if str(r.get("location_id")) in visible]
                return 200, _reviews_page(rows, query)
            if path.startswith("/api/reviews/"):
                # GET /api/reviews/<review_id> -> one review + drafts + audit.
                rest = path[len("/api/reviews/"):]
                if not rest or "/" in rest:
                    return _err(404, "not_found",
                                detail=f"no route for GET {path}")
                review_id = _as_int(rest)
                if review_id is None:
                    return _err(404, "not_found",
                                detail=f"review not found: {rest!r}")
                if use_auth:
                    loc = service.repo.conn.execute(
                        "SELECT location_id FROM reviews WHERE id = ?",
                        (review_id,)).fetchone()
                    if loc is not None:
                        visible = self._visible(user)
                        if visible is not None and str(loc["location_id"]) not in visible:
                            return _err(403, "forbidden",
                                        detail=f"no access to review {rest!r}")
                detail = _review_detail(service.repo, review_id)
                if detail is None:
                    return _err(404, "not_found",
                                detail=f"review not found: {rest!r}")
                return 200, detail
            if path == "/api/locations/status":
                rows = _locations_status(service.repo)
                visible = self._visible(user) if use_auth else None
                if visible is not None:
                    rows = [r for r in rows if str(r.get("location_id")) in visible]
                return 200, rows
            if path == "/api/usage":
                # Global billing/usage carries no location column, so it is an
                # admin-only view: exposing it to a location-scoped user would
                # leak every other location's aggregate (cross-location leak).
                if use_auth and not auth.authorize(user, "admin", None):
                    return _err(403, "forbidden",
                                detail="usage stats require admin")
                return 200, _usage_stats(service.repo)
            if path == "/api/locations":
                rows = _location_rows(service.repo, service)
                visible = self._visible(user) if use_auth else None
                if visible is not None:
                    rows = [r for r in rows if str(r.get("id")) in visible]
                # Backward compatible: a bare call keeps the legacy list shape;
                # any pagination/filter/sort param returns the V2 envelope.
                if any(_first(query, key) is not None for key in _LOC_PAGE_PARAMS):
                    return 200, _locations_page(rows, query)
                return 200, rows
            if path.startswith("/api/locations/"):
                # GET /api/locations/<id> -> one location + its aggregates.
                rest = path[len("/api/locations/"):]
                # GET /api/locations/<id>/sync-runs -> the run ledger, read-only.
                # Data comes straight from sync_run_log; an empty ledger is
                # reported as an empty list (nothing is invented).
                if rest.endswith("/sync-runs"):
                    loc_ref = rest[: -len("/sync-runs")]
                    loc_id = _as_int(loc_ref)
                    match = None
                    if loc_id is not None:
                        match = next(
                            (r for r in _location_rows(service.repo, service)
                             if r.get("id") == loc_id), None)
                    if match is None:
                        return _err(404, "not_found",
                                    detail=f"location not found: {loc_ref!r}")
                    if use_auth:
                        visible = self._visible(user)
                        if visible is not None and str(loc_id) not in visible:
                            return _err(403, "forbidden",
                                        detail=f"no access to location {loc_ref!r}")
                    limit = _as_int(_first(query, "limit")) or 20
                    limit = max(1, min(limit, 50))
                    # Unified history (Group 8B / T27): the canonical
                    # append-only ledger PLUS any legacy per-run summary rows
                    # that were never mirrored, so no attempt disappears and
                    # nothing is reported twice. Identity = resource name; the
                    # numeric id is accepted only to match rows older code wrote.
                    from gbp.sync.state import SyncState

                    row = service.repo.conn.execute(
                        "SELECT location_name FROM business_locations"
                        " WHERE id = ?", (int(loc_id),)).fetchone()
                    resource = (row["location_name"] if row is not None
                                else str(loc_id))
                    runs = SyncState(service.repo).history(
                        resource, legacy_id=str(loc_id), limit=limit)
                    return 200, {
                        "location_id": loc_id,
                        "location_resource_id": resource,
                        "runs": runs,
                        "count": len(runs),
                    }
                if not rest or "/" in rest:
                    return _err(404, "not_found",
                                detail=f"no route for GET {path}")
                loc_id = _as_int(rest)
                match = None
                if loc_id is not None:
                    match = next(
                        (r for r in _location_rows(service.repo, service)
                         if r.get("id") == loc_id), None)
                if match is None:
                    return _err(404, "not_found",
                                detail=f"location not found: {rest!r}")
                if use_auth:
                    visible = self._visible(user)
                    if visible is not None and str(loc_id) not in visible:
                        return _err(403, "forbidden",
                                    detail=f"no access to location {rest!r}")
                detail = dict(match)
                detail["brand_profile"] = _brand_profile_summary(
                    service.repo, loc_id)
                return 200, detail
            if path == "/api/audit":
                location_id = _first(query, "location_id")
                if use_auth:
                    if location_id is not None:
                        if not auth.authorize(user, "view", location_id):
                            return _err(403, "forbidden",
                                        detail=f"no access to location {location_id!r}")
                    elif not auth.authorize(user, "admin", None):
                        return _err(403, "forbidden",
                                    detail="audit access requires an admin or a location scope")
                rows = service.get_audit(
                    entity=_first(query, "entity"),
                    entity_id=_first(query, "entity_id"),
                    actor=_first(query, "actor"),
                    limit=_as_int(_first(query, "limit")),
                )
                if use_auth:
                    # audit_logs has no location column: derive each row's
                    # location from its entity and drop anything outside the
                    # viewer's scope (admin sees all). Fail closed on unknown.
                    visible = self._visible(user)
                    if visible is not None:
                        rows = [r for r in rows
                                if str(_audit_row_location(service.repo, r)) in visible]
                return 200, rows
            return _err(404, "not_found", detail=f"no route for GET {path}")

        def _route_post(
            self,
            path: str,
            query: Dict[str, Any],
            body: Dict[str, Any],
            user: Any = None,
        ) -> Tuple[int, Any]:
            parts = [p for p in path.split("/") if p]
            # /api/drafts/bulk-approve
            if parts[:2] == ["api", "drafts"] and len(parts) == 3 and parts[2] == "bulk-approve":
                if use_auth:
                    _reject_unknown(body, ("ids",))
                ids = body.get("ids") or []
                if not isinstance(ids, list):
                    return _err(400, "invalid_request", detail="ids must be a list")
                if use_auth:
                    if len(ids) > 100:
                        return _err(400, "invalid_request", detail="too many ids")
                    for raw_id in ids:
                        if not _valid_id(raw_id):
                            return _err(400, "invalid_request", detail=f"invalid draft id {raw_id!r}")
                        loc = self._draft_location(_as_int(raw_id))
                        if loc is None:
                            return _err(404, "not_found", detail=f"draft not found: {raw_id!r}")
                        if not auth.authorize(user, "approve", loc):
                            return _err(403, "forbidden", detail=f"no access to location {loc!r}")
                    actor = user.username
                else:
                    actor = str(body.get("actor") or "unknown")
                return 200, service.bulk_approve(ids, actor)

            # /api/drafts/<id>/<action>
            if parts[:2] == ["api", "drafts"] and len(parts) == 4:
                raw_id = parts[2]
                if use_auth:
                    if not _valid_id(raw_id):
                        return _err(400, "invalid_request", detail="invalid draft id")
                    _reject_unknown(body, ("text", "note", "idempotency_key", "expected_version"))
                draft_id = _as_int(raw_id)
                if draft_id is None:
                    return _err(400, "invalid_request", detail="draft id must be an int")
                action = parts[3]
                idem = body.get("idempotency_key")
                if idem is not None and not _valid_id(idem):
                    return _err(400, "invalid_request", detail="invalid idempotency_key")
                expected = _as_int(body.get("expected_version"))

                if use_auth:
                    need = {"edit": "edit", "approve": "approve",
                            "reject": "reject", "publish-guard": "view"}.get(action, "view")
                    loc = self._draft_location(draft_id)
                    if loc is None:
                        return _err(404, "not_found", detail=f"draft not found: {draft_id!r}")
                    if not auth.authorize(user, need, loc):
                        return _err(403, "forbidden", detail=f"no access to location {loc!r}")
                    # The actor is the authenticated user -- never client-supplied.
                    actor = user.username
                else:
                    actor = str(body.get("actor") or "unknown")

                if action == "edit":
                    if "text" not in body:
                        return _err(400, "invalid_request", detail="text is required")
                    text = str(body.get("text") or "")
                    if use_auth and len(text) > MAX_TEXT_CHARS:
                        return _err(400, "invalid_request", detail="text too long")
                    return 200, service.edit_draft(
                        draft_id, text, actor,
                        expected_version=expected, idempotency_key=idem,
                    )
                if action == "approve":
                    return 200, service.approve(
                        draft_id, actor,
                        expected_version=expected, idempotency_key=idem,
                    )
                if action == "reject":
                    note = str(body.get("note") or "")
                    if use_auth and len(note) > MAX_NOTE_CHARS:
                        return _err(400, "invalid_request", detail="note too long")
                    return 200, service.reject(
                        draft_id, actor, note,
                        expected_version=expected, idempotency_key=idem,
                    )
                if action == "publish-guard":
                    return 200, service.publish_guard(draft_id)
                return _err(404, "not_found", detail=f"no route for POST {path}")

            return _err(404, "not_found", detail=f"no route for POST {path}")

    return Handler


def _first(query: Dict[str, Any], key: str) -> Optional[str]:
    values = query.get(key)
    if not values:
        return None
    return values[0]


def create_server(
    service: Any,
    host: str = "127.0.0.1",
    port: int = 0,
    auth: Any = None,
    rate_limit: Optional[int] = None,
    rate_window: float = DEFAULT_RATE_WINDOW,
    login_limit: Optional[int] = None,
) -> ThreadingHTTPServer:
    """Create (but do not start) a threaded HTTP server bound to ``host:port``.

    Defaults to ``127.0.0.1`` and ``port=0`` (ephemeral) so tests can start it
    in-process. The real bound port is ``server.server_address[1]``. Pass
    ``auth`` to enable authenticated/authorized mode.
    """
    handler = make_handler(
        service, auth=auth, rate_limit=rate_limit,
        rate_window=rate_window, login_limit=login_limit,
    )
    return ThreadingHTTPServer((host, port), handler)
