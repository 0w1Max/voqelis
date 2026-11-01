"""Review workflow for daily task evaluation and final reflections.

Called while PlannerService holds its per-user lock. ReviewWorkflow owns
review session transitions, not Telegram routing or scheduling.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .ai import PlannerAI
from .config import PlannerConfig
from .models import PlannerAIError, PlannerAIUnavailable
from .render import render_full_review_proposal, render_plan_text, render_review_prompt
from .scheduler import fmt_time
from .store import PlannerStore

logger = logging.getLogger(__name__)


class ReviewWorkflow:
    def __init__(self, store: PlannerStore, config: PlannerConfig, ai: PlannerAI | None):
        self.store = store
        self.config = config
        self.ai = ai

    def _planner_today(self) -> date:
        return datetime.now(ZoneInfo(self.config.timezone)).date()

    @staticmethod
    def future_review_message(day: date) -> str:
        return (
            f"⏳ Анализ дня {day.strftime('%d.%m.%Y')} пока недоступен: "
            "этот день ещё не наступил."
        )


    def _set_review_session(self, user_id: int, state: str, day: date, payload: dict | None = None, *, preserve_active_day: bool = False) -> None:
        session = self.store.session(user_id)
        previous_payload = self.store.session_payload(user_id)
        preserve = preserve_active_day or bool(previous_payload.get("_preserve_active_day")) or (
            session is not None
            and session["state"] == "history_day"
            and session["target_day"] == day.isoformat()
        )
        review_payload = dict(payload or {})
        if preserve:
            review_payload["_preserve_active_day"] = True
        self.store.set_session(
            user_id,
            state,
            day,
            review_payload,
            sync_active_day=not preserve,
        )


    async def start_review(
        self, user_id: int, day: date, today: date | None = None, *, preserve_active_day: bool = False
    ) -> str:
        today = today or self._planner_today()
        if day > today:
            return self.future_review_message(day)
        self.store.ensure_daily_plan(user_id, day, self.config)
        pending_item = next((x for x in self.store.reviews(user_id, day) if x.status is None), None)
        if pending_item:
            self._set_review_session(
                user_id,
                "review_status",
                day,
                {"current_item_id": pending_item.plan_item.id},
                preserve_active_day=preserve_active_day,
            )
            return (
                f"🔎 Продолжаем анализ дня.\n\n"
                f"{render_review_prompt(pending_item)}\n\nВыполнено?"
            )

        day_review = self.store.day_review(user_id, day)
        if day_review is None:
            self._set_review_session(
                user_id,
                "review_final1",
                day,
                {},
                preserve_active_day=preserve_active_day,
            )
            return (
                "Все задачи обработаны.\n\n"
                "Что бы ты изменил, если бы следовал рекомендации по оздоровлению?"
            )

        if not day_review.completed:
            self._set_review_session(
                user_id,
                "review_final2",
                day,
                {},
                preserve_active_day=preserve_active_day,
            )
            return (
                "Продолжаем финальную часть анализа.\n\n"
                "Теперь расскажи признаки срыва. Можно назвать несколько наблюдений "
                "одним сообщением. Или нажми «Пропустить»."
            )

        return "Все задачи этого дня уже проанализированы."

    async def start_full_review(
        self, user_id: int, day: date, today: date | None = None, *, preserve_active_day: bool = False
    ) -> str:
        today = today or self._planner_today()
        if day > today:
            return self.future_review_message(day)
        self.store.ensure_daily_plan(user_id, day, self.config)
        day_review = self.store.day_review(user_id, day)
        if day_review is not None and day_review.completed:
            return "Этот день уже полностью проанализирован. Для изменения используй «✏️ Исправить анализ»."
        self._set_review_session(
            user_id,
            "review_full_input",
            day,
            {},
            preserve_active_day=preserve_active_day,
        )
        return (
            "🎙️ Расскажи одним сообщением, как прошёл весь день. "
            "Я попробую сопоставить рассказ с задачами, а перед сохранением покажу результат для проверки."
        )

    async def review_full_input(self, user_id: int, text: str, day: date) -> list[str]:
        if self.ai is None:
            return ["Для общего голосового анализа нужен настроенный AI-провайдер. Используй последовательный анализ задач."]
        items = self.store.reviews(user_id, day)
        payload = [
            {"plan_item_id": x.plan_item.id, "time": f"{fmt_time(x.plan_item.start_minute)}–{fmt_time(x.plan_item.end_minute)}",
             "title": x.plan_item.title, "why": x.plan_item.why}
            for x in items
        ]
        try:
            extracted = await self.ai.extract_full_review(text, items=payload)
        except PlannerAIError as exc:
            logger.warning(
                "PLANNER_AI_FULL_REVIEW_FALLBACK reason=%s",
                exc,
            )
            pending_item = next((x for x in items if x.status is None), None)
            if pending_item is None:
                self._set_review_session(user_id, "review_final1", day, {})
                return [
                    (
                        "⚠️ Общий анализ сейчас недоступен. Все задачи уже обработаны — "
                        "переходим к финальному анализу."
                    )
                ]
            self._set_review_session(
                user_id,
                "review_status",
                day,
                {"current_item_id": pending_item.plan_item.id},
            )
            return [
                (
                    "⚠️ Общий анализ сейчас недоступен. Ничего не сохранено. "
                    "Переходим к последовательному анализу задач."
                ),
                f"{render_review_prompt(pending_item)}\n\nВыполнено?",
            ]
        except (ValueError, TypeError):
            return ["⚠️ Не удалось разобрать общий обзор дня. Попробуй ещё раз."]
        by_id = {x.plan_item.id: x.plan_item for x in items}
        proposal = []
        for raw in extracted:
            try:
                item_id = int(raw["plan_item_id"])
                status = raw.get("status")
                if item_id not in by_id or status not in {"+", "-", "+-"}:
                    continue
                reason = raw.get("missed_reason")
                if status == "-" and not str(reason or "").strip():
                    continue
                feelings = tuple(str(x).strip() for x in raw.get("feelings", []) if str(x).strip())
                proposal.append((item_id, status, raw.get("activity"), feelings, reason))
            except (KeyError, TypeError, ValueError):
                continue
        if not proposal:
            return ["Не удалось уверенно сопоставить рассказ с задачами. Попробуй последовательный анализ по пунктам."]
        normalized = []
        seen_ids: set[int] = set()
        for item_id, status, activity, feelings, reason in proposal:
            if item_id in seen_ids:
                continue
            seen_ids.add(item_id)
            normalized.append((by_id[item_id], status, activity, feelings, reason))
        self._set_review_session(
            user_id, "review_full_confirm", day,
            {"items": [
                {
                    "plan_item_id": item.id,
                    "status": status,
                    "activity": activity,
                    "feelings": list(feelings),
                    "reason": reason,
                }
                for item, status, activity, feelings, reason in normalized
            ]},
        )
        return [render_full_review_proposal(normalized)]
 
    async def review_full_confirm(self, user_id: int, answer: str, day: date) -> list[str]:
        normalized = answer.strip().lower()
        if normalized in {"нет", "no", "отмена", "cancel"}:
            self.store.clear_session(user_id)
            return ["Хорошо, общий разбор не сохранён."]
        if normalized not in {"да", "д", "yes", "сохранить"}:
            return ["Нажми «Сохранить» или «Отмена»."]
        payload = self.store.session_payload(user_id)
        for item in payload.get("items", []):
            self.store.save_review(
                int(item["plan_item_id"]), item["status"], item.get("activity"),
                tuple(item.get("feelings", [])), item.get("reason"),
            )
        next_item = next((x for x in self.store.reviews(user_id, day) if x.status is None), None)
        if next_item:
            self._set_review_session(
                user_id, "review_status", day, {"current_item_id": next_item.plan_item.id}
            )
            return [
                (
                    "✅ Общий разбор сохранён для уверенно сопоставленных задач.\n\n"
                    "Остались пункты, по которым AI не смог уверенно определить результат. "
                    "Проверим их по очереди.\n\n"
                    f"{render_review_prompt(next_item)}\n\nВыполнено?"
                )
            ]
        self._set_review_session(user_id, "review_final1", day, {})
        return [
            (
                "✅ Общий разбор сохранён.\n\n"
                "Все задачи сопоставлены.\n\n"
                "Что бы ты изменил, если бы следовал рекомендации по оздоровлению?"
            )
        ]

    async def start_review_edit(
        self, user_id: int, day: date, today: date | None = None, *, preserve_active_day: bool = False
    ) -> str:
        today = today or self._planner_today()
        if day > today:
            return self.future_review_message(day)
        items = [x for x in self.store.reviews(user_id, day) if x.status is not None]
        if not items:
            return "На этот день пока нет заполненных ответов для редактирования."
        self._set_review_session(
            user_id,
            "review_edit_select",
            day,
            {},
            preserve_active_day=preserve_active_day,
        )
        lines = ["✏️ Выбери задачу для исправления анализа:"]
        for index, item in enumerate(items, 1):
            lines.append(
                f"{index}. {fmt_time(item.plan_item.start_minute)}–{fmt_time(item.plan_item.end_minute)} — "
                f"{item.plan_item.title} [{item.status}]"
            )
        lines.append("Напиши номер задачи или «отмена».")
        return "\n".join(lines)

    async def review_edit_select(self, user_id: int, text: str, day: date) -> list[str]:
        answer = text.strip().lower()
        if answer in {"отмена", "cancel"}:
            self.store.clear_session(user_id)
            return ["Редактирование отменено."]
        try:
            selected = int(answer)
        except ValueError:
            return ["Напиши номер задачи из списка или «отмена»."]
        items = [x for x in self.store.reviews(user_id, day) if x.status is not None]
        item = items[selected - 1] if 1 <= selected <= len(items) else next(
            (x for x in items if x.plan_item.id == selected),
            None,
        )
        if item is None:
            return ["Эта задача больше недоступна для редактирования. Открой «✏️ Исправить анализ» заново."]
        self._set_review_session(
            user_id,
            "review_status",
            day,
            {"current_item_id": item.plan_item.id, "editing": True},
        )
        return [
            "✏️ Исправление анализа.\n\n"
            + render_review_prompt(item)
            + "\n\nВыбери новый статус: +, - или +-."
        ]

    async def review_status(self, user_id: int, text: str, day: date) -> list[str]:
        status = {"+": "+", "-": "-", "+-": "+-", "да": "+", "нет": "-", "частично": "+-"}.get(text.strip().lower())
        if status is None:
            return ["Выбери +, - или +-. Можно также написать «да», «нет» или «частично»."]
        current_payload = self.store.session_payload(user_id)
        try:
            item_id = int(current_payload["current_item_id"])
        except (KeyError, TypeError, ValueError):
            self.store.clear_session(user_id)
            return ["Сессия анализа устарела. Открой «🔎 Анализ сегодня» заново."]
        item = next((x for x in self.store.reviews(user_id, day) if x.plan_item.id == item_id), None)
        if item is None:
            self.store.clear_session(user_id)
            return ["Эта задача больше недоступна. Открой «🔎 Анализ сегодня» заново."]
        self._set_review_session(
            user_id, "review_detail", day,
            {"current_item_id": item_id, "status": status, "editing": bool(current_payload.get("editing"))},
        )
        prompt = "Расскажи, что произошло: что делал, что чувствовал и почему не выполнил." if status == "-" else "Расскажи, что делал и что чувствовал."
        return [f"{render_review_prompt(item)}\n\n{prompt}"]

    async def review_detail(self, user_id: int, text: str, day: date) -> list[str]:
        payload = self.store.session_payload(user_id)
        try:
            item_id, status = int(payload["current_item_id"]), str(payload["status"])
        except (KeyError, TypeError, ValueError):
            self.store.clear_session(user_id)
            return ["Сессия анализа устарела. Открой «🔎 Анализ сегодня» заново."]
        item = next((x for x in self.store.reviews(user_id, day) if x.plan_item.id == item_id), None)
        if item is None:
            self.store.clear_session(user_id)
            return ["Эта задача больше недоступна. Открой «🔎 Анализ сегодня» заново."]
        try:
            if self.ai is None:
                raise PlannerAIUnavailable("Planner AI is not configured; review extraction is unavailable.")
            activity, feelings, reason = await self.ai.extract_review(
                text, task_title=item.plan_item.title
            )
        except PlannerAIError as exc:
            logger.warning(
                "PLANNER_AI_REVIEW_UNAVAILABLE reason=%s",
                exc,
            )
            return [
                (
                    f"⚠️ Не удалось разобрать ответ для «{item.plan_item.title}»: "
                    "AI сейчас недоступен. Ничего не сохранено. Попробуй ещё раз."
                )
            ]
        except (ValueError, TypeError):
            return [
                (
                    f"⚠️ Не удалось разобрать ответ для «{item.plan_item.title}». "
                    "Попробуй ещё раз."
                )
            ]
        activity = activity.strip() if activity else None
        reason = reason.strip() if reason else None
        if status != "-":
            reason = None
        if status == "-" and not reason:
            return [
                (
                    f"⚠️ Для «{item.plan_item.title}» нужно указать причину невыполнения. "
                    "Ничего не сохранено. Расскажи, почему не выполнил задачу."
                )
            ]

        self.store.save_review(item_id, status, activity, feelings, reason)

        if payload.get("editing"):
            self.store.clear_session(user_id)
            return [
                "✅ Исправление сохранено.\n\n"
                + render_plan_text(day, self.store.plan_items(user_id, day), self.config)
            ]

        next_item = next((x for x in self.store.reviews(user_id, day) if x.status is None), None)
        if next_item:
            self._set_review_session(
                user_id, "review_status", day, {"current_item_id": next_item.plan_item.id}
            )
            return [f"Сохранено.\n\n{render_review_prompt(next_item)}\n\nВыполнено?"]

        self._set_review_session(user_id, "review_final1", day, {})
        return [
            (
                "Все задачи обработаны.\n\n"
                "Что бы ты изменил, если бы следовал рекомендации по оздоровлению?"
            )
        ]

    async def review_final1(self, user_id: int, text: str, day: date) -> list[str]:
        what = None if text.strip().lower() == "пропустить" else text.strip()
        self.store.save_day_review(user_id, day, what, (), False)
        self._set_review_session(user_id, "review_final2", day, {})
        return ["Записал.\n\nТеперь расскажи признаки срыва. Можно назвать несколько наблюдений одним сообщением. Или напиши «пропустить»."]

    async def review_final2(self, user_id: int, text: str, day: date) -> list[str]:
        signs = () if text.strip().lower() == "пропустить" else tuple(x.strip() for x in text.replace("\n", ",").split(",") if x.strip())
        existing = self.store.day_review(user_id, day)
        self.store.save_day_review(user_id, day, existing.what_would_change if existing else None, signs, True)
        self.store.clear_session(user_id)
        return ["✅ Анализ дня завершён.\n\n" + render_plan_text(day, self.store.plan_items(user_id, day), self.config)]

