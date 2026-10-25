"""Telegram UI for deliberate, review-first daily facts dictation."""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from pathlib import Path
from uuid import uuid4

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ..notes.service import NotesService
from ..planner.service import PlannerService
from ..text import chunk_text
from .export import export_docx, export_pdf
from .service import EDIT_ALIASES, DayFactsService
from .store import FactsDraft

logger = logging.getLogger(__name__)

# Planner's permanent reply keyboard stays visible during Day Facts.
# Never mistake a tap on an unrelated menu action for dictated clinical text.
_OTHER_MENU_ACTIONS = frozenset({
    "📅 Планирование дня", "📋 Текущий план", "🗓 История планов",
    "🔎 Анализ активного дня", "🧹 Очистить план", "🗑 Удалить строку",
    "🎙️ Рассказать весь день", "✏️ Редактировать план",
    "✏️ Исправить анализ", "📄 DOCX", "📕 PDF",
    "⏹️ Выйти из режима",
})


def facts_markup(draft: FactsDraft) -> InlineKeyboardMarkup:
    token = draft.token
    if draft.stage == "review":
        rows = [
            [InlineKeyboardButton(text="✅ Сохранить", callback_data=f"df:save:{token}")],
        ]
    else:
        rows = []
        if draft.stage in {"facts", "judgments"}:
            rows.append([
                InlineKeyboardButton(text="➡️ Далее", callback_data=f"df:next:{token}")
            ])
    if draft.stage == "review":
        rows.append([
            InlineKeyboardButton(text="✏️ Как исправить", callback_data=f"df:help:{token}")
        ])
    rows.append([
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"df:cancel:{token}")
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def export_markup(day: date) -> InlineKeyboardMarkup:
    iso = day.isoformat()
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📄 Word", callback_data=f"df:export:word:{iso}"),
            InlineKeyboardButton(text="📕 PDF", callback_data=f"df:export:pdf:{iso}"),
        ],
        [
            InlineKeyboardButton(
                text="📱 PDF для телефона", callback_data=f"df:export:mobile:{iso}"
            ),
        ],
    ])


def _argument(text: str) -> str:
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


async def _send(message: Message, text: str, *, draft: FactsDraft | None = None) -> None:
    parts = chunk_text(text)
    for i, part in enumerate(parts):
        await message.answer(
            part, parse_mode=None,
            reply_markup=facts_markup(draft) if i == len(parts) - 1 and draft else None,
        )


def _day_arg(value: str, default: date) -> date:
    if not value:
        return default
    return date.fromisoformat(value)


