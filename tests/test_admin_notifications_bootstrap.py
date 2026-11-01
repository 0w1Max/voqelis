"""SQLite V1 announcement bootstrap rehearsal on synthetic preexisting databases.

This is not a production-backup restore rehearsal: it verifies that
initializing announcements writes only its own eighth SQLite file.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from voqelis.notifications.store import AnnouncementStore

LEGACY = (
    "planner", "notes", "day_facts", "access",
    "navigation", "todo", "operations",
)


def test_eighth_sqlite_does_not_mutate_seven_existing_databases(tmp_path: Path):
    before: dict[str, str] = {}
    for name in LEGACY:
        path = tmp_path / f"{name}.sqlite3"
        with sqlite3.connect(path) as db:
            db.execute(
                "CREATE TABLE legacy_sentinel (id INTEGER PRIMARY KEY, value TEXT)"
            )
            db.execute(
                "INSERT INTO legacy_sentinel(id,value) VALUES (?,?)",
                (1, f"untouched:{name}"),
            )
        before[name] = hashlib.sha256(path.read_bytes()).hexdigest()

    eighth = tmp_path / "admin_notifications.sqlite3"
    assert not eighth.exists()
    first = AnnouncementStore(eighth, owner_id=1)
    try:
        assert first.seed_suggestions(
            1, (("verified-module-v1", "Доступен проверенный модуль."),)
        ) == 1
        proposals = first.suggested(1)
        assert len(proposals) == 1 and proposals[0].state == "draft"
        assert first.summary(1, proposals[0].id)["pending"] == 0
        assert first.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert first.db.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        first.close()

    reopened = AnnouncementStore(eighth, owner_id=1)
    try:
        assert reopened.seed_suggestions(
            1, (("verified-module-v1", "Доступен проверенный модуль."),)
        ) == 0
        assert len(reopened.suggested(1)) == 1
        assert reopened.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        reopened.close()

    for name in LEGACY:
        path = tmp_path / f"{name}.sqlite3"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == before[name]
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
            assert db.execute(
                "SELECT value FROM legacy_sentinel WHERE id=1"
            ).fetchone() == (f"untouched:{name}",)
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    assert eighth.is_file()
