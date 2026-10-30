"""Owner-only Telegram UI for vetted announcements; no automatic publication."""

from __future__ import annotations

import hashlib
import logging

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
from ..menu import (
    ADMIN_PANEL,
    FACTS_START,
    HOME,
    NOTES_START,
    PLANNER_START,
    REPORT_BUTTON,
    TODO_ACTIVE,
    TODO_CANCEL,
    TODO_DONE,
    TODO_NEW,
    TODO_START,
)
from ..navigation import NavigationStore
from .store import Announcement, AnnouncementStore

MENU_BUTTONS = frozenset({
    ADMIN_PANEL, FACTS_START, HOME, NOTES_START, PLANNER_START,
    REPORT_BUTTON, TODO_ACTIVE, TODO_CANCEL, TODO_DONE, TODO_NEW, TODO_START,
})

logger = logging.getLogger(__name__)


def _buttons(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
        for row in rows
    ])


def _home_markup() -> InlineKeyboardMarkup:
    return _buttons([
        [("➕ Создать объявление", "news:new")],
        [("📝 Черновики и история", "news:history")],
    ])


def _audience_stamp(recipient_ids: list[int]) -> str:
    """Bind owner confirmation to the exact approved audience seen in preview."""
    canonical = ",".join(str(uid) for uid in sorted(recipient_ids))
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()[:16]


def _preview_markup(
    announcement: Announcement, *, enabled: bool, audience_stamp: str,
) -> InlineKeyboardMarkup:
    rows = [
        [("✏️ Изменить", f"news:edit:{announcement.id}")],
    ]
    if enabled:
        rows.append([(
            "✅ Подтвердить рассылку",
            f"news:send:{announcement.id}:{announcement.revision}:"
            f"{announcement.token}:{audience_stamp}",
        )])
    rows.extend([
        [("👁 Проверить текст на себе", f"news:self:{announcement.id}")],
        [("🗑 Отменить", f"news:cancel:{announcement.id}")],
    ])
    return _buttons(rows)


def _status_markup(announcement: Announcement) -> InlineKeyboardMarkup:
    rows: list[list[tuple[str, str]]] = [
        [("📊 Обновить статус", f"news:status:{announcement.id}")],
    ]
    if announcement.state == "queued":
        rows.append([("⏸ Приостановить", f"news:pause:{announcement.id}")])
    if announcement.state == "paused":
        rows.append([("▶️ Продолжить", f"news:resume:{announcement.id}")])
    if announcement.state in {"queued", "paused"}:
        rows.append([("🛑 Остановить рассылку", f"news:cancel:{announcement.id}")])
    return _buttons(rows)


