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
        covered = any(entity.span.overlaps(span) for span in spans)
        if covered:
            continue
        if entity.day is not None and len(date_values) == 1:
            continue
        raise IntentValidationError(
            "Явное ограничение в исходном тексте не связано ни с одной задачей."
        )


def validate_task_intents(
    intents,
    *,
    source_text: str,
    today,
    evidence: IntentEvidence | None = None,
    config=None,
) -> None:
    del config
    if not intents:
        return

    if evidence is None:
        try:
            evidence = recognize_intent_evidence(source_text, today=today)
        except EvidenceRecognitionError as exc:
            raise IntentValidationError(str(exc)) from exc

    spans: list[SourceSpan] = []
    for intent in intents:
        excerpt = (intent.source_excerpt or "").strip()
        if not excerpt:
            if len(intents) != 1:
                raise IntentValidationError(
                    "Для нескольких задач модель обязана указать источник каждой задачи."
                )
            excerpt = source_text.strip()

        try:
            span = locate_source_span(source_text, excerpt)
        except EvidenceRecognitionError as exc:
            raise IntentValidationError(str(exc)) from exc
        spans.append(span)

    _validate_task_span_overlaps(spans)
    _validate_entity_coverage(evidence, spans)

    for intent, span in zip(intents, spans):
        try:
            facts = evidence.for_task(span)
        except EvidenceRecognitionError as exc:
            raise IntentValidationError(str(exc)) from exc

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


def explicit_constraints(
    text: str,
    *,
    today,
    config=None,
) -> TaskEvidence:
    """Compatibility facade for existing internal tests/callers."""
    del config
    try:
        evidence = recognize_intent_evidence(text, today=today)
        span = SourceSpan(0, len(text), text)
        return evidence.for_task(span)
    except EvidenceRecognitionError as exc:
        raise IntentValidationError(str(exc)) from exc
