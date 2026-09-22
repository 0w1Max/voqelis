from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup

from .service import PlannerService


def planner_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📅 Планирование дня")],
            [KeyboardButton(text="📋 План на завтра"), KeyboardButton(text="🔎 Анализ сегодня")],
            [KeyboardButton(text="✏️ Исправить анализ")],
            [KeyboardButton(text="⏹️ Выйти из режима")],
        ],
        resize_keyboard=True,
    )


def review_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="+"), KeyboardButton(text="-"), KeyboardButton(text="+-")]],
        resize_keyboard=True,
    )


def create_planner_router(*, service: PlannerService, allowed_user_ids: frozenset[int]) -> Router:
    planner_tz = ZoneInfo(service.config.timezone)

    def planner_today() -> date:
        return datetime.now(planner_tz).date()
    router = Router(name="planner")

    def allowed(message: Message) -> bool:
        return (
            message.chat.type == ChatType.PRIVATE
            and message.from_user is not None
            and message.from_user.id in allowed_user_ids
        )

    @router.message(Command("plan"))
    async def on_plan(message: Message) -> None:
        if allowed(message):
            await message.answer(
                service.start_planning(message.from_user.id, planner_today() + timedelta(days=1)),
                reply_markup=planner_keyboard(),
            )

    @router.message(Command("review"))
    async def on_review_command(message: Message) -> None:
        if allowed(message):
            await message.answer(
                await service.start_review(message.from_user.id, planner_today()),
                reply_markup=review_keyboard(),
            )

    @router.message(F.text == "📅 Планирование дня")
    async def on_planning(message: Message) -> None:
        if allowed(message):
            await message.answer(
                service.start_planning(message.from_user.id, planner_today() + timedelta(days=1)),
                reply_markup=planner_keyboard(),
            )

    @router.message(F.text == "📋 План на завтра")
    async def on_plan_tomorrow(message: Message) -> None:
        if allowed(message):
            await message.answer(
                await service.show_plan(message.from_user.id, planner_today() + timedelta(days=1)),
                reply_markup=planner_keyboard(),
            )

    @router.message(F.text == "🔎 Анализ сегодня")
    async def on_review(message: Message) -> None:
        if allowed(message):
            await message.answer(
                await service.start_review(message.from_user.id, planner_today()),
                reply_markup=review_keyboard(),
            )

    @router.message(F.text == "✏️ Исправить анализ")
    async def on_review_edit(message: Message) -> None:
        if allowed(message):
            await message.answer(
                await service.start_review_edit(message.from_user.id, planner_today()),
                reply_markup=planner_keyboard(),
            )

    @router.message(F.text == "⏹️ Выйти из режима")
    async def on_stop(message: Message) -> None:
        if allowed(message):
            service.stop(message.from_user.id)
            await message.answer("Режим планирования завершён.", reply_markup=planner_keyboard())

    @router.message(lambda message: message.text is not None and message.from_user is not None and service.store.session(message.from_user.id) is not None)
    async def on_planner_text(message: Message) -> None:
        if not allowed(message) or not message.text or message.text.startswith("/"):
            return
        replies = await service.handle_text(message.from_user.id, message.text, planner_today())
        for reply in replies:
            await message.answer(
                reply,
                reply_markup=review_keyboard() if "Выполнено?" in reply else planner_keyboard(),
            )

    return router