def create_day_facts_router(
    *, service: DayFactsService, allowed_user_ids: frozenset[int],
    planner: PlannerService, notes: NotesService, export_dir: Path,
) -> Router:
    router = Router(name="day_facts")

    def allowed(message: Message) -> bool:
        return (
            message.chat.type == ChatType.PRIVATE
            and message.from_user is not None
            and message.from_user.id in allowed_user_ids
        )

    @router.message(Command("facts"))
    @router.message(F.text == "📘 Факты дня")
    async def on_start(message: Message) -> None:
        if not allowed(message):
            return
        user_id = message.from_user.id
        if planner.store.session(user_id) is not None:
            await message.answer(
                "Сначала заверши активный Planner кнопкой «⏹️ Выйти из режима»."
            )
            return
        if notes.pending_voice(user_id):
            await message.answer(
                "Сначала заверши или отмени ожидание голосовой заметки: /note_cancel."
            )
            return
        draft = service.start(user_id)
        await _send(message, service.render(draft), draft=draft)

    @router.message(Command("facts_cancel"))
    async def on_cancel(message: Message) -> None:
        if allowed(message):
            changed = service.store.cancel(message.from_user.id)
            await message.answer(
                "Черновик «Факты дня» отменён." if changed else "Активного черновика нет."
            )

    async def advance(message: Message) -> None:
        draft = service.store.draft(message.from_user.id)
        if not draft:
            await message.answer("Нет активного черновика. Начни с /facts.")
            return
        if draft.stage == "review":
            await message.answer(
                "Запись уже на проверке. Нажми «Сохранить» или исправь пункты."
            )
            return
        try:
            updated = service.store.next(message.from_user.id, draft.token)
        except ValueError:
            await message.answer("Сначала добавь хотя бы один пункт текущего раздела.")
            return
        if updated is None:
            await message.answer("Черновик изменился. Введи /facts для продолжения.")
        else:
            await _send(message, service.render(updated), draft=updated)

    @router.message(Command("facts_next"))
    async def on_next(message: Message) -> None:
        if allowed(message):
            await advance(message)

    async def _correction(message: Message, *, split: bool) -> None:
        draft = service.store.draft(message.from_user.id)
        if not draft:
            await message.answer("Нет черновика. Введи /facts.")
            return
        raw = _argument(message.text or "")
        left, sep, body = raw.partition("|")
        pieces = left.strip().split()
        if not sep or len(pieces) != 2:
            await message.answer(
                "Формат: /facts_edit facts 1 | новый текст\n"
                "или /facts_split judgments 1 | первый текст | второй текст"
            )
            return
        field = EDIT_ALIASES.get(pieces[0].casefold())
        try:
            index = int(pieces[1])
            if field is None:
                raise ValueError("Unknown field")
            updated = service.store.correct(
                message.from_user.id, draft.token, field, index, body.strip(),
                split=split,
            )
        except ValueError:
            await message.answer(
                "Не удалось исправить пункт. Проверь раздел (event/facts/judgments), "
                "номер и текст после |."
            )
            return
        if updated:
            await _send(message, service.render(updated), draft=updated)
        else:
            await message.answer("Черновик изменился. Введи /facts.")

    @router.message(Command("facts_edit"))
    async def on_edit(message: Message) -> None:
        if allowed(message):
            await _correction(message, split=False)

    @router.message(Command("facts_split"))
    async def on_split(message: Message) -> None:
        if allowed(message):
            await _correction(message, split=True)

    @router.message(Command("facts_history"))
    async def on_history(message: Message) -> None:
        if allowed(message):
            await _send(message, service.history_message(message.from_user.id))

    @router.message(Command("facts_day"))
    async def on_day(message: Message) -> None:
        if not allowed(message):
            return
        try:
            day = _day_arg(_argument(message.text or ""), service.today())
        except ValueError:
            await message.answer("Дата: /facts_day 2026-10-26")
            return
        await _send(message, service.day_message(message.from_user.id, day))

    async def send_export(message: Message, user_id: int, kind: str, day: date) -> None:
        # sqlite3 connections belong to the event-loop thread. The actual
        # rendering is pure and safe to move into a worker thread.
        entries = service.store.for_day(user_id, day)
        if not entries:
            await message.answer("Нет сохранённых ситуаций за указанную дату.")
            return

        extension = "docx" if kind == "word" else "pdf"
        output = export_dir / f"facts-{uuid4().hex}.{extension}"
        try:
            if kind == "word":
                await asyncio.to_thread(export_docx, entries, day, output)
            else:
                await asyncio.to_thread(
                    export_pdf, entries, day, output, mobile=(kind == "mobile"),
                )
            await message.answer_document(
                FSInputFile(
                    output, filename=f"fakty-dnya-{day.isoformat()}-{kind}.{extension}"
                ),
                caption=f"Факты дня · {day:%d.%m.%Y} · {kind}",
            )
        except Exception:
            logger.exception(
                "DAY_FACTS_EXPORT_FAILED user=%s format=%s", user_id, kind
            )
            await message.answer("Не удалось создать документ. Ошибка записана в журнал.")
        finally:
            output.unlink(missing_ok=True)

    @router.message(Command("facts_export"))
    async def on_export(message: Message) -> None:
        if not allowed(message):
            return
        args = _argument(message.text or "").split()
        if not args or len(args) > 2 or args[0] not in {"word", "pdf", "mobile"}:
            await message.answer(
                "Экспорт: /facts_export word, /facts_export pdf, "
                "/facts_export mobile\nДата необязательна: /facts_export pdf 2026-10-26"
            )
            return
        try:
            target_day = _day_arg(args[1] if len(args) == 2 else "", service.today())
        except ValueError:
            await message.answer("Проверь дату в формате ГГГГ-ММ-ДД.")
            return
        await send_export(message, message.from_user.id, args[0], target_day)

    @router.callback_query(F.data.startswith("df:"))
    async def on_callback(callback: CallbackQuery) -> None:
        if (
            callback.from_user.id not in allowed_user_ids
            or callback.message is None
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await callback.answer()
            return
        data = callback.data or ""
        if data.startswith("df:export:"):
            parts = data.split(":")
            if len(parts) != 4 or parts[2] not in {"word", "pdf", "mobile"}:
                await callback.answer("Неизвестный формат.", show_alert=True)
                return
            try:
                target = date.fromisoformat(parts[3])
            except ValueError:
                await callback.answer("Некорректная дата.", show_alert=True)
                return
            await callback.answer()
            await send_export(
                callback.message, callback.from_user.id, parts[2], target
            )
            return

        action, _, token = data[3:].partition(":")
        draft = service.store.draft(callback.from_user.id)
        if not draft or token != draft.token:
            await callback.answer("Эта кнопка устарела. Введи /facts.", show_alert=True)
            return
        if action == "help":
            await callback.answer()
            await callback.message.answer(
                "Изменить пункт: /facts_edit facts 1 | исправленный текст\n"
                "Разделить: /facts_split judgments 1 | первая мысль | вторая мысль\n"
                "Событие: /facts_edit event 1 | исправленное событие"
            )
            return
        if action == "cancel":
            service.store.cancel(callback.from_user.id)
            await callback.answer()
            await callback.message.answer("Черновик отменён.")
            return
        if action == "save":
            saved = service.store.save(callback.from_user.id, token)
            await callback.answer()
            if saved:
                await callback.message.answer(
                    f"✅ Ситуация #{saved.id} сохранена за {saved.day:%d.%m.%Y}.\n"
                    "Добавить ещё: /facts\n"
                    "Экспорт: /facts_export word | pdf | mobile",
                    reply_markup=export_markup(saved.day),
                )
            else:
                await callback.message.answer("Черновик изменился. Введи /facts.")
            return
        if action == "next":
            try:
                updated = service.store.next(callback.from_user.id, token)
            except ValueError:
                await callback.answer(
                    "Сначала добавь хотя бы один пункт.", show_alert=True
                )
                return
            await callback.answer()
            if updated:
                await _send(
                    callback.message, service.render(updated), draft=updated
                )
            return
        await callback.answer()

    @router.message(
        lambda message: (
            message.text is not None
            and not message.text.startswith("/")
            and message.from_user is not None
            and service.store.draft(message.from_user.id) is not None
            and planner.store.session(message.from_user.id) is None
        )
    )
    async def on_field_text(message: Message) -> None:
        if not allowed(message):
            return
        if message.text in _OTHER_MENU_ACTIONS:
            await message.answer(
                "Сейчас открыт черновик «Факты дня». "
                "Заверши запись или используй /facts_cancel, "
                "чтобы вернуться к другим режимам."
            )
            return
        draft = service.store.draft(message.from_user.id)
        if not draft:
            return
        try:
            updated = service.accept(message.from_user.id, draft.token, message.text)
        except ValueError:
            await message.answer("Сообщение должно содержать от 1 до 12000 символов.")
            return
        if updated:
            await _send(message, service.render(updated), draft=updated)
        else:
            await message.answer("Черновик уже изменился. Введи /facts.")

    return router
