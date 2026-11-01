"""Day Facts presentation, explicit correction and export orchestration."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .export import export_docx, export_mobile_docx, export_pdf
from .store import DayFactsStore, FactsDraft

FIELD_NAMES = {
    "event": "Событие", "facts": "Факты дня",
    "judgments": "Ошибочные суждения",
}
EDIT_ALIASES = {
    "event": "event", "событие": "event",
    "facts": "facts", "факты": "facts",
    "judgments": "judgments", "суждения": "judgments",
}


def _enumerate(items: tuple[str, ...]) -> str:
    return "\n".join(f"{i}. {line}" for i, line in enumerate(items, 1)) or "—"


class DayFactsService:
    def __init__(
        self, store: DayFactsStore, *, timezone: str = "Europe/Moscow"
    ) -> None:
        self.store = store
        self.tz = ZoneInfo(timezone)

    def today(self) -> date:
        return datetime.now(self.tz).date()

    def start(self, user_id: int) -> FactsDraft:
        return self.store.begin(user_id, self.today())

    def accept(
        self, user_id: int, token: str, text: str, *,
        raw: str | None = None, voice_dictation: bool = False,
    ) -> FactsDraft | None:
        return self.store.accept(
            user_id, token, text, raw_text=raw, voice_dictation=voice_dictation,
        )

    def render(self, draft: FactsDraft) -> str:
        heading = f"📘 Факты дня · {draft.day:%d.%m.%Y}"
        if draft.edit_field == "select":
            return (
                f"{heading}\n\n✏️ Выбери, что исправить, кнопками ниже. "
                "Пока пункт не выбран, голосовые и текст не добавляются."
            )
        if draft.edit_field is not None:
            if draft.edit_field == "event":
                current = draft.event
                label = "событие"
            else:
                items = draft.facts if draft.edit_field == "facts" else draft.judgments
                current = items[draft.edit_index - 1]
                label = (
                    f"факт №{draft.edit_index}"
                    if draft.edit_field == "facts"
                    else f"суждение №{draft.edit_index}"
                )
            return (
                f"{heading}\n\n✏️ Исправляем {label}\n\n"
                f"Сейчас записано:\n{current}\n\n"
                "Отправь новый текст или голосовое сообщение. "
                "Оно заменит ТОЛЬКО этот пункт, остальные останутся без изменений.\n"
                "Чтобы ничего не менять, нажми «Отмена исправления»."
            )
        if draft.stage == "event":
            return (
                f"{heading}\n\nЭтап 1/4 — Событие\n\n"
                "Расскажи, какое событие произошло. "
                "Отправь голосовое сообщение или напиши текст."
            )
        if draft.stage == "facts":
            if not draft.facts:
                return (
                    f"{heading}\n\n✅ Событие записано:\n{draft.event}\n\n"
                    "Этап 2/4 — Факты дня\n\n"
                    "Теперь отправь следующее голосовое сообщение или текст. "
                    "Это будет ПЕРВЫЙ ФАКТ, а не новое событие.\n\n"
                    "Одно голосовое — один факт. Для нескольких фактов в одной "
                    "записи скажи «следующий факт».\n"
                    "Кнопка «➡️ Далее» появится после первого факта."
                )
            return (
                f"{heading}\n\nЭтап 2/4 — Факты дня\n\n"
                f"Событие:\n{draft.event}\n\n"
                f"Факты дня:\n{_enumerate(draft.facts)}\n\n"
                "Чтобы добавить ещё один факт, отправь голосовое или текст. "
                "Одно голосовое — один факт, даже если внутри несколько "
                "предложений. Для нескольких фактов в одном голосовом "
                "скажи «следующий факт». Текст после точки разделяется "
                "на пункты.\n"
                "Для исправления нажми «✏️ Исправить», для записи заново — "
                "«🔄 Повторить факты».\n"
                "Когда все факты добавлены, нажми «➡️ Далее»."
            )
        if draft.stage == "judgments":
            if not draft.judgments:
                return (
                    f"{heading}\n\n✅ Факты записаны.\n\n"
                    "Этап 3/4 — Ошибочные суждения\n\n"
                    "Теперь отправь голосовое сообщение или текст с ПЕРВЫМ "
                    "суждением. Оно не заменит факты.\n\n"
                    "Одно голосовое — одно суждение. Для нескольких "
                    "скажи «следующее суждение».\n"
                    "Кнопка «➡️ Далее» появится после первого суждения."
                )
            return (
                f"{heading}\n\nЭтап 3/4 — Ошибочные суждения\n\n"
                f"Ошибочные суждения:\n{_enumerate(draft.judgments)}\n\n"
                "Чтобы добавить ещё одно суждение, отправь голосовое или "
                "текст. Для нескольких пунктов в одном голосовом скажи "
                "«следующее суждение». Текстовые предложения после точки "
                "разделяются. Бот не оценивает формулировки.\n"
                "Для замены списка нажми «🔄 Повторить суждения».\n"
                "Когда все суждения добавлены, нажми «➡️ Далее»."
            )
        return (
            f"{heading}\n\nЭтап 4/4 — ПРОВЕРЬ ПЕРЕД СОХРАНЕНИЕМ\n\n"
            f"Событие:\n{draft.event}\n\n"
            f"Факты дня:\n{_enumerate(draft.facts)}\n\n"
            f"Ошибочные суждения:\n{_enumerate(draft.judgments)}\n\n"
            "Для новой диктовки нажми «🔄 Повторить факты» или "
            "«🔄 Повторить суждения».\n"
            "Для исправления одного пункта нажми «✏️ Исправить».\n"
            "Когда всё верно, нажми «✅ Сохранить»."
        )

    def day_message(self, user_id: int, day: date) -> str:
        entries = self.store.for_day(user_id, day)
        if not entries:
            return f"За {day:%d.%m.%Y} сохранённых ситуаций нет."
        parts = [f"📘 Факты дня · {day:%d.%m.%Y}"]
        for i, entry in enumerate(entries, 1):
            parts.append(
                f"\nСитуация {i}\n"
                f"Событие: {entry.event}\n\n"
                f"Факты:\n{_enumerate(entry.facts)}\n\n"
                f"Ошибочные суждения:\n{_enumerate(entry.judgments)}"
            )
        return "\n\n".join(parts)

    def history_message(self, user_id: int) -> str:
        days = self.store.history(user_id)
        if not days:
            return "Записей пока нет. Начни с команды /facts."
        lines = ["📆 История «Факты дня»"]
        for day, count in days:
            lines.append(f"• {date.fromisoformat(day):%d.%m.%Y}: ситуаций {count}")
        lines.append("\nВыбери дату кнопкой ниже или введи /facts_day ГГГГ-ММ-ДД.")
        return "\n".join(lines)

    def export(
        self, user_id: int, day: date, kind: str, output: Path
    ) -> Path:
        entries = self.store.for_day(user_id, day)
        if kind == "word":
            return export_docx(entries, day, output)
        if kind == "word_mobile":
            return export_mobile_docx(entries, day, output)
        if kind in {"pdf", "mobile"}:
            return export_pdf(entries, day, output, mobile=(kind == "mobile"))
        raise ValueError("Unknown export kind")
