"""Compact top-level module navigation and module-specific Telegram keyboards."""

from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

ADMIN_PANEL = "🛡 Админ-панель"
HOME = "⬅️ Главное меню"
FACTS_START = "📘 Факты дня"
FACTS_NEW = "➕ Новая запись"
FACTS_RESUME = "✏️ Продолжить черновик"
FACTS_TODAY = "📋 Текущие факты дня"
FACTS_HISTORY = "🗓 История фактов"
FACTS_WORD = "📄 Word"
FACTS_WORD_MOBILE = "📱 Word для телефона"
FACTS_PDF = "📕 PDF"
FACTS_MOBILE = "📱 PDF для телефона"
NOTES_START = "📝 Заметки"
NOTES_NEW = "🎙 Новая заметка"
NOTES_LIST = "📋 Мои заметки"
PLANNER_START = "📅 Планировщик"
TRANSCRIBE_START = "🎙 Расшифровка"  # Legacy button: redirects to home, not a module
TODO_START = "✅ Список дел"
TODO_LEGACY = "✅ Список дел (скоро)"
TODO_NEW = "➕ Добавить дело"
TODO_ACTIVE = "📋 Активные дела"
TODO_DONE = "✅ Выполненные"
TODO_CANCEL = "❌ Отменить ввод"


def _keyboard(rows: list[list[str]]) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=item) for item in row] for row in rows],
        resize_keyboard=True,
    )


def main_keyboard(*, admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [[FACTS_START, TODO_START]]
    if admin:
        rows.extend([
            [NOTES_START, PLANNER_START],
            [ADMIN_PANEL],
        ])
    return _keyboard(rows)


def facts_keyboard() -> ReplyKeyboardMarkup:
    return _keyboard([
        [FACTS_NEW, FACTS_RESUME],
        [FACTS_TODAY, FACTS_HISTORY],
        [HOME],
    ])


def notes_keyboard() -> ReplyKeyboardMarkup:
    return _keyboard([[NOTES_NEW, NOTES_LIST], [HOME]])


def planner_section_keyboard() -> ReplyKeyboardMarkup:
    return _keyboard([
        ["📅 Планирование дня", "📋 Текущий план"],
        ["🗓 История планов", "✏️ Редактировать план"],
        ["🔎 Анализ активного дня", "🎙️ Рассказать весь день"],
        ["🧹 Очистить план", "⏹️ Выйти из режима"],
        ["📄 DOCX плана", "📕 PDF плана"],
        [HOME],
    ])


def todo_keyboard() -> ReplyKeyboardMarkup:
    return _keyboard([[TODO_NEW, TODO_ACTIVE], [TODO_DONE, HOME]])


def todo_capture_keyboard() -> ReplyKeyboardMarkup:
    return _keyboard([[TODO_CANCEL], [HOME]])
