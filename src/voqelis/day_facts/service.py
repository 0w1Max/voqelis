"""Day Facts presentation, explicit correction and export orchestration."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .export import export_docx, export_pdf
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
        self, user_id: int, token: str, text: str, *, raw: str | None = None
    ) -> FactsDraft | None:
        return self.store.accept(user_id, token, text, raw_text=raw)

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
            return f"{heading}\n\nРасскажи, какое событие произошло."
        if draft.stage == "facts":
            return (
                f"{heading}\n\nСобытие:\n{draft.event}\n\n"
                f"Факты дня:\n{_enumerate(draft.facts)}\n\n"
                "Расскажи факты голосом или текстом. Предложения после точки "
                "предварительно разделяются на отдельные пункты.\n"
                "Новое сообщение добавляет пункты. Чтобы передиктовать всё, "
                "нажми «🔄 Повторить факты». Чтобы исправить одну запись — "
                "«✏️ Исправить».\n"
                "Закончил — нажми «➡️ Далее»."
            )
        if draft.stage == "judgments":
            return (
                f"{heading}\n\nОшибочные суждения:\n"
                f"{_enumerate(draft.judgments)}\n\n"
                "Перечисли свои суждения голосом или текстом. "
                "Это твои формулировки, бот не оценивает их правильность.\n"
                "Новое сообщение добавляет пункты. Для замены нажми "
                "«🔄 Повторить суждения».\n"
                "Закончил — нажми «➡️ Далее»."
            )
        return (
            f"{heading}\n\nПРОВЕРЬ ПЕРЕД СОХРАНЕНИЕМ\n\n"
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
        lines.append("\nОткрыть: /facts_day 2026-10-26")
        return "\n".join(lines)

    def export(
        self, user_id: int, day: date, kind: str, output: Path
    ) -> Path:
        entries = self.store.for_day(user_id, day)
        if kind == "word":
            return export_docx(entries, day, output)
        if kind in {"pdf", "mobile"}:
            return export_pdf(entries, day, output, mobile=(kind == "mobile"))
        raise ValueError("Unknown export kind")
