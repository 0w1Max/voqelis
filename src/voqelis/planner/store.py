from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path

from .config import PlannerConfig
from .models import DayReview, PlanItem, ReviewItem, ScheduleMove, TaskKind


SCHEMA = """
CREATE TABLE IF NOT EXISTS recurring_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    why TEXT,
    start_minute INTEGER NOT NULL,
    duration_minutes INTEGER NOT NULL,
    recurrence TEXT NOT NULL DEFAULT 'daily',
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS plan_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    title TEXT NOT NULL,
    why TEXT,
    start_minute INTEGER NOT NULL,
    end_minute INTEGER NOT NULL,
    kind TEXT NOT NULL,
    recurring_template_id INTEGER,
    urgent INTEGER NOT NULL DEFAULT 0,
    source_text TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_item_id INTEGER NOT NULL UNIQUE,
    status TEXT,
    activity TEXT,
    feelings TEXT,
    missed_reason TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS day_reviews (
    user_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    what_would_change TEXT,
    relapse_signs TEXT,
    completed INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, day)
);
CREATE TABLE IF NOT EXISTS planner_sessions (
    user_id INTEGER PRIMARY KEY,
    mode TEXT NOT NULL DEFAULT 'idle',
    state TEXT NOT NULL DEFAULT 'idle',
    target_day TEXT,
    payload TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);
"""


class PlannerStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self._migrate_sessions()
        self.db.commit()

    def _migrate_sessions(self) -> None:
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(planner_sessions)")}
        if "mode" not in columns:
            self.db.execute("ALTER TABLE planner_sessions ADD COLUMN mode TEXT NOT NULL DEFAULT 'idle'")
        if "state" not in columns:
            self.db.execute("ALTER TABLE planner_sessions ADD COLUMN state TEXT NOT NULL DEFAULT 'idle'")
        if "target_day" not in columns:
            self.db.execute("ALTER TABLE planner_sessions ADD COLUMN target_day TEXT")
        if "payload" not in columns:
            self.db.execute("ALTER TABLE planner_sessions ADD COLUMN payload TEXT NOT NULL DEFAULT '{}'")
        if "updated_at" not in columns:
            self.db.execute("ALTER TABLE planner_sessions ADD COLUMN updated_at TEXT")
        self.db.execute("UPDATE planner_sessions SET state=mode WHERE state IS NULL OR state=''")

    def close(self) -> None:
        self.db.close()

    def session(self, user_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM planner_sessions WHERE user_id=?", (user_id,)).fetchone()

    def session_payload(self, user_id: int) -> dict:
        row = self.session(user_id)
        if not row:
            return {}
        try:
            return json.loads(row["payload"] or "{}")
        except json.JSONDecodeError:
            return {}

    def set_session(self, user_id: int, state: str, target_day: date | None, payload: dict | None = None) -> None:
        now = datetime.utcnow().isoformat()
        self.db.execute(
            "INSERT INTO planner_sessions(user_id,mode,state,target_day,payload,updated_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET mode=excluded.mode,state=excluded.state,"
            "target_day=excluded.target_day,payload=excluded.payload,updated_at=excluded.updated_at",
            (user_id, state, state, target_day.isoformat() if target_day else None, json.dumps(payload or {}, ensure_ascii=False), now),
        )
        self.db.commit()

    def clear_session(self, user_id: int) -> None:
        self.db.execute("DELETE FROM planner_sessions WHERE user_id=?", (user_id,))
        self.db.commit()

    def recurring(self, user_id: int) -> list[sqlite3.Row]:
        return list(self.db.execute(
            "SELECT * FROM recurring_templates WHERE user_id=? AND active=1 ORDER BY start_minute,id",
            (user_id,),
        ))

    def seed_defaults(self, user_id: int, config: PlannerConfig) -> None:
        if self.recurring(user_id):
            return
        self.db.executemany(
            "INSERT INTO recurring_templates(user_id,title,why,start_minute,duration_minutes) VALUES(?,?,?,?,?)",
            [(user_id, x.title, x.why, x.start_minute, x.duration_minutes) for x in config.recurring_templates],
        )
        self.db.commit()

    def plan_items(self, user_id: int, day: date) -> list[PlanItem]:
        rows = self.db.execute(
            "SELECT * FROM plan_items WHERE user_id=? AND day=? ORDER BY start_minute,id",
            (user_id, day.isoformat()),
        ).fetchall()
        return [self._item(row) for row in rows]

    def get_plan_item(self, item_id: int) -> PlanItem:
        row = self.db.execute("SELECT * FROM plan_items WHERE id=?", (item_id,)).fetchone()
        if not row:
            raise KeyError(item_id)
        return self._item(row)

    def add_item(self, item: PlanItem) -> int:
        cur = self.db.execute(
            "INSERT INTO plan_items(user_id,day,title,why,start_minute,end_minute,kind,recurring_template_id,urgent,source_text,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (item.user_id, item.day.isoformat(), item.title, item.why, item.start_minute, item.end_minute,
             item.kind.value, item.recurring_template_id, int(item.urgent), item.source_text, datetime.utcnow().isoformat()),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def apply_moves_and_add(self, *, moves: tuple[ScheduleMove, ...], item: PlanItem) -> int:
        try:
            self.db.execute("BEGIN IMMEDIATE")

            move_ids = {move.plan_item_id for move in moves}
            existing_rows = self.db.execute(
                "SELECT * FROM plan_items WHERE user_id=? AND day=? ORDER BY start_minute,id",
                (item.user_id, item.day.isoformat()),
            ).fetchall()

            current_by_id = {}
            for row in existing_rows:
                current_by_id[int(row["id"])] = row

            for move in moves:
                row = current_by_id.get(move.plan_item_id)
                if row is None:
                    raise ValueError(f"Plan item {move.plan_item_id} no longer exists")
                if move.old_start_minute is not None and int(row["start_minute"]) != move.old_start_minute:
                    raise ValueError("A conflicting task changed before confirmation")
                if move.old_end_minute is not None and int(row["end_minute"]) != move.old_end_minute:
                    raise ValueError("A conflicting task changed before confirmation")

            def overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
                return a_start < b_end and b_start < a_end

            planned_ranges: dict[int, tuple[int, int]] = {}
            for row in existing_rows:
                item_id = int(row["id"])
                if item_id in move_ids:
                    continue
                planned_ranges[item_id] = (int(row["start_minute"]), int(row["end_minute"]))

            for move in moves:
                new_range = (move.new_start_minute, move.new_end_minute)
                if new_range[0] >= new_range[1]:
                    raise ValueError("A proposed move has an invalid time range")
                for other_start, other_end in planned_ranges.values():
                    if overlaps(*new_range, other_start, other_end):
                        raise ValueError("A proposed move collides with another scheduled task")
                for other_id, (other_start, other_end) in list(planned_ranges.items()):
                    if other_id in move_ids:
                        continue
                planned_ranges[move.plan_item_id] = new_range

            for move in moves:
                self.db.execute(
                    "UPDATE plan_items SET start_minute=?,end_minute=? WHERE id=?",
                    (move.new_start_minute, move.new_end_minute, move.plan_item_id),
                )

            for existing_id, existing_range in planned_ranges.items():
                if existing_id in move_ids:
                    continue
                if overlaps(
                    item.start_minute,
                    item.end_minute,
                    existing_range[0],
                    existing_range[1],
                ):
                    raise ValueError("The new task conflicts with another scheduled task")

            for first_id, first_range in planned_ranges.items():
                for second_id, second_range in planned_ranges.items():
                    if first_id >= second_id:
                        continue
                    if overlaps(*first_range, *second_range):
                        raise ValueError("Scheduled tasks would overlap after applying moves")

            cur = self.db.execute(
                "INSERT INTO plan_items(user_id,day,title,why,start_minute,end_minute,kind,recurring_template_id,urgent,source_text,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (item.user_id, item.day.isoformat(), item.title, item.why, item.start_minute, item.end_minute,
                 item.kind.value, item.recurring_template_id, int(item.urgent), item.source_text, datetime.utcnow().isoformat()),
            )
            self.db.commit()
            return int(cur.lastrowid)
        except Exception:
            self.db.rollback()
            raise

    def ensure_daily_plan(self, user_id: int, day: date, config: PlannerConfig | None = None) -> list[PlanItem]:
        existing = self.plan_items(user_id, day)
        if existing:
            return existing

        config = config or PlannerConfig()
        self.seed_defaults(user_id, config)
        rows = self.recurring(user_id)
        try:
            self.db.execute("BEGIN IMMEDIATE")
            for row in rows:
                start = int(row["start_minute"])
                end = start + int(row["duration_minutes"])
                self.db.execute(
                    "INSERT INTO plan_items(user_id,day,title,why,start_minute,end_minute,kind,recurring_template_id,urgent,source_text,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        user_id, day.isoformat(), row["title"], row["why"], start, end,
                        TaskKind.RECURRING.value, row["id"], 0, None, datetime.utcnow().isoformat(),
                    ),
                )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return self.plan_items(user_id, day)

    def save_review(self, plan_item_id: int, status: str, activity: str | None, feelings: tuple[str, ...], reason: str | None) -> None:
        self.db.execute(
            "INSERT INTO task_reviews(plan_item_id,status,activity,feelings,missed_reason,updated_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(plan_item_id) DO UPDATE SET status=excluded.status,activity=excluded.activity,"
            "feelings=excluded.feelings,missed_reason=excluded.missed_reason,updated_at=excluded.updated_at",
            (plan_item_id, status, activity, json.dumps(feelings, ensure_ascii=False), reason, datetime.utcnow().isoformat()),
        )
        self.db.commit()

    def reviews(self, user_id: int, day: date) -> list[ReviewItem]:
        rows = self.db.execute(
            "SELECT p.*,r.status,r.activity,r.feelings,r.missed_reason FROM plan_items p "
            "LEFT JOIN task_reviews r ON r.plan_item_id=p.id WHERE p.user_id=? AND p.day=? ORDER BY p.start_minute,p.id",
            (user_id, day.isoformat()),
        ).fetchall()
        result = []
        for row in rows:
            try:
                feelings = tuple(json.loads(row["feelings"] or "[]"))
            except json.JSONDecodeError:
                feelings = ()
            result.append(ReviewItem(self._item(row), row["status"], row["activity"], feelings, row["missed_reason"]))
        return result

    def save_day_review(self, user_id: int, day: date, what: str | None, signs: tuple[str, ...], completed: bool) -> None:
        self.db.execute(
            "INSERT INTO day_reviews(user_id,day,what_would_change,relapse_signs,completed,updated_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(user_id,day) DO UPDATE SET what_would_change=excluded.what_would_change,"
            "relapse_signs=excluded.relapse_signs,completed=excluded.completed,updated_at=excluded.updated_at",
            (user_id, day.isoformat(), what, json.dumps(signs, ensure_ascii=False), int(completed), datetime.utcnow().isoformat()),
        )
        self.db.commit()

    def day_review(self, user_id: int, day: date) -> DayReview | None:
        row = self.db.execute("SELECT * FROM day_reviews WHERE user_id=? AND day=?", (user_id, day.isoformat())).fetchone()
        if not row:
            return None
        try:
            signs = tuple(json.loads(row["relapse_signs"] or "[]"))
        except json.JSONDecodeError:
            signs = ()
        return DayReview(day, row["what_would_change"], signs, bool(row["completed"]))

    @staticmethod
    def _item(row: sqlite3.Row) -> PlanItem:
        return PlanItem(
            int(row["id"]), int(row["user_id"]), date.fromisoformat(row["day"]), row["title"], row["why"],
            int(row["start_minute"]), int(row["end_minute"]), TaskKind(row["kind"]),
            row["recurring_template_id"], bool(row["urgent"]), row["source_text"],
        )
