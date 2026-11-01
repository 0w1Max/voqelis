"""Compact, private Telegram UI for ToDo settings."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ..access import AccessStore
from ..menu import TODO_SETTINGS
from ..navigation import NavigationStore
from .settings import (
    TIMEZONE_CHOICES,
    TodoPreferences,
    TodoSettingsRepository,
    timezone_label,
)

PAGE_SIZE = 8


def _keyboard(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=value) for label, value in row]
        for row in rows
    ])


def _overview(prefs: TodoPreferences, nav_revision: int) -> InlineKeyboardMarkup:
    return _keyboard([[
        ("🌍 Изменить часовой пояс",
         f"td:settings:choose:{nav_revision}:{prefs.revision}:0"),
    ]])


def _choices(prefs: TodoPreferences, nav_revision: int, page: int) -> InlineKeyboardMarkup:
    start = page * PAGE_SIZE
    entries = []
    for index in range(start, min(start + PAGE_SIZE, len(TIMEZONE_CHOICES))):
        label, zone = TIMEZONE_CHOICES[index]
        caption = ("✓ " if zone == prefs.timezone else "") + label
        entries.append(
            (caption, f"td:settings:set:{nav_revision}:{prefs.revision}:{index}")
        )
    rows = [entries[i:i + 2] for i in range(0, len(entries), 2)]
    navigation = []
    if page > 0:
        navigation.append(
            ("⬅️", f"td:settings:choose:{nav_revision}:{prefs.revision}:{page - 1}")
        )
    if start + PAGE_SIZE < len(TIMEZONE_CHOICES):
        navigation.append(
            ("➡️", f"td:settings:choose:{nav_revision}:{prefs.revision}:{page + 1}")
        )
    if navigation:
        rows.append(navigation)
    rows.append([("⚙️ Назад к настройкам",
                  f"td:settings:show:{nav_revision}:{prefs.revision}")])
    return _keyboard(rows)


def create_todo_settings_router(
    *, settings: TodoSettingsRepository, navigation: NavigationStore, access: AccessStore,
) -> Router:
    router = Router(name="todo_settings")

    @router.message(Command("todo_settings"))
    @router.message(F.text == TODO_SETTINGS)
    async def on_settings(message: Message) -> None:
        if (
            message.from_user is None or message.chat.type != ChatType.PRIVATE
            or not access.is_allowed(message.from_user.id)
        ):
            return
        uid = message.from_user.id
        section, revision = navigation.current(uid)
        if section != "todo":
            await message.answer("Сначала открой «✅ Список дел».")
            return
        prefs = settings.get(uid)
        await message.answer(
            "⚙️ Настройки списка дел\n\n"
            f"🌍 Часовой пояс: {timezone_label(prefs.timezone)}\n\n"
            "Выбранный пояс применяется к новым напоминаниям. "
            "Ранее установленные напоминания не сдвигаются.",
            reply_markup=_overview(prefs, revision),
        )

    @router.callback_query(F.data.startswith("td:settings:"))
    async def on_settings_callback(callback: CallbackQuery) -> None:
        if (
            callback.message is None or callback.message.chat.type != ChatType.PRIVATE
            or not access.is_allowed(callback.from_user.id)
        ):
            await callback.answer("Нет доступа.", show_alert=True)
            return

        uid = callback.from_user.id
        parts = (callback.data or "").split(":")
        section, nav_revision = navigation.current(uid)
        prefs = settings.get(uid)
        if (
            len(parts) not in {5, 6} or parts[2] not in {"show", "choose", "set"}
            or not parts[3].isdigit() or not parts[4].isdigit()
            or section != "todo" or int(parts[3]) != nav_revision
            or int(parts[4]) != prefs.revision
        ):
            await callback.answer("Кнопка устарела. Открой настройки заново.",
                                  show_alert=True)
            return

        action = parts[2]
        if action == "show" and len(parts) == 5:
            await callback.answer()
            await callback.message.answer(
                f"⚙️ Настройки списка дел\n🌍 Часовой пояс: {timezone_label(prefs.timezone)}",
                reply_markup=_overview(prefs, nav_revision),
            )
            return

        if action == "choose" and len(parts) == 6 and parts[5].isdigit():
            page = int(parts[5])
            if not 0 <= page < (len(TIMEZONE_CHOICES) + PAGE_SIZE - 1) // PAGE_SIZE:
                await callback.answer("Страница недоступна.", show_alert=True)
                return
            await callback.answer()
            await callback.message.answer(
                "🌍 Выбери город, по времени которого назначать новые напоминания:",
                reply_markup=_choices(prefs, nav_revision, page),
            )
            return

        if action == "set" and len(parts) == 6 and parts[5].isdigit():
            index = int(parts[5])
            if not 0 <= index < len(TIMEZONE_CHOICES):
                await callback.answer("Неизвестный город.", show_alert=True)
                return
            _, zone = TIMEZONE_CHOICES[index]
            updated = settings.set_timezone(
                uid, zone, expected_revision=prefs.revision,
            )
            if updated is None:
                await callback.answer("Настройки уже изменились. Открой их заново.",
                                      show_alert=True)
                return
            await callback.answer("Сохранено.")
            await callback.message.answer(
                f"✓ Часовой пояс: {timezone_label(updated.timezone)}\n\n"
                "Новые напоминания будут использовать этот пояс. "
                "Уже сохранённые не изменены. Незавершённое назначение напоминания отменено.",
                reply_markup=_overview(updated, nav_revision),
            )
            return

        await callback.answer("Неизвестная кнопка.", show_alert=True)

    return router
