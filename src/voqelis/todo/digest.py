"""Durable, per-user daily summary schedules and at-most-once delivery claims."""

from __future__ import annotations

import json
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .settings import TodoSettingsRepository
from .store import TodoItem, TodoStore

DEFAULT_MINUTE = 16 * 60
MAX_TIMES = 5
INPUT_TTL = 10 * 60
DELIVERY_WINDOW = 10 * 60
_HHMM = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")


def clock(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def parse_clock(text: str) -> int:
    matched = _HHMM.fullmatch(text.strip())
    if matched is None:
        raise ValueError("Напиши время в формате ЧЧ:ММ, например 15:30.")
    return int(matched[1]) * 60 + int(matched[2])


@dataclass(frozen=True, slots=True)
class DigestPreferences:
    enabled: bool
    times: tuple[int, ...]
    revision: int


@dataclass(frozen=True, slots=True)
class DigestInput:
    token: str
    nav_revision: int
    digest_revision: int


@dataclass(frozen=True, slots=True)
class DueDigest:
    user_id: int
    day: str
    minute: int
    timezone: str
    items: tuple[TodoItem, ...]
    count: int


class TodoDigestRepository:
    """Lives on the TodoStore connection; no independent scheduler DB."""

    def __init__(self, todo: TodoStore, timezone_settings: TodoSettingsRepository) -> None:
        self.todo = todo
        self.db = todo.db
        self.timezones = timezone_settings

    def get(self, uid: int) -> DigestPreferences:
        if uid <= 0:
            raise ValueError("Invalid user")
        row = self.db.execute(
            "SELECT enabled, times_json, revision FROM todo_digest_settings WHERE user_id=?",
            (uid,),
        ).fetchone()
        if row is None:
            return DigestPreferences(True, (DEFAULT_MINUTE,), 0)
        values = tuple(json.loads(row["times_json"]))
        if (
            not isinstance(row["enabled"], int)
            or len(values) > MAX_TIMES
            or any(type(m) is not int or not 0 <= m < 1440 for m in values)
            or tuple(sorted(set(values))) != values
        ):
            raise ValueError("Invalid persisted daily digest configuration")
        return DigestPreferences(bool(row["enabled"]), values, int(row["revision"]))

    def _change(
        self, uid: int, expected_revision: int, *,
        action: str, minute: int | None = None,
    ) -> DigestPreferences | None:
        if uid <= 0 or expected_revision < 0:
            raise ValueError("Invalid update")
        if action in {"add", "remove"} and (
            minute is None or type(minute) is not int or not 0 <= minute < 1440
        ):
            raise ValueError("Invalid time")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            current = self.get(uid)
            if current.revision != expected_revision:
                return None
            times = list(current.times)
            enabled = current.enabled
            if action == "toggle":
                enabled = not enabled
                if enabled and not times:
                    times = [DEFAULT_MINUTE]
            elif action == "add":
                if minute in times:
                    raise ValueError("Это время уже добавлено.")
                if len(times) >= MAX_TIMES:
                    raise ValueError("Можно настроить не более пяти времён.")
                times.append(minute)
                times.sort()
                enabled = True
            elif action == "remove":
                if minute not in times:
                    raise ValueError("Это время уже удалено.")
                times.remove(minute)
                if not times:
                    enabled = False
            else:
                raise ValueError("Unknown operation")
            self.db.execute(
                "INSERT INTO todo_digest_settings(user_id, enabled, times_json, revision) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET "
                "enabled=excluded.enabled, times_json=excluded.times_json, "
                "revision=excluded.revision",
                (uid, int(enabled), json.dumps(times), expected_revision + 1),
            )
            self.db.execute("DELETE FROM todo_digest_input WHERE user_id=?", (uid,))
        return self.get(uid)

    def toggle(self, uid: int, expected_revision: int) -> DigestPreferences | None:
        return self._change(uid, expected_revision, action="toggle")

    def add(self, uid: int, expected_revision: int, minute: int) -> DigestPreferences | None:
        return self._change(uid, expected_revision, action="add", minute=minute)

    def remove(self, uid: int, expected_revision: int, minute: int) -> DigestPreferences | None:
        return self._change(uid, expected_revision, action="remove", minute=minute)

    def begin_input(self, uid: int, *, nav_revision: int, digest_revision: int) -> None:
        if uid <= 0 or nav_revision < 0 or self.get(uid).revision != digest_revision:
            raise ValueError("Stale settings")
        with self.db:
            self.db.execute(
                "INSERT INTO todo_digest_input "
                "(user_id, token, navigation_revision, digest_revision, expires_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET "
                "token=excluded.token, navigation_revision=excluded.navigation_revision, "
                "digest_revision=excluded.digest_revision, expires_at=excluded.expires_at",
                (uid, secrets.token_hex(12), nav_revision, digest_revision,
                 int(time.time()) + INPUT_TTL),
            )
            # Reminder text mode and digest text mode are mutually exclusive.
            self.db.execute("DELETE FROM todo_reminder_drafts WHERE user_id=?", (uid,))

    def cancel_input(self, uid: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM todo_digest_input WHERE user_id=?", (uid,))

    def pending_input(self, uid: int, *, now: int | None = None) -> DigestInput | None:
        ts = int(time.time()) if now is None else now
        row = self.db.execute(
            "SELECT token, navigation_revision, digest_revision "
            "FROM todo_digest_input WHERE user_id=? AND expires_at>?",
            (uid, ts),
        ).fetchone()
        if row is None:
            return None
        return DigestInput(str(row["token"]), int(row["navigation_revision"]),
                           int(row["digest_revision"]))

    def submit_input(
        self, uid: int, token: str, nav_revision: int, text: str,
    ) -> DigestPreferences | None:
        minute = parse_clock(text)
        pending = self.pending_input(uid)
        if (
            pending is None or pending.token != token
            or pending.nav_revision != nav_revision
        ):
            return None
        # _change checks revision in BEGIN IMMEDIATE and removes the input atomically.
        return self.add(uid, pending.digest_revision, minute)

    def recover_inflight(self) -> int:
        with self.db:
            cur = self.db.execute(
                "UPDATE todo_digest_delivery SET state='uncertain' WHERE state='sending'",
            )
        return cur.rowcount

    def claim_due(
        self, approved_ids: frozenset[int], *, now: int | None = None, limit: int = 5,
    ) -> list[DueDigest]:
        ts = int(time.time()) if now is None else now
        if limit < 1 or limit > 20:
            raise ValueError("Invalid batch size")
        results: list[DueDigest] = []
        for uid in sorted(approved_ids):
            if uid <= 0:
                continue
            prefs = self.get(uid)
            if not prefs.enabled:
                continue
            zone_name = self.timezones.get(uid).timezone
            zone = ZoneInfo(zone_name)
            local = datetime.fromtimestamp(ts, zone)
            day = local.date().isoformat()
            for minute in prefs.times:
                scheduled = datetime(
                    local.year, local.month, local.day, minute // 60, minute % 60,
                    tzinfo=zone,
                )
                # Skip nonexistent local wall times, e.g. spring DST gaps.
                round_trip = datetime.fromtimestamp(scheduled.timestamp(), zone)
                if round_trip.date() != local.date() or (
                    round_trip.hour, round_trip.minute
                ) != (minute // 60, minute % 60):
                    continue
                elapsed = ts - int(scheduled.timestamp())
                if not 0 <= elapsed < DELIVERY_WINDOW:
                    continue
                items, count = self.todo.list_items(uid, completed=False)
                state = "sending" if count else "skipped"
                with self.db:
                    cur = self.db.execute(
                        "INSERT INTO todo_digest_delivery "
                        "(user_id, local_day, local_minute, timezone, state, claimed_at_utc) "
                        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                        (uid, day, minute, zone_name, state, ts),
                    )
                if cur.rowcount == 1 and count:
                    results.append(DueDigest(uid, day, minute, zone_name, tuple(items), count))
                    if len(results) >= limit:
                        return results
        return results

    def finish(self, digest: DueDigest, state: str, *, now: int | None = None) -> bool:
        if state not in {"sent", "failed", "uncertain", "skipped"}:
            raise ValueError("Invalid delivery status")
        with self.db:
            cur = self.db.execute(
                "UPDATE todo_digest_delivery SET state=?, finished_at_utc=? "
                "WHERE user_id=? AND local_day=? AND local_minute=? AND state='sending'",
                (state, int(time.time()) if now is None else now,
                 digest.user_id, digest.day, digest.minute),
            )
        return cur.rowcount == 1
