"""ToDo V2.1a reminder parser, durable state and backward-compatible SQLite tests."""

from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from voqelis.todo.reminders import MAX_REMINDERS, ReminderRepository
from voqelis.todo.store import TodoStore
from voqelis.todo.temporal import ReminderTimeError, parse_reminder_time

NOW = datetime(2026, 10, 30, 10, 0, tzinfo=UTC)


@pytest.mark.parametrize(("text", "expected"), [
    ("сегодня в 16:00", "2026-10-30T13:00:00+00:00"),
    ("завтра в 9 утра", "2026-10-31T06:00:00+00:00"),
    ("напомни мне послезавтра в девять вечера", "2026-11-01T18:00:00+00:00"),
    ("5 ноября в 18:30", "2026-11-05T15:30:00+00:00"),
    ("05.11.2026 18:30", "2026-11-05T15:30:00+00:00"),
    ("05.11 в 18:30", "2026-11-05T15:30:00+00:00"),
    ("послезавтра в 12 дня", "2026-11-01T09:00:00+00:00"),
])
def test_russian_reminder_dates(text, expected):
    assert parse_reminder_time(text, now=NOW).isoformat() == expected


@pytest.mark.parametrize("text", [
    "сегодня в 10:00", "завтра", "послезавтра",
    "в девять утра", "сегодня в девять", "сегодня в 24:30",
    "сегодня в 19:65", "32.12.2026 в 18:30", "завтра в 8 вечера завтра",
    "5 ноября", "сегодня вечером", "послезавтра в 13 утра",
])
def test_ambiguous_or_invalid_date_rejected(text):
    with pytest.raises(ReminderTimeError):
        parse_reminder_time(text, now=NOW)



@pytest.mark.parametrize(("text", "expected_clock"), [
    ("сегодня в 22:10", "22:10"),
    ("сегодня в 22.10", "22:10"),
    ("сегодня в 22 10", "22:10"),
    ("сегодня в 22 10 минут", "22:10"),
    ("сегодня в 22 часа 10 минут", "22:10"),
    ("сегодня в двадцать два десять", "22:10"),
    ("сегодня в двадцать два часа десять минут", "22:10"),
    ("сегодня в десять вечера десять минут", "22:10"),
    ("сегодня в 10 вечера 10 минут", "22:10"),
    ("сегодня в десять часов десять минут вечера", "22:10"),
    ("сегодня в 10:10 вечера", "22:10"),
    ("сегодня в 22:00", "22:00"),
    ("сегодня в 22", "22:00"),
    ("сегодня в десять вечера", "22:00"),
    ("сегодня в 18 часов пять минут", "18:05"),
    ("сегодня в 18 05", "18:05"),
    ("сегодня в десять вечера пятьдесят девять минут", "22:59"),
    ("напомни сегодня в 22 10", "22:10"),
])
def test_asr_reminder_time_with_spoken_hours_and_minutes(text, expected_clock):
    # 15:00 Moscow: all tested times are strictly in the future.
    from zoneinfo import ZoneInfo

    now = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
    result = parse_reminder_time(text, now=now)
    assert result.astimezone(ZoneInfo("Europe/Moscow")).strftime("%H:%M") == expected_clock


@pytest.mark.parametrize("text", [
    "сегодня в десять",
    "сегодня в 10 10",
    "сегодня в 12",
    "сегодня в 22 65",
    "сегодня в 22 часа 70 минут",
    "сегодня в 22 10 20",
    "сегодня в 24:10",
    "сегодня в двадцать четыре десять",
    "сегодня в 22 утра 10 минут",
    "сегодня в десять вечера шестьдесят минут",
    "сегодня в десять вечера десять минут и двадцать",
])
def test_spoken_time_rejects_invalid_or_ambiguous_minutes(text):
    now = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
    with pytest.raises(ReminderTimeError):
        parse_reminder_time(text, now=now)


def test_year_rollover_and_leap_day():
    now = datetime(2026, 12, 31, 10, 0, tzinfo=UTC)
    assert parse_reminder_time("01.01 09:00", now=now).year == 2027
    with pytest.raises(ReminderTimeError):
        parse_reminder_time("29.02.2027 09:00", now=NOW)


