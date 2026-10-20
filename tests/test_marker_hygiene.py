"""Regression tests for marker hygiene: one-shot settlement and legacy migration."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from bridge_commands import LedgerSnapshot, _settle_ledger_for_execution  # noqa: E402
from migrate_markers import migrate                                      # noqa: E402

REPO = "StevenMarketing/steven-ai-os"
SOURCE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "scripts", "bridge_commands.py")


def envelope():
    return {"operation": "queue_issue", "issue": 57, "request_id": "req-h1",
            "idempotency_key": "idem-h1", "dry_run": False}


class OneShotSettlementTests(unittest.TestCase):
    """The one-shot --command path must settle the durable marker like process_comment does."""

    def setUp(self):
        self.store = tempfile.mkdtemp()
        self.snapshot = LedgerSnapshot(claims_dir=self.store)

    def _settle(self, result):
        payload = envelope()
        self.snapshot.claim(payload["idempotency_key"], payload["request_id"], issue=57)
        return payload, _settle_ledger_for_execution(self.snapshot, payload, {"result": result})

    def test_confirmed(self):
        payload, state = self._settle({"mutated": True,
                                       "receipt": {"mutation": {"attempted": True, "mutated": True}}})
        self.assertEqual(state, "confirmed")
        self.assertEqual(self.snapshot.pending_state(payload["idempotency_key"]), "confirmed")

    def test_unknown_when_attempted_but_unproven(self):
        _, state = self._settle({"mutated": False,
                                 "receipt": {"mutation": {"attempted": True, "mutated": False}}})
        self.assertEqual(state, "unknown")

    def test_failure_when_never_attempted(self):
        _, state = self._settle({"mutated": False,
                                 "receipt": {"admitted": False,
                                             "mutation": {"attempted": False, "mutated": False}}})
        self.assertEqual(state, "failed")

    def test_replay_is_idempotent_and_survives_a_restart(self):
        payload, state = self._settle({"mutated": True,
                                       "receipt": {"mutation": {"attempted": True, "mutated": True}}})
        self.assertEqual(state, "confirmed")
        _settle_ledger_for_execution(self.snapshot, payload, {"result": {"mutated": True,
                                  "receipt": {"mutation": {"attempted": True, "mutated": True}}}})
        restarted = LedgerSnapshot(claims_dir=self.store)
        self.assertEqual(restarted.pending_state(payload["idempotency_key"]), "confirmed")
        self.assertEqual(restarted.pending_state(payload["request_id"]), "confirmed")

    def test_one_shot_path_actually_calls_the_settlement(self):
        """Wiring guard: the call must exist inside the one-shot branch and be dry-run safe."""
        source = open(SOURCE, encoding="utf-8").read()
        self.assertIn('results[0]["execution"] = executor.execute(', source)
        tail = source.split('results[0]["execution"] = executor.execute(', 1)[1]
        window = tail[:600]
        self.assertIn("_settle_ledger_for_execution(", window)
        self.assertIn('if not validated.get("dry_run")', window)
        self.assertLess(window.index('if not validated.get("dry_run")'),
                        window.index("_settle_ledger_for_execution("))


class LegacyMigrationTests(unittest.TestCase):
    def setUp(self):
        self.store = tempfile.mkdtemp()
        self.backups = tempfile.mkdtemp()
        for index in (1, 2):
            with open(os.path.join(self.store, "legacy%d.claim" % index), "w", encoding="utf-8") as handle:
                handle.write("claimed\n")
        with open(os.path.join(self.store, "modern.claim"), "w", encoding="utf-8") as handle:
            json.dump({"state": "confirmed", "value": "x"}, handle)

    def _names(self):
        return sorted(os.listdir(self.store))

    def test_dry_run_changes_nothing(self):
        before = self._names()
        summary = migrate(self.store, apply=False)
        self.assertEqual(summary["legacy"], 2)
        self.assertEqual(summary["already_json"], 1)
        self.assertEqual(summary["migrated"], 0)
        self.assertEqual(before, self._names())
        self.assertEqual(open(os.path.join(self.store, "legacy1.claim")).read().strip(), "claimed")

    def test_apply_migrates_to_unknown_with_a_backup_and_never_deletes(self):
        summary = migrate(self.store, apply=True, backup_root=self.backups)
        self.assertEqual(summary["migrated"], 2)
        self.assertTrue(os.path.isdir(summary["backup"]))
        self.assertEqual(len(os.listdir(summary["backup"])), 2)
        for name in ("legacy1.claim", "legacy2.claim"):
            document = json.load(open(os.path.join(self.store, name)))
            self.assertEqual(document["state"], "unknown")
            self.assertNotEqual(document["state"], "confirmed")
            self.assertIn("legacy_plain_text_marker_migrated", document["reason"])
        self.assertEqual(len(self._names()), 3)          # nothing deleted

    def test_migration_is_idempotent(self):
        migrate(self.store, apply=True, backup_root=self.backups)
        second = migrate(self.store, apply=True, backup_root=self.backups)
        self.assertEqual(second["legacy"], 0)
        self.assertEqual(second["migrated"], 0)
        self.assertEqual(second["already_json"], 3)

    def test_unreadable_marker_is_reported_and_left_alone(self):
        os.makedirs(self.store, exist_ok=True)
        path = os.path.join(self.store, "blocked.claim")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("claimed")
        os.chmod(path, 0)
        try:
            summary = migrate(self.store, apply=True, backup_root=self.backups)
            if os.geteuid() != 0:
                self.assertEqual(summary["unreadable"], 1)
            self.assertTrue(os.path.exists(path))
        finally:
            os.chmod(path, 0o600)


if __name__ == "__main__":
    unittest.main()
