#!/usr/bin/env python3
"""Manage GBP dashboard users (create / list / disable / set roles+locations).

Offline, standard library only. Passwords are read from the environment
(``GBP_USER_PASSWORD``) or an interactive no-echo prompt -- NEVER from argv,
never echoed back, and only ever stored as a PBKDF2 hash (the store's
``hash_password``). The tool prints only the username and its roles; it never
prints a password or a hash.

Usage:
    cd /opt/data/gbp-integration
    PYTHONPATH=src python3 scripts/manage_users.py list
    GBP_USER_PASSWORD=... PYTHONPATH=src python3 scripts/manage_users.py \
        create --username alice --role owner --location 3 --location 4
    PYTHONPATH=src python3 scripts/manage_users.py disable --username alice
    PYTHONPATH=src python3 scripts/manage_users.py unlock --username alice

Passwords must satisfy :mod:`gbp.security.policy` (>= 12 characters, not a
well-known weak password, no straight run like ``abcd``/``1234``, and not
derived from the account name).
    GBP_USER_PASSWORD=... PYTHONPATH=src python3 scripts/manage_users.py \
        set --username alice --role admin

The ``--db`` default matches ``scripts/serve_ui.py`` so the demo DB and this
tool share the same users table.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from typing import List, Optional

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from gbp.authz import ROLE_PERMISSIONS, hash_password  # noqa: E402
from gbp.authz.store import UserStore  # noqa: E402
from gbp.authz.auth import AuthService  # noqa: E402
from gbp.security.policy import (  # noqa: E402
    MIN_PASSWORD_LENGTH,
    PasswordPolicyError,
    validate_password,
)

DEFAULT_DB = os.path.join(_ROOT, "gbp_ui.db")
PASSWORD_ENV = "GBP_USER_PASSWORD"


def _read_password(confirm: bool = True) -> Optional[str]:
    """Return the new password from the env or an interactive prompt.

    Returns ``None`` when nothing is available (non-interactive and the env var
    is unset) so the caller can fail cleanly instead of inventing a default.
    """
    env_pw = os.environ.get(PASSWORD_ENV)
    if env_pw:
        return env_pw
    if not sys.stdin.isatty():
        return None
    try:
        first = getpass.getpass("Password: ")
        if not first:
            return None
        if confirm and getpass.getpass("Confirm password: ") != first:
            print("passwords did not match", file=sys.stderr)
            return None
    except (EOFError, KeyboardInterrupt):
        print("", file=sys.stderr)
        return None
    return first


def _validate_roles(roles: List[str]) -> Optional[str]:
    unknown = [r for r in roles if r not in ROLE_PERMISSIONS]
    if unknown:
        return f"unknown role(s): {', '.join(unknown)} (valid: {', '.join(ROLE_PERMISSIONS)})"
    return None


def _print_user(username: str, roles: List[str]) -> None:
    # Only identity + roles: NEVER a password or hash.
    print(f"{username}\troles={','.join(roles) if roles else '-'}")


def cmd_list(store: UserStore, args: argparse.Namespace) -> int:
    users = store.list()
    if not users:
        print("(no users)")
        return 0
    for u in users:
        state = "" if u.active else " (disabled)"
        _print_user(u.username + state, list(u.roles))
    return 0


def cmd_create(store: UserStore, args: argparse.Namespace) -> int:
    if store.get(args.username) is not None:
        print(f"user {args.username!r} already exists", file=sys.stderr)
        return 1
    err = _validate_roles(args.role)
    if err:
        print(err, file=sys.stderr)
        return 2
    password = _read_password()
    if password is None:
        print(
            f"no password available: set {PASSWORD_ENV} or run interactively",
            file=sys.stderr,
        )
        return 2
    try:
        validate_password(password, username=args.username)
    except PasswordPolicyError as exc:
        # The message lists the RULES that failed; the password is never echoed.
        print(f"password rejected: {exc}", file=sys.stderr)
        return 2
    user = store.create_user(
        args.username, hash_password(password),
        list(args.role or []), [str(loc) for loc in (args.location or [])],
    )
    _print_user(user.username, list(user.roles))
    return 0


def cmd_set(store: UserStore, args: argparse.Namespace) -> int:
    user = store.get(args.username)
    if user is None:
        print(f"no such user {args.username!r}", file=sys.stderr)
        return 1
    if args.role is not None:
        err = _validate_roles(args.role)
        if err:
            print(err, file=sys.stderr)
            return 2
        store.update_roles(args.username, list(args.role))
    if args.location is not None:
        store.update_locations(args.username, [str(loc) for loc in args.location])
    if args.password:
        password = _read_password()
        if password is None:
            print(
                f"no password available: set {PASSWORD_ENV} or run interactively",
                file=sys.stderr,
            )
            return 2
        try:
            validate_password(password, username=args.username)
        except PasswordPolicyError as exc:
            print(f"password rejected: {exc}", file=sys.stderr)
            return 2
        # create_user upserts the hash while preserving roles/locations.
        current = store.get(args.username)
        store.create_user(
            args.username, hash_password(password),
            list(current.roles), list(current.locations),
            active=bool(current.active), mfa_required=bool(current.mfa_required),
        )
    final = store.get(args.username)
    _print_user(final.username, list(final.roles))
    return 0


def _set_active(store: UserStore, username: str, active: bool) -> int:
    if store.get(username) is None:
        print(f"no such user {username!r}", file=sys.stderr)
        return 1
    store.set_active(username, active)
    user = store.get(username)
    _print_user(user.username, list(user.roles))
    return 0


def cmd_disable(store: UserStore, args: argparse.Namespace) -> int:
    return _set_active(store, args.username, False)


def cmd_enable(store: UserStore, args: argparse.Namespace) -> int:
    return _set_active(store, args.username, True)


def cmd_unlock(store: UserStore, args: argparse.Namespace) -> int:
    """Clear login-failure counters and any lock for one account.

    Local recovery path for a locked-out admin. Deliberately CLI-only: it is
    never exposed over HTTP, so an attacker cannot reset their own throttle.
    """
    if store.get(args.username) is None:
        print(f"no such user {args.username!r}", file=sys.stderr)
        return 1
    service = AuthService(db_path_or_conn=store.conn, users=[])
    cleared = service.unlock_user(args.username)
    user = store.get(args.username)
    _print_user(user.username, list(user.roles))
    print(f"cleared {cleared} login-throttle row(s)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage GBP dashboard users (offline).")
    parser.add_argument("--db", default=DEFAULT_DB,
                        help="SQLite file holding the users table")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="list users (username + roles only)")

    p_create = sub.add_parser("create", help="create a user")
    p_create.add_argument("--username", required=True)
    p_create.add_argument("--role", action="append", default=[],
                          help="role (repeatable): admin | owner | reviewer")
    p_create.add_argument("--location", action="append", default=[],
                          help="allowed location id (repeatable)")

    p_set = sub.add_parser("set", help="update roles, locations and/or password")
    p_set.add_argument("--username", required=True)
    p_set.add_argument("--role", action="append", default=None,
                       help="replace roles (repeatable)")
    p_set.add_argument("--location", action="append", default=None,
                       help="replace allowed locations (repeatable)")
    p_set.add_argument("--password", action="store_true",
                       help=f"also reset the password (from {PASSWORD_ENV} or prompt)")

    p_disable = sub.add_parser("disable", help="deactivate a user")
    p_disable.add_argument("--username", required=True)

    p_enable = sub.add_parser("enable", help="reactivate a user")
    p_enable.add_argument("--username", required=True)

    p_unlock = sub.add_parser(
        "unlock", help="clear login failures / lockout for a user")
    p_unlock.add_argument("--username", required=True)
    return parser


_DISPATCH = {
    "list": cmd_list,
    "create": cmd_create,
    "set": cmd_set,
    "disable": cmd_disable,
    "enable": cmd_enable,
    "unlock": cmd_unlock,
}


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    store = UserStore(args.db)
    return _DISPATCH[args.command](store, args)


if __name__ == "__main__":
    raise SystemExit(main())
