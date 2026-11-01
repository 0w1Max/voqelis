"""Owner-visible announcement ideas for verified Voqelis features.

These are deliberately curated, not inferred from CI or GitHub commit messages.
Every entry is stored as a private, editable draft when the bot starts.
A feature must be available to approved bot users before adding an entry.
No message is ever sent by this module.
"""

CURATED_SUGGESTIONS: tuple[tuple[str, str], ...] = (
    (
        "todo-reminders-v21",
        "📋 Список дел в Voqelis\n\n"
        "В боте доступен раздел «✅ Список дел». "
        "Добавляйте дела текстом или голосом и при необходимости "
        "назначайте напоминания на выбранное время.\n\n"
        "Откройте «✅ Список дел», чтобы попробовать.",
    ),
    (
        "day-facts-v1",
        "📘 Факты дня в Voqelis\n\n"
        "В боте доступен раздел «📘 Факты дня» для записи "
        "наблюдений и мыслей. Можно просматривать историю "
        "и экспортировать записи.\n\n"
        "Откройте «📘 Факты дня» в главном меню.",
    ),
)
