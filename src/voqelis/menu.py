"""Primary Telegram reply keyboard for the Day Facts workflow.

Planner remains available through its commands; only its old shortcut buttons
are removed from the visible home menu.
"""

from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

ADMIN_PANEL = "🛡 Админ-панель"
FACTS_START = "📘 Факты дня"
FACTS_TODAY = "📋 Текущие факты дня"
FACTS_HISTORY = "🗓 История фактов"
FACTS_WORD = "📄 Word"
FACTS_WORD_MOBILE = "📱 Word для телефона"
FACTS_PDF = "📕 PDF"
FACTS_MOBILE = "📱 PDF для телефона"


def main_keyboard(*, admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
            [KeyboardButton(text=FACTS_START)],
            [KeyboardButton(text=FACTS_TODAY), KeyboardButton(text=FACTS_HISTORY)],
            [KeyboardButton(text=FACTS_WORD), KeyboardButton(text=FACTS_PDF)],
            [KeyboardButton(text=FACTS_WORD_MOBILE)],
            [KeyboardButton(text=FACTS_MOBILE)],
    ]
    if admin:
        rows.append([KeyboardButton(text=ADMIN_PANEL)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)
