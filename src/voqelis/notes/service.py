"""Application-facing Notes V1 functions; no AI extraction or scheduling."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .store import Note, NotesStore

_NOTE_PREVIEW_CHARS = 110


def _preview(body: str) -> str:
    text = " ".join(body.split())
    return text if len(text) <= _NOTE_PREVIEW_CHARS else text[:_NOTE_PREVIEW_CHARS] + "…"


class NotesService:
    def __init__(self, store: NotesStore, *, timezone: str = "Europe/Moscow") -> None:
        self.store = store
        self.timezone = ZoneInfo(timezone)

    def create_text(self, user_id: int, text: str) -> Note:
        return self.store.create_text(user_id, text)

    def arm_voice(self, user_id: int) -> None:
        self.store.arm_voice(user_id)

    def cancel_voice(self, user_id: int) -> bool:
        return self.store.cancel_voice(user_id)

    def pending_voice(self, user_id: int) -> bool:
        return self.store.pending_voice(user_id)

    def claim_voice(self, user_id: int) -> str | None:
        return self.store.claim_voice(user_id)

    def capture_voice(
        self, user_id: int, text: str, *, original: str, capture_token: str
    ) -> Note | None:
        return self.store.capture_voice(
            user_id, text, original_text=original, capture_token=capture_token
        )

    def saved_message(self, note: Note) -> str:
        return f"📝 Заметка #{note.id} сохранена.\n\n{note.body}"

    def _list_lines(self, notes: list[Note]) -> str:
        return "\n".join(
            f"#{note.id} · "
            f"{datetime.fromisoformat(note.created_at).astimezone(self.timezone):%d.%m %H:%M}"
            f" · {_preview(note.body)}"
            for note in notes
        )

    def list_message(self, user_id: int, *, page: int = 1) -> str:
        notes = self.store.recent(user_id, page=page)
        if not notes:
            return "Заметок на этой странице нет." if page > 1 else (
                "Пока нет заметок. Отправь /note текст или /note, затем голосовое."
            )
        return (
            f"🗒 Последние заметки · страница {page}\n"
            f"{self._list_lines(notes)}\n\n"
            f"Открыть: /note_show ID · Следующая: /notes {page + 1}"
        )

    def search_message(self, user_id: int, query: str) -> str:
        notes = self.store.search(user_id, query)
        if not notes:
            return "По запросу заметки не найдены."
        return f"🔎 Найденные заметки:\n{self._list_lines(notes)}\n\nОткрыть: /note_show ID"

    def get(self, user_id: int, note_id: int) -> Note | None:
        return self.store.get(user_id, note_id)

    def show_message(self, note: Note) -> str:
        created = datetime.fromisoformat(note.created_at).astimezone(self.timezone)
        source = "голос" if note.source == "voice" else "текст"
        return f"📝 Заметка #{note.id} · {created:%d.%m.%Y %H:%M} · {source}\n\n{note.body}"
