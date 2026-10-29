"""Telegram UI for private checklist capture and explicit, confirmed actions."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

from ..menu import (
    FACTS_NEW, FACTS_RESUME, FACTS_START, FACTS_TODAY, FACTS_HISTORY,
    HOME, NOTES_LIST, NOTES_NEW, NOTES_START, PLANNER_START,
    TODO_ACTIVE, TODO_CANCEL, TODO_DONE, TODO_NEW, TODO_START,
    TODO_LEGACY, todo_capture_keyboard, todo_keyboard,
)
from ..navigation import NavigationStore
from .service import TodoService
from .store import MAX_PAGE, PAGE_SIZE, TodoItem

_OTHER_BUTTONS = frozenset({
    HOME, FACTS_START, FACTS_NEW, FACTS_RESUME, FACTS_TODAY,
    FACTS_HISTORY, NOTES_START, NOTES_NEW, NOTES_LIST, PLANNER_START,
    TODO_START, TODO_LEGACY, TODO_NEW, TODO_ACTIVE, TODO_DONE,
    TODO_CANCEL, "🛡 Админ-панель", "📅 Планирование дня",
    "📋 Текущий план", "🗓 История планов", "🎙 Расшифровка",
})


def _preview(value: str, limit: int = 35) -> str:
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def item_markup(item: TodoItem) -> InlineKeyboardMarkup:
    action = (
        InlineKeyboardButton(text="↩️ Вернуть в активные",
                             callback_data=f"td:reopen:{item.id}")
        if item.completed_at else
        InlineKeyboardButton(text="✅ Выполнено",
                             callback_data=f"td:done:{item.id}")
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [action],
        [InlineKeyboardButton(text="✏️ Изменить",
                              callback_data=f"td:edit:{item.id}")],
        [InlineKeyboardButton(text="🗑 Удалить",
                              callback_data=f"td:ask:{item.id}")],
    ])


def list_markup(
    items: list[TodoItem], *, completed: bool, page: int, total: int,
) -> InlineKeyboardMarkup | None:
    if not items:
        return None
    rows = [
        [InlineKeyboardButton(
            text=f"#{item.id} · {_preview(item.text)}",
            callback_data=f"td:item:{item.id}",
        )] for item in items
    ]
    nav: list[InlineKeyboardButton] = []
    code = "d" if completed else "a"
    if page > 1:
        nav.append(InlineKeyboardButton(
            text="⬅️", callback_data=f"td:list:{code}:{page - 1}",
        ))
    if page * PAGE_SIZE < total and page < MAX_PAGE:
        nav.append(InlineKeyboardButton(
            text="➡️", callback_data=f"td:list:{code}:{page + 1}",
        ))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def create_todo_router(
    *, service: TodoService, navigation: NavigationStore,
    allowed_user_ids: frozenset[int],
) -> Router:
    router = Router(name="todo")

    def permitted(message: Message) -> bool:
        return (
            message.chat.type == ChatType.PRIVATE
            and message.from_user is not None
            and message.from_user.id in allowed_user_ids
        )

    async def send_list(
        message: Message, user_id: int, *, completed: bool, page: int = 1
    ) -> None:
        try:
            body, items, total = service.list_message(
                user_id, completed=completed, page=page,
            )
        except ValueError:
            await message.answer("Номер страницы должен быть от 1 до 50.")
            return
        await message.answer(
            body, parse_mode=None,
            reply_markup=list_markup(
                items, completed=completed, page=page, total=total,
            ),
        )

    @router.message(Command("todo"))
    async def on_todo_command(message: Message) -> None:
        if not permitted(message):
            return
        navigation.activate(message.from_user.id, "todo")
        await message.answer(
            "✅ Список дел\nВыбери действие. Даты и напоминания пока не используются.",
            reply_markup=todo_keyboard(),
        )

    @router.message(Command("todo_add"))
    @router.message(F.text == TODO_NEW)
    async def on_add(message: Message) -> None:
        if not permitted(message):
            return
        user_id = message.from_user.id
        if message.text == TODO_NEW and navigation.current(user_id)[0] != "todo":
            await message.answer("Сначала открой модуль «✅ Список дел».")
            return
        service.store.begin_capture(user_id)
        navigation.activate(user_id, "todo_capture")
        await message.answer(
            "➕ Новое дело\n\nОтправь ОДНО голосовое или текстовое сообщение. "
            "Это станет одним делом. Даты и сроки не назначаются.\n"
            "Отмена: /todo_cancel.",
            reply_markup=todo_capture_keyboard(),
        )

    @router.message(Command("todo_cancel"))
    @router.message(F.text == TODO_CANCEL)
    async def on_cancel(message: Message) -> None:
        if not permitted(message):
            return
        user_id = message.from_user.id
        if (message.text == TODO_CANCEL
                and navigation.current(user_id)[0] != "todo_capture"):
            return
        cancelled = service.store.cancel_capture(user_id)
        if navigation.current(user_id)[0] == "todo_capture":
            navigation.activate(user_id, "todo")
        await message.answer(
            "Ввод дела отменён." if cancelled else "Ожидания нового дела нет.",
            reply_markup=todo_keyboard(),
        )

    @router.message(Command("todo_active"))
    @router.message(F.text == TODO_ACTIVE)
    async def on_active(message: Message) -> None:
        if not permitted(message):
            return
        if message.text == TODO_ACTIVE and navigation.current(message.from_user.id)[0] != "todo":
            await message.answer("Сначала открой модуль «✅ Список дел».")
            return
        navigation.activate(message.from_user.id, "todo")
        await send_list(message, message.from_user.id, completed=False)

    @router.message(Command("todo_done"))
    @router.message(F.text == TODO_DONE)
    async def on_done(message: Message) -> None:
        if not permitted(message):
            return
        if message.text == TODO_DONE and navigation.current(message.from_user.id)[0] != "todo":
            await message.answer("Сначала открой модуль «✅ Список дел».")
            return
        navigation.activate(message.from_user.id, "todo")
        await send_list(message, message.from_user.id, completed=True)

    async def active_capture_text(message: Message) -> bool:
        """SQLite checks must run on the event-loop thread."""
        return (
            permitted(message)
            and message.text is not None
            and not message.text.startswith("/")
            and message.text not in _OTHER_BUTTONS
            and navigation.current(message.from_user.id)[0] == "todo_capture"
        )

    @router.message(active_capture_text)
    async def on_capture_text(message: Message) -> None:
        user_id = message.from_user.id
        capture = service.store.pending(user_id)
        if capture is None:
            await message.answer(
                "Ввод истёк. Нажми «⬅️ Главное меню», затем открой «✅ Список дел»."
            )
            return
        if capture.claimed:
            await message.answer(
                "Голосовое уже принято в обработку. Дождись результата "
                "или нажми «❌ Отменить ввод»."
            )
            return
        try:
            item = service.store.capture_text(user_id, capture.token, message.text)
        except ValueError:
            await message.answer("Дело должно содержать от 1 до 1000 символов.")
            return
        if item is None:
            await message.answer("Ввод изменился или истёк. Начни добавление заново.")
            return
        navigation.activate(user_id, "todo")
        await message.answer(
            service.saved_message(item, edited=capture.mode == "edit"),
            parse_mode=None, reply_markup=todo_keyboard(),
        )
        await message.answer(
            service.item_message(item), reply_markup=item_markup(item),
        )

    @router.callback_query(F.data.startswith("td:"))
    async def on_callback(callback: CallbackQuery) -> None:
        if (
            callback.from_user.id not in allowed_user_ids
            or callback.message is None
            or callback.message.chat.type != ChatType.PRIVATE
        ):
            await callback.answer()
            return
        user_id = callback.from_user.id
        if navigation.current(user_id)[0] != "todo":
            await callback.answer(
                "Сначала открой модуль «✅ Список дел».", show_alert=True,
            )
            return
        parts = (callback.data or "").split(":")
        action = parts[1] if len(parts) >= 2 else ""

        if action == "list" and len(parts) == 4:
            if parts[2] not in {"a", "d"} or not parts[3].isdigit():
                await callback.answer("Неизвестная страница.", show_alert=True)
                return
            page = int(parts[3])
            if not 1 <= page <= MAX_PAGE:
                await callback.answer("Неверная страница.", show_alert=True)
                return
            await callback.answer()
            await send_list(callback.message, user_id, completed=parts[2] == "d", page=page)
            return

        if action == "cancel_del" and len(parts) == 2:
            service.store.cancel_delete(user_id)
            await callback.answer("Удаление отменено.")
            return

        if action not in {"item", "done", "reopen", "edit", "ask", "del"}:
            await callback.answer("Неизвестная кнопка.", show_alert=True)
            return
        if len(parts) != (4 if action == "del" else 3) or not parts[2].isdigit():
            await callback.answer("Неверная кнопка.", show_alert=True)
            return
        item_id = int(parts[2])
        item = service.store.get(user_id, item_id)
        if item is None:
            await callback.answer("Дело не найдено.", show_alert=True)
            return

        if action == "del":
            if not service.store.confirm_delete(user_id, item_id, parts[3]):
                await callback.answer(
                    "Подтверждение устарело или недействительно.", show_alert=True,
                )
                return
            await callback.answer()
            await callback.message.answer(f"🗑 Дело #{item_id} удалено.")
            return

        if action == "ask":
            token = service.store.ask_delete(user_id, item_id)
            if token is None:
                await callback.answer("Дело не найдено.", show_alert=True)
                return
            await callback.answer()
            await callback.message.answer(
                f"Удалить дело #{item_id}?\n\n{item.text}\n\n"
                "Подтверждение действует 2 минуты.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(
                        text="🗑 Да, удалить",
                        callback_data=f"td:del:{item_id}:{token}",
                    )],
                    [InlineKeyboardButton(
                        text="↩️ Не удалять",
                        callback_data="td:cancel_del",
                    )],
                ]),
            )
            return

        if action == "edit":
            service.store.begin_capture(user_id, mode="edit", item_id=item_id)
            navigation.activate(user_id, "todo_capture")
            await callback.answer()
            await callback.message.answer(
                f"✏️ Исправляем дело #{item_id}\n\nСейчас:\n{item.text}\n\n"
                "Отправь новый текст или одно голосовое. "
                "Оно заменит только это дело.",
                reply_markup=todo_capture_keyboard(),
            )
            return

        if action in {"done", "reopen"}:
            item = service.store.set_completed(
                user_id, item_id, completed=action == "done",
            )
            if item is None:
                await callback.answer("Дело не найдено.", show_alert=True)
                return
        await callback.answer()
        await callback.message.answer(
            service.item_message(item), reply_markup=item_markup(item),
        )

    return router
