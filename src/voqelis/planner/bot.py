from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from .service import PlannerService


logger = logging.getLogger(__name__)


def planner_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📅 Планирование дня")],
            [KeyboardButton(text="📋 Текущий план"), KeyboardButton(text="🔎 Анализ сегодня")],
            [KeyboardButton(text="🎙️ Рассказать весь день")],
            [KeyboardButton(text="✏️ Редактировать план")],
            [KeyboardButton(text="✏️ Исправить анализ")],
            [KeyboardButton(text="📄 DOCX"), KeyboardButton(text="📕 PDF")],
            [KeyboardButton(text="⏹️ Выйти из режима")],
        ],
        resize_keyboard=True,
    )


def review_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ + Выполнено", callback_data="pl:review:+"),
                InlineKeyboardButton(text="➖ - Не выполнено", callback_data="pl:review:-"),
            ],
            [InlineKeyboardButton(text="🌓 +- Частично", callback_data="pl:review:partial")],
        ]
    )


def planner_markup_for_state(
    service: PlannerService, user_id: int
) -> InlineKeyboardMarkup | ReplyKeyboardMarkup | None:
    session = service.store.session(user_id)
    if not session:
        return None

    state = session["state"]
    payload = service.store.session_payload(user_id)

    if state == "planning_why":
        if payload.get("suggested_why"):
            return InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Использовать", callback_data="pl:why:yes"),
                InlineKeyboardButton(text="✏️ Другая причина", callback_data="pl:why:other"),
            ], [
                InlineKeyboardButton(text="⏭ Оставить пустым", callback_data="pl:why:skip"),
            ]])
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⏭ Оставить пустым", callback_data="pl:why:skip"),
        ]])

    if state == "planning_conflict":
        rows = []
        if payload.get("moves"):
            rows.append(
                [
                    InlineKeyboardButton(
                        text="✅ Перенести и добавить",
                        callback_data="pl:conf:yes",
                    )
                ]
            )
            rows.append(
                [
                    InlineKeyboardButton(
                        text="❌ Не переносить",
                        callback_data="pl:conf:no",
                    )
                ]
            )
        else:
            for index, pair in enumerate(payload.get("alternatives", [])[:3]):
                rows.append(
                    [
                        InlineKeyboardButton(
                            text=(
                                f"🕐 {pair[0] // 60:02d}:{pair[0] % 60:02d}–"
                                f"{pair[1] // 60:02d}:{pair[1] % 60:02d}"
                            ),
                            callback_data=f"pl:alt:{index}",
                        )
                    ]
                )
            rows.append(
                [InlineKeyboardButton(text="❌ Отмена", callback_data="pl:conf:no")]
            )
        return InlineKeyboardMarkup(inline_keyboard=rows)

    if state == "review_full_confirm":
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Сохранить", callback_data="pl:full:yes"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="pl:full:no"),
        ]])

    if state == "review_status":
        return review_keyboard()

    if state == "review_edit_select":
        target_day = date.fromisoformat(session["target_day"])
        items = [
            item
            for item in service.store.reviews(user_id, target_day)
            if item.status is not None
        ]
        rows = [
            [
                InlineKeyboardButton(
                    text=f"{index + 1}. {item.plan_item.title[:35]}",
                    callback_data=f"pl:edit:{item.plan_item.id}",
                )
            ]
            for index, item in enumerate(items)
        ]
        return InlineKeyboardMarkup(inline_keyboard=rows) if rows else planner_keyboard()

    if state == "review_final1":
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⏭ Пропустить",
                        callback_data="pl:skip:final1",
                    )
                ]
            ]
        )

    if state == "review_final2":
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⏭ Пропустить",
                        callback_data="pl:skip:final2",
                    )
                ]
            ]
        )

    return planner_keyboard()


