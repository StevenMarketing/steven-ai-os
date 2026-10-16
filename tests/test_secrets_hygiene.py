"""Group 8A / T24 -- session-secret fail-closed, redaction and no-secrets hygiene.

Covered here (all offline, stdlib only, no network):

* with real auth enabled and ``GBP_SESSION_SECRET`` unset, the secret is NOT
  silently invented: :class:`AuthService` refuses (fail closed) unless the
  operator explicitly opts into an ephemeral secret.
* a too-short secret is rejected; a valid one is accepted.
* the redaction helper masks session cookies, Authorization headers, passwords,
  CSRF tokens and provider-style ``sk-or-v1...`` tokens.
* a login and a failed login emit no password, session token or signing secret
  to stdout/stderr/logs (routes through the redaction helper).
* a repository scan FAILS if it finds secret material or credential files.
* any export/ZIP builder (if present) excludes .env/auth.json/*.db.

Run with:
    cd /opt/data/gbp-integration && PYTHONPATH=src python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import io
import logging
import os
import re
import sys
import unittest
import warnings

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_ROOT, "src"))

from gbp.authz import AuthService  # noqa: E402
from gbp.authz.models import hash_password  # noqa: E402
from gbp.authz.store import UserStore  # noqa: E402
from gbp.repo import Repository  # noqa: E402
from gbp.security import (  # noqa: E402
    MIN_SECRET_LENGTH,
    REDACTED,
    SessionSecretError,
    WeakSessionSecretError,
    is_sensitive_key,
    load_session_secret,
    redact,
    redact_headers,
    redact_text,
)

SCHEMA_PATH = os.path.join(_ROOT, "sql", "schema.sql")
SECRET_ENV = "GBP_SESSION_SECRET"
VALID_SECRET = "hygiene-test-secret-value-0123456789abcdef"  # >= 32 chars
PASSWORD = "Sup3rSecret-Admin!"  # used to prove it never leaks

# Build token-looking strings at runtime so this file holds no token literal.
FAKE_OR_TOKEN = "sk-or-v1-" + ("A1b2C3d4E5f6G7h8I9j0" * 2)
FAKE_SESSION = "eyJ1IjoiYWRtaW4ifQ." + ("a" * 40)  # looks like a signed token


def _make_repo() -> Repository:
    repo = Repository(":memory:")
    repo.init_db(SCHEMA_PATH)
    return repo


class SessionSecretRequiredTest(unittest.TestCase):
    """Real auth must never silently invent a signing secret."""

    def test_missing_secret_refuses_real_auth(self):
        with self.assertRaises(SessionSecretError) as cm:
            AuthService(db_path_or_conn=None, env={}, require_secret=True,
                        users=[])
        msg = str(cm.exception)
        self.assertIn(SECRET_ENV, msg)
        # the actionable message must not contain any invented secret value
        self.assertIn("--allow-ephemeral-secret", msg)

    def test_missing_secret_still_warns_when_not_strict(self):
        # legacy library call keeps working, but never silently: it warns.
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            svc = AuthService(env={}, users=[])
        self.assertTrue(svc.ephemeral_secret)
        self.assertTrue(any(SECRET_ENV in str(w.message) for w in caught))

    def test_ephemeral_is_opt_in_and_warns_loudly(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            svc = AuthService(env={}, require_secret=True, allow_ephemeral=True,
                              users=[])
        self.assertTrue(svc.ephemeral_secret)
        self.assertTrue(any(issubclass(w.category, RuntimeWarning) for w in caught))

    def test_ephemeral_not_enabled_by_default(self):
        # require_secret=True with NO explicit opt-out must refuse, not warn.
        with self.assertRaises(SessionSecretError):
            AuthService(env={}, require_secret=True, users=[])


class SecretStrengthTest(unittest.TestCase):
    def test_too_short_secret_is_rejected(self):
        short = "x" * (MIN_SECRET_LENGTH - 1)
        with self.assertRaises(WeakSessionSecretError):
            AuthService(env={SECRET_ENV: short}, require_secret=True, users=[])

    def test_valid_secret_is_accepted(self):
        svc = AuthService(env={SECRET_ENV: VALID_SECRET}, require_secret=True,
                          users=[])
        self.assertFalse(svc.ephemeral_secret)

    def test_loader_min_length_boundary(self):
        sec = load_session_secret({SECRET_ENV: "y" * MIN_SECRET_LENGTH},
                                  require=True)
        self.assertFalse(sec.ephemeral)
        with self.assertRaises(WeakSessionSecretError):
            load_session_secret({SECRET_ENV: "y" * (MIN_SECRET_LENGTH - 1)},
                                require=True)


class RedactionTest(unittest.TestCase):
    def test_mapping_keys_are_masked(self):
        out = redact({
            "password": PASSWORD,
            "session_token": FAKE_SESSION,
            "csrf_token": "deadbeef" * 8,
            "Authorization": "Bearer " + FAKE_OR_TOKEN,
            "Cookie": f"gbp_session={FAKE_SESSION}",
        })
        for key in ("password", "session_token", "csrf_token",
                    "Authorization", "Cookie"):
            self.assertEqual(out[key], REDACTED, key)

    def test_sensitive_key_detection(self):
        for k in ("password", "Authorization", "Cookie", "X-CSRF-Token",
                  "session", "api_key"):
            self.assertTrue(is_sensitive_key(k), k)
        for k in ("username", "location_id", "rating"):
            self.assertFalse(is_sensitive_key(k), k)

    def test_redact_text_masks_free_form_secrets(self):
        blob = (
            f"Authorization: Bearer {FAKE_SESSION}\n"
            f"Cookie: gbp_session={FAKE_SESSION}; theme=dark\n"
            f"password={PASSWORD}\n"
            f'"csrf_token": "abc123abc123abc123"\n'
            f"api_key={FAKE_OR_TOKEN}\n"
        )
        out = redact_text(blob)
        for raw in (FAKE_SESSION, PASSWORD, FAKE_OR_TOKEN):
            self.assertNotIn(raw, out)
        self.assertIn(REDACTED, out)

    def test_redact_headers(self):
        headers = {
            "Authorization": "Bearer " + FAKE_OR_TOKEN,
            "Cookie": f"gbp_session={FAKE_SESSION}",
            "X-CSRF-Token": "cafe" * 8,
            "Content-Type": "application/json",
        }
        out = redact_headers(headers)
        self.assertEqual(out["Authorization"], REDACTED)
        self.assertEqual(out["Cookie"], REDACTED)
        self.assertEqual(out["X-CSRF-Token"], REDACTED)
        self.assertEqual(out["Content-Type"], "application/json")
        joined = str(out)
        self.assertNotIn(FAKE_OR_TOKEN, joined)
        self.assertNotIn(FAKE_SESSION, joined)

    def test_nested_structures(self):
        out = redact({"users": [{"username": "admin", "password": PASSWORD}]})
        self.assertEqual(out["users"][0]["password"], REDACTED)
        self.assertEqual(out["users"][0]["username"], "admin")


class NoLeakDuringLoginTest(unittest.TestCase):
    """A login and a failed login must emit no secret material."""

    def test_login_output_contains_no_secrets(self):
        repo = _make_repo()
        UserStore(repo.conn).create_user(
            "admin", hash_password(PASSWORD), ["admin"], [])
        auth = AuthService(repo.conn, env={SECRET_ENV: VALID_SECRET}, ttl=3600)

        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        root = logging.getLogger()
        old_level = root.level
        root.addHandler(handler)
        old_stdout, old_stderr = sys.stdout, sys.stderr
        cap = io.StringIO()
        sys.stdout = sys.stderr = cap
        try:
            token = auth.login("admin", PASSWORD)
            auth.login("admin", PASSWORD + "-wrong")  # failed login
            # a log line that routes request headers through the helper
            logging.getLogger("gbp.hygiene").warning(
                "request headers: %s",
                redact_headers({"Authorization": "Bearer " + token,
                                "Cookie": f"gbp_session={token}"}),
            )
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr
            root.removeHandler(handler)
            root.setLevel(old_level)

        captured = cap.getvalue() + buf.getvalue()
        self.assertIsNotNone(token)
        self.assertNotIn(PASSWORD, captured)
        self.assertNotIn(PASSWORD + "-wrong", captured)
        self.assertNotIn(token, captured)
        self.assertNotIn(VALID_SECRET, captured)
        # the helper actually ran
        self.assertIn(REDACTED, captured)
        repo.close()


class RepoTreeScannerTest(unittest.TestCase):
    """Walk the project and fail on credential files / secret literals."""

    SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache",
                 "node_modules", ".venv", "venv"}
    SKIP_FILES = {"test_secrets_hygiene.py"}  # this scanner's own fixtures
    CREDENTIAL_FILES = {".env", "auth.json"}
    CREDENTIAL_EXT = {".db", ".sqlite", ".sqlite3", ".pem", ".key"}

    # Token/secret patterns that must never appear in tracked source.
    TOKEN_PATTERNS = (
        re.compile(r"sk-or-v1[A-Za-z0-9]{16,}"),
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        re.compile(r"OPENROUTER_API_KEY\s*=\s*[\"']?[A-Za-z0-9\-_]{8,}"),
        re.compile(r"GBP_SESSION_SECRET\s*=\s*[\"'][^\"']{4,}[\"']"),
        re.compile(r"\bpassword\s*=\s*[\"'][^\"']{3,}[\"']"),
    )

    def _iter_files(self):
        for dirpath, dirnames, filenames in os.walk(_ROOT):
            dirnames[:] = [d for d in dirnames if d not in self.SKIP_DIRS]
            for name in filenames:
                if name in self.SKIP_FILES:
                    continue
                yield os.path.join(dirpath, name), name

    def test_no_credential_files_are_committed(self):
        offenders = []
        for path, name in self._iter_files():
            ext = os.path.splitext(name)[1].lower()
            if name in self.CREDENTIAL_FILES or ext in self.CREDENTIAL_EXT:
                offenders.append(os.path.relpath(path, _ROOT))
        self.assertEqual(offenders, [], f"credential files present: {offenders}")

    def test_no_secret_literals_in_source(self):
        offenders = []
        tests_dir = os.path.join(_ROOT, "tests") + os.sep
        for path, _name in self._iter_files():
            # tests/ holds clearly-labelled fixtures -- not production source.
            if os.path.abspath(path).startswith(tests_dir):
                continue
            ext = os.path.splitext(path)[1].lower()
            if ext not in {".py", ".sql", ".json", ".txt", ".cfg", ".ini",
                           ".yml", ".yaml", ".sh", ".md"}:
                continue
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                    text = fh.read()
            except OSError:
                continue
            for pat in self.TOKEN_PATTERNS:
                m = pat.search(text)
                if m:
                    offenders.append(
                        f"{os.path.relpath(path, _ROOT)}: {m.group(0)[:40]!r}")
        self.assertEqual(offenders, [], f"secret literals found: {offenders}")

    def test_export_zip_builder_excludes_secrets(self):
        builders = []
        scripts_dir = os.path.join(_ROOT, "scripts")
        for base in (scripts_dir, os.path.join(_ROOT, "src")):
            if not os.path.isdir(base):
                continue
            for dirpath, _dirnames, filenames in os.walk(base):
                for name in filenames:
                    if not name.endswith(".py"):
                        continue
                    p = os.path.join(dirpath, name)
                    with open(p, "r", encoding="utf-8", errors="ignore") as fh:
                        text = fh.read()
                    if "zipfile" in text or "make_archive" in text:
                        builders.append((p, text))
        # There is currently no export/ZIP builder in the project. If one is
        # added it MUST exclude .env / auth.json / *.db.
        for path, text in builders:
            low = text.lower()
            self.assertIn(".env", low, path)
            self.assertIn("auth.json", low, path)
            self.assertIn(".db", low, path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
