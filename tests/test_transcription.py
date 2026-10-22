import math

import pytest

from voqelis.domain import TranscriptSegment
from voqelis.transcription import build_pause_aware_text


def _segment(text: str, start: float, end: float) -> TranscriptSegment:
    return TranscriptSegment(text=text, start_seconds=start, end_seconds=end)


def test_pause_aware_text_adds_commas_between_vad_segments_after_long_silence() -> None:
    text = "для здоровья для настроения для духовного опыта"
    segments = (
        _segment("для здоровья", 0.0, 0.5),
        _segment("для настроения", 1.1, 1.5),
        _segment("для духовного опыта", 2.1, 2.8),
    )

    assert build_pause_aware_text(text, segments) == (
        "для здоровья, для настроения, для духовного опыта"
    )


def test_pause_aware_text_does_not_add_commas_for_short_gaps() -> None:
    text = "для здоровья для настроения"
    segments = (
        _segment("для здоровья", 0.0, 0.5),
        _segment("для настроения", 0.7, 1.1),
    )

    assert build_pause_aware_text(text, segments) == text


def test_long_segment_duration_is_not_mistaken_for_a_pause() -> None:
    text = "длинное произнесённое слово дальше"
    segments = (
        _segment("длинное произнесённое слово", 0.0, 1.2),
        _segment("дальше", 1.3, 1.6),
    )

    assert build_pause_aware_text(text, segments) == text


@pytest.mark.parametrize("ending", [".", "!", "?", ",", ";", ":"])
def test_pause_aware_text_does_not_duplicate_existing_punctuation(ending: str) -> None:
    text = f"готово{ending} дальше"
    segments = (
        _segment(f"готово{ending}", 0.0, 0.4),
        _segment("дальше", 1.2, 1.6),
    )

    assert build_pause_aware_text(text, segments) == text


@pytest.mark.parametrize(
    "segments",
    [
        (),
        (_segment("другое", 0.0, 0.4),),
        (_segment("слова", 0.0, math.nan),),
        (_segment("слова", 1.0, 0.5),),
        (_segment("сначала", 1.0, 1.2), _segment("потом", 0.5, 0.8)),
    ],
)
def test_pause_aware_text_falls_back_when_segment_metadata_is_invalid(segments) -> None:
    text = "исходный текст"
    assert build_pause_aware_text(text, segments) == text


def test_pause_aware_text_handles_punctuation_at_start_of_segment() -> None:
    text = "готово, дальше"
    segments = (
        _segment("готово", 0.0, 0.4),
        _segment(",", 1.2, 1.2),
        _segment("дальше", 1.3, 1.6),
    )

    assert build_pause_aware_text(text, segments) == text