def create_planner_router(
    *, service: PlannerService, allowed_user_ids: frozenset[int]
) -> Router:
    planner_tz = ZoneInfo(service.config.timezone)

    def planner_today() -> date:
        return datetime.now(planner_tz).date()

    router = Router(name="planner")

    def active_planner_day(user_id: int) -> date:
        session = service.store.session(user_id)
        if session and session["target_day"]:
            return date.fromisoformat(session["target_day"])
        return planner_today() + timedelta(days=1)

    def allowed(message: Message) -> bool:
        return (
            message.chat.type == ChatType.PRIVATE
            and message.from_user is not None
            and message.from_user.id in allowed_user_ids
        )

    @router.message(Command("plan"))
    async def on_plan(message: Message) -> None:
        if allowed(message):
            response = await service.start_planning(
                message.from_user.id, planner_today() + timedelta(days=1)
            )
            if response:
                await message.answer(response, reply_markup=planner_keyboard())

    @router.message(Command("review"))
    async def on_review_command(message: Message) -> None:
        if allowed(message):
            await message.answer(
                await service.start_review(message.from_user.id, planner_today()),
                reply_markup=planner_markup_for_state(service, message.from_user.id),
            )

    @router.message(F.text == "📅 Планирование дня")
    async def on_planning(message: Message) -> None:
        if allowed(message):
            response = await service.start_planning(
                message.from_user.id, planner_today() + timedelta(days=1)
            )
            if response:
                await message.answer(response, reply_markup=planner_keyboard())

    @router.message(F.text == "📋 Текущий план")
    async def on_plan_tomorrow(message: Message) -> None:
        if allowed(message):
            await message.answer(
                await service.show_plan(
                    message.from_user.id, active_planner_day(message.from_user.id)
                ),
                reply_markup=planner_keyboard(),
            )

    @router.message(F.text == "✏️ Редактировать план")
    async def on_plan_edit(message: Message) -> None:
        if allowed(message):
            await message.answer(
                await service.start_plan_edit(
                    message.from_user.id, active_planner_day(message.from_user.id)
                ),
                reply_markup=planner_keyboard(),
            )

    @router.message(F.text == "🔎 Анализ сегодня")
    async def on_review(message: Message) -> None:
        if allowed(message):
            await message.answer(
                await service.start_review(message.from_user.id, planner_today()),
                reply_markup=planner_markup_for_state(service, message.from_user.id),
            )

    @router.message(F.text == "🎙️ Рассказать весь день")
    async def on_full_review(message: Message) -> None:
        if allowed(message):
            await message.answer(
                await service.start_full_review(message.from_user.id, planner_today()),
                reply_markup=planner_markup_for_state(service, message.from_user.id),
            )

    @router.message(F.text == "✏️ Исправить анализ")
    async def on_review_edit(message: Message) -> None:
        if allowed(message):
            await message.answer(
                await service.start_review_edit(message.from_user.id, planner_today()),
                reply_markup=planner_markup_for_state(service, message.from_user.id),
            )

    async def _send_export(message: Message, fmt: str) -> None:
        if not allowed(message):
            return
        session = service.store.session(message.from_user.id)
        day = (
            date.fromisoformat(session["target_day"])
            if session and session.get("target_day")
            else planner_today()
        )
        output = None
        try:
            output = await service.export_day(message.from_user.id, day, fmt)
            logger.info(
                "PLANNER_EXPORT user=%s day=%s format=%s path=%s size=%s",
                message.from_user.id,
                day,
                fmt,
                output,
                output.stat().st_size,
            )
            await message.answer_document(
                FSInputFile(output),
                caption=f"План на {day.strftime('%d.%m.%Y')} — {fmt.upper()}",
            )
        except (ImportError, OSError, RuntimeError, ValueError):
            logger.exception(
                "PLANNER_EXPORT_FAILED user=%s day=%s format=%s",
                message.from_user.id,
                day,
                fmt,
            )
            await message.answer("⚠️ Не удалось сформировать или отправить файл экспорта. Ошибка записана в журнал.")
        finally:
            if output is not None:
                output.unlink(missing_ok=True)

    @router.message(F.text == "📄 DOCX")
    async def on_docx(message: Message) -> None:
        await _send_export(message, "docx")

    @router.message(F.text == "📕 PDF")
    async def on_pdf(message: Message) -> None:
        await _send_export(message, "pdf")

    @router.message(F.text == "⏹️ Выйти из режима")
    async def on_stop(message: Message) -> None:
        if allowed(message):
            await service.stop(message.from_user.id)
            await message.answer(
                "Режим планирования завершён.", reply_markup=planner_keyboard()
            )

    @router.callback_query(lambda callback: callback.data is not None and callback.data.startswith("pl:"))
    async def on_planner_callback(callback: CallbackQuery) -> None:
        if (
            callback.from_user.id not in allowed_user_ids
            or callback.message is None
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await callback.answer()
            return

        replies = await service.handle_callback(
            callback.from_user.id, callback.data or "", planner_today()
        )
        await callback.answer()

        if callback.message:
            for reply in replies:
                await callback.message.answer(
                    reply,
                    reply_markup=planner_markup_for_state(
                        service, callback.from_user.id
                    ),
                )

    @router.message(
        lambda message: (
            message.text is not None
            and message.from_user is not None
            and service.store.session(message.from_user.id) is not None
        )
    )
    async def on_planner_text(message: Message) -> None:
        if not allowed(message) or not message.text or message.text.startswith("/"):
            return

        replies = await service.handle_text(
            message.from_user.id, message.text, planner_today()
        )
        markup = planner_markup_for_state(service, message.from_user.id)
        for reply in replies:
            await message.answer(reply, reply_markup=markup)

    return router
