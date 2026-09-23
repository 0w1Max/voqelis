from __future__ import annotations

from dataclasses import asdict
from datetime import date, timedelta

from .ai import PlannerAI
from .config import PlannerConfig
from .models import Conflict, ConflictProposal, PlanItem, ScheduleMove, ScheduleValidationError, TaskDraft, TaskKind
from .parser import parse_voice
from .render import render_plan_text, render_review_prompt
from .scheduler import Scheduler, fmt_time
from .store import PlannerStore


class PlannerService:
    def __init__(
        self,
        store: PlannerStore,
        config: PlannerConfig | None = None,
        ai: PlannerAI | None = None,
    ):
        self.store = store
        self.config = config or PlannerConfig()
        self.scheduler = Scheduler(store, self.config)
        self.ai = ai

    def start_planning(self, user_id: int, day: date) -> str:
        items = self.store.ensure_daily_plan(user_id, day, self.config)
        self.store.set_session(user_id, "planning", day, {})
        recurring = sum(item.kind == TaskKind.RECURRING for item in items)
        return (
            f"📅 Планирование включено. План на {day.strftime('%d.%m.%Y')}.\n"
            f"КД сформированы первыми: {recurring}.\n"
            "Присылай задачи голосом или текстом."
        )

    async def _extract(self, text: str, user_id: int, today: date) -> list[TaskDraft]:
        session = self.store.session(user_id)
        target = date.fromisoformat(session["target_day"]) if session and session["target_day"] else today + timedelta(days=1)
        if self.ai is not None:
            return await self.ai.extract_tasks(text, today=today, target_day=target, config=self.config)
        # Explicit fallback for development/tests. It is never presented as AI.
        return parse_voice(text, today=today, config=self.config)

    @staticmethod
    def _proposal_payload(proposal: Conflict, pending: list[TaskDraft]) -> dict:
        p = proposal.proposal
        return {
            "draft": asdict(p.draft) | {"day": p.draft.day.isoformat()},
            "desired": [p.desired_start_minute, p.desired_end_minute],
            "conflicts": [
                {
                    "id": x.id, "title": x.title, "start": x.start_minute,
                    "end": x.end_minute, "kind": x.kind.value,
                }
                for x in p.conflicts
            ],
            "alternatives": [list(x) for x in p.alternatives],
            "moves": [asdict(x) for x in p.moves],
            "reason": proposal.reason,
            "pending": [asdict(x) | {"day": x.day.isoformat()} for x in pending],
        }

    @staticmethod
    def _draft(data: dict) -> TaskDraft:
        return TaskDraft(
            title=data["title"], day=date.fromisoformat(data["day"]),
            start_minute=data.get("start_minute"), end_minute=data.get("end_minute"),
            duration_minutes=data.get("duration_minutes"), period=data.get("period"),
            preferred_minute=data.get("preferred_minute"), relation=data.get("relation"),
            anchor=data.get("anchor"), why=data.get("why"), urgent=bool(data.get("urgent")),
            source_text=data.get("source_text", ""),
        )

    def _conflict_text(self, conflict: Conflict) -> str:
        p = conflict.proposal
        lines = [f"⚠️ {p.draft.title}: {conflict.reason}"]
        for item in p.conflicts:
            lines.append(f"Занято: {fmt_time(item.start_minute)}–{fmt_time(item.end_minute)} — {item.title}")
        if p.moves:
            lines.append("Предлагаю разовый перенос конфликтующих задач:")
            for move in p.moves:
                item = next(x for x in p.conflicts if x.id == move.plan_item_id)
                lines.append(f"• {item.title}: {fmt_time(move.new_start_minute)}–{fmt_time(move.new_end_minute)}")
            lines.append(f"Новую задачу поставить на {fmt_time(p.desired_start_minute)}–{fmt_time(p.desired_end_minute)}.")
            lines.append("Подтвердить перенос? Напиши «да» или «нет».")
        elif p.alternatives:
            lines.append("Свободные альтернативы:")
            lines.extend(f"{i}. {fmt_time(s)}–{fmt_time(e)}" for i, (s, e) in enumerate(p.alternatives, 1))
            lines.append("Выбери 1–3 или напиши «отмена».")
        else:
            lines.append("Подходящего свободного окна нет.")
        return "\n".join(lines)

    async def _add_drafts(self, user_id: int, drafts: list[TaskDraft], today: date) -> list[str]:
        replies: list[str] = []
        for index, draft in enumerate(drafts):
            try:
                result = self.scheduler.schedule(user_id, draft)
            except ScheduleValidationError as exc:
                replies.append(f"⚠️ {draft.title}: {exc}")
                continue
            if isinstance(result, Conflict):
                self.store.set_session(
                    user_id,
                    "planning_conflict",
                    draft.day,
                    self._proposal_payload(result, drafts[index + 1:]),
                )
                replies.append(self._conflict_text(result))
                break
            message = f"✅ Добавил: {fmt_time(result.start_minute)}–{fmt_time(result.end_minute)} — {result.title}"
            message += f"\nЗачем: {result.why}" if result.why else "\nЗачем: не указано."
            replies.append(message)
        return replies

    async def add_from_text(self, user_id: int, text: str, today: date) -> list[str]:
        try:
            drafts = await self._extract(text, user_id, today)
        except Exception as exc:
            return [f"⚠️ Не удалось разобрать задачу: {exc}"]
        if not drafts:
            return ["Не удалось выделить задачу. Назови дело и, если важно, время или период."]
        return await self._add_drafts(user_id, drafts, today)

    async def _resolve_conflict(self, user_id: int, text: str, today: date) -> list[str]:
        session = self.store.session(user_id)
        payload = self.store.session_payload(user_id)
        if not session or not payload.get("draft"):
            self.store.set_session(user_id, "planning", today + timedelta(days=1), {})
            return ["Конфликт больше не актуален. Возвращаюсь к планированию."]

        answer = text.strip().lower()
        draft = self._draft(payload["draft"])
        conflicts = tuple(
            PlanItem(
                int(x["id"]), user_id, draft.day, x["title"], None,
                int(x["start"]), int(x["end"]), TaskKind(x["kind"])
            )
            for x in payload["conflicts"]
        )
        moves = tuple(ScheduleMove(**x) for x in payload["moves"])

        if moves:
            if answer not in {"да", "д", "yes", "подтверждаю"}:
                self.store.set_session(user_id, "planning", draft.day, {})
                replies = ["Хорошо, ничего не переношу."]
                replies.extend(await self._add_drafts(
                    user_id,
                    [self._draft(x) for x in payload.get("pending", [])],
                    today,
                ))
                return replies
            proposal = ConflictProposal(
                draft, payload["desired"][0], payload["desired"][1],
                conflicts, tuple(tuple(x) for x in payload["alternatives"]), moves,
            )
            try:
                item = self.scheduler.apply_proposal(user_id, proposal)
            except ScheduleValidationError as exc:
                return [f"⚠️ Перенос не применён: {exc}"]
            reply = f"✅ Перенос подтверждён. Добавил {fmt_time(item.start_minute)}–{fmt_time(item.end_minute)} — {item.title}"
        else:
            if answer in {"отмена", "нет", "cancel"}:
                self.store.set_session(user_id, "planning", draft.day, {})
                replies = ["Хорошо, конфликт оставляю без изменений."]
                replies.extend(await self._add_drafts(
                    user_id,
                    [self._draft(x) for x in payload.get("pending", [])],
                    today,
                ))
                return replies
            if answer not in {"1", "2", "3"}:
                return ["Выбери 1–3 или напиши «отмена»."]
            alternatives = [tuple(x) for x in payload["alternatives"]]
            index = int(answer) - 1
            if index >= len(alternatives):
                return ["Такого варианта нет."]
            start, end = alternatives[index]
            item_id = self.store.add_item(
                PlanItem(0, user_id, draft.day, draft.title, draft.why, start, end, TaskKind.ORDINARY, None, draft.urgent, draft.source_text)
            )
            reply = f"✅ Добавил: {fmt_time(start)}–{fmt_time(end)} — {draft.title}"

        pending = [self._draft(x) for x in payload.get("pending", [])]
        self.store.set_session(user_id, "planning", draft.day, {})
        if pending:
            replies = [reply]
            replies.extend(await self._add_drafts(user_id, pending, today))
            return replies
        return [reply]

    async def start_review(self, user_id: int, day: date) -> str:
        self.store.ensure_daily_plan(user_id, day, self.config)
        pending_item = next((x for x in self.store.reviews(user_id, day) if x.status is None), None)
        if pending_item:
            self.store.set_session(
                user_id, "review_status", day, {"current_item_id": pending_item.plan_item.id}
            )
            return (
                f"🔎 Продолжаем анализ дня.\n\n"
                f"{render_review_prompt(pending_item)}\n\nВыполнено?"
            )

        day_review = self.store.day_review(user_id, day)
        if day_review is None:
            self.store.set_session(user_id, "review_final1", day, {})
            return (
                "Все задачи обработаны.\n\n"
                "Что бы ты изменил, если бы следовал рекомендации по оздоровлению?"
            )

        if not day_review.completed:
            self.store.set_session(user_id, "review_final2", day, {})
            return (
                "Продолжаем финальную часть анализа.\n\n"
                "Теперь расскажи признаки срыва. Можно назвать несколько наблюдений "
                "одним сообщением. Или нажми «Пропустить»."
            )

        return "Все задачи этого дня уже проанализированы."

    async def start_review_edit(self, user_id: int, day: date) -> str:
        items = [x for x in self.store.reviews(user_id, day) if x.status is not None]
        if not items:
            return "На этот день пока нет заполненных ответов для редактирования."
        self.store.set_session(user_id, "review_edit_select", day, {})
        lines = ["✏️ Выбери задачу для исправления анализа:"]
        for index, item in enumerate(items, 1):
            lines.append(
                f"{index}. {fmt_time(item.plan_item.start_minute)}–{fmt_time(item.plan_item.end_minute)} — "
                f"{item.plan_item.title} [{item.status}]"
            )
        lines.append("Напиши номер задачи или «отмена».")
        return "\n".join(lines)

    async def _review_edit_select(self, user_id: int, text: str, day: date) -> list[str]:
        answer = text.strip().lower()
        if answer in {"отмена", "cancel"}:
            self.store.clear_session(user_id)
            return ["Редактирование отменено."]
        try:
            index = int(answer) - 1
        except ValueError:
            return ["Напиши номер задачи из списка или «отмена»."]
        items = [x for x in self.store.reviews(user_id, day) if x.status is not None]
        if index < 0 or index >= len(items):
            return ["Такого номера нет."]
        item = items[index]
        self.store.set_session(user_id, "review_status", day, {"current_item_id": item.plan_item.id, "editing": True})
        return [
            "✏️ Исправление анализа.\n\n"
            + render_review_prompt(item)
            + "\n\nВыбери новый статус: +, - или +-."
        ]

    async def _review_status(self, user_id: int, text: str, day: date) -> list[str]:
        status = {"+": "+", "-": "-", "+-": "+-", "да": "+", "нет": "-", "частично": "+-"}.get(text.strip().lower())
        if status is None:
            return ["Выбери +, - или +-. Можно также написать «да», «нет» или «частично»."]
        current_payload = self.store.session_payload(user_id)
        item_id = int(current_payload["current_item_id"])
        item = next(x for x in self.store.reviews(user_id, day) if x.plan_item.id == item_id)
        self.store.set_session(
            user_id, "review_detail", day,
            {"current_item_id": item_id, "status": status, "editing": bool(current_payload.get("editing"))},
        )
        prompt = "Расскажи, что произошло: что делал, что чувствовал и почему не выполнил." if status == "-" else "Расскажи, что делал и что чувствовал."
        return [f"{render_review_prompt(item)}\n\n{prompt}"]

    async def _review_detail(self, user_id: int, text: str, day: date) -> list[str]:
        payload = self.store.session_payload(user_id)
        item_id, status = int(payload["current_item_id"]), payload["status"]
        item = next(x for x in self.store.reviews(user_id, day) if x.plan_item.id == item_id)
        try:
            if self.ai is not None:
                activity, feelings, reason = await self.ai.extract_review(
                    text, task_title=item.plan_item.title
                )
            else:
                activity, feelings, reason = text.strip(), (), None
        except Exception as exc:
            return [
                f"⚠️ Не удалось разобрать ответ для «{item.plan_item.title}». {exc}\n"
                "Попробуй ещё раз."
            ]
        self.store.save_review(item_id, status, activity or None, feelings, reason)

        if payload.get("editing"):
            self.store.clear_session(user_id)
            return [
                "✅ Исправление сохранено.\n\n"
                + render_plan_text(day, self.store.plan_items(user_id, day), self.config)
            ]

        next_item = next((x for x in self.store.reviews(user_id, day) if x.status is None), None)
        if next_item:
            self.store.set_session(
                user_id, "review_status", day, {"current_item_id": next_item.plan_item.id}
            )
            return [f"Сохранено.\n\n{render_review_prompt(next_item)}\n\nВыполнено?"]

        self.store.set_session(user_id, "review_final1", day, {})
        return [
            "Все задачи обработаны.\n\n"
            "Что бы ты изменил, если бы следовал рекомендации по оздоровлению?"
        ]

    async def _review_final1(self, user_id: int, text: str, day: date) -> list[str]:
        what = None if text.strip().lower() == "пропустить" else text.strip()
        self.store.save_day_review(user_id, day, what, (), False)
        self.store.set_session(user_id, "review_final2", day, {})
        return ["Записал.\n\nТеперь расскажи признаки срыва. Можно назвать несколько наблюдений одним сообщением. Или напиши «пропустить»."]

    async def _review_final2(self, user_id: int, text: str, day: date) -> list[str]:
        signs = () if text.strip().lower() == "пропустить" else tuple(x.strip() for x in text.replace("\n", ",").split(",") if x.strip())
        existing = self.store.day_review(user_id, day)
        self.store.save_day_review(user_id, day, existing.what_would_change if existing else None, signs, True)
        self.store.clear_session(user_id)
        return ["✅ Анализ дня завершён.\n\n" + render_plan_text(day, self.store.plan_items(user_id, day), self.config)]

    async def handle_text(self, user_id: int, text: str, today: date) -> list[str]:
        session = self.store.session(user_id)
        if not session:
            return []
        state = session["state"]
        day = date.fromisoformat(session["target_day"]) if session["target_day"] else today
        if state == "planning":
            return await self.add_from_text(user_id, text, today)
        if state == "planning_conflict":
            return await self._resolve_conflict(user_id, text, today)
        if state == "review_edit_select":
            return await self._review_edit_select(user_id, text, day)
        if state == "review_status":
            return await self._review_status(user_id, text, day)
        if state == "review_detail":
            return await self._review_detail(user_id, text, day)
        if state == "review_final1":
            return await self._review_final1(user_id, text, day)
        if state == "review_final2":
            return await self._review_final2(user_id, text, day)
        return []

    async def handle_callback(self, user_id: int, callback_data: str, today: date) -> list[str]:
        """Handle inline Planner actions and reject stale buttons safely."""
        session = self.store.session(user_id)
        state = session["state"] if session else None

        if callback_data.startswith("pl:conf:"):
            if state != "planning_conflict":
                return ["Эта кнопка больше не актуальна. Текущий конфликт уже изменён или закрыт."]
            return await self._resolve_conflict(
                user_id,
                "да" if callback_data == "pl:conf:yes" else "нет",
                today,
            )

        if callback_data.startswith("pl:alt:"):
            if state != "planning_conflict":
                return ["Этот вариант времени больше не актуален."]
            value = callback_data.removeprefix("pl:alt:")
            if value.isdigit():
                return await self._resolve_conflict(user_id, str(int(value) + 1), today)
            return ["Некорректный вариант времени."]

        if callback_data.startswith("pl:review:"):
            if state != "review_status":
                return ["Эта кнопка больше не актуальна. Текущий пункт анализа уже изменён."]
            status = {
                "pl:review:+": "+",
                "pl:review:-": "-",
                "pl:review:partial": "+-",
            }.get(callback_data)
            if status is None:
                return ["Неизвестное действие анализа."]
            day = date.fromisoformat(session["target_day"]) if session and session["target_day"] else today
            return await self._review_status(user_id, status, day)

        if callback_data.startswith("pl:edit:"):
            if state != "review_edit_select":
                return ["Эта кнопка больше не актуальна. Снова открой «Исправить анализ»."]
            value = callback_data.removeprefix("pl:edit:")
            if value.isdigit():
                day = date.fromisoformat(session["target_day"]) if session and session["target_day"] else today
                return await self._review_edit_select(user_id, value, day)
            return ["Некорректный номер задачи."]

        if callback_data == "pl:skip:final1":
            if state != "review_final1":
                return ["Эта кнопка больше не актуальна."]
            day = date.fromisoformat(session["target_day"]) if session and session["target_day"] else today
            return await self._review_final1(user_id, "пропустить", day)

        if callback_data == "pl:skip:final2":
            if state != "review_final2":
                return ["Эта кнопка больше не актуальна."]
            day = date.fromisoformat(session["target_day"]) if session and session["target_day"] else today
            return await self._review_final2(user_id, "пропустить", day)

        return ["Эта кнопка больше не актуальна. Повтори действие из текущего сообщения."]

    async def show_plan(self, user_id: int, day: date) -> str:
        return render_plan_text(day, self.store.ensure_daily_plan(user_id, day, self.config), self.config)

    def stop(self, user_id: int) -> None:
        self.store.clear_session(user_id)
