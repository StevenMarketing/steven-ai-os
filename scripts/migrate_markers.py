#!/usr/bin/env python3
"""One-off, idempotent migration of legacy plain-text claim markers to JSON.

The marker store began as plain files containing the word "claimed". Those markers cannot
be interpreted (the gate reports them as `unknown` and asks for manual review on every
reconcile pass). This migration rewrites them as JSON with an explicit UNKNOWN state and a
reason, so the store becomes readable again.

Safety properties:
  * dry-run by default -- nothing is written unless --apply is passed;
  * a timestamped backup copy of every marker is taken before the first write;
  * markers are never deleted, and a marker is never marked CONFIRMED here: the real
    outcome of an ancient command cannot be established, so UNKNOWN is the honest state;
  * idempotent -- markers already in JSON are left untouched, so re-running is a no-op.
"""
import argparse
import json
import os
import shutil
import sys
import time

DEFAULT_STORE = "/opt/data/ops/bridge/ledger-claims"
REASON = "legacy_plain_text_marker_migrated"


def classify(path):
    try:
        with open(path, "r", encoding="utf-8") as stream:
            raw = stream.read()
    except OSError as exc:
        return "unreadable", str(exc)
    try:
        document = json.loads(raw)
    except Exception:
        return "legacy", raw.strip()[:40]
    if isinstance(document, dict) and document.get("state"):
        return "already-json", document.get("state")
    return "legacy", raw.strip()[:40]


def migrate(store, apply=False, backup_root=None):
    summary = {"scanned": 0, "legacy": 0, "already_json": 0, "unreadable": 0,
               "migrated": 0, "backup": None}
    if not os.path.isdir(store):
        return summary
    names = sorted(n for n in os.listdir(store) if n.endswith(".claim"))
    legacy = []
    for name in names:
        summary["scanned"] += 1
        kind, _detail = classify(os.path.join(store, name))
        if kind == "legacy":
            summary["legacy"] += 1
            legacy.append(name)
        elif kind == "already-json":
            summary["already_json"] += 1
        else:
            summary["unreadable"] += 1
    if not legacy or not apply:
        return summary
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup = os.path.join(backup_root or os.path.join(os.path.dirname(store), "backups"),
                          "markers-%s" % stamp)
    os.makedirs(backup, mode=0o700, exist_ok=True)
    summary["backup"] = backup
    for name in legacy:
        path = os.path.join(store, name)
        shutil.copy2(path, os.path.join(backup, name))
        document = {"state": "unknown", "value": os.path.splitext(name)[0],
                    "reason": REASON, "migrated_at": int(time.time()),
                    "attribution_uncertain": True}
        temporary = path + ".migrate-%d" % os.getpid()
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump(document, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        summary["migrated"] += 1
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", default=DEFAULT_STORE)
    parser.add_argument("--apply", action="store_true", help="write the migration (default: dry-run)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    summary = migrate(args.store, apply=args.apply)
    if args.json:
        print(json.dumps(summary))
    else:
        print("markers scanned................ %d" % summary["scanned"])
        print("legacy plain-text markers...... %d" % summary["legacy"])
        print("already JSON................... %d" % summary["already_json"])
        print("unreadable..................... %d" % summary["unreadable"])
        print("migrated....................... %d" % summary["migrated"])
        print("backup......................... %s" % summary["backup"])
        if not args.apply and summary["legacy"]:
            print("DRY-RUN: nothing written. Re-run with --apply to migrate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
