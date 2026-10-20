"""The one-time restore must not damage a live target on failed imports."""
import sqlite3

import pytest

from full_db_sync.engine import export_snapshot
from tools.restore_legacy_snapshot import restore


def make_db(path, value="original", required_column=False):
    with sqlite3.connect(path) as con:
        con.executescript('''
            CREATE TABLE user (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE client (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE entry (id INTEGER PRIMARY KEY, value TEXT);
            INSERT INTO user VALUES (1, 'legacy-user');
            INSERT INTO client VALUES (1, 'legacy-client');
        ''')
        con.execute("INSERT INTO entry VALUES (1, ?)", (value,))
        if required_column:
            con.execute("ALTER TABLE entry ADD COLUMN new_required TEXT")


def test_restore_existing_target_backs_up_and_is_one_time(tmp_path):
    original, snapshot, target = [tmp_path / n for n in ("source.db", "snapshot.amsdb", "target.db")]
    make_db(original)
    export_snapshot(original, snapshot)
    make_db(target, value="new-server-data")
    before = snapshot.read_bytes()
    result = restore(snapshot, target)
    assert result["ok"]
    assert result["total_rows"] == 3
    assert result["fk_violations"] == 0
    with sqlite3.connect(target) as con:
        assert con.execute("SELECT value FROM entry").fetchone()[0] == "original"
    with sqlite3.connect(result["backup_path"]) as con:
        assert con.execute("SELECT value FROM entry").fetchone()[0] == "new-server-data"
    assert snapshot.read_bytes() == before
    with pytest.raises(ValueError, match="Already restored once"):
        restore(snapshot, target)


def test_failed_staging_leaves_runtime_untouched(tmp_path):
    original, snapshot, target = [tmp_path / n for n in ("source.db", "snapshot.amsdb", "target.db")]
    make_db(original)
    export_snapshot(original, snapshot)
    make_db(target, value="keep-me", required_column=True)
    before = target.read_bytes()
    with pytest.raises(Exception, match="columns the source lacks"):
        restore(snapshot, target)
    assert target.read_bytes() == before
    assert not list(tmp_path.glob("*.legacy-20261020-restored.json"))


def test_restore_new_runtime(tmp_path):
    original, snapshot, target = [tmp_path / n for n in ("source.db", "snapshot.amsdb", "instance/target.db")]
    make_db(original)
    export_snapshot(original, snapshot)
    result = restore(snapshot, target)
    assert result["ok"]
    assert result["total_rows"] == 3
    assert result["backup_path"] is None


def test_refuse_invalid_snapshot_before_creating_target(tmp_path):
    missing = tmp_path / "missing.amsdb"
    target = tmp_path / "target.db"
    with pytest.raises(ValueError, match="verification failed"):
        restore(missing, target)
    assert not target.exists()
