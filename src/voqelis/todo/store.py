"""Private, transactional SQLite checklist; no due dates or Planner side effects."""

from __future__ import annotations

import os
import secrets
import sqlite3
import stat
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

MAX_TEXT_CHARS = 1000
CAPTURE_TTL_SECONDS = 15 * 60
DELETE_TTL_SECONDS = 2 * 60
PAGE_SIZE = 10
MAX_PAGE = 50

SCHEMA = """
CREATE TABLE IF NOT EXISTS todo_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    source TEXT NOT NULL CHECK(source IN ('voice', 'text')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_todo_user
    ON todo_items(user_id, deleted_at, completed_at, id DESC);
CREATE TABLE IF NOT EXISTS todo_captures (
    user_id INTEGER PRIMARY KEY,
    token TEXT NOT NULL,
    mode TEXT NOT NULL CHECK(mode IN ('add', 'edit')),
    item_id INTEGER,
    expires_at INTEGER NOT NULL,
    claimed INTEGER NOT NULL DEFAULT 0 CHECK(claimed IN (0, 1))
);
CREATE TABLE IF NOT EXISTS todo_deletions (
    user_id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL,
    token TEXT NOT NULL,
    expires_at INTEGER NOT NULL
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _text(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("Task must be text")
    clean = value.strip()
    if not clean or len(clean) > MAX_TEXT_CHARS:
        raise ValueError("Task must contain 1–1000 characters")
    return clean


@dataclass(frozen=True, slots=True)
class TodoItem:
    id: int
    user_id: int
    text: str
    source: str
    created_at: str
    updated_at: str
    completed_at: str | None


def _item(row: sqlite3.Row) -> TodoItem:
    return TodoItem(
        id=int(row["id"]), user_id=int(row["user_id"]),
        text=str(row["text"]), source=str(row["source"]),
        created_at=str(row["created_at"]), updated_at=str(row["updated_at"]),
        completed_at=row["completed_at"],
    )


@dataclass(frozen=True, slots=True)
class TodoCapture:
    token: str
    mode: str
    item_id: int | None
    expires_at: int
    claimed: bool


class TodoStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("Todo database must be a regular file")
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        self.db = sqlite3.connect(path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def get(self, user_id: int, item_id: int) -> TodoItem | None:
        if user_id <= 0 or item_id <= 0:
            return None
        row = self.db.execute(
            "SELECT * FROM todo_items WHERE user_id=? AND id=? "
            "AND deleted_at IS NULL", (user_id, item_id),
        ).fetchone()
        return _item(row) if row else None

    def list_items(
        self, user_id: int, *, completed: bool, page: int = 1
    ) -> tuple[list[TodoItem], int]:
        if user_id <= 0 or not 1 <= page <= MAX_PAGE:
            raise ValueError("Invalid page")
        condition = "IS NOT NULL" if completed else "IS NULL"
        total = self.db.execute(
            f"SELECT COUNT(*) FROM todo_items WHERE user_id=? "
            f"AND deleted_at IS NULL AND completed_at {condition}",
            (user_id,),
        ).fetchone()[0]
        rows = self.db.execute(
            f"SELECT * FROM todo_items WHERE user_id=? AND deleted_at IS NULL "
            f"AND completed_at {condition} ORDER BY id DESC LIMIT ? OFFSET ?",
            (user_id, PAGE_SIZE, (page - 1) * PAGE_SIZE),
        ).fetchall()
        return ([_item(row) for row in rows], int(total))

    def begin_capture(
        self, user_id: int, *, mode: str = "add", item_id: int | None = None,
        now: int | None = None,
    ) -> TodoCapture:
        if user_id <= 0 or mode not in {"add", "edit"}:
            raise ValueError("Invalid capture")
        if mode == "edit" and (item_id is None or self.get(user_id, item_id) is None):
            raise ValueError("Task not found")
        if mode == "add" and item_id is not None:
            raise ValueError("Unexpected item ID")
        current = int(time.time()) if now is None else now
        token = secrets.token_hex(16)
        with self.db:
            self.db.execute(
                "INSERT INTO todo_captures "
                "(user_id, token, mode, item_id, expires_at, claimed) "
                "VALUES (?, ?, ?, ?, ?, 0) ON CONFLICT(user_id) DO UPDATE SET "
                "token=excluded.token, mode=excluded.mode, item_id=excluded.item_id, "
                "expires_at=excluded.expires_at, claimed=0",
                (user_id, token, mode, item_id, current + CAPTURE_TTL_SECONDS),
            )
        return TodoCapture(token, mode, item_id, current + CAPTURE_TTL_SECONDS, False)

    def pending(self, user_id: int, *, now: int | None = None) -> TodoCapture | None:
        current = int(time.time()) if now is None else now
        row = self.db.execute(
            "SELECT * FROM todo_captures WHERE user_id=? AND expires_at>?",
            (user_id, current),
        ).fetchone()
        if row is None:
            return None
        return TodoCapture(
            str(row["token"]), str(row["mode"]), row["item_id"],
            int(row["expires_at"]), bool(row["claimed"]),
        )

    def claim_voice(self, user_id: int, *, now: int | None = None) -> str | None:
        """Atomically admit only one audio job per explicit capture."""
        current = int(time.time()) if now is None else now
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT token FROM todo_captures "
                "WHERE user_id=? AND claimed=0 AND expires_at>?",
                (user_id, current),
            ).fetchone()
            if row is None:
                return None
            token = str(row["token"])
            self.db.execute(
                "UPDATE todo_captures SET claimed=1 WHERE user_id=? AND token=?",
                (user_id, token),
            )
            return token

    def cancel_capture(self, user_id: int) -> bool:
        with self.db:
            cur = self.db.execute(
                "DELETE FROM todo_captures WHERE user_id=?", (user_id,),
            )
        return cur.rowcount > 0

    def _apply_capture(
        self, user_id: int, token: str, value: str, *,
        source: str, require_claimed: bool, now: int | None = None,
    ) -> TodoItem | None:
        clean = _text(value)
        current = int(time.time()) if now is None else now
        if source not in {"text", "voice"}:
            raise ValueError("Invalid source")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT * FROM todo_captures WHERE user_id=? AND token=?",
                (user_id, token),
            ).fetchone()
            if row is None or bool(row["claimed"]) != require_claimed:
                return None
            # A queued voice was accepted before expiry, so it may finish
            # after expiry. New text must still be submitted within the TTL.
            if not require_claimed and int(row["expires_at"]) <= current:
                return None
            timestamp = _now()
            if row["mode"] == "add":
                cur = self.db.execute(
                    "INSERT INTO todo_items "
                    "(user_id, text, source, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (user_id, clean, source, timestamp, timestamp),
                )
                item_id = int(cur.lastrowid)
            else:
                item_id = int(row["item_id"])
                changed = self.db.execute(
                    "UPDATE todo_items SET text=?, source=?, updated_at=? "
                    "WHERE user_id=? AND id=? AND deleted_at IS NULL",
                    (clean, source, timestamp, user_id, item_id),
                )
                if not changed.rowcount:
                    return None
            self.db.execute(
                "DELETE FROM todo_captures WHERE user_id=? AND token=?",
                (user_id, token),
            )
        return self.get(user_id, item_id)

    def capture_text(self, user_id: int, token: str, value: str) -> TodoItem | None:
        return self._apply_capture(
            user_id, token, value, source="text", require_claimed=False
        )

    def capture_voice(self, user_id: int, token: str, value: str) -> TodoItem | None:
        return self._apply_capture(
            user_id, token, value, source="voice", require_claimed=True
        )

    def set_completed(
        self, user_id: int, item_id: int, *, completed: bool
    ) -> TodoItem | None:
        item = self.get(user_id, item_id)
        if item is None:
            return None
        with self.db:
            self.db.execute(
                "UPDATE todo_items SET completed_at=?, updated_at=? "
                "WHERE user_id=? AND id=? AND deleted_at IS NULL",
                (_now() if completed else None, _now(), user_id, item_id),
            )
        return self.get(user_id, item_id)

    def ask_delete(
        self, user_id: int, item_id: int, *, now: int | None = None,
    ) -> str | None:
        if self.get(user_id, item_id) is None:
            return None
        current = int(time.time()) if now is None else now
        token = secrets.token_hex(12)
        with self.db:
            self.db.execute(
                "INSERT INTO todo_deletions(user_id, item_id, token, expires_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET "
                "item_id=excluded.item_id, token=excluded.token, "
                "expires_at=excluded.expires_at",
                (user_id, item_id, token, current + DELETE_TTL_SECONDS),
            )
        return token

    def confirm_delete(
        self, user_id: int, item_id: int, token: str, *, now: int | None = None,
    ) -> bool:
        current = int(time.time()) if now is None else now
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT item_id FROM todo_deletions "
                "WHERE user_id=? AND item_id=? AND token=? AND expires_at>?",
                (user_id, item_id, token, current),
            ).fetchone()
            if row is None:
                return False
            changed = self.db.execute(
                "UPDATE todo_items SET deleted_at=?, updated_at=? "
                "WHERE user_id=? AND id=? AND deleted_at IS NULL",
                (_now(), _now(), user_id, item_id),
            )
            self.db.execute(
                "DELETE FROM todo_deletions WHERE user_id=?", (user_id,),
            )
            self.db.execute(
                "DELETE FROM todo_captures "
                "WHERE user_id=? AND mode='edit' AND item_id=?",
                (user_id, item_id),
            )
        return changed.rowcount == 1

    def cancel_delete(self, user_id: int) -> None:
        with self.db:
            self.db.execute(
                "DELETE FROM todo_deletions WHERE user_id=?", (user_id,),
            )
