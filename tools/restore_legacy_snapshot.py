"""Explicit, one-time offline restore of the October 2026 legacy snapshot.

Stop site traffic/workers first. Never called by startup or the deploy webhook.
Uses stdlib only; verifies on a staging DB before touching the runtime DB.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from full_db_sync.engine import backup_database, import_snapshot, verify_snapshot

SNAPSHOT = ROOT / "LEGACY DATA" / "AMS_FULL_20261020-085604.compatible.amsdb"


def copy_database(source: Path, target: Path) -> None:
    src = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
    dst = sqlite3.connect(str(target), timeout=120)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def restore(source: Path, target: Path) -> dict:
    source, target = source.resolve(), target.resolve()
    if source == target:
        raise ValueError("Source and runtime database must differ")
    receipt = target.with_name(target.name + ".legacy-20261020-restored.json")
    if receipt.exists():
        raise ValueError(f"Already restored once; refusing to overwrite later transactions. Receipt: {receipt}")
    verified = verify_snapshot(source)
    if not verified["ok"]:
        raise ValueError(f"Snapshot verification failed: {verified['issues']}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="legacy-restore-", dir=target.parent) as tmp:
        staged = Path(tmp) / "staged.db"
        if target.exists():
            # Import only into a disposable copy; even an insert failure cannot
            # leave the real runtime DB empty or partially imported.
            copy_database(target, staged)
            imported = import_snapshot(source, staged, backup_target=False)
            if not imported["ok"] or imported["tables_only_source"]:
                raise ValueError(f"Staging import failed or would omit source tables: {imported}")
        else:
            copy_database(source, staged)
        check = verify_snapshot(staged, expect_meta=False)
        if not check["ok"]:
            raise ValueError(f"Staged verification failed: {check['issues']}")
        # Check exact original values, not merely row counts, before publish.
        original = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
        candidate = sqlite3.connect(str(staged))
        try:
            candidate.execute("ATTACH DATABASE ? AS original", (str(source),))
            for table in verified["tables"]:
                q = '"' + table.replace('"', '""') + '"'
                cols = [r[1] for r in original.execute(f"PRAGMA table_info({q})")]
                names = ','.join('"' + c.replace('"', '""') + '"' for c in cols)
                # SQLite EXCEPT compares typed values, including NULLs, in both directions.
                for left, right in (("main", "original"), ("original", "main")):
                    diff = candidate.execute(f"SELECT {names} FROM {left}.{q} EXCEPT SELECT {names} FROM {right}.{q} LIMIT 1").fetchone()
                    if diff is not None:
                        raise ValueError(f"Data differs in {table}; runtime untouched")
                count = candidate.execute(f"SELECT count(*) FROM {q}").fetchone()[0]
                if count != verified["tables"][table]:
                    raise ValueError(f"Row count differs in {table}")
        finally:
            original.close()
            candidate.close()
        backup = backup_database(target, prefix="pre_legacy_restore_") if target.exists() else None
        # SQLite online-backup destination is transactional, unlike file copy;
        # it also respects an existing target's SQLite journal/locking state.
        copy_database(staged, target)
    final = verify_snapshot(target, expect_meta=False)
    if not final["ok"]:
        raise RuntimeError(f"Post-restore verification failed; backup: {backup}")
    report = {
        "ok": True, "restored_at": datetime.now(timezone.utc).isoformat(),
        "source": source.name, "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "backup_path": str(backup) if backup else None,
        "total_rows": final["total_rows"], "tables": final["tables"],
        "integrity": final["integrity"], "fk_violations": final["fk_violations"],
    }
    receipt.write_text(json.dumps(report, indent=2) + "\n")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SNAPSHOT)
    parser.add_argument("--db", type=Path, default=ROOT / "instance/ahmed_cement_v44_fresh.db")
    parser.add_argument("--confirm-offline", action="store_true", help="Confirm all web/console writers are stopped and full data replacement is intended (users included)")
    args = parser.parse_args()
    if not args.confirm_offline:
        parser.error("Stop web/console writers first, then pass --confirm-offline (FULL REPLACEMENT)")
    try:
        report = restore(args.source, args.db)
    except Exception as exc:
        print(f"RESTORE FAILED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
