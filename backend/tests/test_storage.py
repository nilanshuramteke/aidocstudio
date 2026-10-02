import sqlite3

from adstudio.core.ids import new_id
from adstudio.storage.db import Database, probe_capabilities


def test_capabilities_probe():
    caps = probe_capabilities()
    assert caps["fts5"] is True
    assert "sqlite" in caps


def test_migrate_and_idempotent(tmp_path):
    db = Database(tmp_path / "s.sqlite")
    assert db.migrate() >= 1
    v = db.user_version()
    assert db.migrate() == v  # no-op second time
    with db.read() as c:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"settings", "jobs", "audit_log"} <= tables
    assert db.integrity_ok()
    db.close()


def test_backup_before_migration(tmp_path, monkeypatch):
    from adstudio.storage import db as dbmod

    mdir = tmp_path / "mig"
    mdir.mkdir()
    (mdir / "001_a.sql").write_text("CREATE TABLE a(x TEXT);")
    monkeypatch.setattr(dbmod, "MIGRATIONS_DIR", mdir)
    db = Database(tmp_path / "s.sqlite")
    db.migrate(backup_dir=tmp_path / "b")
    assert not (tmp_path / "b").exists()  # fresh DB: nothing to back up
    with db.write() as c:
        c.execute("INSERT INTO a VALUES('keep')")
    (mdir / "002_b.sql").write_text("CREATE TABLE b(y TEXT);")
    assert db.migrate(backup_dir=tmp_path / "b") == 2
    backup = sqlite3.connect(tmp_path / "b" / "pre-migration-v1.sqlite")
    assert backup.execute("SELECT x FROM a").fetchone()[0] == "keep"
    backup.close()
    db.close()


def test_write_rolls_back_on_error(tmp_path):
    db = Database(tmp_path / "s.sqlite")
    db.migrate()
    try:
        with db.write() as c:
            c.execute("INSERT INTO settings(key,value_json) VALUES('a','1')")
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    with db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0
    db.close()


def test_strict_and_json_check(tmp_path):
    db = Database(tmp_path / "s.sqlite")
    db.migrate()
    try:
        with db.write() as c:
            c.execute("INSERT INTO settings(key,value_json) VALUES('a','not json')")
        raise AssertionError("expected CHECK failure")
    except sqlite3.IntegrityError:
        pass
    db.close()


def test_ids_sortable_unique():
    ids = [new_id() for _ in range(2000)]
    assert len(set(ids)) == 2000
    assert ids == sorted(ids)
    assert all(len(i) == 26 for i in ids)
