"""Deterministic, immutable workflow state transitions; no Telegram/ASR/SQL.

ASR and sentence segmentation are input adapters. The runtime only accepts
already selected, user-approved text items; it never changes words or labels.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, replace
from typing import Literal

from .spec import WorkflowSpec

Stage = Literal["collect", "review", "completed"]
_MAX_ITEM_CHARS = 12_000
_MAX_ITEMS_PER_FIELD = 250


def _token() -> str:
    return secrets.token_hex(12)


def _input(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > _MAX_ITEM_CHARS:
        raise ValueError("An item must contain between 1 and 12000 characters")
    return value.strip()


@dataclass(frozen=True, slots=True)
class WorkflowDraft:
    user_id: int
    template_id: str
    template_version: int
    template_fingerprint: str
    step: int
    stage: Stage
    values: tuple[tuple[str, tuple[str, ...]], ...]
    token: str
    edit: tuple[str, int] | None = None

    def field_values(self, key: str) -> tuple[str, ...]:
        for field_key, values in self.values:
            if field_key == key:
                return values
        raise KeyError("Unknown field")


@dataclass(frozen=True, slots=True)
class WorkflowRecord:
    user_id: int
    template_id: str
    template_version: int
    template_fingerprint: str
    values: tuple[tuple[str, tuple[str, ...]], ...]


def start_workflow(spec: WorkflowSpec, user_id: int) -> WorkflowDraft:
    if type(user_id) is not int or user_id <= 0:
        raise ValueError("Authenticated user ID required")
    return WorkflowDraft(
        user_id=user_id, template_id=spec.id,
        template_version=spec.version, template_fingerprint=spec.fingerprint,
        step=0, stage="collect", token=_token(),
        values=tuple((field.key, ()) for field in spec.fields),
    )


def _ensure(draft: WorkflowDraft, spec: WorkflowSpec, user_id: int, token: str) -> None:
    if user_id != draft.user_id or type(user_id) is not int or user_id <= 0:
        raise ValueError("Unauthorized workflow operation")
    if token != draft.token:
        raise ValueError("Stale workflow operation")
    if (
        draft.template_id != spec.id
        or draft.template_version != spec.version
        or draft.template_fingerprint != spec.fingerprint
    ):
        raise ValueError("Immutable template revision mismatch")
    if tuple(field.key for field in spec.fields) != tuple(key for key, _ in draft.values):
        raise ValueError("Draft field structure mismatch")


def _changed(
    draft: WorkflowDraft, key: str, items: tuple[str, ...]
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    return tuple((k, items if k == key else value) for k, value in draft.values)


def submit(
    draft: WorkflowDraft, spec: WorkflowSpec,
    user_id: int, token: str, items: tuple[str, ...]
) -> WorkflowDraft:
    """Submit one text response or one/many already segmented list items."""
    _ensure(draft, spec, user_id, token)
    if not isinstance(items, tuple) or not 1 <= len(items) <= _MAX_ITEMS_PER_FIELD:
        raise ValueError("Expected one or more items")
    clean = tuple(_input(item) for item in items)
    if draft.edit is not None:
        if draft.stage != "review" or len(clean) != 1:
            raise ValueError("Editing replaces exactly one reviewed item")
        key, index = draft.edit
        current = list(draft.field_values(key))
        if not 1 <= index <= len(current):
            raise ValueError("Selected item no longer exists")
        current[index - 1] = clean[0]
        return replace(
            draft, values=_changed(draft, key, tuple(current)),
            token=_token(), edit=None,
        )

    if draft.stage != "collect":
        raise ValueError("Workflow is not collecting")
    field = spec.fields[draft.step]
    if field.kind == "text":
        if len(clean) != 1:
            raise ValueError("Text field accepts one value")
        values = _changed(draft, field.key, clean)
        next_step = draft.step + 1
        return replace(
            draft, values=values, step=next_step,
            stage="review" if next_step == len(spec.fields) else "collect",
            token=_token(),
        )
    old = draft.field_values(field.key)
    if len(old) + len(clean) > _MAX_ITEMS_PER_FIELD:
        raise ValueError("List limit exceeded")
    return replace(
        draft, values=_changed(draft, field.key, old + clean),
        token=_token(),
    )


def advance(
    draft: WorkflowDraft, spec: WorkflowSpec,
    user_id: int, token: str
) -> WorkflowDraft:
    """Close one nonempty repeated list and advance to next question."""
    _ensure(draft, spec, user_id, token)
    if draft.stage != "collect" or draft.edit is not None:
        raise ValueError("Not collecting")
    field = spec.fields[draft.step]
    if field.kind != "list" or not draft.field_values(field.key):
        raise ValueError("Only populated lists can be advanced manually")
    next_step = draft.step + 1
    return replace(
        draft, step=next_step,
        stage="review" if next_step == len(spec.fields) else "collect",
        token=_token(),
    )


def begin_edit(
    draft: WorkflowDraft, spec: WorkflowSpec, user_id: int, token: str,
    field_key: str, item_index: int
) -> WorkflowDraft:
    _ensure(draft, spec, user_id, token)
    if draft.stage != "review" or draft.edit is not None:
        raise ValueError("Edits are only allowed during review")
    current = draft.field_values(field_key)
    if type(item_index) is not int or not 1 <= item_index <= len(current):
        raise ValueError("Invalid item number")
    return replace(draft, edit=(field_key, item_index), token=_token())


def cancel_edit(
    draft: WorkflowDraft, spec: WorkflowSpec, user_id: int, token: str
) -> WorkflowDraft:
    _ensure(draft, spec, user_id, token)
    if draft.edit is None:
        raise ValueError("No active edit")
    return replace(draft, edit=None, token=_token())


def confirm(
    draft: WorkflowDraft, spec: WorkflowSpec, user_id: int, token: str
) -> tuple[WorkflowDraft, WorkflowRecord]:
    """Return immutable record only after explicit review and confirmation."""
    _ensure(draft, spec, user_id, token)
    if draft.stage != "review" or draft.edit is not None:
        raise ValueError("Review and finish corrections before saving")
    if any(not items for _, items in draft.values):
        raise ValueError("Workflow is incomplete")
    completed = replace(draft, stage="completed", token=_token())
    return completed, WorkflowRecord(
        user_id=draft.user_id,
        template_id=spec.id,
        template_version=spec.version,
        template_fingerprint=spec.fingerprint,
        values=draft.values,
    )
