"""Per-user ToDo timezone preferences, independent of existing reminder instants."""

from __future__ import annotations

from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .store import TodoStore

DEFAULT_TIMEZONE = "Europe/Moscow"

# User-facing names are never IANA identifiers. Stored values remain standard IANA IDs.
TIMEZONE_CHOICES = (
    ("Москва", "Europe/Moscow"),
    ("Калининград", "Europe/Kaliningrad"),
    ("Самара", "Europe/Samara"),
    ("Екатеринбург", "Asia/Yekaterinburg"),
    ("Омск", "Asia/Omsk"),
    ("Новосибирск", "Asia/Novosibirsk"),
    ("Красноярск", "Asia/Krasnoyarsk"),
    ("Иркутск", "Asia/Irkutsk"),
    ("Якутск", "Asia/Yakutsk"),
    ("Владивосток", "Asia/Vladivostok"),
    ("Магадан", "Asia/Magadan"),
    ("Камчатка", "Asia/Kamchatka"),
    ("Берлин", "Europe/Berlin"),
    ("Лондон", "Europe/London"),
    ("Дубай", "Asia/Dubai"),
    ("Алматы", "Asia/Almaty"),
)
_TIMEZONE_NAMES = {zone: label for label, zone in TIMEZONE_CHOICES}
_ALLOWED_ZONES = frozenset(_TIMEZONE_NAMES)


def timezone_label(zone: str) -> str:
    """Human-readable city label without exposing internal region identifiers."""
    return _TIMEZONE_NAMES.get(zone, zone.rsplit("/", 1)[-1].replace("_", " "))


@dataclass(frozen=True, slots=True)
class TodoPreferences:
    timezone: str
    revision: int


class TodoSettingsRepository:
    """Owner-scoped settings on the existing ToDo SQLite connection."""

    def __init__(self, todo: TodoStore) -> None:
        self.db = todo.db

    def get(self, user_id: int) -> TodoPreferences:
        if user_id <= 0:
            raise ValueError("Invalid user")
        row = self.db.execute(
            "SELECT timezone, revision FROM todo_settings WHERE user_id=?",
            (user_id,),
        ).fetchone()
        if row is None:
            return TodoPreferences(DEFAULT_TIMEZONE, 0)
        return TodoPreferences(str(row["timezone"]), int(row["revision"]))

    def set_timezone(
        self, user_id: int, timezone: str, *, expected_revision: int,
    ) -> TodoPreferences | None:
        if user_id <= 0 or expected_revision < 0 or timezone not in _ALLOWED_ZONES:
            raise ValueError("Invalid timezone selection")
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Timezone data unavailable") from exc

        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT revision FROM todo_settings WHERE user_id=?", (user_id,),
            ).fetchone()
            current = int(row["revision"]) if row else 0
            if current != expected_revision:
                return None
            self.db.execute(
                "INSERT INTO todo_settings(user_id, timezone, revision) "
                "VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET "
                "timezone=excluded.timezone, revision=excluded.revision",
                (user_id, timezone, current + 1),
            )
            # An old reminder draft must never be confirmed in the previous timezone.
            # Already confirmed UTC instants and their historical zones are untouched.
            self.db.execute(
                "DELETE FROM todo_reminder_drafts WHERE user_id=?", (user_id,),
            )
        return self.get(user_id)
