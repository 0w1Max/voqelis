"""Probe Notes V1 SQLite safely in a disposable directory on the VPS.

Reads no production databases and never touches Telegram or Planner.
Usage:
    PYTHONPATH=src python -m tools.notes_v1_smoke --work-dir /var/lib/voqelis
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

from voqelis.notes.store import NotesStore


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", required=True, type=Path)
    args = parser.parse_args()

    root = args.work_dir.resolve(strict=True)
    if not root.is_dir():
        parser.error("--work-dir must be a directory")

    with TemporaryDirectory(prefix="notes-v1-smoke-", dir=root) as scratch:
        db_path = Path(scratch) / "notes.sqlite3"
        store = NotesStore(db_path)
        try:
            text = store.create_text(1, "Проверить заметки и купить хлеб")
            store.arm_voice(1)
            token = store.claim_voice(1)
            assert token
            voice = store.capture_voice(
                1,
                "Сегодня хорошая погода.",
                original_text="сегодня хорошая погода",
                capture_token=token,
            )
            assert voice is not None
            assert not store.pending_voice(1)
            assert store.get(2, voice.id) is None
            assert len(store.search(1, "КУПИТЬ")) == 1
            assert len(store.recent(2)) == 0
            assert len(store.recent(1)) == 2
            assert store.get(1, text.id) == text
        finally:
            store.close()

        reopened = NotesStore(db_path)
        try:
            assert reopened.get(1, voice.id) == voice
            assert reopened.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            reopened.close()

        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as db:
            assert db.execute("SELECT count(*) FROM notes").fetchone()[0] == 2
        print("NOTES V1 SQLITE SMOKE: PASS")
        print("Isolated storage, read/write, restart, user isolation and integrity: OK")


if __name__ == "__main__":
    main()
