"""Plan browsing, editing and removal operations.

PlannerService owns the user lock; these operations run while it is held.
Session and SQLite state remain owned by the shared PlannerStore.
"""

from __future__ import annotations

from datetime import date, timedelta

from .config import PlannerConfig
from .models import TaskKind
from .render import render_plan_text
from .scheduler import fmt_time
from .store import PlannerStore


class PlanOperationsWorkflow:
    def __init__(self, store: PlannerStore, config: PlannerConfig) -> None:
        self.store = store
        self.config = config

    async def start_plan_edit(self, user_id: int, day: date) -> str:
        items = self.store.ensure_daily_plan(user_id, day, self.config)
        if not items:
            return "На этот день пока нет задач для редактирования."
        self.store.set_session(user_id, "plan_edit_select", day, {})
        lines = ["✏️ Что изменить? Пришли номер задачи из плана."]
        for index, item in enumerate(items, 1):
            lines.append(
                f"{index}. {fmt_time(item.start_minute)}–{fmt_time(item.end_minute)} — {item.title}"
            )
        lines.append("")
        lines.append("После номера выберем: «дело» или «зачем».")
        return "\n".join(lines)

    async def plan_edit_select(self, user_id: int, text: str, day: date) -> list[str]:
        items = self.store.plan_items(user_id, day)
        value = text.strip()
        if not value.isdigit():
            return ["Напиши номер задачи из текущего плана."]
        index = int(value) - 1
        if index < 0 or index >= len(items):
            return ["Такого номера нет в текущем плане."]
        item = items[index]
        self.store.set_session(
            user_id,
            "plan_edit_field",
            day,
            {"item_id": item.id},
        )
        return [
            (
                f"Выбрано: {fmt_time(item.start_minute)}–{fmt_time(item.end_minute)} — {item.title}\n"
                "Что изменить: напиши «дело» или «зачем»?"
            )
        ]

    async def plan_edit_field(self, user_id: int, text: str, day: date) -> list[str]:
        session = self.store.session(user_id)
        payload = self.store.session_payload(user_id)
        if not session or not payload.get("item_id"):
            return ["Редактирование больше не актуально."]
        field = text.strip().casefold()
        if field in {"дело", "название", "задача", "активность"}:
            payload["field"] = "title"
            self.store.set_session(user_id, "plan_edit_value", day, payload)
            return ["Хорошо. Напиши или продиктуй новое запланированное дело."]
        if field in {"зачем", "причина", "почему"}:
            payload["field"] = "why"
            self.store.set_session(user_id, "plan_edit_value", day, payload)
            return ["Хорошо. Напиши или продиктуй новую причину."]
        return ["Напиши «дело» или «зачем»."]

    async def plan_edit_value(self, user_id: int, text: str, day: date) -> list[str]:
        session = self.store.session(user_id)
        payload = self.store.session_payload(user_id)
        if not session or not payload.get("item_id") or payload.get("field") not in {"title", "why"}:
            return ["Редактирование больше не актуально."]
        item_id = int(payload["item_id"])
        field = payload["field"]
        try:
            item = self.store.update_plan_item(
                item_id,
                title=text if field == "title" else None,
                why=text if field == "why" else None,
            )
        except (KeyError, ValueError):
            self.store.set_session(user_id, "planning", day, {})
            return ["Не удалось изменить эту задачу. Возвращаюсь к планированию."]
        self.store.set_session(user_id, "planning", day, {})
        label = "дело" if field == "title" else "причину"
        return [
            f"✅ Изменил {label}: {fmt_time(item.start_minute)}–{fmt_time(item.end_minute)} — {item.title}"
            + (f"\nЗачем: {item.why}" if item.why else "")
        ]

    async def start_history(self, user_id: int, today: date) -> str:
        active = self.store.active_plan_day(user_id)
        if active is None:
            active = today + timedelta(days=1)
            self.store.set_active_plan_day(user_id, active)
        self.store.set_session(user_id, "history_select", active, {})
        end_day = today + timedelta(days=1)
        start_day = today - timedelta(days=5)
        rows = self.store.history_counts(user_id, start_day, end_day)
        lines = ["🗓 История планов", f"Активный план: {active.strftime('%d.%m.%Y')}", ""]
        for plan_day, count in reversed(rows):
            marker = " ← активный" if plan_day == active else ""
            lines.append(f"{plan_day.strftime('%d.%m.%Y')} — {count} задач{marker}")
        lines.append("")
        lines.append("Чтобы открыть конкретный день, напиши дату в формате ДД.ММ.ГГГГ.")
        return "\n".join(lines)

    async def history_select(self, user_id: int, text: str, today: date) -> list[str]:
        try:
            day, month, year = (int(part) for part in text.strip().split("."))
            selected = date(year, month, day)
        except (TypeError, ValueError):
            return ["Напиши дату в формате ДД.ММ.ГГГГ."]
        if selected < today - timedelta(days=30) or selected > today + timedelta(days=30):
            return ["Эта дата вне доступного диапазона истории."]

        if selected > today:
            self.store.set_active_plan_day(user_id, selected)
            self.store.set_session(user_id, "planning", selected, {})
        else:
            self.store.set_session(
                user_id, "history_day", selected, {}, sync_active_day=False
            )
        if selected > today:
            items = self.store.ensure_daily_plan(user_id, selected, self.config)
        else:
            items = self.store.plan_items(user_id, selected)

        if not items:
            return [f"🗓 Исторический план: {selected.strftime('%d.%m.%Y')}\n\nНа эту дату пока нет сохранённого плана."]
        return [
            f"🗓 Исторический план: {selected.strftime('%d.%m.%Y')}\n\n"
            + render_plan_text(selected, items, self.config)
        ]

    async def start_delete_item(self, user_id: int, day: date) -> str:
        items = self.store.plan_items(user_id, day)
        if not items:
            return f"На {day.strftime('%d.%m.%Y')} нет задач для удаления."
        self.store.set_session(user_id, "plan_delete_select", day, {})
        lines = [f"🗑 Выбери номер строки для удаления из плана {day.strftime('%d.%m.%Y')}:"]
        for index, item in enumerate(items, 1):
            lines.append(f"{index}. {fmt_time(item.start_minute)}–{fmt_time(item.end_minute)} — {item.title}")
        return "\n".join(lines)

    async def delete_item_select(self, user_id: int, text: str, day: date) -> list[str]:
        items = self.store.plan_items(user_id, day)
        if not text.strip().isdigit():
            return ["Напиши номер строки для удаления."]
        index = int(text.strip()) - 1
        if not 0 <= index < len(items):
            return ["Такого номера нет в текущем плане."]
        item = self.store.delete_plan_item(user_id, items[index].id)
        self.store.set_session(user_id, "planning", day, {})
        return [f"🗑 Удалил: {item.title}\n\n" + render_plan_text(day, self.store.plan_items(user_id, day), self.config)]

    async def start_clear_plan(self, user_id: int, day: date) -> str:
        items = self.store.plan_items(user_id, day)
        ordinary = sum(item.kind != TaskKind.RECURRING for item in items)
        recurring = len(items) - ordinary
        self.store.set_session(
            user_id,
            "plan_clear_confirm",
            day,
            {"ordinary": ordinary, "recurring": recurring},
        )
        return (
            f"🧹 Очистить план на {day.strftime('%d.%m.%Y')}?\n\n"
            f"Обычных задач: {ordinary}. Ежедневных задач: {recurring}.\n\n"
            "По умолчанию ежедневные задачи сохраняются. Выбери вариант:"
        )

    async def clear_plan_confirm(
        self,
        user_id: int,
        text: str,
        day: date,
        *,
        include_recurring: bool = False,
    ) -> list[str]:
        answer = text.strip().casefold()
        if answer in {"нет", "no", "отмена", "cancel"}:
            self.store.clear_session(user_id)
            return ["Очистка отменена."]
        if answer not in {"да", "д", "yes"}:
            return ["Выбери вариант очистки или напиши «нет»."]
        count = (
            self.store.clear_all_items(user_id, day)
            if include_recurring
            else self.store.clear_ordinary_items(user_id, day)
        )
        self.store.set_session(user_id, "planning", day, {})
        suffix = " вместе с ежедневными задачами" if include_recurring else " (ежедневные задачи сохранены)"
        return [f"🧹 План на {day.strftime('%d.%m.%Y')} очищен. Удалено строк: {count}{suffix}."]

    async def delete_plan_item(self, user_id: int, item_id: int, day: date) -> str:
        item = self.store.delete_plan_item(user_id, item_id)
        return f"🗑 Удалил: {item.title}\nПлан: {day.strftime('%d.%m.%Y')}."

    async def show_plan(self, user_id: int, day: date) -> str:
        items = self.store.ensure_daily_plan(user_id, day, self.config)
        return f"🗓 Активный план: {day.strftime('%d.%m.%Y')}\n\n" + render_plan_text(day, items, self.config)

