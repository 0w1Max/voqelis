"""Owner-only announcement outbox; separate from user reminders and diagnostics."""

from __future__ import annotations

import os
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

MAX_TEXT = 3500
MAX_RECIPIENTS = 5000
EDIT_TTL = 900
APPROVAL_TTL = 900

SCHEMA = """
CREATE TABLE IF NOT EXISTS announcements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 0,
    token TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'draft'
        CHECK(state IN ('draft','queued','paused','completed','cancelled')),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS announcement_inputs (
    owner_id INTEGER PRIMARY KEY,
    announcement_id INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS announcement_deliveries (
    announcement_id INTEGER NOT NULL REFERENCES announcements(id),
    recipient_id INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK(state IN ('pending','sending','sent','blocked','uncertain',
                        'failed','skipped')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY(announcement_id, recipient_id)
);
CREATE INDEX IF NOT EXISTS idx_announcement_due
    ON announcement_deliveries(state,next_attempt);
CREATE TABLE IF NOT EXISTS announcement_optouts (
    recipient_id INTEGER PRIMARY KEY,
    opted_out_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS announcement_proposals (
    owner_id INTEGER NOT NULL,
    proposal_key TEXT NOT NULL,
    announcement_id INTEGER NOT NULL UNIQUE REFERENCES announcements(id),
    created_at INTEGER NOT NULL,
    PRIMARY KEY(owner_id, proposal_key)
);
"""


@dataclass(frozen=True, slots=True)
class Announcement:
    id: int
    owner_id: int
    body: str
    revision: int
    token: str
    state: str


@dataclass(frozen=True, slots=True)
class Delivery:
    announcement_id: int
    recipient_id: int
    body: str
    attempts: int


def _ann(row: sqlite3.Row) -> Announcement:
    return Announcement(
        id=int(row["id"]), owner_id=int(row["owner_id"]),
        body=str(row["body"]), revision=int(row["revision"]),
        token=str(row["token"]), state=str(row["state"]),
    )


