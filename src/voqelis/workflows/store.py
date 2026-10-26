"""Owner-isolated SQLite storage for immutable workflow definitions and drafts.

Not wired to Telegram yet. All writes are transactional and require the
authenticated owner. Historical template revisions cannot be overwritten.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import stat
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from .runtime import (
    WorkflowDraft,
    WorkflowRecord,
    advance,
    begin_edit,
    cancel_edit,
    confirm,
    start_workflow,
    submit,
)
from .spec import WorkflowSpec

_SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_templates (
    owner_id INTEGER NOT NULL,
    template_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (owner_id, template_id, version)
);
CREATE TABLE IF NOT EXISTS workflow_drafts (
    id TEXT PRIMARY KEY,
    owner_id INTEGER NOT NULL,
    template_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    token TEXT NOT NULL,
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (owner_id, template_id)
);
CREATE TABLE IF NOT EXISTS workflow_entries (
    id INTEGER PRIMARY KEY,
    owner_id INTEGER NOT NULL,
    template_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    values_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS workflow_entries_owner
    ON workflow_entries(owner_id, template_id, id);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _authorized_user(user_id: int) -> None:
    if type(user_id) is not int or user_id <= 0:
        raise ValueError("An authenticated owner ID is required")


def _serialize(draft: WorkflowDraft) -> str:
    return json.dumps(asdict(draft), ensure_ascii=False, separators=(",", ":"))


def _deserialize(text: str) -> WorkflowDraft:
    raw = json.loads(text)
    if not isinstance(raw, dict) or set(raw) != {
        "user_id", "template_id", "template_version", "template_fingerprint",
        "step", "stage", "values", "token", "edit",
    }:
        raise ValueError("Invalid persisted workflow state")
    if not isinstance(raw["values"], list) or len(raw["values"]) > 12:
        raise ValueError("Invalid workflow values")
    values = []
    for pair in raw["values"]:
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or not isinstance(pair[0], str)
            or not isinstance(pair[1], list)
            or not all(isinstance(item, str) for item in pair[1])
        ):
            raise ValueError("Invalid workflow field values")
        values.append((pair[0], tuple(pair[1])))
    selected = raw["edit"]
    if selected is not None:
        if (
            not isinstance(selected, list)
            or len(selected) != 2
            or not isinstance(selected[0], str)
            or type(selected[1]) is not int
        ):
            raise ValueError("Invalid workflow edit state")
        selected = (selected[0], selected[1])
    if (
        type(raw["user_id"]) is not int
        or type(raw["step"]) is not int
        or type(raw["template_version"]) is not int
        or not isinstance(raw["token"], str)
        or not isinstance(raw["template_id"], str)
        or not isinstance(raw["template_fingerprint"], str)
        or raw["stage"] not in {"collect", "review", "completed"}
    ):
        raise ValueError("Invalid workflow identifiers")
    return WorkflowDraft(
        user_id=raw["user_id"],
        template_id=raw["template_id"],
        template_version=raw["template_version"],
        template_fingerprint=raw["template_fingerprint"],
        step=raw["step"], stage=raw["stage"],
        values=tuple(values), token=raw["token"],
        edit=selected,
    )


class WorkflowStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("Workflow DB must be a regular file")
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        self.db = sqlite3.connect(path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(_SCHEMA)

    def close(self) -> None:
        self.db.close()

    def publish(self, owner_id: int, spec: WorkflowSpec) -> None:
        """Insert-only template version. A version cannot silently change."""
        _authorized_user(owner_id)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT fingerprint FROM workflow_templates WHERE "
                "owner_id=? AND template_id=? AND version=?",
                (owner_id, spec.id, spec.version),
            ).fetchone()
            if row:
                if row["fingerprint"] != spec.fingerprint:
                    raise ValueError("Template version is immutable")
                return
            self.db.execute(
                "INSERT INTO workflow_templates "
                "(owner_id,template_id,version,fingerprint,spec_json,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    owner_id, spec.id, spec.version,
                    spec.fingerprint, spec.canonical_json(), _now(),
                ),
            )

    def template(self, owner_id: int, template_id: str, version: int) -> WorkflowSpec | None:
        _authorized_user(owner_id)
        row = self.db.execute(
            "SELECT spec_json,fingerprint FROM workflow_templates "
            "WHERE owner_id=? AND template_id=? AND version=?",
            (owner_id, template_id, version),
        ).fetchone()
        if row is None:
            return None
        spec = WorkflowSpec.from_json(row["spec_json"])
        if spec.fingerprint != row["fingerprint"]:
            raise ValueError("Template integrity mismatch")
        return spec

    def _current(
        self, owner_id: int, draft_id: str
    ) -> tuple[WorkflowDraft, WorkflowSpec] | None:
        row = self.db.execute(
            "SELECT state_json,token,template_id,version FROM workflow_drafts "
            "WHERE id=? AND owner_id=?",
            (draft_id, owner_id),
        ).fetchone()
        if row is None:
            return None
        draft = _deserialize(row["state_json"])
        spec = self.template(owner_id, row["template_id"], row["version"])
        if (
            spec is None or draft.user_id != owner_id
            or row["token"] != draft.token
            or draft.template_id != row["template_id"]
            or draft.template_version != row["version"]
        ):
            raise ValueError("Persisted draft integrity mismatch")
        return draft, spec

    def begin(self, owner_id: int, template_id: str, version: int) -> tuple[str, WorkflowDraft]:
        _authorized_user(owner_id)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            spec = self.template(owner_id, template_id, version)
            if spec is None:
                raise ValueError("Template not available for owner")
            row = self.db.execute(
                "SELECT id,state_json,version FROM workflow_drafts "
                "WHERE owner_id=? AND template_id=?",
                (owner_id, template_id),
            ).fetchone()
            if row:
                if row["version"] != version:
                    raise ValueError("Finish existing draft before changing revision")
                restored = _deserialize(row["state_json"])
                return row["id"], restored
            draft = start_workflow(spec, owner_id)
            draft_id = secrets.token_hex(16)
            self.db.execute(
                "INSERT INTO workflow_drafts "
                "(id,owner_id,template_id,version,token,state_json,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    draft_id, owner_id, template_id, version,
                    draft.token, _serialize(draft), _now(),
                ),
            )
            return draft_id, draft

    def draft(self, owner_id: int, draft_id: str) -> WorkflowDraft | None:
        _authorized_user(owner_id)
        row = self._current(owner_id, draft_id)
        return row[0] if row is not None else None

    def _transition(
        self, owner_id: int, draft_id: str, token: str,
        action: Callable[[WorkflowDraft, WorkflowSpec], WorkflowDraft],
    ) -> WorkflowDraft:
        _authorized_user(owner_id)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            data = self._current(owner_id, draft_id)
            if data is None:
                raise ValueError("Draft not available for owner")
            draft, spec = data
            if token != draft.token:
                raise ValueError("Stale workflow operation")
            updated = action(draft, spec)
            cursor = self.db.execute(
                "UPDATE workflow_drafts SET state_json=?,token=?,updated_at=? "
                "WHERE id=? AND owner_id=? AND token=?",
                (
                    _serialize(updated), updated.token,
                    _now(), draft_id, owner_id, token,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("Concurrent workflow change")
            return updated

    def submit(
        self, owner_id: int, draft_id: str, token: str, items: tuple[str, ...]
    ) -> WorkflowDraft:
        return self._transition(
            owner_id, draft_id, token,
            lambda draft, spec: submit(draft, spec, owner_id, token, items),
        )

    def advance(self, owner_id: int, draft_id: str, token: str) -> WorkflowDraft:
        return self._transition(
            owner_id, draft_id, token,
            lambda draft, spec: advance(draft, spec, owner_id, token),
        )

    def begin_edit(
        self, owner_id: int, draft_id: str, token: str,
        field_key: str, item_index: int
    ) -> WorkflowDraft:
        return self._transition(
            owner_id, draft_id, token,
            lambda draft, spec: begin_edit(
                draft, spec, owner_id, token, field_key, item_index,
            ),
        )

    def cancel_edit(self, owner_id: int, draft_id: str, token: str) -> WorkflowDraft:
        return self._transition(
            owner_id, draft_id, token,
            lambda draft, spec: cancel_edit(draft, spec, owner_id, token),
        )

    def confirm(
        self, owner_id: int, draft_id: str, token: str
    ) -> tuple[int, WorkflowRecord]:
        _authorized_user(owner_id)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            data = self._current(owner_id, draft_id)
            if data is None:
                raise ValueError("Draft not available for owner")
            draft, spec = data
            _, record = confirm(draft, spec, owner_id, token)
            cursor = self.db.execute(
                "INSERT INTO workflow_entries "
                "(owner_id,template_id,version,fingerprint,values_json,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    owner_id, record.template_id, record.template_version,
                    record.template_fingerprint,
                    json.dumps(record.values, ensure_ascii=False), _now(),
                ),
            )
            deleted = self.db.execute(
                "DELETE FROM workflow_drafts WHERE id=? AND owner_id=? AND token=?",
                (draft_id, owner_id, token),
            )
            if deleted.rowcount != 1:
                raise ValueError("Concurrent workflow change")
            return int(cursor.lastrowid), record

    def records(
        self, owner_id: int, template_id: str, *, limit: int = 50
    ) -> list[tuple[int, WorkflowRecord]]:
        _authorized_user(owner_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid history limit")
        rows = self.db.execute(
            "SELECT id,version,fingerprint,values_json FROM workflow_entries "
            "WHERE owner_id=? AND template_id=? ORDER BY id DESC LIMIT ?",
            (owner_id, template_id, limit),
        ).fetchall()
        records = []
        for row in rows:
            spec = self.template(owner_id, template_id, row["version"])
            if spec is None or spec.fingerprint != row["fingerprint"]:
                raise ValueError("Historical template mismatch")
            entries = json.loads(row["values_json"])
            values = tuple((key, tuple(texts)) for key, texts in entries)
            records.append((
                int(row["id"]),
                WorkflowRecord(
                    owner_id, template_id, row["version"],
                    row["fingerprint"], values,
                ),
            ))
        return records
