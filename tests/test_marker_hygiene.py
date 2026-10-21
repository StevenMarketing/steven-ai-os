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
    """Hardened migration: only the exact known legacy format is touched, backups verified
    before any write, and concurrent modification, symlinks and partial failure abort safely."""

    def setUp(self):
        self.store = tempfile.mkdtemp()
        self.backups = tempfile.mkdtemp()

    def _write(self, name, content):
        path = os.path.join(self.store, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        return path

    def _legacy(self, count=2):
        for index in range(1, count + 1):
            self._write("legacy%d.claim" % index, "claimed\n")

    # --- 1. only the exact known legacy format is migrated -------------------------------
    def test_malformed_json_missing_state_and_unknown_content_are_kept_and_reported(self):
        self._legacy(1)
        self._write("bad.claim", "{not json")
        self._write("nostate.claim", '{"value": "x"}')
        self._write("weird.claim", "[1, 2, 3]")                       # valid JSON, not an object
        summary = migrate(self.store, apply=True, backup_root=self.backups)
        self.assertEqual(summary["legacy"], 1)
        self.assertEqual(summary["migrated"], 1)
        self.assertEqual(summary["malformed"], 1)
        self.assertEqual(summary["json_without_state"], 1)
        self.assertEqual(summary["unknown_content"], 1)
        self.assertEqual(open(os.path.join(self.store, "bad.claim")).read(), "{not json")
        self.assertEqual(open(os.path.join(self.store, "nostate.claim")).read(), '{"value": "x"}')
        self.assertEqual(open(os.path.join(self.store, "weird.claim")).read(), "[1, 2, 3]")
        kinds = {p["kind"] for p in summary["problems"]}
        self.assertEqual(kinds, {"malformed", "json_without_state", "unknown_content"})

    def test_unreadable_marker_is_kept(self):
        path = self._write("blocked.claim", "claimed")
        os.chmod(path, 0)
        try:
            # UID 0 does not imply DAC override in restricted containers.
            try:
                with open(path, encoding="utf-8") as handle:
                    handle.read()
                readable = True
            except PermissionError:
                readable = False
            summary = migrate(self.store, apply=True, backup_root=self.backups)
            if readable:
                self.assertEqual(summary["migrated"], 1)
            else:
                self.assertEqual(summary["unreadable"], 1)
                self.assertEqual(summary["migrated"], 0)
            self.assertTrue(os.path.exists(path))
        finally:
            os.chmod(path, 0o600)

    def test_symlink_is_refused_and_its_target_untouched(self):
        outside = tempfile.mkdtemp()                      # target lives outside the store
        real = os.path.join(outside, "real.claim")
        with open(real, "w", encoding="utf-8") as handle:
            handle.write("claimed\n")
        link = os.path.join(self.store, "link.claim")
        os.symlink(real, link)
        summary = migrate(self.store, apply=True, backup_root=self.backups)
        self.assertEqual(summary["symlink"], 1)
        self.assertEqual(summary["migrated"], 0, summary)  # nothing else was migratable
        self.assertEqual(open(real).read(), "claimed\n")  # the target is untouched
        self.assertTrue(os.path.islink(link))

    # --- 2/3. backups verified before any write; concurrent change and partial failure ---
    def test_backup_verified_before_any_write_and_aborts_on_failure(self):
        self._legacy(2)
        import migrate_markers
        real_digest = migrate_markers.digest

        def flaky(path):
            if path.startswith(self.backups) and path.endswith("legacy2.claim"):
                return "0" * 64
            return real_digest(path)

        original = migrate_markers.digest
        migrate_markers.digest = flaky
        try:
            summary = migrate(self.store, apply=True, backup_root=self.backups)
        finally:
            migrate_markers.digest = original
        self.assertTrue(summary["aborted"])
        self.assertIn("backup verification failed", summary["aborted_reason"])
        self.assertEqual(summary["migrated"], 0)
        for name in ("legacy1.claim", "legacy2.claim"):
            self.assertEqual(open(os.path.join(self.store, name)).read(), "claimed\n")

    def test_concurrent_modification_aborts_without_writing(self):
        self._legacy(2)
        import migrate_markers
        real_digest = migrate_markers.digest
        calls = {"n": 0}

        def changing(path):
            if path.endswith("legacy2.claim") and not path.startswith(self.backups):
                calls["n"] += 1
                if calls["n"] >= 2:                # the pre-write re-check
                    return "f" * 64
            return real_digest(path)

        migrate_markers.digest = changing
        try:
            summary = migrate(self.store, apply=True, backup_root=self.backups)
        finally:
            migrate_markers.digest = real_digest
        self.assertTrue(summary["aborted"])
        self.assertIn("concurrent modification", summary["aborted_reason"])
        self.assertEqual(open(os.path.join(self.store, "legacy2.claim")).read(), "claimed\n")
        self.assertTrue(os.path.isdir(summary["backup"]))       # rollback material exists

    def test_partial_failure_stops_with_rollback_available(self):
        self._legacy(3)
        import migrate_markers
        real_replace = os.replace
        state = {"n": 0}

        def failing(src, dst):
            if dst.endswith(".claim"):
                state["n"] += 1
                if state["n"] == 2:
                    raise OSError("disk full")
            return real_replace(src, dst)

        migrate_markers.os.replace = failing
        try:
            summary = migrate(self.store, apply=True, backup_root=self.backups)
        finally:
            migrate_markers.os.replace = real_replace
        self.assertTrue(summary["aborted"])
        self.assertEqual(summary["migrated"], 1)
        self.assertIn("rollback: copy from", summary["aborted_reason"])
        self.assertEqual(len(os.listdir(summary["backup"])), 3)   # every file backed up first

    # --- existing guarantees -------------------------------------------------------------
    def test_dry_run_changes_nothing(self):
        self._legacy(2)
        before = sorted(os.listdir(self.store))
        summary = migrate(self.store, apply=False)
        self.assertEqual(summary["legacy"], 2)
        self.assertEqual(summary["migrated"], 0)
        self.assertEqual(before, sorted(os.listdir(self.store)))
        self.assertEqual(open(os.path.join(self.store, "legacy1.claim")).read().strip(), "claimed")

    def test_apply_migrates_only_the_known_format_and_never_deletes(self):
        self._legacy(2)
        self._write("modern.claim", json.dumps({"state": "confirmed", "value": "x"}))
        summary = migrate(self.store, apply=True, backup_root=self.backups)
        self.assertEqual(summary["migrated"], 2)
        self.assertEqual(summary["already_json"], 1)
        for name in ("legacy1.claim", "legacy2.claim"):
            document = json.load(open(os.path.join(self.store, name)))
            self.assertEqual(document["state"], "unknown")
            self.assertIn("legacy_plain_text_marker_migrated", document["reason"])
        self.assertEqual(len(os.listdir(self.store)), 3)
        self.assertEqual(json.load(open(os.path.join(self.store, "modern.claim")))["state"], "confirmed")

    def test_migration_is_idempotent(self):
        self._legacy(2)
        migrate(self.store, apply=True, backup_root=self.backups)
        second = migrate(self.store, apply=True, backup_root=self.backups)
        self.assertEqual(second["legacy"], 0)
        self.assertEqual(second["migrated"], 0)
        self.assertEqual(second["already_json"], 2)


if __name__ == "__main__":
    unittest.main()
