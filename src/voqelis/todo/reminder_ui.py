"""Inline Telegram reminder picker, safe text capture and owner-scoped task actions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from ..access import AccessStore
from ..menu import HOME, TODO_ACTIVE, TODO_DONE, TODO_NEW, TODO_START
from ..navigation import NavigationStore
from .bot import item_markup
from .reminders import ReminderDraft, ReminderRepository
from .temporal import ReminderTimeError, parse_reminder_time

TIMEZONE = "Europe/Moscow"
QUICK_TIMES = ("09:00", "12:00", "16:00", "18:00", "21:00")
MENU_BUTTONS = frozenset({HOME, TODO_NEW, TODO_ACTIVE, TODO_DONE, TODO_START})


def _keyboard(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=data) for label, data in row]
        for row in rows
    ])


def reminder_confirm_keyboard(token: str) -> InlineKeyboardMarkup:
    return _keyboard([
        [("✅ Сохранить напоминание", f"td:rem:ok:{token}")],
        [("❌ Отменить", "td:rem:cancel")],
    ])


def _picker(draft: ReminderDraft) -> InlineKeyboardMarkup:
    return _keyboard([
        [(label, f"td:rem:day:{draft.token}:{offset}") for offset, label
         in enumerate(("Сегодня", "Завтра", "Послезавтра"))],
        [("📅 Другая дата / ⌨️ Написать", f"td:rem:text:{draft.token}")],
        [("🎙 Сказать голосом", f"td:rem:voice:{draft.token}")],
        [("❌ Не сейчас", "td:rem:cancel")],
    ])


def _time_picker(draft: ReminderDraft, selected: datetime) -> InlineKeyboardMarkup:
    day = selected.strftime("%Y%m%d")
    rows = [
        [(v, f"td:rem:at:{draft.token}:{day}:{v.replace(':', '')}")
         for v in QUICK_TIMES[:3]],
        [(v, f"td:rem:at:{draft.token}:{day}:{v.replace(':', '')}")
         for v in QUICK_TIMES[3:]],
        [("⌨️ Другое время", f"td:rem:text:{draft.token}")],
        [("⬅️ Выбрать дату", f"td:rem:choose:{draft.token}")],
    ]
    return _keyboard(rows)


def _display(when: int, timezone: str) -> str:
    return datetime.fromtimestamp(when, ZoneInfo(timezone)).strftime("%d.%m.%Y · %H:%M")


def create_reminder_router(
    *, reminders: ReminderRepository, navigation: NavigationStore,
    access: AccessStore,
) -> Router:
    router = Router(name="todo_reminders")

    def allowed(uid: int) -> bool:
        return access.is_allowed(uid)

    def draft_current(uid: int, token: str) -> ReminderDraft | None:
        draft = reminders.pending(uid)
        if (
            draft is None or draft.token != token
            or draft.navigation_revision != navigation.current(uid)[1]
            or navigation.current(uid)[0] != "todo"
        ):
            return None
        return draft

    async def typed_reminder(message: Message) -> bool:
        if (
            message.from_user is None or message.chat.type != ChatType.PRIVATE
            or not allowed(message.from_user.id)
            or message.text is None or message.text.startswith("/")
            or message.text in MENU_BUTTONS
            or navigation.current(message.from_user.id)[0] != "todo"
        ):
            return False
        draft = reminders.pending(message.from_user.id)
        return draft is not None and draft.navigation_revision == navigation.current(
            message.from_user.id,
        )[1]

    @router.message(typed_reminder)
    async def on_reminder_text(message: Message) -> None:
        uid = message.from_user.id
        draft = reminders.pending(uid)
        if draft is None or draft.navigation_revision != navigation.current(uid)[1]:
            return
        if draft.claimed:
            await message.answer("🎙 Голосовое обрабатывается. Дождись результата.")
            return
        try:
            at = parse_reminder_time(message.text, now=datetime.now(UTC),
                                     timezone=draft.timezone)
            updated = reminders.propose(uid, draft.token, at)
        except ReminderTimeError as exc:
            await message.answer(
                f"⚠ {exc}\nНапример: «завтра в 18:30», «5 ноября в 10:00»."
            )
            return
        except ValueError:
            await message.answer("Указанное время не подходит. Попробуй другое.")
            return
        if updated is None:
            await message.answer("Этот ввод уже устарел. Открой напоминания заново.")
            return
        await message.answer(
            "🔔 Подтверди напоминание:\n"
            f"{_display(updated.proposed_at_utc, updated.timezone)} "
            f"({updated.timezone})",
            reply_markup=reminder_confirm_keyboard(updated.token),
        )

    @router.callback_query(F.data.startswith("td:rem:"))
    async def on_reminder_callback(callback: CallbackQuery) -> None:
        uid = callback.from_user.id
        if (
            callback.message is None
            or callback.message.chat.type != ChatType.PRIVATE
            or not allowed(uid)
        ):
            await callback.answer("Нет доступа.", show_alert=True)
            return
        parts = (callback.data or "").split(":")
        if len(parts) < 3:
            await callback.answer("Неизвестная кнопка.", show_alert=True)
            return
        action = parts[2]
        if action not in {"complete", "open"} and navigation.current(uid)[0] != "todo":
            await callback.answer("Сначала открой «✅ Список дел».", show_alert=True)
            return

        if action == "skip" and len(parts) == 3:
            await callback.answer()
            await callback.message.answer("Дело сохранено без личного напоминания.")
            return
        if action == "cancel" and len(parts) == 3:
            reminders.cancel_draft(uid)
            await callback.answer()
            await callback.message.answer("Настройка напоминания отменена. Дело сохранено.")
            return

        if action in {"new", "list"} and len(parts) == 4 and parts[3].isdigit():
            item_id = int(parts[3])
            task = reminders.todo.get(uid, item_id)
            if task is None:
                await callback.answer("Дело не найдено.", show_alert=True)
                return
            if action == "new":
                if task.completed_at is not None:
                    await callback.answer("Дело уже выполнено.", show_alert=True)
                    return
                draft = reminders.begin(
                    uid, item_id, timezone=TIMEZONE,
                    navigation_revision=navigation.current(uid)[1],
                )
                await callback.answer()
                await callback.message.answer(
                    f"🔔 Когда напомнить о деле?\n\n{task.text}\n\n"
                    "Выбери дату или напиши/скажи её вместе со временем.",
                    reply_markup=_picker(draft),
                )
            else:
                rows = []
                items = reminders.for_item(uid, item_id)
                active = [r for r in items if r.state == "pending"]
                for r in active[:10]:
                    rows.append([
                        (f"🗑 {_display(r.remind_at_utc, r.timezone)}",
                         f"td:rem:del:{item_id}:{r.id}"),
                    ])
                if task.completed_at is None:
                    rows.append([("➕ Добавить", f"td:rem:new:{item_id}")])
                await callback.answer()
                await callback.message.answer(
                    f"🔔 Напоминания о деле #{item_id}\n\n{task.text}\n\n"
                    f"Будущих активных напоминаний: {len(active)}\n"
                    "Для удаления нажми на соответствующую дату.",
                    reply_markup=_keyboard(rows) if rows else None,
                )
            return

        if action == "del" and len(parts) == 5 and all(x.isdigit() for x in parts[3:]):
            item_id, rid = map(int, parts[3:])
            reminder = reminders.get(uid, rid)
            if reminder is None or reminder.item_id != item_id:
                await callback.answer("Не найдено.", show_alert=True)
                return
            done = reminders.cancel(uid, rid)
            await callback.answer("Удалено." if done else "Уже не активно.", show_alert=True)
            return

        if action == "complete" and len(parts) == 4 and parts[3].isdigit():
            current = reminders.todo.get(uid, int(parts[3]))
            if current is None:
                await callback.answer("Дело не найдено.", show_alert=True)
                return
            item = reminders.todo.set_completed(uid, current.id, completed=True)
            await callback.answer("✅ Выполнено." if item else "Не найдено.")
            return
        if action == "open" and len(parts) == 4 and parts[3].isdigit():
            item = reminders.todo.get(uid, int(parts[3]))
            if item is None:
                await callback.answer("Дело не найдено.", show_alert=True)
                return
            navigation.activate(uid, "todo")
            await callback.answer()
            await callback.message.answer(
                f"{'✅' if item.completed_at else '⬜'} Дело #{item.id}\n\n{item.text}",
                reply_markup=item_markup(item), parse_mode=None,
            )
            return

        if action in {"choose", "day", "at", "text", "voice", "ok"}:
            token = parts[3] if len(parts) >= 4 else ""
            draft = draft_current(uid, token)
            if draft is None:
                await callback.answer("Кнопка устарела. Начни настройку снова.",
                                      show_alert=True)
                return
            if draft.claimed:
                await callback.answer("Сначала дождись голосового.", show_alert=True)
                return
            if action == "choose" and len(parts) == 4:
                await callback.answer()
                await callback.message.answer("Выбери дату.", reply_markup=_picker(draft))
                return
            if action == "day" and len(parts) == 5 and parts[4] in {"0", "1", "2"}:
                zone = ZoneInfo(draft.timezone)
                date = datetime.now(zone) + timedelta(days=int(parts[4]))
                await callback.answer()
                await callback.message.answer(
                    f"📅 {date:%d.%m.%Y}\nВо сколько напомнить?",
                    reply_markup=_time_picker(draft, date),
                )
                return
            if action == "at" and len(parts) == 6:
                day, hhmm = parts[4], parts[5]
                if (
                    len(day) != 8 or not day.isdigit()
                    or len(hhmm) != 4 or not hhmm.isdigit()
                ):
                    await callback.answer("Неверная дата.", show_alert=True)
                    return
                text = f"{day[6:8]}.{day[4:6]}.{day[:4]} {hhmm[:2]}:{hhmm[2:]}"
                try:
                    at = parse_reminder_time(
                        text, now=datetime.now(UTC), timezone=draft.timezone,
                    )
                    proposed = reminders.propose(uid, token, at)
                except ValueError as exc:
                    await callback.answer(str(exc), show_alert=True)
                    return
                if proposed is None:
                    await callback.answer("Ввод устарел.", show_alert=True)
                    return
                await callback.answer()
                await callback.message.answer(
                    f"🔔 Подтверди: {_display(proposed.proposed_at_utc, proposed.timezone)} "
                    f"({proposed.timezone})",
                    reply_markup=reminder_confirm_keyboard(token),
                )
                return
            if action in {"text", "voice"} and len(parts) == 4:
                await callback.answer()
                await callback.message.answer(
                    "⌨️ Напиши дату и время, например «завтра в 18:30»."
                    if action == "text" else
                    "🎙 Отправь одно голосовое с датой и временем, например "
                    "«напомни послезавтра в девять утра».",
                )
                return
            if action == "ok" and len(parts) == 4:
                try:
                    saved = reminders.confirm(uid, token)
                except ValueError as exc:
                    await callback.answer(str(exc), show_alert=True)
                    return
                if saved is None:
                    await callback.answer("Повтор или недействительная дата.",
                                          show_alert=True)
                    return
                await callback.answer("Сохранено.")
                await callback.message.answer(
                    f"✅ Напоминание установлено: {_display(saved.remind_at_utc, saved.timezone)} "
                    f"({saved.timezone})",
                    reply_markup=_keyboard([[
                        ("➕ Ещё напоминание", f"td:rem:new:{saved.item_id}"),
                        ("🔔 Все напоминания", f"td:rem:list:{saved.item_id}"),
                    ]]),
                )
                return
        await callback.answer("Неизвестная кнопка.", show_alert=True)

    return router