@pytest.mark.parametrize("text", [
    "29.03.2026 02:30", "25.10.2026 02:30",
])
def test_berlin_dst_gap_and_fold_rejected(text):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ReminderTimeError, match="часов"):
        parse_reminder_time(text, now=now, timezone="Europe/Berlin")


def test_timezone_is_user_specific():
    x = parse_reminder_time("завтра в 18:00", now=NOW, timezone="Europe/Moscow")
    y = parse_reminder_time("завтра в 18:00", now=NOW, timezone="Europe/Berlin")
    assert x != y
    assert x.astimezone(ZoneInfo("Europe/Moscow")).hour == 18
    assert y.astimezone(ZoneInfo("Europe/Berlin")).hour == 18


@pytest.fixture
def data(tmp_path: Path):
    store = TodoStore(tmp_path / "todo.sqlite3")
    try:
        yield store, ReminderRepository(store)
    finally:
        store.close()


def add_item(store: TodoStore, user_id: int):
    capture = store.begin_capture(user_id)
    item = store.capture_text(user_id, capture.token, "Купить лекарства")
    assert item
    return item


def make_reminder(store, reminder, user_id=1, days=1):
    item = add_item(store, user_id)
    draft = reminder.begin(user_id, item.id)
    now = int(time.time())
    moment = datetime.fromtimestamp(now + days * 86400, UTC)
    assert reminder.propose(user_id, draft.token, moment, now=now)
    saved = reminder.confirm(user_id, draft.token, now=now)
    assert saved
    return item, saved


def test_multiple_reminders_owner_isolation_and_duplicate_rejection(data):
    store, reminders = data
    item, first = make_reminder(store, reminders)
    assert reminders.get(2, first.id) is None
    assert reminders.for_item(2, item.id) == []
    draft = reminders.begin(1, item.id)
    now = int(time.time())
    date = datetime.fromtimestamp(now + 2 * 86400, UTC)
    reminders.propose(1, draft.token, date, now=now)
    second = reminders.confirm(1, draft.token, now=now)
    assert second
    assert second.remind_at_utc != first.remind_at_utc
    assert len(reminders.for_item(1, item.id)) == 2
    # Identical date: must not duplicate.
    draft = reminders.begin(1, item.id)
    reminders.propose(1, draft.token, date, now=now)
    assert reminders.confirm(1, draft.token, now=now) is None
    assert len(reminders.for_item(1, item.id)) == 2


def test_draft_replay_and_expiry_fail_closed(data):
    store, reminders = data
    item = add_item(store, 1)
    ts = int(time.time())
    first = reminders.begin(1, item.id, now=ts)
    second = reminders.begin(1, item.id, now=ts)
    when = datetime.fromtimestamp(ts + 180, UTC)
    assert reminders.propose(1, first.token, when, now=ts) is None
    assert reminders.confirm(1, first.token, now=ts) is None
    assert reminders.propose(1, second.token, when, now=ts)
    assert reminders.confirm(1, second.token, now=ts + 901) is None
    assert reminders.pending(1, now=ts + 901) is None


def test_completion_cancellation_and_reopen_stays_cancelled(data):
    store, reminders = data
    item, first = make_reminder(store, reminders)
    assert store.set_completed(1, item.id, completed=True)
    assert reminders.get(1, first.id).state == "cancelled"
    with pytest.raises(ValueError):
        reminders.begin(1, item.id)
    assert store.set_completed(1, item.id, completed=False)
    assert reminders.get(1, first.id).state == "cancelled"


def test_delete_cancels_pending_and_removes_draft(data):
    store, reminders = data
    item, first = make_reminder(store, reminders)
    assert reminders.begin(1, item.id)
    token = store.ask_delete(1, item.id)
    assert token and store.confirm_delete(1, item.id, token)
    assert reminders.pending(1) is None
    assert reminders.get(1, first.id) is None
    state = store.db.execute(
        "SELECT state FROM todo_reminders WHERE id=?", (first.id,),
    ).fetchone()[0]
    assert state == "cancelled"


