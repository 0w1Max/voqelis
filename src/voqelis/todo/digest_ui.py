"""Telegram settings for daily active-task summaries."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ..access import AccessStore
from ..menu import TODO_SETTINGS
from ..navigation import NavigationStore
from .bot import _OTHER_BUTTONS, list_markup
from .digest import DigestPreferences, TodoDigestRepository, clock
from .service import TodoService
from .settings import TodoSettingsRepository

PRESETS = (540, 720, 960, 1080, 1260)


def _keyboard(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=title, callback_data=value) for title, value in row]
        for row in rows
    ])


def _panel(prefs: DigestPreferences, nav_revision: int, timezone_rev: int):
    prefix = f"td:digest:"
    rows = [
        [("🔕 Отключить" if prefs.enabled else "🔔 Включить",
          f"{prefix}toggle:{nav_revision}:{prefs.revision}")],
        [("➕ Добавить время", f"{prefix}add:{nav_revision}:{prefs.revision}")],
    ]
    rows.extend([
        [(f"🗑 {clock(minute)}",
          f"{prefix}remove:{nav_revision}:{prefs.revision}:{minute}")]
        for minute in prefs.times
    ])
    rows.append([(
        "⚙️ Назад к настройкам", f"td:settings:show:{nav_revision}:{timezone_rev}",
    )])
    return _keyboard(rows)


def _add_picker(prefs: DigestPreferences, nav_revision: int, timezone_rev: int):
    rows = [
        [(clock(m), f"td:digest:pick:{nav_revision}:{prefs.revision}:{m}")
         for m in PRESETS[:3]],
        [(clock(m), f"td:digest:pick:{nav_revision}:{prefs.revision}:{m}")
         for m in PRESETS[3:]],
        [("⌨️ Другое время", f"td:digest:type:{nav_revision}:{prefs.revision}")],
        [("⬅️ Назад", f"td:digest:menu:{nav_revision}:{prefs.revision}")],
    ]
    return _keyboard(rows)


def _summary(prefs: DigestPreferences) -> str:
    status = "Включена" if prefs.enabled else "Выключена"
    times = ", ".join(clock(x) for x in prefs.times) or "Не задано"
    return (
        f"🔔 Ежедневная сводка: {status}\n"
        f"⏰ Время: {times}\n\n"
        "Напоминаем обо всех активных делах. Если дел нет, сообщение не приходит.\n"
        "Можно выбрать до пяти времён на каждый день."
    )


def create_todo_digest_router(
    *, digests: TodoDigestRepository, timezones: TodoSettingsRepository,
    navigation: NavigationStore, access: AccessStore,
) -> Router:
    router = Router(name="todo_digest")

    async def digest_text(message: Message) -> bool:
        if (
            message.from_user is None or message.chat.type != ChatType.PRIVATE
            or not access.is_allowed(message.from_user.id)
            or message.text is None or message.text.startswith("/")
            or message.text in _OTHER_BUTTONS or message.text == TODO_SETTINGS
        ):
            return False
        uid = message.from_user.id
        section, revision = navigation.current(uid)
        draft = digests.pending_input(uid)
        return section == "todo" and draft is not None and draft.nav_revision == revision

    @router.message(digest_text)
    async def on_digest_text(message: Message) -> None:
        uid = message.from_user.id
        revision = navigation.current(uid)[1]
        draft = digests.pending_input(uid)
        if draft is None or draft.nav_revision != revision:
            return
        try:
            updated = digests.submit_input(uid, draft.token, revision, message.text)
        except ValueError as exc:
            await message.answer(f"⚠ {exc}")
            return
        if updated is None:
            await message.answer("Настройки изменились. Открой раздел сводки заново.")
            return
        await message.answer(
            "✅ Время добавлено.\n\n" + _summary(updated),
            reply_markup=_panel(updated, revision, timezones.get(uid).revision),
        )

    @router.callback_query(F.data.startswith("td:digest:"))
    async def on_digest_callback(callback: CallbackQuery) -> None:
        if (
            callback.message is None or callback.message.chat.type != ChatType.PRIVATE
            or not access.is_allowed(callback.from_user.id)
        ):
            await callback.answer("Нет доступа.", show_alert=True)
            return

        uid = callback.from_user.id
        parts = (callback.data or "").split(":")
        action = parts[2] if len(parts) >= 3 else ""
        if action == "open" and len(parts) == 3:
            navigation.activate(uid, "todo")
            service = TodoService(digests.todo)
            body, items, total = service.list_message(uid, completed=False, page=1)
            await callback.answer()
            await callback.message.answer(
                body, reply_markup=list_markup(items, completed=False, page=1, total=total),
                parse_mode=None,
            )
            return

        section, revision = navigation.current(uid)
        current = digests.get(uid)
        if (
            section != "todo" or len(parts) not in {5, 6}
            or action not in {"menu", "toggle", "add", "pick", "remove", "type"}
            or not parts[3].isdigit() or not parts[4].isdigit()
            or revision != int(parts[3]) or current.revision != int(parts[4])
        ):
            await callback.answer("Кнопка устарела. Открой настройки заново.",
                                  show_alert=True)
            return

        if action == "menu" and len(parts) == 5:
            # Returning to the panel disarms any open custom-time entry.
            digests.cancel_input(uid)
            await callback.answer()
            await callback.message.answer(
                _summary(current),
                reply_markup=_panel(current, revision, timezones.get(uid).revision),
            )
            return
        if action == "add" and len(parts) == 5:
            if len(current.times) >= 5:
                await callback.answer("Лимит: 5 напоминаний в день.", show_alert=True)
                return
            await callback.answer()
            await callback.message.answer(
                "⏰ Выбери время или напиши своё:",
                reply_markup=_add_picker(current, revision, timezones.get(uid).revision),
            )
            return
        if action == "type" and len(parts) == 5:
            if len(current.times) >= 5:
                await callback.answer("Лимит: 5 времён.", show_alert=True)
                return
            digests.begin_input(uid, nav_revision=revision, digest_revision=current.revision)
            await callback.answer()
            await callback.message.answer(
                "⌨️ Напиши одно время в формате ЧЧ:ММ, например 07:45.\n"
                "Для отмены открой «⚙️ Настройки»."
            )
            return

        try:
            if action == "toggle" and len(parts) == 5:
                updated = digests.toggle(uid, current.revision)
            elif action in {"pick", "remove"} and len(parts) == 6 and parts[5].isdigit():
                minute = int(parts[5])
                updated = (
                    digests.add(uid, current.revision, minute)
                    if action == "pick" else digests.remove(uid, current.revision, minute)
                )
            else:
                await callback.answer("Неизвестная кнопка.", show_alert=True)
                return
        except ValueError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        if updated is None:
            await callback.answer("Настройки уже изменились.", show_alert=True)
            return
        await callback.answer("Сохранено.")
        await callback.message.answer(
            _summary(updated),
            reply_markup=_panel(updated, revision, timezones.get(uid).revision),
        )

    return router
