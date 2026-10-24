"""Independent SQLite storage for explicit user-owned notes.

No Planner tables or schema changes. Voice capture is one-shot and expires.
"""

from __future__ import annotations

import sqlite3
import time
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

MAX_NOTE_CHARS = 12_000
CAPTURE_TTL_SECONDS = 15 * 60
MAX_PAGE = 50

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    body TEXT NOT NULL,
    original_text TEXT NOT NULL,
    source TEXT NOT NULL CHECK(source IN ('text', 'voice')),
    search_text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notes_user_id ON notes(user_id, id DESC);

CREATE TABLE IF NOT EXISTS note_voice_capture (
    user_id INTEGER PRIMARY KEY,
    expires_at INTEGER NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class Note:
    id: int
    user_id: int
    body: str
    original_text: str
    source: str
    created_at: str


def _normalized_search(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _validate_body(body: str) -> str:
    if not isinstance(body, str):
        raise ValueError("Note body must be text")
    clean = body.strip()
    if not clean or len(clean) > MAX_NOTE_CHARS:
        raise ValueError(f"Note must contain 1–{MAX_NOTE_CHARS} characters")
    return clean


def _note_from_row(row: sqlite3.Row) -> Note:
    return Note(
        id=int(row["id"]),
        user_id=int(row["user_id"]),
        body=row["body"],
        original_text=row["original_text"],
        source=row["source"],
        created_at=row["created_at"],
    )


class NotesStore:
    """Small single-process SQLite store, with atomic one-shot voice capture."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def _insert(
        self, user_id: int, body: str, original_text: str, source: str
    ) -> Note:
        created_at = datetime.now(UTC).isoformat(timespec="seconds")
        cursor = self.db.execute(
            "INSERT INTO notes "
            "(user_id, body, original_text, source, search_text, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                user_id, body, original_text, source,
                _normalized_search(body), created_at,
            ),
        )
        return Note(
            id=int(cursor.lastrowid),
            user_id=user_id,
            body=body,
            original_text=original_text,
            source=source,
            created_at=created_at,
        )

    def create_text(self, user_id: int, body: str) -> Note:
        validated = _validate_body(body)
        with self.db:
            return self._insert(user_id, validated, validated, "text")

    def arm_voice(self, user_id: int, *, now: int | None = None) -> None:
        current = int(time.time()) if now is None else now
        with self.db:
            self.db.execute(
                "INSERT INTO note_voice_capture(user_id, expires_at) VALUES (?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET expires_at=excluded.expires_at",
                (user_id, current + CAPTURE_TTL_SECONDS),
            )

    def cancel_voice(self, user_id: int) -> bool:
        with self.db:
            result = self.db.execute(
                "DELETE FROM note_voice_capture WHERE user_id=?",
                (user_id,),
            )
        return result.rowcount > 0

    def pending_voice(self, user_id: int, *, now: int | None = None) -> bool:
        current = int(time.time()) if now is None else now
        row = self.db.execute(
            "SELECT expires_at FROM note_voice_capture WHERE user_id=?",
            (user_id,),
        ).fetchone()
        return row is not None and int(row["expires_at"]) > current

    def capture_voice(
        self,
        user_id: int,
        body: str,
        *,
        original_text: str,
        now: int | None = None,
    ) -> Note | None:
        validated = _validate_body(body)
        original = _validate_body(original_text)
        current = int(time.time()) if now is None else now
        with self.db:
            # Reserve the database write transaction before checking one-shot state.
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT expires_at FROM note_voice_capture WHERE user_id=?",
                (user_id,),
            ).fetchone()
            if row is None or int(row["expires_at"]) <= current:
                return None
            note = self._insert(user_id, validated, original, "voice")
            self.db.execute(
                "DELETE FROM note_voice_capture WHERE user_id=?",
                (user_id,),
            )
            return note

    def get(self, user_id: int, note_id: int) -> Note | None:
        row = self.db.execute(
            "SELECT id, user_id, body, original_text, source, created_at "
            "FROM notes WHERE user_id=? AND id=?",
            (user_id, note_id),
        ).fetchone()
        return _note_from_row(row) if row is not None else None

    def recent(self, user_id: int, *, page: int = 1, page_size: int = 10) -> list[Note]:
        if not 1 <= page <= MAX_PAGE or not 1 <= page_size <= 20:
            raise ValueError("Invalid notes page")
        rows = self.db.execute(
            "SELECT id, user_id, body, original_text, source, created_at "
            "FROM notes WHERE user_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
            (user_id, page_size, (page - 1) * page_size),
        ).fetchall()
        return [_note_from_row(row) for row in rows]

    def search(self, user_id: int, query: str, *, limit: int = 10) -> list[Note]:
        needle = _normalized_search(query.strip())
        if not 2 <= len(needle) <= 100 or not 1 <= limit <= 20:
            raise ValueError("Search query must be 2–100 characters")
        rows = self.db.execute(
            "SELECT id, user_id, body, original_text, source, created_at "
            "FROM notes WHERE user_id=? AND instr(search_text, ?) > 0 "
            "ORDER BY id DESC LIMIT ?",
            (user_id, needle, limit),
        ).fetchall()
        return [_note_from_row(row) for row in rows]
