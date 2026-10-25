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


# Telegram/ASR regression tests: model text is presentation, not a fact source of truth.
import asyncio
from types import SimpleNamespace

from voqelis.bot import run_worker
from voqelis.day_facts.bot import create_day_facts_router, export_markup, facts_markup
from voqelis.domain import AudioJob, TranscriptionResult


class _FakeBot:
    def __init__(self):
        self.replies = []

    async def send_chat_action(self, *_args):
        pass

    async def send_message(self, _chat_id, body, **kwargs):
        self.replies.append((body, kwargs))


class _FakeQueue:
    def __init__(self, job):
        self.job = job
        self.n = 0

    async def get(self):
        self.n += 1
        if self.n > 1:
            raise asyncio.CancelledError
        return self.job

    async def release(self, _id):
        pass

    def task_done(self):
        pass


class _FakeASR:
    async def transcribe(self, _path):
        return TranscriptionResult(
            text="друг общался раздраженно был в своих мыслях мы разошлись по домам",
            pause_aware_text=None,
            language="ru",
            language_probability=0,
            duration_seconds=3,
            duration_after_vad_seconds=3,
            processing_seconds=0.1,
        )


class _FakePunctuation:
    async def format(self, source, *, fallback_text):
        assert source.startswith("друг общался")
        return (
            "Друг общался раздражённо, был в своих мыслях. "
            "Мы разошлись по домам."
        )


async def _worker_case(tmp_path, monkeypatch, service, token):
    from voqelis import bot as bot_module

    path = tmp_path / "sample.ogg"
    path.write_bytes(b"x")
    monkeypatch.setattr(bot_module, "probe_duration_seconds", lambda _path: 3)
    bot = _FakeBot()
    job = AudioJob(
        chat_id=7, reply_to_message_id=10, user_id=1,
        file_path=path, day_facts_token=token,
    )
    with pytest.raises(asyncio.CancelledError):
        await run_worker(
            bot=bot,
            queue=_FakeQueue(job),
            transcriber=_FakeASR(),
            settings=SimpleNamespace(max_audio_seconds=90),
            day_facts=service,
            punctuation_processor=_FakePunctuation(),
        )
    return bot.replies


@pytest.mark.asyncio
async def test_voice_to_facts_preserves_original_words_and_multiple_items(
    store, tmp_path, monkeypatch
):
    service = DayFactsService(store)
    draft = store.begin(1, date(2026, 10, 26))
    draft = store.accept(1, draft.token, "Прогулка с другом.")
    result = await _worker_case(tmp_path, monkeypatch, service, draft.token)
    after = store.draft(1)
    assert after is not None and after.stage == "facts"
    assert len(after.facts) == 2
    assert "Друг общался" in result[0][0]
    assert "2. Мы разошлись" in result[0][0]
    assert result[0][1]["reply_markup"] is not None
    raw = store.db.execute(
        "SELECT raw_inputs_json FROM day_facts_drafts WHERE user_id=1"
    ).fetchone()[0]
    assert "друг общался раздраженно" in raw


@pytest.mark.asyncio
async def test_stale_voice_queued_before_step_change_cannot_mutate_facts(
    store, tmp_path, monkeypatch
):
    service = DayFactsService(store)
    draft = store.begin(1, date(2026, 10, 26))
    stale = draft.token
    changed = store.accept(1, stale, "Уже другое событие.")
    assert changed is not None
    result = await _worker_case(tmp_path, monkeypatch, service, stale)
    assert "не сохранено" in result[0][0]
    assert store.draft(1) == changed


def test_day_facts_router_has_command_and_keyboard_handlers(store, tmp_path):
    service = DayFactsService(store)
    planner = SimpleNamespace(store=SimpleNamespace(session=lambda _id: None))
    notes = SimpleNamespace(pending_voice=lambda _id: False)
    router = create_day_facts_router(
        service=service, allowed_user_ids=frozenset({1}), planner=planner,
        notes=notes, export_dir=tmp_path,
    )
    callbacks = {h.callback.__name__ for h in router.message.handlers}
    assert {
        "on_start", "on_cancel", "on_next", "on_edit", "on_split",
        "on_history", "on_day", "on_export", "on_field_text",
    } <= callbacks
    assert len(router.callback_query.handlers) == 1
    d = store.begin(1, date(2026, 10, 26))
    assert len(facts_markup(d).inline_keyboard) == 1
    export_buttons = [
        button.callback_data
        for row in export_markup(date(2026, 10, 26)).inline_keyboard
        for button in row
    ]
    assert export_buttons == [
        "df:export:word:2026-10-26",
        "df:export:pdf:2026-10-26",
        "df:export:mobile:2026-10-26",
    ]


@pytest.mark.asyncio
async def test_export_command_executes_real_threaded_docx_without_sqlite_error(
    store, tmp_path
):
    service = DayFactsService(store)
    draft = fill_record(store, 1, date(2026, 10, 26))
    store.save(1, draft.token)

    class FakeMessage:
        text = "/facts_export word"
        from_user = SimpleNamespace(id=1)
        chat = SimpleNamespace(type="private")

        def __init__(self):
            self.documents = []
            self.messages = []

        async def answer(self, value, **kwargs):
            self.messages.append(value)

        async def answer_document(self, attachment, **kwargs):
            # FSInputFile is lazily opened; inspect while still inside the
            # export handler, before the generated temporary file is removed.
            self.documents.append((attachment.path, Path(attachment.path).read_bytes()))

    planner = SimpleNamespace(store=SimpleNamespace(session=lambda _id: None))
    notes = SimpleNamespace(pending_voice=lambda _id: False)
    router = create_day_facts_router(
        service=service, allowed_user_ids=frozenset({1}), planner=planner,
        notes=notes, export_dir=tmp_path,
    )
    handler = next(
        h.callback for h in router.message.handlers
        if h.callback.__name__ == "on_export"
    )
    msg = FakeMessage()
    await handler(msg)
    assert len(msg.documents) == 1
    assert msg.documents[0][1][:2] == b"PK"
    assert not Path(msg.documents[0][0]).exists()
    assert msg.messages == []
