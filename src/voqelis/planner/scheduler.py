from __future__ import annotations

from .config import PlannerConfig
from .models import (
    Conflict,
    ConflictProposal,
    PlanItem,
    ScheduleMove,
    ScheduleValidationError,
    TaskDraft,
    TaskKind,
)
from .store import PlannerStore


class Scheduler:
    def __init__(self, store: PlannerStore, config: PlannerConfig):
        self.store = store
        self.config = config

    @staticmethod
    def _overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
        return a[0] < b[1] and b[0] < a[1]

    def _valid(self, start: int, end: int) -> bool:
        return (
            self.config.plan_start_minute <= start < end <= self.config.plan_end_minute
            and start % self.config.slot_minutes == 0
            and end % self.config.slot_minutes == 0
        )

    def _duration(self, draft: TaskDraft) -> int:
        if draft.start_minute is not None and draft.end_minute is not None:
            duration = draft.end_minute - draft.start_minute
            if duration <= 0 or duration % self.config.slot_minutes:
                raise ScheduleValidationError("Точный диапазон должен соответствовать часовой сетке V1.")
            return duration
        duration = draft.duration_minutes or self.config.default_duration_minutes
        if duration <= 0:
            raise ScheduleValidationError("Длительность должна быть больше нуля.")
        # V1 renders an hourly grid. A non-grid duration is rounded up for placement,
        # while the PlanItem remains a single logical task.
        return ((duration + self.config.slot_minutes - 1) // self.config.slot_minutes) * self.config.slot_minutes

    def _candidates(self, draft: TaskDraft, duration: int) -> list[tuple[int, int]]:
        ws, we = self.config.plan_start_minute, self.config.plan_end_minute
        if draft.start_minute is not None:
            start = draft.start_minute
            if start < ws and start == 0:
                start = 24 * 60
            return [(start, start + duration)]

        if draft.relation and draft.anchor:
            anchor = self.config.anchor(draft.anchor)
            if anchor is None:
                raise ScheduleValidationError(f"Неизвестная опорная точка: {draft.anchor}")
            if draft.relation == "after":
                lo, hi = anchor[1], we
                preferred = lo
            elif draft.relation == "before":
                lo, hi = ws, anchor[0]
                preferred = max(ws, hi - duration)
            else:
                raise ScheduleValidationError(f"Неизвестное отношение: {draft.relation}")
        else:
            period = self.config.period(draft.period) if draft.period else None
            if period is None:
                lo, hi = ws, we
            elif period.name == "night":
                lo = max(ws, 24 * 60)
                hi = min(we, 30 * 60)
            else:
                lo = max(ws, period.start)
                hi = min(we, period.end)
            preferred = draft.preferred_minute
            if preferred is not None and period is not None and period.name == "night" and preferred < 24 * 60:
                preferred += 24 * 60

        if hi - lo < duration:
            return []
        first = lo + ((self.config.slot_minutes - lo % self.config.slot_minutes) % self.config.slot_minutes)
        last = hi - duration
        last -= last % self.config.slot_minutes
        starts = list(range(first, last + 1, self.config.slot_minutes))
        if preferred is not None:
            starts.sort(key=lambda value: (abs(value - preferred), value))
        return [(start, start + duration) for start in starts]

    def _moves(
        self,
        draft: TaskDraft,
        desired: tuple[int, int],
        conflicts: list[PlanItem],
        occupied: list[PlanItem],
    ) -> tuple[ScheduleMove, ...]:
        if not conflicts:
            return ()
        if any(x.kind == TaskKind.RECURRING for x in conflicts) and not draft.urgent:
            return ()
        planned = [x for x in occupied if x.id not in {c.id for c in conflicts}]
        moves: list[ScheduleMove] = []
        for blocker in sorted(conflicts, key=lambda x: (x.start_minute, x.id)):
            duration = blocker.end_minute - blocker.start_minute
            found = None
            for start in range(
                max(blocker.end_minute, self.config.plan_start_minute),
                self.config.plan_end_minute - duration + 1,
                self.config.slot_minutes,
            ):
                candidate = (start, start + duration)
                if self._overlap(candidate, desired):
                    continue
                if any(self._overlap(candidate, (x.start_minute, x.end_minute)) for x in planned):
                    continue
                if any(self._overlap(candidate, (m.new_start_minute, m.new_end_minute)) for m in moves):
                    continue
                found = candidate
                break
            if found is None:
                return ()
            moves.append(ScheduleMove(blocker.id, *found, blocker.start_minute, blocker.end_minute))
        return tuple(moves)

    def schedule(self, user_id: int, draft: TaskDraft) -> PlanItem | Conflict:
        duration = self._duration(draft)
        candidates = self._candidates(draft, duration)
        if not candidates:
            proposal = ConflictProposal(draft, 0, 0, (), (), ())
            return Conflict(proposal, "В заданном окне нет подходящего места.")

        occupied = self.store.plan_items(user_id, draft.day)
        desired = candidates[0]
        if not self._valid(*desired):
            raise ScheduleValidationError("Задача выходит за границы планировочной таблицы.")

        # Without an exact start/end, choose the earliest candidate that is
        # actually free. Periods/relations are search windows, not reasons to
        # force a conflict at the first occupied slot.
        if draft.start_minute is None:
            for candidate in candidates:
                if not any(
                    self._overlap(candidate, (x.start_minute, x.end_minute))
                    for x in occupied
                ):
                    desired = candidate
                    break

        conflicts = [x for x in occupied if self._overlap(desired, (x.start_minute, x.end_minute))]
        if not conflicts:
            item_id = self.store.add_item(
                PlanItem(0, user_id, draft.day, draft.title, draft.why, *desired, TaskKind.ORDINARY, None, draft.urgent, draft.source_text)
            )
            return self.store.get_plan_item(item_id)

        if draft.start_minute is not None:
            # Exact-time requests keep the requested slot as the primary choice,
            # but conflicts expose the nearest free hourly slots instead of forcing
            # the user to invent another time manually.
            alternatives_pool = [
                (start, start + duration)
                for start in range(
                    self.config.plan_start_minute,
                    self.config.plan_end_minute - duration + 1,
                    self.config.slot_minutes,
                )
            ]
            alternatives = tuple(
                candidate for candidate in sorted(
                    alternatives_pool,
                    key=lambda value: (abs(value[0] - desired[0]), value[0]),
                )
                if candidate != desired
                and not any(self._overlap(candidate, (x.start_minute, x.end_minute)) for x in occupied)
            )[:3]
        else:
            alternatives = tuple(
                candidate for candidate in candidates
                if self._valid(*candidate)
                and not any(self._overlap(candidate, (x.start_minute, x.end_minute)) for x in occupied)
            )[:3]
        moves = self._moves(draft, desired, conflicts, occupied)
        reason = "Запрошенное время занято."
        if moves:
            reason = "Можно разово перенести конфликтующую задачу только после подтверждения."
            if any(x.kind == TaskKind.RECURRING for x in conflicts):
                reason = "Срочная задача конфликтует с КД; предлагаю разовый перенос КД только на этот день, после подтверждения."
        elif draft.urgent and any(x.kind == TaskKind.RECURRING for x in conflicts):
            reason = "Срочная задача конфликтует с КД; разовый перенос КД требует подтверждения."
        proposal = ConflictProposal(draft, *desired, tuple(conflicts), alternatives, moves)
        return Conflict(proposal, reason)

    def apply_proposal(self, user_id: int, proposal: ConflictProposal) -> PlanItem:
        current = self.store.plan_items(user_id, proposal.draft.day)
        current_conflicts = tuple(
            x for x in current
            if self._overlap(
                (proposal.desired_start_minute, proposal.desired_end_minute),
                (x.start_minute, x.end_minute),
            )
        )
        if {x.id for x in current_conflicts} != {x.id for x in proposal.conflicts}:
            raise ScheduleValidationError("План изменился после предложения; требуется новое согласование.")
        try:
            item_id = self.store.apply_moves_and_add(
                moves=proposal.moves,
                item=PlanItem(
                    0, user_id, proposal.draft.day, proposal.draft.title, proposal.draft.why,
                    proposal.desired_start_minute, proposal.desired_end_minute, TaskKind.ORDINARY,
                    None, proposal.draft.urgent, proposal.draft.source_text,
                ),
            )
        except ValueError as exc:
            raise ScheduleValidationError(
                f"План изменился до подтверждения: {exc}. Повтори согласование."
            ) from exc
        return self.store.get_plan_item(item_id)


def fmt_time(m: int) -> str:
    m %= 24 * 60
    return f"{m // 60:02d}:{m % 60:02d}"
