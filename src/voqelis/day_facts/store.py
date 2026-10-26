"""Independent SQLite-backed day-facts state machine.

One user -> at most one draft. State changes are transactional and token-checked,
so stale voice jobs and old callback buttons cannot mutate a newer draft.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import stat
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

from .sentences import MAX_INPUT_CHARS, split_explicit, split_sentences

Stage = Literal["event", "facts", "judgments", "review"]
STAGES = ("event", "facts", "judgments", "review")

SCHEMA = """
CREATE TABLE IF NOT EXISTS day_facts_entries (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    event TEXT NOT NULL,
    facts_json TEXT NOT NULL,
    judgments_json TEXT NOT NULL,
    raw_inputs_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_day_facts_user_day
    ON day_facts_entries(user_id, day, id);
CREATE TABLE IF NOT EXISTS day_facts_drafts (
    user_id INTEGER PRIMARY KEY,
    day TEXT NOT NULL,
    stage TEXT NOT NULL,
    token TEXT NOT NULL,
    event TEXT NOT NULL DEFAULT '',
    facts_json TEXT NOT NULL DEFAULT '[]',
    judgments_json TEXT NOT NULL DEFAULT '[]',
    raw_inputs_json TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL,
    edit_field TEXT,
    edit_index INTEGER
);
"""


@dataclass(frozen=True, slots=True)
class FactsEntry:
    id: int
    user_id: int
    day: date
    event: str
    facts: tuple[str, ...]
    judgments: tuple[str, ...]
    created_at: str


@dataclass(frozen=True, slots=True)
class FactsDraft:
    user_id: int
    day: date
    stage: Stage
    token: str
    event: str
    facts: tuple[str, ...]
    judgments: tuple[str, ...]
    edit_field: str | None = None
    edit_index: int | None = None


def _token() -> str:
    return secrets.token_hex(12)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _checked_text(text: str) -> str:
    clean = text.strip()
    if not clean or len(clean) > MAX_INPUT_CHARS:
        raise ValueError("Text must contain 1–12000 characters")
    return clean


class DayFactsStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        # This database contains sensitive personal journal entries. Create it
        # with owner-only permissions and reject symlink/directory targets.
        # SQLite's WAL/SHM files inherit the main database's secure mode.
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("Day Facts database must be a regular file")
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        self.db = sqlite3.connect(path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        # Existing SQLite databases may contain unfinished production drafts.
        # Only add nullable columns; never delete or recreate existing tables.
        columns = {
            row["name"]
            for row in self.db.execute("PRAGMA table_info(day_facts_drafts)")
        }
        for name, kind in (("edit_field", "TEXT"), ("edit_index", "INTEGER")):
            if name not in columns:
                self.db.execute(f"ALTER TABLE day_facts_drafts ADD COLUMN {name} {kind}")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    @staticmethod
    def _draft(row: sqlite3.Row) -> FactsDraft:
        return FactsDraft(
            user_id=row["user_id"], day=date.fromisoformat(row["day"]),
            stage=row["stage"], token=row["token"], event=row["event"],
            facts=tuple(json.loads(row["facts_json"])),
            judgments=tuple(json.loads(row["judgments_json"])),
            edit_field=row["edit_field"], edit_index=row["edit_index"],
        )

    def draft(self, user_id: int) -> FactsDraft | None:
        row = self.db.execute(
            "SELECT * FROM day_facts_drafts WHERE user_id=?", (user_id,)
        ).fetchone()
        return self._draft(row) if row else None

    def begin(self, user_id: int, day: date) -> FactsDraft:
        # Do not overwrite an unfinished daily record.
        existing = self.draft(user_id)
        if existing:
            return existing
        with self.db:
            self.db.execute(
                "INSERT INTO day_facts_drafts "
                "(user_id, day, stage, token, updated_at) VALUES (?, ?, 'event', ?, ?)",
                (user_id, day.isoformat(), _token(), _now()),
            )
        result = self.draft(user_id)
        assert result is not None
        return result

    def cancel(self, user_id: int) -> bool:
        with self.db:
            result = self.db.execute(
                "DELETE FROM day_facts_drafts WHERE user_id=?", (user_id,)
            )
        return result.rowcount > 0

    def accept(
        self, user_id: int, token: str, text: str, *, raw_text: str | None = None
    ) -> FactsDraft | None:
        clean = _checked_text(text)
        raw = _checked_text(raw_text if raw_text is not None else text)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT * FROM day_facts_drafts WHERE user_id=? AND token=?",
                (user_id, token),
            ).fetchone()
            if not row or (row["stage"] == "review" and row["edit_field"] is None):
                return None
            field = row["stage"]
            event = row["event"]
            facts = json.loads(row["facts_json"])
            judgments = json.loads(row["judgments_json"])
            originals = json.loads(row["raw_inputs_json"])
            edit_field = row["edit_field"]
            if edit_field is not None:
                index = row["edit_index"]
                if edit_field == "event":
                    if index != 1:
                        raise ValueError("Invalid event selection")
                    event = clean
                else:
                    items = facts if edit_field == "facts" else judgments
                    if index is None or not 1 <= index <= len(items):
                        raise ValueError("Selected item no longer exists")
                    # One edit replaces precisely one item, even when the
                    # punctuation model emits several sentences.
                    items[index - 1] = clean
                new_stage = field
                originals.append({
                    "field": edit_field, "action": "edit",
                    "index": index, "raw": raw, "visible": clean,
                })
            else:
                if field == "event":
                    event = clean
                    new_stage = "facts"
                elif field == "facts":
                    facts.extend(split_sentences(clean))
                    new_stage = "facts"
                else:
                    judgments.extend(split_sentences(clean))
                    new_stage = "judgments"
                originals.append({"field": field, "raw": raw, "visible": clean})
            self.db.execute(
                "UPDATE day_facts_drafts SET stage=?, token=?, event=?, "
                "facts_json=?, judgments_json=?, raw_inputs_json=?, updated_at=?, "
                "edit_field=NULL, edit_index=NULL "
                "WHERE user_id=? AND token=?",
                (
                    new_stage, _token(), event,
                    json.dumps(facts, ensure_ascii=False),
                    json.dumps(judgments, ensure_ascii=False),
                    json.dumps(originals, ensure_ascii=False),
                    _now(), user_id, token,
                ),
            )
        return self.draft(user_id)

    def next(self, user_id: int, token: str) -> FactsDraft | None:
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT * FROM day_facts_drafts WHERE user_id=? AND token=?",
                (user_id, token),
            ).fetchone()
            if row is None:
                return None
            if row["edit_field"] is not None:
                raise ValueError("Finish or cancel the selected edit")
            if row["stage"] == "facts" and json.loads(row["facts_json"]):
                stage = "judgments"
            elif row["stage"] == "judgments" and json.loads(row["judgments_json"]):
                stage = "review"
            else:
                raise ValueError("Finish the current section before continuing")
            self.db.execute(
                "UPDATE day_facts_drafts SET stage=?, token=?, updated_at=? "
                "WHERE user_id=? AND token=?",
                (stage, _token(), _now(), user_id, token),
            )
        return self.draft(user_id)

    def select_edit(
        self, user_id: int, token: str, field: str, index: int
    ) -> FactsDraft | None:
        """Select a single item for the next voice/text replacement."""
        if field not in {"event", "facts", "judgments"}:
            raise ValueError("Unknown section")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT * FROM day_facts_drafts WHERE user_id=? AND token=?",
                (user_id, token),
            ).fetchone()
            if row is None:
                return None
            if row["edit_field"] is not None:
                raise ValueError("Finish current edit first")
            if field == "event":
                if index != 1 or not row["event"]:
                    raise ValueError("Event not available for editing")
            else:
                if field == "facts" and row["stage"] == "event":
                    raise ValueError("Facts have not been dictated yet")
                if field == "judgments" and row["stage"] not in {"judgments", "review"}:
                    raise ValueError("Judgments have not been dictated yet")
                items = json.loads(row[f"{field}_json"])
                if not 1 <= index <= len(items):
                    raise ValueError("Item number not found")
            self.db.execute(
                "UPDATE day_facts_drafts SET edit_field=?, edit_index=?, "
                "token=?, updated_at=? WHERE user_id=? AND token=?",
                (field, index, _token(), _now(), user_id, token),
            )
        return self.draft(user_id)

    def cancel_edit(self, user_id: int, token: str) -> FactsDraft | None:
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT edit_field FROM day_facts_drafts WHERE user_id=? AND token=?",
                (user_id, token),
            ).fetchone()
            if row is None:
                return None
            if row["edit_field"] is None:
                raise ValueError("Nothing is being edited")
            self.db.execute(
                "UPDATE day_facts_drafts SET edit_field=NULL, edit_index=NULL, "
                "token=?, updated_at=? WHERE user_id=? AND token=?",
                (_token(), _now(), user_id, token),
            )
        return self.draft(user_id)

    def correct(
        self, user_id: int, token: str, field: str, index: int, text: str,
        *, split: bool = False,
    ) -> FactsDraft | None:
        if field not in {"event", "facts", "judgments"}:
            raise ValueError("Unknown field")
        pieces = split_explicit(text) if split else (_checked_text(text),)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT * FROM day_facts_drafts WHERE user_id=? AND token=?",
                (user_id, token),
            ).fetchone()
            if not row:
                return None
            if row["edit_field"] is not None:
                raise ValueError("Finish or cancel the selected edit")
            if field == "event":
                if index != 1 or split:
                    raise ValueError("Event is one text field")
                event = pieces[0]
                facts = json.loads(row["facts_json"])
                judgments = json.loads(row["judgments_json"])
            else:
                event = row["event"]
                facts = json.loads(row["facts_json"])
                judgments = json.loads(row["judgments_json"])
                items = facts if field == "facts" else judgments
                if not 1 <= index <= len(items):
                    raise ValueError("Item number not found")
                items[index - 1:index] = pieces
            self.db.execute(
                "UPDATE day_facts_drafts SET token=?, event=?, facts_json=?, "
                "judgments_json=?, updated_at=? WHERE user_id=? AND token=?",
                (
                    _token(), event, json.dumps(facts, ensure_ascii=False),
                    json.dumps(judgments, ensure_ascii=False), _now(), user_id, token,
                ),
            )
        return self.draft(user_id)

    def reset_section(self, user_id: int, token: str, field: str) -> FactsDraft | None:
        """Discard only the selected list and re-open its dictation step.

        Requires the current draft's token: previously queued voice messages
        and old callback buttons become stale after resetting a section.
        """
        if field not in {"facts", "judgments"}:
            raise ValueError("Only facts and judgments can be re-dictated")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT stage, raw_inputs_json, edit_field FROM day_facts_drafts "
                "WHERE user_id=? AND token=?",
                (user_id, token),
            ).fetchone()
            if row is None:
                return None
            if row["edit_field"] is not None:
                raise ValueError("Finish or cancel the selected edit")
            if field == "facts" and row["stage"] not in {"facts", "review"}:
                raise ValueError("Facts cannot be reset in this stage")
            if field == "judgments" and row["stage"] not in {"judgments", "review"}:
                raise ValueError("Judgments cannot be reset in this stage")
            originals = json.loads(row["raw_inputs_json"])
            originals.append({"field": field, "action": "reset"})
            column = "facts_json" if field == "facts" else "judgments_json"
            self.db.execute(
                f"UPDATE day_facts_drafts SET {column}='[]', stage=?, "
                "token=?, raw_inputs_json=?, updated_at=? "
                "WHERE user_id=? AND token=?",
                (
                    field, _token(), json.dumps(originals, ensure_ascii=False),
                    _now(), user_id, token,
                ),
            )
        return self.draft(user_id)

    def replace_section(
        self, user_id: int, token: str, field: str, text: str
    ) -> FactsDraft | None:
        """Replace the entire numbered list using corrected written sentences."""
        if field not in {"facts", "judgments"}:
            raise ValueError("Unknown section")
        clean = _checked_text(text)
        items = split_sentences(clean)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT stage, raw_inputs_json, edit_field FROM day_facts_drafts "
                "WHERE user_id=? AND token=?",
                (user_id, token),
            ).fetchone()
            if row is None:
                return None
            if row["edit_field"] is not None:
                raise ValueError("Finish or cancel the selected edit")
            if field == "facts" and row["stage"] not in {"facts", "review"}:
                raise ValueError("Facts cannot be replaced in this stage")
            if field == "judgments" and row["stage"] not in {"judgments", "review"}:
                raise ValueError("Judgments cannot be replaced in this stage")
            originals = json.loads(row["raw_inputs_json"])
            originals.append({"field": field, "action": "replace", "visible": clean})
            column = "facts_json" if field == "facts" else "judgments_json"
            self.db.execute(
                f"UPDATE day_facts_drafts SET {column}=?, token=?, "
                "raw_inputs_json=?, updated_at=? "
                "WHERE user_id=? AND token=?",
                (
                    json.dumps(items, ensure_ascii=False), _token(),
                    json.dumps(originals, ensure_ascii=False),
                    _now(), user_id, token,
                ),
            )
        return self.draft(user_id)

    def save(self, user_id: int, token: str) -> FactsEntry | None:
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT * FROM day_facts_drafts WHERE user_id=? AND token=? "
                "AND stage='review' AND edit_field IS NULL",
                (user_id, token),
            ).fetchone()
            if row is None:
                return None
            if not row["event"] or not json.loads(row["facts_json"]) or not json.loads(
                row["judgments_json"]
            ):
                raise ValueError("Record is incomplete")
            cursor = self.db.execute(
                "INSERT INTO day_facts_entries "
                "(user_id,day,event,facts_json,judgments_json,raw_inputs_json,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    user_id, row["day"], row["event"], row["facts_json"],
                    row["judgments_json"], row["raw_inputs_json"], _now(),
                ),
            )
            self.db.execute(
                "DELETE FROM day_facts_drafts WHERE user_id=? AND token=?",
                (user_id, token),
            )
            new_id = int(cursor.lastrowid)
        return self.get(user_id, new_id)

    def get(self, user_id: int, entry_id: int) -> FactsEntry | None:
        row = self.db.execute(
            "SELECT * FROM day_facts_entries WHERE user_id=? AND id=?",
            (user_id, entry_id),
        ).fetchone()
        return self._entry(row) if row else None

    @staticmethod
    def _entry(row: sqlite3.Row) -> FactsEntry:
        return FactsEntry(
            id=row["id"], user_id=row["user_id"],
            day=date.fromisoformat(row["day"]), event=row["event"],
            facts=tuple(json.loads(row["facts_json"])),
            judgments=tuple(json.loads(row["judgments_json"])),
            created_at=row["created_at"],
        )

    def for_day(self, user_id: int, day: date) -> list[FactsEntry]:
        rows = self.db.execute(
            "SELECT * FROM day_facts_entries WHERE user_id=? AND day=? ORDER BY id",
            (user_id, day.isoformat()),
        ).fetchall()
        return [self._entry(row) for row in rows]

    def history(self, user_id: int, limit: int = 12) -> list[tuple[str, int]]:
        if not 1 <= limit <= 50:
            raise ValueError("Bad limit")
        return [
            (str(row["day"]), int(row["n"]))
            for row in self.db.execute(
                "SELECT day,COUNT(*) AS n FROM day_facts_entries WHERE user_id=? "
                "GROUP BY day ORDER BY day DESC LIMIT ?",
                (user_id, limit),
            )
        ]
