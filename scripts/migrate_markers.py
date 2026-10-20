#!/usr/bin/env python3
"""One-off, idempotent migration of KNOWN legacy plain-text claim markers to JSON.

The marker store began as plain files whose entire content is the word "claimed". Those
markers cannot be interpreted, so the gate reports them as `unknown` and asks for manual
review on every reconcile pass. This migration rewrites exactly those markers as JSON with an
explicit UNKNOWN state and a reason.

Safety properties (all enforced, all tested):
  * dry-run by default -- nothing is written unless --apply is passed;
  * ONLY the exact known legacy format is touched. Malformed JSON, JSON without a state
    field, unrecognised content, unreadable files and symlinks are left byte-for-byte as they
    are and reported explicitly;
  * every file scheduled for migration is copied to a timestamped backup directory AND the
    copy is verified by digest BEFORE the first write; if any backup fails verification the
    run aborts with nothing written;
  * concurrent modification is detected: size, mtime and digest are captured during the scan
    and re-checked immediately before each write; a changed file aborts the run;
  * a failure part-way through stops the run and reports exactly how many were migrated, and
    the verified backup keeps rollback possible;
  * markers are never deleted and never marked CONFIRMED -- the real outcome of an ancient
    command cannot be established, so UNKNOWN is the honest state;
  * idempotent -- markers already in JSON are untouched, so re-running is a no-op.
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
import time

DEFAULT_STORE = "/opt/data/ops/bridge/ledger-claims"
REASON = "legacy_plain_text_marker_migrated"
LEGACY_CONTENT = "claimed"


def digest(path):
    with open(path, "rb") as stream:
        return hashlib.sha256(stream.read()).hexdigest()


def classify(path):
    """Return (kind, detail). Only ('legacy', None) is ever migrated."""
    if os.path.islink(path):
        return "symlink", os.readlink(path)
    if not os.path.isfile(path):
        return "not_a_file", ""
    try:
        with open(path, "r", encoding="utf-8") as stream:
            raw = stream.read()
    except OSError as exc:
        return "unreadable", str(exc)
    if raw.strip() == LEGACY_CONTENT:
        return "legacy", None
    try:
        document = json.loads(raw)
    except Exception as exc:
        return "malformed", "%s: %s" % (type(exc).__name__, str(exc)[:60])
    if not isinstance(document, dict):
        return "unknown_content", type(document).__name__
    if not document.get("state"):
        return "json_without_state", "missing state field"
    return "already_json", str(document.get("state"))


def migrate(store, apply=False, backup_root=None, clock=time.time):
    summary = {"scanned": 0, "legacy": 0, "migrated": 0, "already_json": 0, "malformed": 0,
               "json_without_state": 0, "unknown_content": 0, "unreadable": 0, "symlink": 0,
               "not_a_file": 0, "aborted": False, "aborted_reason": "", "backup": None,
               "problems": []}
    if not os.path.isdir(store):
        return summary
    names = sorted(n for n in os.listdir(store) if n.endswith(".claim"))
    legacy = []
    for name in names:
        summary["scanned"] += 1
        path = os.path.join(store, name)
        kind, detail = classify(path)
        if kind == "legacy":
            summary["legacy"] += 1
            info = os.stat(path)
            legacy.append((name, info.st_size, info.st_mtime_ns, digest(path)))
        elif kind in summary:
            summary[kind] += 1
            if kind != "already_json":
                summary["problems"].append({"marker": name, "kind": kind, "detail": str(detail)[:80]})
        else:
            summary["problems"].append({"marker": name, "kind": kind, "detail": str(detail)[:80]})
    if not legacy or not apply:
        return summary

    # Phase 1: back up EVERY file and verify the copy before writing anything.
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(clock()))
    backup = os.path.join(backup_root or os.path.join(os.path.dirname(store), "backups"),
                          "markers-%s" % stamp)
    try:
        os.makedirs(backup, mode=0o700, exist_ok=True)
        for name, _size, _mtime, digest_value in legacy:
            source = os.path.join(store, name)
            target = os.path.join(backup, name)
            shutil.copy2(source, target)
            if digest(target) != digest_value:
                summary.update(aborted=True, aborted_reason="backup verification failed for %s" % name,
                               backup=backup)
                return summary
    except OSError as exc:
        summary.update(aborted=True, aborted_reason="backup failed: %s" % exc, backup=backup)
        return summary
    summary["backup"] = backup

    # Phase 2: write, re-checking for concurrent modification first.
    for name, size, mtime, digest_value in legacy:
        path = os.path.join(store, name)
        try:
            info = os.stat(path)
            if (info.st_size != size or info.st_mtime_ns != mtime or digest(path) != digest_value
                    or os.path.islink(path)):
                summary.update(aborted=True,
                               aborted_reason="concurrent modification detected on %s" % name)
                return summary
        except OSError as exc:
            summary.update(aborted=True, aborted_reason="cannot re-read %s: %s" % (name, exc))
            return summary
        document = {"state": "unknown", "value": os.path.splitext(name)[0], "reason": REASON,
                    "migrated_at": int(clock()), "attribution_uncertain": True}
        temporary = "%s.migrate-%d" % (path, os.getpid())
        try:
            with open(temporary, "w", encoding="utf-8") as stream:
                json.dump(document, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            with open(path, "r", encoding="utf-8") as stream:
                if json.load(stream).get("state") != "unknown":
                    raise ValueError("post-write verification failed")
        except Exception as exc:
            summary.update(aborted=True,
                           aborted_reason="write failed on %s: %s (rollback: copy from %s)"
                           % (name, str(exc)[:80], backup))
            return summary
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
        return 0
    for key in ("scanned", "legacy", "already_json", "malformed", "json_without_state",
                "unknown_content", "unreadable", "symlink", "not_a_file", "migrated"):
        print("%-20s %d" % (key, summary[key]))
    print("%-20s %s" % ("backup", summary["backup"]))
    print("%-20s %s" % ("aborted", summary["aborted"]))
    if summary["aborted"]:
        print("%-20s %s" % ("aborted_reason", summary["aborted_reason"]))
    for problem in summary["problems"][:10]:
        print("kept intact: %(marker)s (%(kind)s) %(detail)s" % problem)
    if not args.apply and summary["legacy"]:
        print("DRY-RUN: nothing written. Re-run with --apply to migrate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
