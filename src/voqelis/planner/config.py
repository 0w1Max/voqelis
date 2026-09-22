from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Period:
    name: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class RecurringTemplateSpec:
    title: str
    why: str | None
    start_minute: int
    duration_minutes: int


@dataclass(frozen=True, slots=True)
class PlannerConfig:
    default_duration_minutes: int = 60
    slot_minutes: int = 60
    plan_start_minute: int = 9 * 60
    plan_end_minute: int = 25 * 60
    periods: tuple[Period, ...] = field(default_factory=lambda: (
        Period("morning", 6 * 60, 12 * 60),
        Period("day", 12 * 60, 17 * 60),
        Period("evening", 17 * 60, 24 * 60),
        Period("night", 0, 6 * 60),
    ))
    anchors: dict[str, tuple[int, int]] = field(default_factory=lambda: {
        "breakfast": (10 * 60, 11 * 60),
        "lunch": (15 * 60, 16 * 60),
        "dinner": (21 * 60, 22 * 60),
    })
    recurring_templates: tuple[RecurringTemplateSpec, ...] = field(default_factory=lambda: (
        RecurringTemplateSpec("Проснуться + молитва + умыться + зарядка", "Чтобы быстрее проснуться и заняться делами. Настроиться на день. Для здоровья.", 540, 60),
        RecurringTemplateSpec("Завтрак + душ", "Для здоровья", 600, 60),
        RecurringTemplateSpec("Послушать спикерскую + заниматься проектами", "Прокачивать опыт, для резюме", 660, 60),
        RecurringTemplateSpec("Читать книгу", "Получить новые знания", 780, 60),
        RecurringTemplateSpec("Переделать резюме", "Чтобы найти работу", 840, 60),
        RecurringTemplateSpec("Обед + отдых", "Для здоровья, убрать чувство голода, пополнить энергию", 900, 60),
        RecurringTemplateSpec("Делать домашку по психотерапии", "Для получения нового опыта и практики. Для выздоровления, трезвости", 960, 60),
        RecurringTemplateSpec("Собираться на группу", "Чтобы прийти вовремя", 1020, 60),
        RecurringTemplateSpec("Дорога на группу + собрание + прогулка", "Для моего выздоровления, чтобы быть полезным, для общения", 1080, 180),
        RecurringTemplateSpec("Дорога домой + ужин", "Добраться домой, пополнить энергию", 1260, 60),
        RecurringTemplateSpec("Делать домашку по шагам", "Для получения нового опыта и практики. Для выздоровления, трезвости", 1320, 60),
        RecurringTemplateSpec("Читать книгу", "Получить новые знания", 1380, 60),
        RecurringTemplateSpec("Подготовка ко сну + дневник успеха + молитва + благодарности за день", "Для восстановления энергии, выздоровления, чтобы оставаться трезвым", 1440, 60),
    ))

    def period(self, name: str) -> Period | None:
        aliases = {
            "утром": "morning", "утро": "morning",
            "днём": "day", "днем": "day", "день": "day",
            "вечером": "evening", "вечер": "evening",
            "ночью": "night", "ночь": "night",
        }
        normalized = aliases.get(name.strip().lower(), name.strip().lower())
        return next((p for p in self.periods if p.name == normalized), None)

    def anchor(self, name: str) -> tuple[int, int] | None:
        aliases = {
            "завтрака": "breakfast", "завтраком": "breakfast", "завтрак": "breakfast",
            "обеда": "lunch", "обедом": "lunch", "обед": "lunch",
            "ужина": "dinner", "ужином": "dinner", "ужин": "dinner",
        }
        return self.anchors.get(aliases.get(name.strip().lower(), name.strip().lower()))
