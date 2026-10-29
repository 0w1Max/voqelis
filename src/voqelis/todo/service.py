"""Application-facing ToDo V1: a personal checklist without deadlines."""

from __future__ import annotations

from .store import PAGE_SIZE, TodoItem, TodoStore


def _preview(value: str, length: int = 70) -> str:
    compact = " ".join(value.split())
    return compact if len(compact) <= length else compact[: length - 1].rstrip() + "…"


class TodoService:
    def __init__(self, store: TodoStore) -> None:
        self.store = store

    @staticmethod
    def item_message(item: TodoItem) -> str:
        label = "✅ Выполнено" if item.completed_at else "⬜ Активное"
        return f"{label} · Дело #{item.id}\n\n{item.text}"

    @staticmethod
    def saved_message(item: TodoItem, *, edited: bool) -> str:
        action = "изменено" if edited else "добавлено"
        return f"✅ Дело #{item.id} {action}.\n\n{item.text}"

    def list_message(
        self, user_id: int, *, completed: bool, page: int = 1,
    ) -> tuple[str, list[TodoItem], int]:
        items, total = self.store.list_items(
            user_id, completed=completed, page=page
        )
        title = "✅ Выполненные" if completed else "📋 Активные дела"
        if not items:
            if page > 1:
                return (f"{title}\n\nНа странице {page} дел нет.", [], total)
            return (
                f"{title}\n\n"
                + ("Пока нет выполненных дел." if completed
                   else "Список пуст. Нажми «➕ Добавить дело»."),
                [], total,
            )
        lines = [
            f"{'✅' if completed else '⬜'} #{item.id} · {_preview(item.text)}"
            for item in items
        ]
        message = (
            f"{title} · страница {page}\n"
            + "\n".join(lines)
            + "\n\nВыбери дело кнопкой ниже, чтобы открыть действия."
            + f"\nВсего: {total}. На странице: {len(items)} из {PAGE_SIZE} возможных."
        )
        return message, items, total