def test_exactly_one_due_claim_and_crash_uncertainty(data):
    store, reminders = data
    _, first = make_reminder(store, reminders)
    at = first.remind_at_utc
    assert reminders.claim_due(now=at - 1) == []
    batch = reminders.claim_due(now=at)
    assert len(batch) == 1
    assert batch[0].id == first.id
    assert batch[0].state == "sending"
    assert reminders.claim_due(now=at) == []
    assert reminders.recover_inflight() == 1
    assert reminders.get(1, first.id).state == "uncertain"
    assert reminders.claim_due(now=at + 1) == []


def test_missed_older_than_two_hours_not_sent(data):
    store, reminders = data
    _, first = make_reminder(store, reminders)
    assert reminders.claim_due(now=first.remind_at_utc + 7201) == []
    assert reminders.get(1, first.id).state == "missed"


def test_final_delivery_status(data):
    store, reminders = data
    _, first = make_reminder(store, reminders)
    claimed = reminders.claim_due(now=first.remind_at_utc)
    assert claimed
    assert reminders.finish_send(first.id, sent=True, now=first.remind_at_utc)
    assert not reminders.finish_send(first.id, sent=True)
    assert reminders.get(1, first.id).state == "sent"


def test_limit_ten_future_pending_reminders(data):
    store, reminders = data
    item = add_item(store, 1)
    ts = int(time.time())
    for i in range(MAX_REMINDERS):
        draft = reminders.begin(1, item.id, now=ts)
        when = datetime.fromtimestamp(ts + 86400 * (i + 1), UTC)
        assert reminders.propose(1, draft.token, when, now=ts)
        assert reminders.confirm(1, draft.token, now=ts)
    draft = reminders.begin(1, item.id, now=ts)
    when = datetime.fromtimestamp(ts + 86400 * 20, UTC)
    assert reminders.propose(1, draft.token, when, now=ts)
    with pytest.raises(ValueError, match="10"):
        reminders.confirm(1, draft.token, now=ts)
    assert len(reminders.for_item(1, item.id)) == 10


def test_sqlite_schema_upgrade_preserves_v1_tasks(tmp_path):
    path = tmp_path / "todo.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE todo_items (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "user_id INTEGER NOT NULL, text TEXT NOT NULL, source TEXT NOT NULL,"
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL,"
            "completed_at TEXT, deleted_at TEXT)"
        )
        db.execute(
            "INSERT INTO todo_items "
            "(id,user_id,text,source,created_at,updated_at) "
            "VALUES(17,1,'Старое дело','voice','old','old')"
        )
    store = TodoStore(path)
    try:
        assert store.get(1, 17).text == "Старое дело"
        assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert store.db.execute(
            "SELECT name FROM sqlite_master WHERE name='todo_reminders'"
        ).fetchone()
    finally:
        store.close()



def test_cancellation_allows_same_time_again_without_erasing_history(data):
    store, reminders = data
    item, original = make_reminder(store, reminders)
    timestamp = original.remind_at_utc
    assert reminders.cancel(1, original.id)
    assert reminders.get(1, original.id).state == "cancelled"
    next_draft = reminders.begin(1, item.id)
    now = int(time.time())
    at = datetime.fromtimestamp(timestamp, UTC)
    assert reminders.propose(1, next_draft.token, at, now=now)
    again = reminders.confirm(1, next_draft.token, now=now)
    assert again is not None
    assert again.id != original.id
    assert again.remind_at_utc == original.remind_at_utc
    assert again.state == "pending"
    assert reminders.get(1, original.id).state == "cancelled"
    assert len(reminders.for_item(1, item.id)) == 2
    # A second identical active reminder is still rejected.
    repeated = reminders.begin(1, item.id)
    assert reminders.propose(1, repeated.token, at, now=now)
    assert reminders.confirm(1, repeated.token, now=now) is None
    assert len(reminders.for_item(1, item.id)) == 2
