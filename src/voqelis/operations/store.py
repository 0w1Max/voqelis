"""Small privacy-preserving diagnostics and explicit user reports."""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import stat
import time
from dataclasses import dataclass
from pathlib import Path

WINDOWS = frozenset({10, 30, 60})
REPORT_TTL = 10 * 60
EVENT_RETENTION_SECONDS = 7 * 86400
REPORT_RETENTION_SECONDS = 30 * 86400
MAX_DESCRIPTION = 800
MAX_EVENTS = 50_000
ERROR_CODES = frozenset({
    "NONE", "HANDLER_EXCEPTION", "AUDIO_DECODE_FAILED",
    "ASR_EMPTY", "TELEGRAM_SEND_FAILED", "TELEGRAM_RATE_LIMIT",
    "WORKER_EXCEPTION",
})
EXPORT_ERRORS = frozenset({
    "APP_AUTH_FAILED", "APP_SCOPE_INVALID", "APP_TOKEN_INVALID",
    "GITHUB_REJECTED", "CREATE_OUTCOME_UNKNOWN",
    "RECONCILE_UNAVAILABLE", "MULTIPLE_ISSUES",
})

SCHEMA = """
CREATE TABLE IF NOT EXISTS ops_config (
    id INTEGER PRIMARY KEY CHECK(id=1),
    reporter_user_id INTEGER
);
INSERT OR IGNORE INTO ops_config(id, reporter_user_id) VALUES (1, NULL);
CREATE TABLE IF NOT EXISTS ops_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    section TEXT NOT NULL,
    operation TEXT NOT NULL CHECK(operation IN ('message', 'callback', 'audio')),
    outcome TEXT NOT NULL CHECK(outcome IN ('handled', 'failed', 'completed')),
    elapsed_ms INTEGER NOT NULL,
    error_code TEXT NOT NULL DEFAULT 'NONE'
);
CREATE INDEX IF NOT EXISTS idx_ops_events_user_time
    ON ops_events(user_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_ops_events_time ON ops_events(occurred_at);
CREATE TABLE IF NOT EXISTS ops_report_requests (
    user_id INTEGER PRIMARY KEY,
    token TEXT NOT NULL,
    description TEXT NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS ops_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    window_minutes INTEGER NOT NULL CHECK(window_minutes IN (10, 30, 60)),
    description TEXT NOT NULL,
    snapshot TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new' CHECK(status IN ('new', 'resolved'))
);
CREATE INDEX IF NOT EXISTS idx_ops_reports_user_time
    ON ops_reports(user_id, created_at);
CREATE TABLE IF NOT EXISTS ops_exports (
    report_id INTEGER PRIMARY KEY REFERENCES ops_reports(id),
    marker TEXT UNIQUE NOT NULL,
    token TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending','sending','uncertain','failed','sent')),
    issue_body TEXT NOT NULL,
    issue_number INTEGER,
    last_error TEXT NOT NULL DEFAULT '',
    updated_at INTEGER NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class Report:
    id: int
    user_id: int
    created_at: int
    window_minutes: int
    description: str
    snapshot: dict[str, int | dict[str, int]]
    status: str


@dataclass(frozen=True, slots=True)
class ExportTicket:
    report_id: int
    marker: str
    token: str
    expires_at: int
    state: str
    issue_body: str
    issue_number: int | None
    last_error: str


def _ticket(row: sqlite3.Row) -> ExportTicket:
    return ExportTicket(
        report_id=int(row["report_id"]),
        marker=str(row["marker"]),
        token=str(row["token"]),
        expires_at=int(row["expires_at"]),
        state=str(row["state"]),
        issue_body=str(row["issue_body"]),
        issue_number=(int(row["issue_number"]) if row["issue_number"] is not None else None),
        last_error=str(row["last_error"]),
    )


def _report(row: sqlite3.Row) -> Report:
    return Report(
        id=int(row["id"]),
        user_id=int(row["user_id"]),
        created_at=int(row["created_at"]),
        window_minutes=int(row["window_minutes"]),
        description=str(row["description"]),
        snapshot=json.loads(row["snapshot"]),
        status=str(row["status"]),
    )


class OperationsStore:
    def __init__(self, path: Path, *, owner_id: int | None) -> None:
        self.path = path
        self.owner_id = owner_id
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("Operations database must be a regular file")
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        self.db = sqlite3.connect(path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(ops_events)")}
        if "error_code" not in columns:
            with self.db:
                self.db.execute(
                    "ALTER TABLE ops_events "
                    "ADD COLUMN error_code TEXT NOT NULL DEFAULT 'NONE'"
                )
        # A request sent before a crash may have created an issue. Never retry blindly.
        with self.db:
            self.db.execute(
                "UPDATE ops_exports SET state='uncertain', "
                "last_error='CREATE_OUTCOME_UNKNOWN' WHERE state='sending'"
            )

    def close(self) -> None:
        self.db.close()

    def reporter_id(self) -> int | None:
        row = self.db.execute(
            "SELECT reporter_user_id FROM ops_config WHERE id=1"
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else None

    def eligible(self, user_id: int, *, approved: bool) -> bool:
        return (
            approved and self.owner_id is not None
            and (user_id == self.owner_id or user_id == self.reporter_id())
        )

    def set_reporter(
        self, actor_id: int, target_id: int | None, *, target_approved: bool,
    ) -> bool:
        if self.owner_id is None or actor_id != self.owner_id:
            return False
        if target_id is not None and (
            target_id <= 0 or target_id == self.owner_id or not target_approved
        ):
            return False
        with self.db:
            self.db.execute(
                "UPDATE ops_config SET reporter_user_id=? WHERE id=1",
                (target_id,),
            )
        return True

    def record(
        self, user_id: int, section: str, operation: str,
        outcome: str, elapsed_ms: int = 0, *, now: int | None = None,
        error_code: str = "NONE",
    ) -> None:
        if (
            user_id <= 0 or operation not in {"message", "callback", "audio"}
            or outcome not in {"handled", "failed", "completed"}
            or error_code not in ERROR_CODES
            or (outcome != "failed" and error_code != "NONE")
        ):
            raise ValueError("Invalid diagnostic event")
        when = int(time.time()) if now is None else now
        safe_section = section if section in {
            "home", "facts", "facts_capture", "todo", "todo_capture",
            "notes", "planner", "unknown",
        } else "unknown"
        with self.db:
            cur = self.db.execute(
                "INSERT INTO ops_events "
                "(occurred_at,user_id,section,operation,outcome,elapsed_ms,error_code) "
                "VALUES(?,?,?,?,?,?,?)",
                (when, user_id, safe_section, operation, outcome,
                 min(max(0, int(elapsed_ms)), 3_600_000), error_code),
            )
            if int(cur.lastrowid) % 100 == 0:
                self.db.execute(
                    "DELETE FROM ops_events WHERE occurred_at<?",
                    (when - EVENT_RETENTION_SECONDS,),
                )
                self.db.execute(
                    "DELETE FROM ops_events WHERE id <= "
                    "(SELECT MAX(id) - ? FROM ops_events)",
                    (MAX_EVENTS,),
                )

    def summary(
        self, user_id: int | None, window_minutes: int, *,
        now: int | None = None,
    ) -> dict[str, int]:
        if window_minutes not in WINDOWS:
            raise ValueError("Invalid time window")
        when = int(time.time()) if now is None else now
        where = "occurred_at>=?"
        params: tuple[int, ...] = (when - 60 * window_minutes,)
        if user_id is not None:
            where += " AND user_id=?"
            params = (*params, user_id)
        rows = self.db.execute(
            f"SELECT outcome, COUNT(*) AS n FROM ops_events WHERE {where} "
            "GROUP BY outcome", params,
        ).fetchall()
        counts = {"handled": 0, "completed": 0, "failed": 0}
        for row in rows:
            counts[row["outcome"]] = int(row["n"])
        return counts

    def classified_failures(
        self, user_id: int, window_minutes: int, *, now: int,
    ) -> dict[str, int]:
        if window_minutes not in WINDOWS:
            raise ValueError("Invalid time window")
        rows = self.db.execute(
            "SELECT error_code,COUNT(*) FROM ops_events WHERE user_id=? "
            "AND occurred_at>=? AND occurred_at<=? AND outcome='failed' "
            "AND error_code!='NONE' GROUP BY error_code",
            (user_id, now - window_minutes * 60, now),
        ).fetchall()
        return {
            str(code): int(count) for code, count in rows
            if code in ERROR_CODES and code != "NONE"
        }

    def begin_report(self, user_id: int, description: str = "") -> str:
        clean = "".join(c for c in description.strip() if c.isprintable())[:MAX_DESCRIPTION]
        token = secrets.token_hex(12)
        now = int(time.time())
        with self.db:
            self.db.execute(
                "INSERT INTO ops_report_requests(user_id,token,description,expires_at) "
                "VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                "token=excluded.token, description=excluded.description, "
                "expires_at=excluded.expires_at",
                (user_id, token, clean, now + REPORT_TTL),
            )
        return token

    def submit_report(
        self, user_id: int, token: str, window_minutes: int,
        *, now: int | None = None,
    ) -> Report | None:
        if window_minutes not in WINDOWS or user_id <= 0:
            return None
        when = int(time.time()) if now is None else now
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            pending = self.db.execute(
                "SELECT description FROM ops_report_requests "
                "WHERE user_id=? AND token=? AND expires_at>?",
                (user_id, token, when),
            ).fetchone()
            if pending is None:
                return None
            recent = self.db.execute(
                "SELECT COUNT(*) FROM ops_reports WHERE user_id=? AND created_at>?",
                (user_id, when - 3600),
            ).fetchone()[0]
            if recent >= 3:
                return None
            snapshot = self.summary(user_id, window_minutes, now=when)
            snapshot["error_codes"] = self.classified_failures(
                user_id, window_minutes, now=when,
            )
            cur = self.db.execute(
                "INSERT INTO ops_reports "
                "(user_id,created_at,window_minutes,description,snapshot) "
                "VALUES(?,?,?,?,?)",
                (user_id, when, window_minutes, pending["description"],
                 json.dumps(snapshot, ensure_ascii=False)),
            )
            report_id = int(cur.lastrowid)
            self.db.execute(
                "DELETE FROM ops_report_requests WHERE user_id=? AND token=?",
                (user_id, token),
            )
            self.db.execute(
                "DELETE FROM ops_reports WHERE created_at<?",
                (when - REPORT_RETENTION_SECONDS,),
            )
        return self.get_report(report_id)

    def get_report(self, report_id: int) -> Report | None:
        row = self.db.execute(
            "SELECT * FROM ops_reports WHERE id=?", (report_id,),
        ).fetchone()
        return _report(row) if row else None

    def reports(self, page: int = 1) -> list[Report]:
        if not 1 <= page <= 100:
            raise ValueError("Invalid page")
        rows = self.db.execute(
            "SELECT * FROM ops_reports ORDER BY id DESC LIMIT 5 OFFSET ?",
            ((page - 1) * 5,),
        ).fetchall()
        return [_report(row) for row in rows]

    def resolve(self, report_id: int) -> bool:
        with self.db:
            cur = self.db.execute(
                "UPDATE ops_reports SET status='resolved' "
                "WHERE id=? AND status='new'", (report_id,),
            )
        return cur.rowcount == 1


    def get_export(self, report_id: int) -> ExportTicket | None:
        row = self.db.execute(
            "SELECT * FROM ops_exports WHERE report_id=?", (report_id,),
        ).fetchone()
        return _ticket(row) if row else None

    def prepare_export(
        self, actor_id: int, report_id: int, revision: str, *,
        now: int | None = None,
    ) -> ExportTicket | None:
        """Owner-initiated preview: capture sanitized content once, never original comment."""
        from .export import build_issue_body

        if self.owner_id is None or actor_id != self.owner_id:
            return None
        when = int(time.time()) if now is None else now
        report = self.get_report(report_id)
        if report is None:
            return None
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            existing = self.get_export(report_id)
            if existing is None:
                marker = f"VQEXP-{secrets.token_hex(16)}"
                body = build_issue_body(report, marker, revision)
                self.db.execute(
                    "INSERT INTO ops_exports "
                    "(report_id,marker,token,expires_at,state,issue_body,updated_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (report_id, marker, secrets.token_hex(12), when + 600,
                     "pending", body, when),
                )
            elif existing.state in {"pending", "failed"}:
                self.db.execute(
                    "UPDATE ops_exports SET token=?,expires_at=?,state='pending',"
                    "last_error='',updated_at=? WHERE report_id=?",
                    (secrets.token_hex(12), when + 600, when, report_id),
                )
        return self.get_export(report_id)

    def claim_export(
        self, actor_id: int, report_id: int, token: str, *,
        now: int | None = None,
    ) -> ExportTicket | None:
        if self.owner_id is None or actor_id != self.owner_id:
            return None
        when = int(time.time()) if now is None else now
        with self.db:
            cur = self.db.execute(
                "UPDATE ops_exports SET state='sending',updated_at=? "
                "WHERE report_id=? AND token=? AND state='pending' AND expires_at>?",
                (when, report_id, token, when),
            )
        return self.get_export(report_id) if cur.rowcount == 1 else None

    def fail_export(self, report_id: int, marker: str, code: str, *,
                    uncertain: bool) -> bool:
        if code not in EXPORT_ERRORS:
            raise ValueError("Invalid export error classification")
        state = "uncertain" if uncertain else "failed"
        with self.db:
            cur = self.db.execute(
                "UPDATE ops_exports SET state=?,last_error=?,updated_at=? "
                "WHERE report_id=? AND marker=? AND state='sending'",
                (state, code, int(time.time()), report_id, marker),
            )
        return cur.rowcount == 1

    def sent_export(
        self, report_id: int, marker: str, number: int, *,
        reconcile: bool = False,
    ) -> bool:
        if type(number) is not int or number <= 0:
            raise ValueError("Invalid GitHub Issue number")
        status = "uncertain" if reconcile else "sending"
        with self.db:
            cur = self.db.execute(
                "UPDATE ops_exports SET state='sent',issue_number=?,"
                "last_error='',token='',expires_at=0,updated_at=? "
                "WHERE report_id=? AND marker=? AND state=?",
                (number, int(time.time()), report_id, marker, status),
            )
        return cur.rowcount == 1
