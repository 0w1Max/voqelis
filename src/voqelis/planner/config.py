from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo


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
    recurrence: str = "daily"
    days_of_week: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class PlannerConfig:
    timezone: str = "Europe/Moscow"
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
        RecurringTemplateSpec("Проснуться + молитва + умыться + зарядка (КД)", "Чтобы быстрее проснуться и заняться делами. Настроиться на день. Для здоровья.", 540, 60),
        RecurringTemplateSpec("Завтрак + душ (КД)", "Для здоровья", 600, 60),
        RecurringTemplateSpec("Послушать спикерскую + заниматься проектами", "Прокачивать опыт, для резюме", 660, 60),
        RecurringTemplateSpec("Читать книгу", "Получить новые знания", 780, 60),
        RecurringTemplateSpec("Переделать резюме", "Чтобы найти работу", 840, 60),
        RecurringTemplateSpec("Обед + отдых", "Для здоровья, убрать чувство голода, пополнить энергию", 900, 60),
        RecurringTemplateSpec("Делать домашку по психотерапии", "Для получения нового опыта и практики. Для выздоровления, трезвости", 960, 60),
        RecurringTemplateSpec("Собираться на группу", "Чтобы прийти вовремя", 1020, 60),
        RecurringTemplateSpec("Дорога на группу + собрание + прогулка", "Для моего выздоровления, чтобы быть полезным, для общения", 1080, 60),
        RecurringTemplateSpec("Дорога домой + ужин", "Добраться домой, пополнить энергию", 1260, 60),
        RecurringTemplateSpec("Делать домашку по шагам", "Для получения нового опыта и практики. Для выздоровления, трезвости", 1320, 60),
        RecurringTemplateSpec("Читать книгу", "Получить новые знания", 1380, 60),
        RecurringTemplateSpec("Подготовка ко сну + дневник успеха + молитва + благодарности за день", "Для восстановления энергии, выздоровления, чтобы оставаться трезвым", 1440, 60),
    ))

    @classmethod
    def from_json_file(cls, path: Path) -> "PlannerConfig":
        data = json.loads(path.read_text(encoding="utf-8"))
        periods = tuple(
            Period(str(x["name"]), int(x["start"]), int(x["end"]))
            for x in data["periods"]
        )
        anchors = {
            str(name): (int(value[0]), int(value[1]))
            for name, value in data["anchors"].items()
        }
        recurring = tuple(
            RecurringTemplateSpec(
                title=str(x["title"]),
                why=x.get("why"),
                start_minute=int(x["start_minute"]),
                duration_minutes=int(x["duration_minutes"]),
                recurrence=str(x.get("recurrence", "daily")),
                days_of_week=tuple(int(day) for day in x.get("days_of_week", [])),
            )
            for x in data["recurring_templates"]
        )
        config = cls(
            timezone=str(data.get("timezone", "Europe/Moscow")),
            default_duration_minutes=int(data.get("default_duration_minutes", 60)),
            slot_minutes=int(data.get("slot_minutes", 60)),
            plan_start_minute=int(data.get("plan_start_minute", 540)),
            plan_end_minute=int(data.get("plan_end_minute", 1500)),
            periods=periods,
            anchors=anchors,
            recurring_templates=recurring,
        )
        config.validate()
        return config

    def validate(self) -> None:
        try:
            ZoneInfo(self.timezone)
        except Exception as exc:
            raise ValueError(f"Invalid planner timezone: {self.timezone}") from exc
        if self.default_duration_minutes <= 0:
            raise ValueError("Planner default duration must be positive")
        if self.slot_minutes <= 0:
            raise ValueError("Planner slot size must be positive")
        if self.plan_start_minute >= self.plan_end_minute:
            raise ValueError("Planner window must have a positive duration")
        for period in self.periods:
            if not (0 <= period.start < period.end <= 24 * 60):
                raise ValueError(f"Invalid planner period: {period.name}")
        for name, (start, end) in self.anchors.items():
            if not (0 <= start < end <= 24 * 60):
                raise ValueError(f"Invalid planner anchor: {name}")
        for spec in self.recurring_templates:
            if spec.duration_minutes <= 0:
                raise ValueError(f"Invalid recurring duration: {spec.title}")
            if spec.start_minute % self.slot_minutes or spec.duration_minutes % self.slot_minutes:
                raise ValueError(f"Recurring template is not aligned to planner grid: {spec.title}")
            if spec.recurrence not in {"daily", "weekdays", "weekends", "custom"}:
                raise ValueError(f"Invalid recurring rule: {spec.recurrence}")
            if any(day < 0 or day > 6 for day in spec.days_of_week):
                raise ValueError(f"Invalid recurring weekday: {spec.title}")
            if spec.recurrence == "custom" and not spec.days_of_week:
                raise ValueError(f"Custom recurring task needs days_of_week: {spec.title}")

    def __post_init__(self) -> None:
        self.validate()

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


    def recurring_applies_on(self, spec: RecurringTemplateSpec, day: date) -> bool:
        weekday = day.weekday()
        if spec.recurrence == "daily":
            return True
        if spec.recurrence == "weekdays":
            return weekday < 5
        if spec.recurrence == "weekends":
            return weekday >= 5
        return weekday in spec.days_of_week
