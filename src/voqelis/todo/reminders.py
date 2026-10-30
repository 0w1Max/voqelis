"""Durable, per-user reminder storage over the existing ToDo SQLite connection."""

from __future__ import annotations

import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .store import TodoStore

MAX_REMINDERS = 10
MAX_DELAY_SECONDS = 2 * 3600
DRAFT_TTL = 15 * 60


@dataclass(frozen=True, slots=True)
class Reminder:
    id: int
    user_id: int
    item_id: int
    remind_at_utc: int
    timezone: str
    state: str
    attempts: int


@dataclass(frozen=True, slots=True)
class ReminderDraft:
    item_id: int
    token: str
    proposed_at_utc: int | None
    timezone: str
    expires_at: int
    claimed: bool


def _reminder(row: sqlite3.Row) -> Reminder:
    return Reminder(
        id=int(row["id"]), user_id=int(row["user_id"]),
        item_id=int(row["item_id"]), remind_at_utc=int(row["remind_at_utc"]),
        timezone=str(row["timezone"]), state=str(row["state"]),
        attempts=int(row["attempts"]),
    )


class ReminderRepository:
    """Uses TodoStore's connection so task changes and cancellation are atomic."""

    def __init__(self, todo: TodoStore) -> None:
        self.todo = todo
        self.db = todo.db

    @staticmethod
    def _valid_timezone(value: str) -> None:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Invalid reminder timezone") from exc

    def for_item(self, user_id: int, item_id: int) -> list[Reminder]:
        if self.todo.get(user_id, item_id) is None:
            return []
        rows = self.db.execute(
            "SELECT * FROM todo_reminders WHERE user_id=? AND item_id=? "
            "ORDER BY remind_at_utc,id", (user_id, item_id),
        ).fetchall()
        return [_reminder(r) for r in rows]

    def get(self, user_id: int, reminder_id: int) -> Reminder | None:
        row = self.db.execute(
            "SELECT r.* FROM todo_reminders r JOIN todo_items t "
            "ON t.id=r.item_id AND t.user_id=r.user_id "
            "WHERE r.user_id=? AND r.id=? AND t.deleted_at IS NULL",
            (user_id, reminder_id),
        ).fetchone()
        return _reminder(row) if row else None

    def begin(
        self, user_id: int, item_id: int, *,
        timezone: str = "Europe/Moscow", now: int | None = None,
    ) -> ReminderDraft:
        self._valid_timezone(timezone)
        item = self.todo.get(user_id, item_id)
        if item is None or item.completed_at is not None:
            raise ValueError("Only active owned tasks can be reminded")
        ts = int(time.time()) if now is None else now
        with self.db:
            self.db.execute(
                "INSERT INTO todo_reminder_drafts "
                "(user_id,item_id,token,proposed_at_utc,timezone,expires_at) "
                "VALUES(?,?,?,NULL,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                "item_id=excluded.item_id,token=excluded.token,"
                "proposed_at_utc=NULL,timezone=excluded.timezone,"
                "expires_at=excluded.expires_at,claimed=0",
                (user_id, item_id, secrets.token_hex(12), timezone, ts + DRAFT_TTL),
            )
        draft = self.pending(user_id, now=ts)
        assert draft is not None
        return draft

    def pending(self, user_id: int, *, now: int | None = None) -> ReminderDraft | None:
        ts = int(time.time()) if now is None else now
        row = self.db.execute(
            "SELECT * FROM todo_reminder_drafts WHERE user_id=? AND expires_at>?",
            (user_id, ts),
        ).fetchone()
        if row is None:
            return None
        return ReminderDraft(
            item_id=int(row["item_id"]), token=str(row["token"]),
            proposed_at_utc=(
                int(row["proposed_at_utc"]) if row["proposed_at_utc"] is not None else None
            ),
            timezone=str(row["timezone"]), expires_at=int(row["expires_at"]),
            claimed=bool(row["claimed"]),
        )

    def claim_voice(self, user_id: int, token: str, *, now: int | None = None) -> bool:
        """Claim a single voice upload per live draft. Stale captures cannot write."""
        ts = int(time.time()) if now is None else now
        with self.db:
            cur = self.db.execute(
                "UPDATE todo_reminder_drafts SET claimed=1 "
                "WHERE user_id=? AND token=? AND claimed=0 AND expires_at>?",
                (user_id, token, ts),
            )
        return cur.rowcount == 1

    def release_voice(self, user_id: int, token: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE todo_reminder_drafts SET claimed=0 "
                "WHERE user_id=? AND token=?", (user_id, token),
            )

    def propose(
        self, user_id: int, token: str, when: datetime, *, now: int | None = None,
    ) -> ReminderDraft | None:
        if when.tzinfo is None:
            raise ValueError("UTC-aware time required")
        ts = int(time.time()) if now is None else now
        when_utc = int(when.astimezone(UTC).timestamp())
        if when_utc <= ts + 30 or when_utc > ts + 366 * 86400:
            raise ValueError("Date must be future and within one year")
        with self.db:
            cur = self.db.execute(
                "UPDATE todo_reminder_drafts SET proposed_at_utc=? "
                "WHERE user_id=? AND token=? AND expires_at>? "
                "AND EXISTS (SELECT 1 FROM todo_items "
                "WHERE id=todo_reminder_drafts.item_id AND user_id=? "
                "AND completed_at IS NULL AND deleted_at IS NULL)",
                (when_utc, user_id, token, ts, user_id),
            )
        return self.pending(user_id, now=ts) if cur.rowcount == 1 else None

    def confirm(
        self, user_id: int, token: str, *, now: int | None = None,
    ) -> Reminder | None:
        """Consume the confirmation token exactly once, including duplicate times."""
        ts = int(time.time()) if now is None else now
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT d.* FROM todo_reminder_drafts d JOIN todo_items t "
                "ON t.id=d.item_id AND t.user_id=d.user_id "
                "WHERE d.user_id=? AND d.token=? AND d.expires_at>? "
                "AND d.proposed_at_utc>? AND t.completed_at IS NULL "
                "AND t.deleted_at IS NULL",
                (user_id, token, ts, ts + 30),
            ).fetchone()
            if row is None:
                return None
            item_id = int(row["item_id"])
            count = self.db.execute(
                "SELECT COUNT(*) FROM todo_reminders WHERE user_id=? AND item_id=? "
                "AND state IN ('pending','sending','uncertain') "
                "AND remind_at_utc>?",
                (user_id, item_id, ts),
            ).fetchone()[0]
            if count >= MAX_REMINDERS:
                raise ValueError("Limit: 10 future reminders per task")
            try:
                cur = self.db.execute(
                    "INSERT INTO todo_reminders "
                    "(user_id,item_id,remind_at_utc,timezone,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (
                        user_id, item_id, int(row["proposed_at_utc"]),
                        str(row["timezone"]), ts,
                    ),
                )
                reminder_id = int(cur.lastrowid)
            except sqlite3.IntegrityError:
                # Existing identical reminder is not duplicated.
                reminder_id = 0
            self.db.execute(
                "DELETE FROM todo_reminder_drafts WHERE user_id=? AND token=?",
                (user_id, token),
            )
        return self.get(user_id, reminder_id) if reminder_id else None

    def cancel_draft(self, user_id: int) -> bool:
        with self.db:
            cur = self.db.execute(
                "DELETE FROM todo_reminder_drafts WHERE user_id=?", (user_id,),
            )
        return cur.rowcount == 1

    def cancel(self, user_id: int, reminder_id: int) -> bool:
        with self.db:
            cur = self.db.execute(
                "UPDATE todo_reminders SET state='cancelled' "
                "WHERE user_id=? AND id=? AND state='pending'",
                (user_id, reminder_id),
            )
        return cur.rowcount == 1

    def recover_inflight(self) -> int:
        """After restart do not blindly resend a possibly delivered message."""
        with self.db:
            cur = self.db.execute(
                "UPDATE todo_reminders SET state='uncertain' WHERE state='sending'"
            )
        return cur.rowcount

    def claim_due(self, *, now: int, limit: int = 10) -> list[Reminder]:
        if not 1 <= limit <= 50:
            raise ValueError("Invalid batch size")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(
                "UPDATE todo_reminders SET state='missed' "
                "WHERE state='pending' AND remind_at_utc<?",
                (now - MAX_DELAY_SECONDS,),
            )
            rows = self.db.execute(
                "SELECT r.id,r.user_id FROM todo_reminders r JOIN todo_items t "
                "ON t.id=r.item_id AND t.user_id=r.user_id "
                "WHERE r.state='pending' AND r.remind_at_utc<=? "
                "AND t.completed_at IS NULL AND t.deleted_at IS NULL "
                "ORDER BY r.remind_at_utc,r.id LIMIT ?",
                (now, limit),
            ).fetchall()
            items = []
            for row in rows:
                cur = self.db.execute(
                    "UPDATE todo_reminders SET state='sending',attempts=attempts+1,"
                    "claimed_at=? WHERE id=? AND state='pending'",
                    (now, row["id"]),
                )
                if cur.rowcount:
                    items.append(self.get(int(row["user_id"]), int(row["id"])))
        return [item for item in items if item is not None]

    def finish_send(self, reminder_id: int, *, sent: bool, uncertain: bool = False,
                    now: int | None = None) -> bool:
        state = "sent" if sent else "uncertain" if uncertain else "failed"
        ts = int(time.time()) if now is None else now
        with self.db:
            cur = self.db.execute(
                "UPDATE todo_reminders SET state=?,delivered_at=? "
                "WHERE id=? AND state='sending'",
                (state, ts if sent else None, reminder_id),
            )
        return cur.rowcount == 1