class AnnouncementStore:
    def __init__(self, path: Path, *, owner_id: int | None) -> None:
        self.owner_id = owner_id
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise RuntimeError("Announcement database symlink is forbidden")
        mask = os.umask(0o077)
        try:
            self.db = sqlite3.connect(path, timeout=5)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.executescript(SCHEMA)
            self.db.commit()
        finally:
            os.umask(mask)

    def close(self) -> None:
        self.db.close()

    def _admin(self, uid: int) -> bool:
        return self.owner_id is not None and uid == self.owner_id

    def get(self, uid: int, announcement_id: int) -> Announcement | None:
        if not self._admin(uid):
            return None
        row = self.db.execute(
            "SELECT * FROM announcements WHERE id=? AND owner_id=?",
            (announcement_id, uid),
        ).fetchone()
        return _ann(row) if row else None

    def latest(self, uid: int, limit: int = 10) -> list[Announcement]:
        if not self._admin(uid):
            return []
        rows = self.db.execute(
            "SELECT * FROM announcements WHERE owner_id=? "
            "ORDER BY id DESC LIMIT ?", (uid, min(20, max(1, limit))),
        ).fetchall()
        return [_ann(r) for r in rows]

    def seed_suggestions(
        self, uid: int, suggestions: tuple[tuple[str, str], ...],
        *, now: int | None = None,
    ) -> int:
        """Add curated, owner-only draft proposals exactly once per feature key.

        Never queues recipients or sends Telegram messages.
        """
        if not self._admin(uid):
            return 0
        if len(suggestions) > 20 or len({key for key, _ in suggestions}) != len(suggestions):
            raise ValueError("Invalid announcement suggestion catalog")
        for key, body in suggestions:
            if (
                not key or len(key) > 100
                or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for ch in key)
                or not isinstance(body, str) or not 1 <= len(body.strip()) <= MAX_TEXT
                or any(ord(ch) < 32 and ch not in "\n\t" for ch in body)
            ):
                raise ValueError("Invalid announcement suggestion")
        ts = int(time.time()) if now is None else now
        created = 0
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            for key, body in suggestions:
                exists = self.db.execute(
                    "SELECT 1 FROM announcement_proposals "
                    "WHERE owner_id=? AND proposal_key=?",
                    (uid, key),
                ).fetchone()
                if exists:
                    continue
                cur = self.db.execute(
                    "INSERT INTO announcements "
                    "(owner_id,body,revision,token,created_at,updated_at) "
                    "VALUES (?,?,1,?,?,?)",
                    (uid, body.strip(), secrets.token_hex(12), ts, ts),
                )
                self.db.execute(
                    "INSERT INTO announcement_proposals "
                    "(owner_id,proposal_key,announcement_id,created_at) "
                    "VALUES (?,?,?,?)",
                    (uid, key, int(cur.lastrowid), ts),
                )
                created += 1
        return created

    def suggested(self, uid: int, limit: int = 10) -> list[Announcement]:
        if not self._admin(uid):
            return []
        rows = self.db.execute(
            "SELECT a.* FROM announcements a "
            "JOIN announcement_proposals p ON p.announcement_id=a.id "
            "WHERE p.owner_id=? AND a.owner_id=? AND a.state='draft' "
            "ORDER BY a.id DESC LIMIT ?",
            (uid, uid, min(20, max(1, limit))),
        ).fetchall()
        return [_ann(row) for row in rows]

    def create(self, uid: int, *, now: int | None = None) -> Announcement | None:
        if not self._admin(uid):
            return None
        ts = int(time.time()) if now is None else now
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            cur = self.db.execute(
                "INSERT INTO announcements (owner_id,token,created_at,updated_at) "
                "VALUES (?,?,?,?)",
                (uid, secrets.token_hex(12), ts, ts),
            )
            self.db.execute(
                "INSERT INTO announcement_inputs (owner_id,announcement_id,expires_at)"
                " VALUES(?,?,?) ON CONFLICT(owner_id) DO UPDATE SET "
                "announcement_id=excluded.announcement_id,"
                "expires_at=excluded.expires_at",
                (uid, int(cur.lastrowid), ts + EDIT_TTL),
            )
        return self.get(uid, int(cur.lastrowid))

    def begin_edit(self, uid: int, announcement_id: int, *,
                   now: int | None = None) -> bool:
        draft = self.get(uid, announcement_id)
        if draft is None or draft.state != "draft":
            return False
        ts = int(time.time()) if now is None else now
        with self.db:
            self.db.execute(
                "INSERT INTO announcement_inputs (owner_id,announcement_id,expires_at)"
                " VALUES(?,?,?) ON CONFLICT(owner_id) DO UPDATE SET "
                "announcement_id=excluded.announcement_id,"
                "expires_at=excluded.expires_at",
                (uid, announcement_id, ts + EDIT_TTL),
            )
        return True

    def awaiting(self, uid: int, *, now: int | None = None) -> bool:
        if not self._admin(uid):
            return False
        ts = int(time.time()) if now is None else now
        return self.db.execute(
            "SELECT 1 FROM announcement_inputs i JOIN announcements a "
            "ON a.id=i.announcement_id AND a.owner_id=i.owner_id "
            "WHERE i.owner_id=? AND i.expires_at>? AND a.state='draft'",
            (uid, ts),
        ).fetchone() is not None

    def submit_text(self, uid: int, text: str, *,
                    now: int | None = None) -> Announcement | None:
        if not self._admin(uid):
            return None
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= MAX_TEXT:
            raise ValueError("Announcement must be between 1 and 3500 characters")
        if any(ord(c) < 32 and c not in "\n\t" for c in text):
            raise ValueError("Control characters are not allowed")
        ts = int(time.time()) if now is None else now
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT a.id FROM announcement_inputs i JOIN announcements a "
                "ON a.id=i.announcement_id AND a.owner_id=i.owner_id "
                "WHERE i.owner_id=? AND i.expires_at>? AND a.state='draft'",
                (uid, ts),
            ).fetchone()
            if row is None:
                return None
            aid = int(row["id"])
            self.db.execute(
                "UPDATE announcements SET body=?,revision=revision+1,token=?,"
                "updated_at=? WHERE id=? AND owner_id=? AND state='draft'",
                (text.strip(), secrets.token_hex(12), ts, aid, uid),
            )
            self.db.execute(
                "DELETE FROM announcement_inputs WHERE owner_id=?", (uid,),
            )
        return self.get(uid, aid)

    def refresh_preview(
        self, uid: int, aid: int, *, now: int | None = None,
    ) -> Announcement | None:
        """Refresh the one-shot approval button when the owner opens a preview."""
        if not self._admin(uid):
            return None
        ts = int(time.time()) if now is None else now
        with self.db:
            cur = self.db.execute(
                "UPDATE announcements SET token=?,updated_at=? "
                "WHERE id=? AND owner_id=? AND state='draft'",
                (secrets.token_hex(12), ts, aid, uid),
            )
        return self.get(uid, aid) if cur.rowcount == 1 else None

    def confirmation(
        self, uid: int, aid: int, revision: int, token: str,
        recipient_ids: list[int] | tuple[int, ...], *,
        now: int | None = None,
    ) -> int | None:
        """One-shot approval; recipient list captured transactionally."""
        if not self._admin(uid) or not isinstance(recipient_ids, (list, tuple)):
            return None
        if any(type(r) is not int or r <= 0 for r in recipient_ids):
            return None
        recipients = sorted(set(recipient_ids) - {uid})
        if not recipients or len(recipients) > MAX_RECIPIENTS:
            return None
        ts = int(time.time()) if now is None else now
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            cur = self.db.execute(
                "UPDATE announcements SET state='queued',updated_at=?,token=? "
                "WHERE id=? AND owner_id=? AND state='draft' "
                "AND revision=? AND token=? AND length(body)>0 "
                "AND updated_at>? AND updated_at<=?",
                (ts, secrets.token_hex(12), aid, uid, revision, token,
                 ts - APPROVAL_TTL, ts),
            )
            if cur.rowcount != 1:
                return None
            self.db.executemany(
                "INSERT INTO announcement_deliveries "
                "(announcement_id,recipient_id,updated_at) VALUES(?,?,?)",
                [(aid, recipient, ts) for recipient in recipients],
            )
        return len(recipients)

    def cancel(self, uid: int, aid: int) -> bool:
        if not self._admin(uid):
            return False
        with self.db:
            cur = self.db.execute(
                "UPDATE announcements SET state='cancelled' "
                "WHERE id=? AND owner_id=? AND state IN ('draft','queued','paused')",
                (aid, uid),
            )
            if cur.rowcount:
                self.db.execute(
                    "UPDATE announcement_deliveries SET state='skipped' "
                    "WHERE announcement_id=? AND state='pending'",
                    (aid,),
                )
                self.db.execute(
                    "DELETE FROM announcement_inputs WHERE owner_id=? AND "
                    "announcement_id=?", (uid, aid),
                )
        return cur.rowcount == 1

    def pause(self, uid: int, aid: int) -> bool:
        if not self._admin(uid):
            return False
        with self.db:
            cur = self.db.execute(
                "UPDATE announcements SET state='paused' "
                "WHERE id=? AND owner_id=? AND state='queued'",
                (aid, uid),
            )
        return cur.rowcount == 1

    def resume(self, uid: int, aid: int) -> bool:
        if not self._admin(uid):
            return False
        with self.db:
            cur = self.db.execute(
                "UPDATE announcements SET state='queued' "
                "WHERE id=? AND owner_id=? AND state='paused'",
                (aid, uid),
            )
        return cur.rowcount == 1

    def optout(self, uid: int, *, enabled: bool) -> None:
        if type(uid) is not int or uid <= 0:
            raise ValueError("Invalid Telegram user")
        with self.db:
            if enabled:
                self.db.execute(
                    "INSERT INTO announcement_optouts(recipient_id,opted_out_at) "
                    "VALUES(?,?) ON CONFLICT(recipient_id) DO UPDATE SET "
                    "opted_out_at=excluded.opted_out_at",
                    (uid, int(time.time())),
                )
            else:
                self.db.execute(
                    "DELETE FROM announcement_optouts WHERE recipient_id=?", (uid,),
                )

    def opted_out(self, uid: int) -> bool:
        return self.db.execute(
            "SELECT 1 FROM announcement_optouts WHERE recipient_id=?", (uid,),
        ).fetchone() is not None

    def recover(self) -> int:
        """An unknown send outcome is never retried automatically."""
        with self.db:
            cur = self.db.execute(
                "UPDATE announcement_deliveries SET state='uncertain' "
                "WHERE state='sending'",
            )
            self.db.execute(
                "UPDATE announcements SET state='completed' "
                "WHERE state IN ('queued','paused') AND NOT EXISTS "
                "(SELECT 1 FROM announcement_deliveries d WHERE "
                "d.announcement_id=announcements.id "
                "AND d.state IN ('pending','sending'))"
            )
        return cur.rowcount

    def claim(self, *, now: int | None = None) -> Delivery | None:
        ts = int(time.time()) if now is None else now
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT d.announcement_id,d.recipient_id,d.attempts,a.body "
                "FROM announcement_deliveries d JOIN announcements a "
                "ON a.id=d.announcement_id "
                "WHERE a.state='queued' AND d.state='pending' "
                "AND d.next_attempt<=? ORDER BY d.announcement_id,"
                "d.recipient_id LIMIT 1",
                (ts,),
            ).fetchone()
            if row is None:
                return None
            updated = self.db.execute(
                "UPDATE announcement_deliveries "
                "SET state='sending',attempts=attempts+1,updated_at=? "
                "WHERE announcement_id=? AND recipient_id=? AND state='pending'",
                (ts, row["announcement_id"], row["recipient_id"]),
            )
            if updated.rowcount != 1:
                return None
        return Delivery(
            announcement_id=int(row["announcement_id"]),
            recipient_id=int(row["recipient_id"]),
            body=str(row["body"]),
            attempts=int(row["attempts"]) + 1,
        )

    def delivery_state(self, aid: int, uid: int) -> str | None:
        row = self.db.execute(
            "SELECT state FROM announcement_deliveries "
            "WHERE announcement_id=? AND recipient_id=?", (aid, uid),
        ).fetchone()
        return str(row["state"]) if row else None

    def release_claim(self, aid: int, uid: int) -> bool:
        """Return a claimed message to queue before any Telegram request was made."""
        with self.db:
            cur = self.db.execute(
                "UPDATE announcement_deliveries SET "
                "state=CASE WHEN EXISTS "
                "(SELECT 1 FROM announcements a WHERE a.id=? "
                "AND a.state='cancelled') THEN 'skipped' ELSE 'pending' END,"
                "attempts=attempts-1 WHERE announcement_id=? "
                "AND recipient_id=? AND state='sending'",
                (aid, aid, uid),
            )
        return cur.rowcount == 1

    def finish_delivery(self, aid: int, uid: int, status: str) -> bool:
        if status not in {"sent", "blocked", "uncertain", "failed", "skipped"}:
            raise ValueError("Invalid delivery state")
        with self.db:
            cur = self.db.execute(
                "UPDATE announcement_deliveries SET state=?,updated_at=? "
                "WHERE announcement_id=? AND recipient_id=? AND state='sending'",
                (status, int(time.time()), aid, uid),
            )
            if cur.rowcount:
                self._complete_if_done(aid)
        return cur.rowcount == 1

    def defer(self, aid: int, uid: int, delay: int) -> bool:
        if not 1 <= delay <= 3600:
            raise ValueError("Invalid delay")
        with self.db:
            cur = self.db.execute(
                "UPDATE announcement_deliveries SET state='pending',next_attempt=?,"
                "updated_at=? WHERE announcement_id=? AND recipient_id=? "
                "AND state='sending' AND attempts<3",
                (int(time.time()) + delay, int(time.time()), aid, uid),
            )
        return cur.rowcount == 1

    def _complete_if_done(self, aid: int) -> None:
        waiting = self.db.execute(
            "SELECT 1 FROM announcement_deliveries "
            "WHERE announcement_id=? AND state IN ('pending','sending') LIMIT 1",
            (aid,),
        ).fetchone()
        if waiting is None:
            self.db.execute(
                "UPDATE announcements SET state='completed' "
                "WHERE id=? AND state IN ('queued','paused')", (aid,),
            )

    def summary(self, uid: int, aid: int) -> dict[str, int]:
        if self.get(uid, aid) is None:
            return {}
        result = {
            "pending": 0, "sending": 0, "sent": 0, "blocked": 0,
            "uncertain": 0, "failed": 0, "skipped": 0,
        }
        rows = self.db.execute(
            "SELECT state,COUNT(*) AS n FROM announcement_deliveries "
            "WHERE announcement_id=? GROUP BY state", (aid,),
        )
        for row in rows:
            result[str(row["state"])] = int(row["n"])
        return result