def create_notification_router(
    *, store: AnnouncementStore, access: AccessStore,
    navigation: NavigationStore, enabled: bool = False,
) -> Router:
    router = Router(name="admin_notifications")
    owner_id = access.admin_id

    def owner(uid: int) -> bool:
        return (
            owner_id is not None and uid == owner_id
            and access.is_allowed(uid)
        )

    def private_owner(message: Message) -> bool:
        return (
            message.chat.type == ChatType.PRIVATE
            and message.from_user is not None
            and owner(message.from_user.id)
        )

    def eligible_recipients() -> list[int]:
        return [
            uid for uid in access.allowed_ids()
            if uid != owner_id and access.is_allowed(uid) and not store.opted_out(uid)
        ]

    async def show_preview(message: Message, item: Announcement) -> None:
        recipients = eligible_recipients()
        await message.answer(
            "📝 Предпросмотр объявления\n\n"
            f"{item.body or '(текст ещё не введён)'}\n\n"
            f"Версия: {item.revision}\n"
            f"Получателей сейчас: {len(recipients)}\n"
            "Список будет зафиксирован при подтверждении.\n"
            + (
                "⚠ Рассылка пока отключена на сервере. Можно подготовить черновик."
                if not enabled else
                "Это сообщение будет разослано только после нажатия подтверждения."
            ),
            reply_markup=_preview_markup(
                item, enabled=enabled,
                audience_stamp=_audience_stamp(recipients),
            ) if item.body else
            _buttons([[("✏️ Ввести текст", f"news:edit:{item.id}")]]),
            parse_mode=None,
        )

    async def show_status(message: Message, item: Announcement) -> None:
        counts = store.summary(owner_id, item.id)
        await message.answer(
            f"📣 Рассылка #{item.id} · {item.state}\n\n"
            f"Отправлено: {counts.get('sent', 0)}\n"
            f"Не доставлено: {counts.get('blocked', 0) + counts.get('failed', 0)}\n"
            f"Неизвестный результат: {counts.get('uncertain', 0)}\n"
            f"Пропущено: {counts.get('skipped', 0)}\n"
            f"Осталось: {counts.get('pending', 0) + counts.get('sending', 0)}\n"
            "Статус «отправлено» не подтверждает прочтение.",
            reply_markup=_status_markup(item),
        )

    @router.message(Command("notify_off"))
    async def on_optout(message: Message) -> None:
        if (
            message.from_user is None or message.chat.type != ChatType.PRIVATE
            or not access.is_allowed(message.from_user.id)
        ):
            return
        store.optout(message.from_user.id, enabled=True)
        await message.answer(
            "🔕 Объявления Voqelis отключены. "
            "Личные напоминания о делах продолжат приходить. "
            "Вернуть объявления: /notify_on"
        )

    @router.message(Command("notify_on"))
    async def on_optin(message: Message) -> None:
        if (
            message.from_user is None or message.chat.type != ChatType.PRIVATE
            or not access.is_allowed(message.from_user.id)
        ):
            return
        store.optout(message.from_user.id, enabled=False)
        await message.answer("✅ Объявления Voqelis снова включены.")

    @router.message(Command("announce"))
    async def on_announce(message: Message) -> None:
        if not private_owner(message):
            return
        await message.answer(
            "📣 Уведомления пользователям\n\n"
            "Ты можешь подготовить текст, изменить его и подтвердить отправку. "
            "Обычные изменения проекта не публикуются автоматически.",
            reply_markup=_home_markup(),
        )

    async def awaiting_text(message: Message) -> bool:
        return (
            private_owner(message)
            and message.text is not None
            and not message.text.startswith("/")
            and message.text not in MENU_BUTTONS
            and navigation.current(message.from_user.id)[0] == "home"
            and store.awaiting(message.from_user.id)
        )

    @router.message(awaiting_text)
    async def on_text(message: Message) -> None:
        uid = message.from_user.id
        try:
            item = store.submit_text(uid, message.text)
        except ValueError as exc:
            await message.answer(f"⚠ {exc}")
            return
        if item is None:
            await message.answer("Черновик устарел. Открой /announce заново.")
            return
        await show_preview(message, item)

    @router.callback_query(F.data.startswith("news:"))
    async def on_callback(callback: CallbackQuery, bot: object) -> None:
        if (
            callback.message is None or callback.message.chat.type != ChatType.PRIVATE
            or not owner(callback.from_user.id)
        ):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        parts = (callback.data or "").split(":")
        if len(parts) < 2:
            await callback.answer("Неизвестная команда.", show_alert=True)
            return
        action = parts[1]
        uid = callback.from_user.id

        if action == "home" and len(parts) == 2:
            await callback.answer()
            await callback.message.answer(
                "📣 Уведомления пользователям\n"
                "Создай объявление или открой историю.\n"
                + (
                    "⚠ Отправка отключена в настройках VPS."
                    if not enabled else "Публикация только после подтверждения."
                ),
                reply_markup=_home_markup(),
            )
            return

        if action == "new" and len(parts) == 2:
            navigation.activate(uid, "home")
            item = store.create(uid)
            await callback.answer()
            await callback.message.answer(
                f"📝 Черновик #{item.id}\n"
                "Пришли текст нового объявления одним сообщением.\n"
                "Он не отправится без твоего подтверждения.",
                reply_markup=_buttons([
                    [("🗑 Отменить черновик", f"news:cancel:{item.id}")],
                ]),
            )
            return

        if action == "history" and len(parts) == 2:
            entries = store.latest(uid)
            rows = [
                [(f"#{e.id} · {e.state}", f"news:preview:{e.id}"
                  if e.state == "draft" else f"news:status:{e.id}")]
                for e in entries
            ]
            await callback.answer()
            await callback.message.answer(
                "📋 Последние объявления" if entries else "Черновиков и рассылок нет.",
                reply_markup=_buttons(rows) if rows else _home_markup(),
            )
            return

        if len(parts) < 3 or not parts[2].isdigit():
            await callback.answer("Некорректный ID.", show_alert=True)
            return
        aid = int(parts[2])
        item = store.get(uid, aid)
        if item is None:
            await callback.answer("Объявление недоступно.", show_alert=True)
            return

        if action == "preview" and len(parts) == 3 and item.state == "draft":
            await callback.answer()
            await show_preview(callback.message, item)
            return
        if action == "edit" and len(parts) == 3:
            if not store.begin_edit(uid, aid):
                await callback.answer("Редактирование недоступно.", show_alert=True)
                return
            navigation.activate(uid, "home")
            await callback.answer()
            await callback.message.answer(
                "✏️ Пришли исправленный текст одним сообщением. "
                "Старое подтверждение станет недействительным."
            )
            return
        if action == "self" and len(parts) == 3 and item.state == "draft":
            await callback.answer("Отправляю предварительную версию себе.")
            try:
                await bot.send_message(
                    uid, "👁 Тестовый просмотр (только тебе)\n\n" + item.body,
                    parse_mode=None,
                )
            except Exception:  # noqa: BLE001
                logger.warning("ANNOUNCEMENT_SELF_PREVIEW_FAILED")
                await callback.message.answer("⚠ Не удалось отправить тест себе.")
            return
        if action == "send" and len(parts) == 6 and parts[3].isdigit():
            if not enabled:
                await callback.answer("Рассылка пока отключена.", show_alert=True)
                return
            recipients = eligible_recipients()
            if parts[5] != _audience_stamp(recipients):
                await callback.answer(
                    "Список получателей изменился. Проверь новый предпросмотр.",
                    show_alert=True,
                )
                await show_preview(callback.message, item)
                return
            count = store.confirmation(
                uid, aid, int(parts[3]), parts[4], recipients,
            )
            if count is None:
                await callback.answer(
                    "Подтверждение устарело или список получателей пуст.",
                    show_alert=True,
                )
                return
            await callback.answer("Подтверждено.")
            await callback.message.answer(
                f"📤 Рассылка #{aid} поставлена в очередь: {count} получателей.",
                reply_markup=_status_markup(store.get(uid, aid)),
            )
            return
        if action in {"pause", "resume", "cancel"} and len(parts) == 3:
            changed = {
                "pause": store.pause,
                "resume": store.resume,
                "cancel": store.cancel,
            }[action](uid, aid)
            await callback.answer(
                "Состояние изменено." if changed else "Действие уже недоступно.",
                show_alert=not changed,
            )
            if changed:
                await show_status(callback.message, store.get(uid, aid))
            return
        if action == "status" and len(parts) == 3:
            await callback.answer()
            await show_status(callback.message, item)
            return
        await callback.answer("Действие недоступно.", show_alert=True)

    return router
