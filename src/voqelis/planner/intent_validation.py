from __future__ import annotations

from itertools import pairwise

from .evidence import (
    EvidenceRecognitionError,
    IntentEvidence,
    SourceSpan,
    TaskEvidence,
    locate_source_span,
    recognize_intent_evidence,
)


class IntentValidationError(ValueError):
    """The model output contradicts explicit source evidence."""


def _validate_task_span_overlaps(spans: list[SourceSpan]) -> None:
    ordered = sorted(spans, key=lambda span: (span.start, span.end))
    for previous, current in pairwise(ordered):
        if previous.overlaps(current):
            raise IntentValidationError(
                "Источники нескольких задач перекрываются."
            )


def _validate_entity_coverage(
    evidence: IntentEvidence,
    spans: list[SourceSpan],
) -> None:
    date_values = {
        entity.day
        for entity in evidence.entities
        if entity.day is not None
    }

    for entity in evidence.entities:
        covered = any(span.contains(entity.span) for span in spans)
        if covered:
            continue

        # A single explicit date may be shared by several tasks. Other
        # constraints (time, range, period, relation) must be bound by the
        # task's own source span.
        if entity.day is not None and len(date_values) == 1:
            continue

        raise IntentValidationError(
            "Явное ограничение в исходном тексте не связано ни с одной задачей."
        )


def _validate_intent(
    intent,
    *,
    facts: TaskEvidence,
) -> None:
    if facts.day is not None and intent.day != facts.day:
        raise IntentValidationError(
            f"Модель изменила явную дату {facts.day.isoformat()}."
        )

    if facts.start_minute is not None and intent.start_minute != facts.start_minute:
        raise IntentValidationError(
            f"Модель изменила явное время {facts.start_minute}."
        )

    if facts.end_minute is not None and intent.end_minute != facts.end_minute:
        raise IntentValidationError(
            f"Модель изменила явный конец диапазона {facts.end_minute}."
        )
    if facts.end_minute is None and intent.end_minute is not None:
        raise IntentValidationError(
            "Модель добавила конец времени, которого нет в источнике."
        )

    if facts.period is not None and intent.period != facts.period:
        raise IntentValidationError(
            f"Модель изменила явный период {facts.period}."
        )

    if facts.relation is not None:
        if intent.relation != facts.relation or intent.anchor != facts.anchor:
            raise IntentValidationError(
                "Модель изменила явное отношение к приёму пищи."
            )
    elif intent.relation is not None or intent.anchor is not None:
        raise IntentValidationError(
            "Модель добавила отношение к приёму пищи, которого нет в источнике."
        )

    if facts.relation_end is not None:
        if (
            intent.relation_end != facts.relation_end
            or intent.anchor_end != facts.anchor_end
        ):
            raise IntentValidationError(
                "Модель изменила вторую границу окна к приёму пищи."
            )
    elif intent.relation_end is not None or intent.anchor_end is not None:
        raise IntentValidationError(
            "Модель добавила вторую границу окна к приёму пищи, которой нет в источнике."
        )

    if (
        facts.start_minute is None
        and facts.end_minute is None
        and intent.start_minute is not None
    ):
        raise IntentValidationError(
            "Модель придумала точное время, которого нет в источнике."
        )

    if facts.period is None and intent.period is not None:
        raise IntentValidationError(
            "Модель придумала период суток, которого нет в источнике."
        )


def validate_task_intents(
    intents,
    *,
    source_text: str,
    today,
    evidence: IntentEvidence | None = None,
    config=None,
) -> None:
    """Validate AI task drafts against independently recognized source evidence."""
    del config
    if not intents:
        return

    if evidence is None:
        try:
            evidence = recognize_intent_evidence(source_text, today=today)
        except EvidenceRecognitionError as exc:
            raise IntentValidationError(str(exc)) from exc

    task_spans: list[SourceSpan] = []
    for intent in intents:
        excerpt = (intent.source_excerpt or "").strip()
        if not excerpt:
            if len(intents) != 1:
                raise IntentValidationError(
                    "Для нескольких задач модель обязана указать источник каждой задачи."
                )
            excerpt = source_text.strip()

        try:
            task_spans.append(locate_source_span(source_text, excerpt))
        except EvidenceRecognitionError as exc:
            raise IntentValidationError(str(exc)) from exc

    _validate_task_span_overlaps(task_spans)

    # Single-task input is explicitly authoritative: when the model identifies
    # only one task, every explicit source constraint belongs to that task.
    # This is intentionally different from multi-task inputs, where non-date
    # constraints must be tied to their own source spans.
    if len(task_spans) == 1:
        authoritative_span = SourceSpan(0, len(source_text), source_text)
        _validate_entity_coverage(evidence, [authoritative_span])
        facts_by_task = [evidence.for_task(authoritative_span)]
    else:
        _validate_entity_coverage(evidence, task_spans)
        facts_by_task = [
            evidence.for_task(span)
            for span in task_spans
        ]

    for intent, facts in zip(intents, facts_by_task):
        try:
            _validate_intent(intent, facts=facts)
        except EvidenceRecognitionError as exc:
            raise IntentValidationError(str(exc)) from exc

