"""Telegram commands for explicit Notes V1 capture and retrieval."""

from __future__ import annotations

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import Message

from ..planner.service import PlannerService
from ..day_facts.service import DayFactsService
from ..text import chunk_text
from .service import NotesService


def _argument(message_text: str) -> str:
    parts = message_text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


async def _answer_long(message: Message, body: str) -> None:
    for part in chunk_text(body):
        await message.answer(part, parse_mode=None)


def create_notes_router(
    *,
    service: NotesService,
    allowed_user_ids: frozenset[int],
    planner: PlannerService | None = None,
    day_facts: DayFactsService | None = None,
) -> Router:
    router = Router(name="notes")

    def allowed(message: Message) -> bool:
        return (
            message.chat.type == ChatType.PRIVATE
            and message.from_user is not None
            and message.from_user.id in allowed_user_ids
        )

    @router.message(Command("note"))
    async def on_note(message: Message) -> None:
        if not allowed(message):
            return
        user_id = message.from_user.id
        if planner is not None and planner.store.session(user_id) is not None:
            await message.answer(
                "Сейчас открыт Planner. Заверши его через «⏹️ Выйти из режима» "
                "перед созданием заметки."
            )
            return
        if day_facts is not None and day_facts.store.draft(user_id) is not None:
            await message.answer(
                "Сначала заверши или отмени черновик «Факты дня»: /facts_cancel."
            )
            return
        text = _argument(message.text or "")
        if not text:
            service.arm_voice(user_id)
            await message.answer(
                "🎙 Отправь одно голосовое сообщение в течение 15 минут. "
                "Оно будет сохранено как заметка.\n"
                "Отмена: /note_cancel · Текст: /note твоя заметка"
            )
            return
        try:
            note = service.create_text(user_id, text)
        except ValueError:
            await message.answer("Заметка должна содержать от 1 до 12000 символов.")
            return
        service.cancel_voice(user_id)
        await _answer_long(message, service.saved_message(note))

    @router.message(Command("note_cancel"))
    async def on_cancel(message: Message) -> None:
        if allowed(message):
            cancelled = service.cancel_voice(message.from_user.id)
            await message.answer(
                "Ожидание голосовой заметки отменено." if cancelled
                else "Нет ожидающей голосовой заметки."
            )

    @router.message(Command("notes"))
    async def on_list(message: Message) -> None:
        if not allowed(message):
            return
        raw_page = _argument(message.text or "")
        try:
            page = int(raw_page) if raw_page else 1
            body = service.list_message(message.from_user.id, page=page)
        except ValueError:
            body = "Укажи страницу от 1 до 50: /notes 2"
        await _answer_long(message, body)

    @router.message(Command("note_search"))
    async def on_search(message: Message) -> None:
        if not allowed(message):
            return
        query = _argument(message.text or "")
        try:
            body = service.search_message(message.from_user.id, query)
        except ValueError:
            body = "Укажи запрос длиной от 2 до 100 символов: /note_search проект"
        await _answer_long(message, body)

    @router.message(Command("note_show"))
    async def on_show(message: Message) -> None:
        if not allowed(message):
            return
        try:
            note_id = int(_argument(message.text or ""))
        except ValueError:
            await message.answer("Используй /note_show ID, например /note_show 1.")
            return
        note = service.get(message.from_user.id, note_id) if note_id > 0 else None
        if note is None:
            await message.answer("Заметка не найдена.")
            return
        await _answer_long(message, service.show_message(note))

    return router
