"""Offline tests for the private GReply preview deployment path (issue #73).

Standard library only. Nothing here deploys, opens a port to the world, calls
Google or reads a real secret: the guard is exercised as a pure function, the
container/platform config is checked as text, and the one live test starts the
real app on loopback with an ephemeral port to prove unauthenticated API access
is denied.

Run with:
    cd /opt/data/gbp-integration && PYTHONPATH=src python3 -m unittest tests.test_preview_deploy -v
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_ROOT, "src"))
sys.path.insert(0, os.path.join(_ROOT, "scripts"))
sys.path.insert(0, os.path.join(_ROOT, "preview"))

import guard  # noqa: E402  (preview/guard.py)
import serve_ui  # noqa: E402  (scripts/serve_ui.py)
from gbp.api.server import create_server  # noqa: E402

PREVIEW_DIR = os.path.join(_ROOT, "preview")
ENTRYPOINT = os.path.join(PREVIEW_DIR, "entrypoint.sh")
DOCKERFILE = os.path.join(PREVIEW_DIR, "Dockerfile")
FLY_TOML = os.path.join(PREVIEW_DIR, "fly.toml")
RUNBOOK = os.path.join(_ROOT, "docs", "PREVIEW-DEPLOY.md")

STRONG_PASSWORD = "Preview-Demo-Passw0rd!"
LONG_SECRET = "preview-session-secret-value-0123456789abcdef"


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _valid_env():
    return {
        "GBP_PREVIEW": "1",
        "GBP_SESSION_SECRET": LONG_SECRET,
        "GBP_DEMO_PASSWORD": STRONG_PASSWORD,
    }


class PreviewGuardTest(unittest.TestCase):
    """preview/guard.py is fail-closed and never echoes a value."""

    def test_empty_environment_is_refused(self):
        self.assertTrue(guard.check({}))

    def test_valid_environment_is_accepted(self):
        self.assertEqual(guard.check(_valid_env()), [])

    def test_preview_acknowledgement_is_required(self):
        env = _valid_env()
        del env["GBP_PREVIEW"]
        problems = guard.check(env)
        self.assertTrue(problems)
        self.assertTrue(any("GBP_PREVIEW" in p for p in problems))

    def test_missing_session_secret_is_refused(self):
        env = _valid_env()
        del env["GBP_SESSION_SECRET"]
        self.assertTrue(any("GBP_SESSION_SECRET" in p for p in guard.check(env)))

    def test_short_session_secret_is_refused(self):
        env = _valid_env()
        env["GBP_SESSION_SECRET"] = "too-short"
        problems = guard.check(env)
        self.assertTrue(any("at least 32" in p for p in problems))

    def test_missing_demo_password_is_refused(self):
        env = _valid_env()
        env["GBP_DEMO_PASSWORD"] = "   "
        self.assertTrue(any("GBP_DEMO_PASSWORD" in p for p in guard.check(env)))

    def test_anonymous_mode_is_always_refused(self):
        env = _valid_env()
        env["GBP_ALLOW_ANON"] = "1"
        self.assertTrue(any("GBP_ALLOW_ANON" in p for p in guard.check(env)))

    def test_production_and_live_google_variables_are_refused(self):
        env = _valid_env()
        env["GBP_ENV"] = "production"
        env["GOOGLE_APPLICATION_CREDENTIALS"] = "/tmp/creds.json"
        env["GBP_REFRESH_TOKEN"] = "x"
        problems = guard.check(env)
        self.assertTrue(any("GOOGLE_APPLICATION_CREDENTIALS" in p for p in problems))
        self.assertTrue(any("GBP_REFRESH_TOKEN" in p for p in problems))
        self.assertTrue(any("production" in p for p in problems))

    def test_violations_never_contain_a_secret_value(self):
        secret = "S3cr3t-V4lue-That-Must-Not-Appear-In-Logs"
        env = _valid_env()
        env["GBP_SESSION_SECRET"] = secret
        env["GOOGLE_APPLICATION_CREDENTIALS"] = secret
        for problem in guard.check(env):
            self.assertNotIn(secret, problem)
            self.assertNotIn(STRONG_PASSWORD, problem)


class PreviewConfigTest(unittest.TestCase):
    """The image and platform config cannot expose the dashboard by accident."""

    def setUp(self):
        self.entrypoint = _read(ENTRYPOINT)
        self.dockerfile = _read(DOCKERFILE)
        self.fly = _read(FLY_TOML)
        # Comments may *describe* a forbidden directive; only real config lines
        # count. Bracketed sections/keys are what Fly actually parses.
        self.fly_config = "\n".join(
            line for line in self.fly.splitlines()
            if not line.lstrip().startswith("#")
        )

    def test_entrypoint_is_fail_closed_and_loopback_only(self):
        self.assertIn("set -eu", self.entrypoint)
        self.assertIn("preview/guard.py", self.entrypoint)
        self.assertIn("--auth", self.entrypoint)
        self.assertIn("--host 127.0.0.1", self.entrypoint)
        self.assertNotIn("0.0.0.0", self.entrypoint)
        # A rejected guard must abort the container.
        self.assertIn("exit ", self.entrypoint)

    def test_dockerfile_runs_non_root_with_a_healthcheck(self):
        self.assertIn("USER preview", self.dockerfile)
        self.assertIn("HEALTHCHECK", self.dockerfile)
        self.assertNotIn("--privileged", self.dockerfile)
        # No secret is ever baked into the image as an ENV default.
        self.assertNotIn("ENV GBP_SESSION_SECRET", self.dockerfile)
        self.assertNotIn("ENV GBP_DEMO_PASSWORD", self.dockerfile)

    def test_fly_config_is_private(self):
        self.assertNotIn("[http_service]", self.fly_config)
        self.assertNotIn("ports = [", self.fly_config)
        self.assertIn("port = 8080", self.fly_config)
        self.assertIn("dockerfile = \"preview/Dockerfile\"", self.fly_config)
        self.assertRegex(self.fly_config, r"\[checks\.health\]")
        self.assertIn("path = \"/\"", self.fly_config)

    def test_runbook_documents_options_costs_and_approvals(self):
        runbook = _read(RUNBOOK)
        for heading in ("## Options", "## Costs", "## Approvals required",
                        "## Health check, logs, rollback"):
            self.assertIn(heading, runbook)
        self.assertIn("NOT deployed", runbook)
        self.assertRegex(runbook, r"No invented link")


class PreviewUnauthenticatedAccessTest(unittest.TestCase):
    """A preview with auth enabled denies unauthenticated API access."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_secret = os.environ.get("GBP_SESSION_SECRET")
        os.environ["GBP_SESSION_SECRET"] = LONG_SECRET
        service = serve_ui.build_service(os.path.join(self._tmp.name, "preview.db"),
                                         reset=True)
        auth = serve_ui.build_auth(service.repo, password=STRONG_PASSWORD)
        self.server = create_server(service, host="127.0.0.1", port=0, auth=auth)
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=5)
        if self._old_secret is None:
            os.environ.pop("GBP_SESSION_SECRET", None)
        else:
            os.environ["GBP_SESSION_SECRET"] = self._old_secret
        self._tmp.cleanup()

    def test_api_requires_a_session(self):
        url = f"http://127.0.0.1:{self.port}/api/me"
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(url, timeout=5)
        self.assertEqual(caught.exception.code, 401)

    def test_login_page_is_served_but_api_is_protected(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=5) as resp:
            self.assertEqual(resp.status, 200)


if __name__ == "__main__":
    unittest.main()
