from datetime import date

import pytest

from voqelis.planner.evidence import (
    EvidenceKind,
    EvidenceRecognitionError,
    SourceSpan,
    locate_source_span,
    recognize_intent_evidence,
)
from voqelis.planner.intent_validation import IntentValidationError, validate_task_intents
from voqelis.planner.models import TaskDraft


def test_recognizer_returns_canonical_entities_with_source_spans():
    text = "завтра в 21.00 ужинать"
    evidence = recognize_intent_evidence(text, today=date(2026, 10, 4))

    assert [entity.kind for entity in evidence.entities] == [
        EvidenceKind.DATE,
        EvidenceKind.TIME,
    ]
    time_entity = evidence.entities[1]
    assert time_entity.start_minute == 21 * 60
    assert time_entity.span.text == "в 21.00"


def test_clock_meridiem_is_normalized_without_becoming_a_period_constraint():
    text = "завтра в 8 утра отжаться"
    evidence = recognize_intent_evidence(text, today=date(2026, 10, 4))

    time_entity = next(
        entity for entity in evidence.entities if entity.kind == EvidenceKind.TIME
    )
    assert time_entity.start_minute == 8 * 60
    assert not any(entity.kind == EvidenceKind.PERIOD for entity in evidence.entities)


def test_recognizer_captures_range_period_and_relation():
    text = "завтра после обеда с 12:00 до 14:00 читать"
    evidence = recognize_intent_evidence(text, today=date(2026, 10, 4))

    assert any(
        entity.kind == EvidenceKind.TIME_RANGE
        and entity.start_minute == 12 * 60
        and entity.end_minute == 14 * 60
        for entity in evidence.entities
    )
    assert any(
        entity.kind == EvidenceKind.RELATION
        and entity.relation == "after"
        and entity.anchor == "lunch"
        for entity in evidence.entities
    )


def test_shared_single_date_is_inherited_by_tasks_without_date_in_excerpt():
    text = "завтра утром зарядка и вечером прогулка"
    evidence = recognize_intent_evidence(text, today=date(2026, 10, 4))

    second_start = text.index("вечером прогулка")
    second_span = SourceSpan(second_start, len(text), "вечером прогулка")

    facts = evidence.for_task(second_span, task_count=2)

    assert facts.day == date(2026, 10, 5)
    assert facts.period == "evening"


def test_multiple_explicit_dates_must_be_bound_to_task_sources():
    text = "завтра зарядка, послезавтра прогулка"
    evidence = recognize_intent_evidence(text, today=date(2026, 10, 4))

    first = TaskDraft(
        "зарядка",
        date(2026, 10, 5),
        source_text=text,
        source_excerpt="зарядка",
    )
    second = TaskDraft(
        "прогулка",
        date(2026, 10, 5),
        source_text=text,
        source_excerpt="прогулка",
    )

    with pytest.raises(IntentValidationError):
        validate_task_intents(
            [first, second],
            source_text=text,
            today=date(2026, 10, 4),
            evidence=evidence,
        )


def test_explicit_time_must_be_covered_by_a_task_excerpt():
    text = "завтра в 21:00 ужинать и читать"
    evidence = recognize_intent_evidence(text, today=date(2026, 10, 4))

    drafts = [
        TaskDraft(
            "ужинать",
            date(2026, 10, 5),
            source_text=text,
            source_excerpt="ужинать",
        ),
        TaskDraft(
            "читать",
            date(2026, 10, 5),
            source_text=text,
            source_excerpt="читать",
        ),
    ]

    with pytest.raises(IntentValidationError, match="ограничение"):
        validate_task_intents(
            drafts,
            source_text=text,
            today=date(2026, 10, 4),
            evidence=evidence,
        )


def test_ambiguous_source_excerpt_is_rejected():
    text = "завтра гулять и завтра гулять"
    with pytest.raises(EvidenceRecognitionError):
        locate_source_span(text, "завтра гулять")


def test_overlapping_task_excerpts_are_rejected():
    text = "завтра в 21:00 ужинать после ужина"
    evidence = recognize_intent_evidence(text, today=date(2026, 10, 4))
    first = TaskDraft(
        "ужинать",
        date(2026, 10, 5),
        start_minute=21 * 60,
        source_text=text,
        source_excerpt="завтра в 21:00 ужинать",
    )
    second = TaskDraft(
        "после ужина",
        date(2026, 10, 5),
        source_text=text,
        source_excerpt="ужинать после ужина",
    )

    with pytest.raises(IntentValidationError, match="перекрываются"):
        validate_task_intents(
            [first, second],
            source_text=text,
            today=date(2026, 10, 4),
            evidence=evidence,
        )


def test_single_task_may_use_title_only_excerpt_when_no_explicit_constraint_exists():
    text = "ужинать"
    evidence = recognize_intent_evidence(text, today=date(2026, 10, 4))
    draft = TaskDraft(
        "ужинать",
        date(2026, 10, 5),
        source_text=text,
        source_excerpt="ужинать",
    )

    validate_task_intents(
        [draft],
        source_text=text,
        today=date(2026, 10, 4),
        evidence=evidence,
    )


def test_single_task_uses_the_whole_source_as_authoritative_evidence():
    text = "завтра в 21:00 ужинать"
    evidence = recognize_intent_evidence(text, today=date(2026, 10, 4))
    draft = TaskDraft(
        "ужинать",
        date(2026, 10, 5),
        start_minute=21 * 60,
        source_text=text,
        source_excerpt="ужинать",
    )

    validate_task_intents(
        [draft],
        source_text=text,
        today=date(2026, 10, 4),
        evidence=evidence,
    )
