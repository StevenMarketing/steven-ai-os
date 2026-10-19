"""Issue #57 — the real, fail-closed GitHub label mutation for a LIVE queue_issue.

These tests prove the acceptance criteria offline, with a mocked GitHub transport
and no network:

  * no request/token -> refused, no mutation;
  * a confirmed API success -> ``mutated: true`` exactly once;
  * a transport/API failure, or a response that does not echo the label, never
    reports ``mutated: true`` (fail closed);
  * a label already present is a no-op (a replay cannot enqueue twice);
  * the production runner only mutates on a confirmed response, and downgrades a
    failed mutation to ``admitted: false`` / ``mutation_failed``.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "scripts"))

from gbp.bridge import live_mutation as lm  # noqa: E402
from gbp.bridge.commands import CommandLedger  # noqa: E402
from gbp.bridge.identity import TrustedContext  # noqa: E402

REPO = "StevenMarketing/steven-ai-os"
NOW = 1_790_000_000.0

GOOD_BODY = ("## Goal\nx\n## Scope\nx\n## Acceptance Criteria\nx\n"
             "## Tests Required\nx\n## Constraints\nx\n")


def command(**over):
    base = {"request_id": "req-57-1", "idempotency_key": "idem-57-1",
            "operation": "queue_issue", "issue": 60, "dry_run": False,
            "created_at": NOW - 5, "ttl_seconds": 300}
    base.update(over)
    return base


def trusted():
    return TrustedContext(repository=REPO, source_kind="issue_comment", source_id=1,
                          author_login="StevenMarketing", author_association="OWNER",
                          permission="admin", event_type="issue_comment", verified=True)


class TestPerformLabelMutation(unittest.TestCase):
    def _ok(self, seen):
        def request(method, path, token=None, **kw):
            seen.append((method, path, kw.get("body")))
            return ([{"name": "hermes-ready"}], {})
        return request

    def test_missing_transport_or_token_is_refused_without_a_call(self):
        calls = []
        out = lm.perform_label_mutation(None, "tok", REPO, 60)
        self.assertFalse(out["mutated"])
        self.assertFalse(out["attempted"])
        self.assertEqual(out["error"], "mutation_unavailable")
        out = lm.perform_label_mutation(self._ok(calls), "", REPO, 60)
        self.assertFalse(out["attempted"])
        self.assertEqual(out["error"], "mutation_unavailable")
        self.assertEqual(calls, [])

    def test_confirmed_mutation_reports_mutated_true(self):
        seen = []
        out = lm.perform_label_mutation(self._ok(seen), "tok", REPO, 60)
        self.assertTrue(out["mutated"])
        self.assertEqual(out["error"], "")
        self.assertEqual(len(seen), 1)
        method, path, body = seen[0]
        self.assertEqual(method, "POST")
        self.assertTrue(path.endswith("/repos/%s/issues/60/labels" % REPO))
        self.assertEqual(body, {"labels": ["hermes-ready"]})

    def test_transport_error_never_reports_mutated_true(self):
        def request(method, path, token=None, **kw):
            raise RuntimeError("network down")
        out = lm.perform_label_mutation(request, "tok", REPO, 60)
        self.assertFalse(out["mutated"])
        self.assertTrue(out["attempted"])
        self.assertEqual(out["error"], "mutation_transport_error")

    def test_unconfirmed_response_never_reports_mutated_true(self):
        def request(method, path, token=None, **kw):
            return ([{"name": "bug"}], {})  # API answered but did not add the label
        out = lm.perform_label_mutation(request, "tok", REPO, 60)
        self.assertFalse(out["mutated"])
        self.assertEqual(out["error"], "mutation_not_confirmed")

    def test_label_already_present_is_a_noop(self):
        calls = []
        out = lm.perform_label_mutation(self._ok(calls), "tok", REPO, 60,
                                        existing_labels=["hermes-ready"])
        self.assertFalse(out["mutated"])
        self.assertFalse(out["attempted"])
        self.assertEqual(out["error"], "label_already_present")
        self.assertEqual(calls, [])


class TestApplyToReceipt(unittest.TestCase):
    def _admitted(self):
        return {"admitted": True, "operation": "queue_issue", "issue": 60,
                "code": "admitted", "reasons": [], "links": {}, "mutated": False}

    def test_confirmed_success_sets_mutated_and_completed(self):
        def request(method, path, token=None, **kw):
            return ([{"name": "hermes-ready"}], {})
        receipt = lm.apply_mutation_to_receipt(self._admitted(), request=request,
                                               token="tok", repo=REPO, number=60)
        self.assertTrue(receipt["admitted"])
        self.assertTrue(receipt["mutated"])
        self.assertEqual(receipt["code"], "completed")

    def test_failed_mutation_downgrades_to_refusal(self):
        def request(method, path, token=None, **kw):
            raise RuntimeError("boom")
        receipt = lm.apply_mutation_to_receipt(self._admitted(), request=request,
                                               token="tok", repo=REPO, number=60)
        self.assertFalse(receipt["admitted"])
        self.assertFalse(receipt["mutated"])
        self.assertEqual(receipt["code"], "mutation_failed")
        self.assertTrue(any("mutation_failed" in r for r in receipt["reasons"]))

    def test_unadmitted_receipt_is_untouched(self):
        receipt = {"admitted": False, "code": "live_queue_disabled", "mutated": False}
        out = lm.apply_mutation_to_receipt(receipt, request=None, token="", repo=REPO, number=60)
        self.assertFalse(out["mutated"])
        self.assertEqual(out["code"], "live_queue_disabled")


class TestRunnerMutationEndToEnd(unittest.TestCase):
    """The production runner mutates GitHub exactly once, and only on success."""

    def _request(self, *, fail_post=False):
        calls = {"post": 0}

        def request(method, path, token=None, **kw):
            if "/pulls?state=open" in path:
                return ([], {})
            if path.endswith("/labels") and method == "POST":
                calls["post"] += 1
                if fail_post:
                    raise RuntimeError("500")
                add = (kw.get("body") or {}).get("labels") or []
                return ([{"name": n} for n in add], {})
            if "/issues/60" in path:
                return ({"number": 60, "state": "open", "labels": [], "body": GOOD_BODY}, {})
            raise AssertionError(path)

        return request, calls

    def _gate(self, request):
        import bridge_commands as runner
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "activation.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"enabled": True, "operation": "queue_issue", "repository": REPO,
                           "approved_by": "StevenMarketing"}, handle)
            gate = runner.live_gate_factory(request, "tok", REPO,
                                            processed={}, ledger=CommandLedger(),
                                            budget_guard=lambda op, i=None: {"allowed": True},
                                            activation_path=path)
            return gate(command(created_at=time.time()), trusted())

    def test_authorized_command_adds_label_once_and_reports_mutated(self):
        request, calls = self._request()
        out = self._gate(request)
        self.assertTrue(out["admitted"], out["reasons"])
        self.assertTrue(out["mutated"])
        self.assertEqual(out["code"], "completed")
        self.assertEqual(calls["post"], 1)

    def test_api_failure_fails_closed_without_mutated(self):
        request, calls = self._request(fail_post=True)
        out = self._gate(request)
        self.assertFalse(out["admitted"])
        self.assertFalse(out["mutated"])
        self.assertEqual(out["code"], "mutation_failed")
        self.assertEqual(calls["post"], 1)


if __name__ == "__main__":
    unittest.main()
