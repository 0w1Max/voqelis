from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, replace
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

from .ai import PlannerAI
from .config import PlannerConfig
from .evidence import EvidenceRecognitionError, recognize_intent_evidence
from .export import build_docx, build_pdf
from .intent_validation import IntentValidationError, validate_task_intents
from .models import (
    Conflict,
    ConflictProposal,
    PlanItem,
    PlannerAIError,
    PlannerAIUnavailable,
    ScheduleMove,
    ScheduleValidationError,
    TaskDraft,
    TaskKind,
)
from .plan_operations import PlanOperationsWorkflow
from .review_flow import ReviewWorkflow
from .scheduler import Scheduler, fmt_time
from .store import PlannerStore

logger = logging.getLogger(__name__)


class PlannerService:
    _REVIEW_STATES = frozenset({
        "review_full_input",
        "review_full_confirm",
        "review_edit_select",
        "review_status",
        "review_detail",
        "review_final1",
        "review_final2",
    })
    def __init__(
        self,
        store: PlannerStore,
        config: PlannerConfig | None = None,
        ai: PlannerAI | None = None,
        export_dir: Path | None = None,
        log_content: bool = False,
    ):
        self.store = store
        self.config = config or PlannerConfig()
        self.scheduler = Scheduler(store, self.config)
        self.ai = ai
        self.export_dir = export_dir
        self.log_content = log_content
        self._user_locks: dict[int, asyncio.Lock] = {}
        self._reviews = ReviewWorkflow(store, self.config, ai)
        self._plans = PlanOperationsWorkflow(store, self.config)

    def _sync_stale_planning_session(self, user_id: int, today: date) -> None:
        session = self.store.session(user_id)
        if not session or session["state"] != "planning" or not session["target_day"]:
            return
        target_day = date.fromisoformat(session["target_day"])
        if target_day >= today:
            return
        active = self.store.active_plan_day(user_id)
        next_day = active if active is not None and active >= today else today + timedelta(days=1)
        self.store.set_active_plan_day(user_id, next_day)
        self.store.ensure_daily_plan(user_id, next_day, self.config)
        self.store.set_session(user_id, "planning", next_day, {})
        logger.info(
            "PLANNER_STALE_PLANNING_SESSION_ROLLED user=%s old_day=%s new_day=%s",
            user_id,
            target_day,
            next_day,
        )

    async def start_planning(self, user_id: int, day: date) -> str:
        async with self._user_lock(user_id):
            self.store.ensure_daily_plan(user_id, day, self.config)
            self.store.set_active_plan_day(user_id, day)
            self.store.set_session(user_id, "planning", day, {})
            return ""

    async def resolve_active_day(self, user_id: int, today: date) -> date:
        """Resolve active day and materialize its recurring tasks."""
        async with self._user_lock(user_id):
            active = self.store.active_plan_day(user_id)
            if active is None or active < today:
                active = today + timedelta(days=1)
                self.store.set_active_plan_day(user_id, active)
            self.store.ensure_daily_plan(user_id, active, self.config)
            self._sync_stale_planning_session(user_id, today)
            return active


    @staticmethod
    def _validate_extracted_intents(
        drafts: list[TaskDraft],
        *,
        text: str,
        today: date,
        config: PlannerConfig,
    ) -> list[TaskDraft]:
        """Validate AI semantics against independently recognized source evidence."""
        del config
        try:
            evidence = recognize_intent_evidence(text, today=today)
            validate_task_intents(
                drafts,
                source_text=text,
                today=today,
                evidence=evidence,
            )
        except (EvidenceRecognitionError, IntentValidationError) as exc:
            logger.warning("PLANNER_INTENT_VALIDATION_FAILED reason=%s", exc)
            raise PlannerAIError(str(exc)) from exc
        return drafts

    async def _extract(self, text: str, user_id: int, today: date) -> list[TaskDraft]:
        session = self.store.session(user_id)
        target = (
            date.fromisoformat(session["target_day"])
            if session and session["target_day"]
            else today + timedelta(days=1)
        )
        if self.ai is None:
            raise PlannerAIUnavailable(
                "Planner AI is not configured; semantic extraction is unavailable."
            )

        drafts = await self.ai.extract_tasks(
            text,
            today=today,
            target_day=target,
            config=self.config,
        )
        return self._validate_extracted_intents(
            drafts,
            text=text,
            today=today,
            config=self.config,
        )
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
            anchor=data.get("anchor"), relation_end=data.get("relation_end"),
            anchor_end=data.get("anchor_end"), why=data.get("why"),
            urgent=bool(data.get("urgent")),
            source_text=data.get("source_text", ""),
            source_excerpt=data.get("source_excerpt"),
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

    async def _add_drafts(
        self,
        user_id: int,
        drafts: list[TaskDraft],
        today: date,
        *,
        allow_missing_reason_for_first: bool = False,
    ) -> list[str]:
        replies: list[str] = []
        materialized_days: set[date] = set()
        for index, draft in enumerate(drafts):
            if draft.day not in materialized_days:
                self.store.ensure_daily_plan(user_id, draft.day, self.config)
                materialized_days.add(draft.day)

            if not draft.why and not (allow_missing_reason_for_first and index == 0):
                suggested = self.store.previous_why(user_id, draft.title)
                self.store.set_session(
                    user_id,
                    "planning_why",
                    draft.day,
                    {
                        "draft": asdict(draft) | {"day": draft.day.isoformat()},
                        "pending": [
                            asdict(item) | {"day": item.day.isoformat()}
                            for item in drafts[index + 1:]
                        ],
                        "suggested_why": suggested,
                        "why_mode": "confirm" if suggested else "input",
                    },
                )
                if suggested:
                    replies.append(
                        f"❓ Для «{draft.title}» раньше была указана причина:\n"
                        f"«{suggested}»\n\nИспользовать её?"
                    )
                else:
                    replies.append(
                        f"❓ Для «{draft.title}» не указана причина. "
                        "Зачем тебе нужно это сделать? Можешь написать или продиктовать. "
                        "Можно также нажать «Оставить пустым»."
                    )
                break
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
            message = f"🗓 Активный план: {draft.day.strftime('%d.%m.%Y')}\n\n✅ Добавил: {fmt_time(result.start_minute)}–{fmt_time(result.end_minute)} — {result.title}"
            message += f"\nЗачем: {result.why}" if result.why else "\nЗачем: не указано."
            replies.append(message)
        return replies

    async def _resolve_why(self, user_id: int, text: str, today: date) -> list[str]:
        session = self.store.session(user_id)
        payload = self.store.session_payload(user_id)
        if not session or not payload.get("draft"):
            self.store.set_session(user_id, "planning", today + timedelta(days=1), {})
            return ["Запрос причины больше не актуален. Возвращаюсь к планированию."]

        answer = text.strip()
        mode = payload.get("why_mode", "input")
        suggested = str(payload.get("suggested_why") or "").strip()

        if mode == "confirm" and answer.lower() in {"да", "д", "yes", "использовать"}:
            why = suggested or None
        elif answer.lower() in {"пропустить", "skip", "нет", "no"}:
            why = None
        elif mode == "confirm" and answer.lower() in {"другая", "другую", "other"}:
            self.store.set_session(
                user_id,
                "planning_why",
                date.fromisoformat(session["target_day"]),
                payload | {"why_mode": "input"},
            )
            return ["Хорошо. Напиши или продиктуй, зачем тебе нужна эта задача."]
        else:
            why = answer or None

        draft = self._draft(payload["draft"])
        draft = replace(draft, why=why)
        pending = [self._draft(x) for x in payload.get("pending", [])]
        self.store.set_session(user_id, "planning", draft.day, {})
        replies = await self._add_drafts(
            user_id,
            [draft, *pending],
            today,
            allow_missing_reason_for_first=True,
        )
        return replies

    def _user_lock(self, user_id: int) -> asyncio.Lock:
        return self._user_locks.setdefault(user_id, asyncio.Lock())

    async def add_from_text(self, user_id: int, text: str, today: date) -> list[str]:
        try:
            drafts = await self._extract(text, user_id, today)
        except (PlannerAIError, ValueError, TypeError):
            return ["⚠️ Не удалось разобрать задачу. Попробуй ещё раз."]
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
            item_id = self.store.add_item_if_free(
                PlanItem(
                    0, user_id, draft.day, draft.title, draft.why,
                    start, end, TaskKind.ORDINARY, None, draft.urgent, draft.source_text,
                )
            )
            if item_id is None:
                return ["⚠️ Выбранное время уже занято. Повтори согласование конфликта."]
            reply = f"✅ Добавил: {fmt_time(start)}–{fmt_time(end)} — {draft.title}"

        pending = [self._draft(x) for x in payload.get("pending", [])]
        self.store.set_session(user_id, "planning", draft.day, {})
        if pending:
            replies = [reply]
            replies.extend(await self._add_drafts(user_id, pending, today))
            return replies
        return [reply]

    async def start_review(
        self, user_id: int, day: date, today: date | None = None, *, preserve_active_day: bool = False
    ) -> str:
        async with self._user_lock(user_id):
            return await self._reviews.start_review(
                user_id, day, today, preserve_active_day=preserve_active_day
            )

    async def start_full_review(
        self, user_id: int, day: date, today: date | None = None, *, preserve_active_day: bool = False
    ) -> str:
        async with self._user_lock(user_id):
            return await self._reviews.start_full_review(
                user_id, day, today, preserve_active_day=preserve_active_day
            )

    async def start_review_edit(
        self, user_id: int, day: date, today: date | None = None, *, preserve_active_day: bool = False
    ) -> str:
        async with self._user_lock(user_id):
            return await self._reviews.start_review_edit(
                user_id, day, today, preserve_active_day=preserve_active_day
            )

    async def start_plan_edit(self, user_id: int, day: date) -> str:
        async with self._user_lock(user_id):
            return await self._plans.start_plan_edit(user_id, day)

    async def start_history(self, user_id: int, today: date) -> str:
        async with self._user_lock(user_id):
            return await self._plans.start_history(user_id, today)

    async def start_delete_item(self, user_id: int, day: date) -> str:
        async with self._user_lock(user_id):
            return await self._plans.start_delete_item(user_id, day)

    async def start_clear_plan(self, user_id: int, day: date) -> str:
        async with self._user_lock(user_id):
            return await self._plans.start_clear_plan(user_id, day)

    async def delete_plan_item(self, user_id: int, item_id: int, day: date) -> str:
        async with self._user_lock(user_id):
            return await self._plans.delete_plan_item(user_id, item_id, day)

    async def show_plan(self, user_id: int, day: date) -> str:
        async with self._user_lock(user_id):
            return await self._plans.show_plan(user_id, day)

    async def _handle_text(self, user_id: int, text: str, today: date) -> list[str]:
        session = self.store.session(user_id)
        if not session:
            return []
        state = session["state"]
        day = date.fromisoformat(session["target_day"]) if session["target_day"] else today
        if state in self._REVIEW_STATES and day > today:
            self.store.clear_session(user_id)
            return [self._reviews.future_review_message(day)]
        if state == "history_select":
            return await self._plans.history_select(user_id, text, today)
        if state == "history_day":
            return ["Используй кнопки для действий с выбранным историческим днём."]
        if state == "plan_delete_select":
            return await self._plans.delete_item_select(user_id, text, day)
        if state == "planning":
            return await self.add_from_text(user_id, text, today)
        if state == "plan_edit_select":
            return await self._plans.plan_edit_select(user_id, text, day)
        if state == "plan_edit_field":
            return await self._plans.plan_edit_field(user_id, text, day)
        if state == "plan_edit_value":
            return await self._plans.plan_edit_value(user_id, text, day)
        if state == "plan_clear_confirm":
            return await self._plans.clear_plan_confirm(user_id, text, day)
        if state == "planning_why":
            return await self._resolve_why(user_id, text, today)
        if state == "planning_conflict":
            return await self._resolve_conflict(user_id, text, today)
        if state == "review_full_input":
            return await self._reviews.review_full_input(user_id, text, day)
        if state == "review_full_confirm":
            return await self._reviews.review_full_confirm(user_id, text, day)
        if state == "review_edit_select":
            return await self._reviews.review_edit_select(user_id, text, day)
        if state == "review_status":
            return await self._reviews.review_status(user_id, text, day)
        if state == "review_detail":
            return await self._reviews.review_detail(user_id, text, day)
        if state == "review_final1":
            return await self._reviews.review_final1(user_id, text, day)
        if state == "review_final2":
            return await self._reviews.review_final2(user_id, text, day)
        return []

    async def handle_text(self, user_id: int, text: str, today: date) -> list[str]:
        async with self._user_lock(user_id):
            self._sync_stale_planning_session(user_id, today)
            return await self._handle_text(user_id, text, today)

    async def _handle_callback(self, user_id: int, callback_data: str, today: date) -> list[str]:
        """Handle inline Planner actions and reject stale buttons safely."""
        session = self.store.session(user_id)
        state = session["state"] if session else None
        if session and state in self._REVIEW_STATES and session["target_day"]:
            session_day = date.fromisoformat(session["target_day"])
            if session_day > today:
                self.store.clear_session(user_id)
                return [self._reviews.future_review_message(session_day)]

        if callback_data.startswith("pl:why:"):
            if state != "planning_why":
                return ["Эта кнопка больше не актуальна. Причина уже обработана."]
            action = callback_data.removeprefix("pl:why:")
            if action == "yes":
                return await self._resolve_why(user_id, "да", today)
            if action == "skip":
                return await self._resolve_why(user_id, "пропустить", today)
            if action == "other":
                session_payload = self.store.session_payload(user_id)
                if not session_payload:
                    return ["Эта кнопка больше не актуальна."]
                session_payload["why_mode"] = "input"
                self.store.set_session(
                    user_id,
                    "planning_why",
                    date.fromisoformat(session["target_day"]),
                    session_payload,
                )
                return ["Хорошо. Напиши или продиктуй новую причину."]
            return ["Неизвестное действие для причины."]

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

        if callback_data.startswith("pl:history:"):
            if state != "history_day" or not session or not session["target_day"]:
                return ["Эта кнопка больше не актуальна. Открой «🗓 История планов» заново."]
            day = date.fromisoformat(session["target_day"])
            action = callback_data.removeprefix("pl:history:")
            if action == "review":
                return [await self._reviews.start_review(user_id, day, today, preserve_active_day=True)]
            if action == "full":
                return [await self._reviews.start_full_review(user_id, day, today, preserve_active_day=True)]
            if action == "edit":
                return [await self._reviews.start_review_edit(user_id, day, today, preserve_active_day=True)]
            if action == "back":
                return [await self._plans.start_history(user_id, today)]
            return ["Неизвестное действие истории."]

        if callback_data.startswith("pl:full:"):
            if state != "review_full_confirm":
                return ["Эта кнопка больше не актуальна. Общий разбор уже изменён или закрыт."]
            day = date.fromisoformat(session["target_day"]) if session and session["target_day"] else today
            return await self._reviews.review_full_confirm(
                user_id,
                "да" if callback_data == "pl:full:yes" else "нет",
                day,
            )

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
            return await self._reviews.review_status(user_id, status, day)

        if callback_data.startswith("pl:edit:"):
            if state != "review_edit_select":
                return ["Эта кнопка больше не актуальна. Снова открой «Исправить анализ»."]
            value = callback_data.removeprefix("pl:edit:")
            if value.isdigit():
                day = date.fromisoformat(session["target_day"]) if session and session["target_day"] else today
                return await self._reviews.review_edit_select(user_id, value, day)
            return ["Некорректный номер задачи."]

        if callback_data in {"pl:clear:ordinary", "pl:clear:all", "pl:clear:no"}:
            if state != "plan_clear_confirm":
                return ["Эта кнопка больше не актуальна. Открой «🧹 Очистить план» заново."]
            day = date.fromisoformat(session["target_day"]) if session and session["target_day"] else today
            if callback_data == "pl:clear:no":
                return await self._plans.clear_plan_confirm(user_id, "нет", day)
            return await self._plans.clear_plan_confirm(
                user_id,
                "да",
                day,
                include_recurring=callback_data == "pl:clear:all",
            )

        if callback_data == "pl:skip:final1":
            if state != "review_final1":
                return ["Эта кнопка больше не актуальна."]
            day = date.fromisoformat(session["target_day"]) if session and session["target_day"] else today
            return await self._reviews.review_final1(user_id, "пропустить", day)

        if callback_data == "pl:skip:final2":
            if state != "review_final2":
                return ["Эта кнопка больше не актуальна."]
            day = date.fromisoformat(session["target_day"]) if session and session["target_day"] else today
            return await self._reviews.review_final2(user_id, "пропустить", day)

        return ["Эта кнопка больше не актуальна. Повтори действие из текущего сообщения."]

    async def export_day(self, user_id: int, day: date, fmt: str) -> Path:
        async with self._user_lock(user_id):
            if self.export_dir is None:
                raise RuntimeError("Planner export directory is not configured")
            self.store.ensure_daily_plan(user_id, day, self.config)
            if fmt not in {"docx", "pdf"}:
                raise ValueError("Unsupported planner export format")
            output = self.export_dir / f"planner-{uuid4().hex}.{fmt}"
            if fmt == "docx":
                return build_docx(user_id, day, self.store, self.config, output)
            return build_pdf(user_id, day, self.store, self.config, output)

    async def stop(self, user_id: int) -> None:
        async with self._user_lock(user_id):
            self.store.clear_session(user_id)
    async def handle_callback(self, user_id: int, callback_data: str, today: date) -> list[str]:
        async with self._user_lock(user_id):
            return await self._handle_callback(user_id, callback_data, today)
