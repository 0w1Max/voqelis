"""Day Facts: state-machine integrity, boundary correction, exports and isolation."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from voqelis.day_facts.export import TITLE, export_docx, export_pdf
from voqelis.day_facts.sentences import split_explicit, split_sentences
from voqelis.day_facts.service import DayFactsService
from voqelis.day_facts.store import DayFactsStore


@pytest.fixture
def store(tmp_path: Path):
    db = DayFactsStore(tmp_path / "day_facts.sqlite3")
    yield db
    db.close()


def fill_record(store: DayFactsStore, user: int, day: date):
    draft = store.begin(user, day)
    assert draft.stage == "event"
    draft = store.accept(
        user, draft.token, "Гуляли с анонимным другом, он был без настроения."
    )
    assert draft is not None and draft.stage == "facts"
    draft = store.accept(
        user, draft.token,
        "Друг общался раздражённо, был в своих мыслях. Мы разошлись по домам.",
        raw_text="друг общался раздраженно был в своих мыслях мы разошлись по домам",
    )
    assert draft is not None
    assert len(draft.facts) == 2
    draft = store.next(user, draft.token)
    assert draft is not None and draft.stage == "judgments"
    draft = store.accept(
        user, draft.token,
        "Я как-то не так общаюсь. Из меня друг так себе."
    )
    assert draft is not None and len(draft.judgments) == 2
    draft = store.next(user, draft.token)
    assert draft is not None and draft.stage == "review"
    return draft


def test_sentence_split_uses_full_stops_not_commas() -> None:
    assert split_sentences(
        "Друг общался раздражённо, был в своих мыслях. Мы разошлись по домам."
    ) == (
        "Друг общался раздражённо, был в своих мыслях.",
        "Мы разошлись по домам.",
    )
    assert split_sentences("Я не так общаюсь? Я плохой друг!") == (
        "Я не так общаюсь?", "Я плохой друг!"
    )
    assert split_sentences("факт без точки другой факт без точки") == (
        "факт без точки другой факт без точки",
    )
    assert split_sentences(
        "Друг смотрел в окно следующий факт мы разошлись по домам"
    ) == ("Друг смотрел в окно", "мы разошлись по домам")
    assert split_sentences(
        "Я испортил встречу следующее суждение я плохой друг"
    ) == ("Я испортил встречу", "я плохой друг")


def test_explicit_split_allows_boundary_correction() -> None:
    assert split_explicit(
        "Друг был раздражён | Мы разошлись по домам"
    ) == ("Друг был раздражён", "Мы разошлись по домам")
    with pytest.raises(ValueError):
        split_explicit("только один")
    with pytest.raises(ValueError):
        split_explicit("два |  ")


def test_complete_example_saves_one_event_two_facts_two_judgments(store):
    day = date(2026, 10, 26)
    draft = fill_record(store, 7, day)
    note = store.save(7, draft.token)
    assert note is not None
    assert note.event == "Гуляли с анонимным другом, он был без настроения."
    assert len(note.facts) == 2
    assert note.facts[0] == "Друг общался раздражённо, был в своих мыслях."
    assert note.facts[1] == "Мы разошлись по домам."
    assert note.judgments == (
        "Я как-то не так общаюсь.", "Из меня друг так себе."
    )
    assert store.draft(7) is None
    assert [item.id for item in store.for_day(7, day)] == [note.id]
    raw_row = store.db.execute(
        "SELECT raw_inputs_json FROM day_facts_entries WHERE id=?", (note.id,)
    ).fetchone()
    assert "друг общался раздраженно" in raw_row[0]


def test_unfinished_or_empty_draft_is_never_saved(store):
    draft = store.begin(1, date(2026, 10, 26))
    assert store.save(1, draft.token) is None
    with pytest.raises(ValueError):
        store.next(1, draft.token)
    assert store.for_day(1, date(2026, 10, 26)) == []


def test_second_begin_preserves_pending_draft(store):
    day = date(2026, 10, 26)
    a = store.begin(1, day)
    b = store.begin(1, date(2026, 10, 27))
    assert b == a


def test_replay_stale_voice_is_rejected_and_does_not_overwrite(store):
    draft = store.begin(1, date(2026, 10, 26))
    old_token = draft.token
    updated = store.accept(1, draft.token, "Встреча с другом")
    assert updated
    assert store.accept(1, old_token, "чужое сообщение") is None
    assert store.draft(1) == updated
    assert store.for_day(1, date(2026, 10, 26)) == []


def test_new_draft_invalidates_old_token_after_cancel(store):
    day = date(2026, 10, 26)
    old = store.begin(1, day)
    assert store.cancel(1)
    fresh = store.begin(1, day)
    assert fresh.token != old.token
    assert store.accept(1, old.token, "не сохранять") is None


def test_next_requires_at_least_one_fact_and_judgment(store):
    d = store.begin(1, date(2026, 10, 26))
    d = store.accept(1, d.token, "Событие")
    with pytest.raises(ValueError):
        store.next(1, d.token)
    d = store.accept(1, d.token, "Факт.")
    d = store.next(1, d.token)
    with pytest.raises(ValueError):
        store.next(1, d.token)


def test_manual_split_and_correction_even_at_review_stage(store):
    d = fill_record(store, 1, date(2026, 10, 26))
    before = d.token
    d = store.correct(
        1, d.token, "judgments", 1,
        "Я неправильно общаюсь | Я всё испортил",
        split=True,
    )
    assert d
    assert len(d.judgments) == 3
    assert d.judgments[:2] == ("Я неправильно общаюсь", "Я всё испортил")
    assert store.correct(1, before, "facts", 1, "подмена") is None
    d = store.correct(1, d.token, "event", 1, "Другое событие.")
    assert d.event == "Другое событие."
    with pytest.raises(ValueError):
        store.correct(1, d.token, "facts", 99, "нет")
    with pytest.raises(ValueError):
        store.correct(1, d.token, "event", 1, "a | b", split=True)


def test_user_isolation_dates_and_history(store):
    day = date(2026, 10, 26)
    d = fill_record(store, 1, day)
    saved = store.save(1, d.token)
    assert saved
    assert store.get(2, saved.id) is None
    assert store.for_day(2, day) == []
    assert store.history(2) == []
    assert store.history(1) == [(day.isoformat(), 1)]


def test_independent_sqlite_file_does_not_modify_planner(tmp_path):
    planner = tmp_path / "planner.sqlite3"
    with sqlite3.connect(planner) as conn:
        conn.execute("CREATE TABLE preserved(x INTEGER)")
        conn.execute("INSERT INTO preserved VALUES(15)")
    ours = DayFactsStore(tmp_path / "day_facts.sqlite3")
    ours.begin(1, date(2026, 10, 26))
    ours.close()
    with sqlite3.connect(f"file:{planner}?mode=ro", uri=True) as db:
        assert db.execute("SELECT x FROM preserved").fetchone() == (15,)
        names = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert "day_facts_drafts" not in names


def test_persistence_after_restart(tmp_path):
    path = tmp_path / "day_facts.sqlite3"
    one = DayFactsStore(path)
    review = fill_record(one, 1, date(2026, 10, 26))
    saved = one.save(1, review.token)
    one.close()
    two = DayFactsStore(path)
    try:
        assert two.get(1, saved.id) == saved
        assert two.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        two.close()


def test_service_review_displays_individual_items(store):
    service = DayFactsService(store)
    draft = fill_record(store, 1, date(2026, 10, 26))
    rendered = service.render(draft)
    assert "1. Друг общался" in rendered
    assert "2. Мы разошлись" in rendered
    assert "1. Я как-то" in rendered
    assert "2. Из меня" in rendered
    store.save(1, draft.token)
    assert "Ситуация 1" in service.day_message(1, date(2026, 10, 26))
    assert "26.10.2026" in service.history_message(1)


def test_docx_pdf_and_phone_pdf_include_cyrillic_and_three_fields(store, tmp_path):
    d = fill_record(store, 1, date(2026, 10, 26))
    store.save(1, d.token)
    entries = store.for_day(1, date(2026, 10, 26))
    word = export_docx(entries, d.day, tmp_path / "sample.docx")
    pdf = export_pdf(entries, d.day, tmp_path / "sample.pdf")
    phone = export_pdf(entries, d.day, tmp_path / "phone.pdf", mobile=True)

    from docx import Document

    opened = Document(word)
    assert TITLE in "\n".join(p.text for p in opened.paragraphs)
    assert len(opened.tables) == 1
    assert len(opened.tables[0].columns) == 3
    assert len(opened.tables[0].rows) == 2
    assert "Друг общался" in opened.tables[0].cell(1, 1).text
    assert "2. Из меня" in opened.tables[0].cell(1, 2).text
    assert pdf.read_bytes().startswith(b"%PDF")
    assert phone.read_bytes().startswith(b"%PDF")
    assert all(p.stat().st_size > 2000 for p in (word, pdf, phone))


def test_many_day_entries_export_more_than_one_page(store, tmp_path):
    day = date(2026, 10, 26)
    for i in range(25):
        d = store.begin(1, day)
        d = store.accept(1, d.token, f"Ситуация номер {i}")
        d = store.accept(1, d.token, "Проверяем факт номер один. Факт номер два.")
        d = store.next(1, d.token)
        d = store.accept(1, d.token, "Это первая мысль. Это вторая мысль.")
        d = store.next(1, d.token)
        store.save(1, d.token)
    entries = store.for_day(1, day)
    assert len(entries) == 25
    output = export_pdf(entries, day, tmp_path / "many.pdf")
    output_phone = export_pdf(entries, day, tmp_path / "many-mobile.pdf", mobile=True)
    assert output.stat().st_size > 3000
    assert output_phone.stat().st_size > 3000


def test_invalid_export_inputs_fail_closed(store, tmp_path):
    with pytest.raises(ValueError):
        export_pdf([], date(2026, 10, 26), tmp_path / "empty.pdf")
    d = fill_record(store, 1, date(2026, 10, 26))
    item = store.save(1, d.token)
    with pytest.raises(ValueError):
        export_docx([item], date(2026, 10, 27), tmp_path / "wrong.docx")
