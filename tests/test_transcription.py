import math

import pytest

from voqelis.transcription import build_pause_aware_text


def test_pause_aware_text_adds_commas_at_long_word_boundary_gaps() -> None:
    text = "для здоровья для настроения для духовного опыта"
    tokens = [
        "для",
        " здоровья",
        " для",
        " настроения",
        " для",
        " духовного",
        " опыта",
    ]
    timestamps = [0.0, 0.15, 0.95, 1.10, 1.95, 2.10, 2.25]

    assert build_pause_aware_text(text, tokens, timestamps) == (
        "для здоровья, для настроения, для духовного опыта"
    )


def test_pause_aware_text_does_not_add_commas_for_short_gaps() -> None:
    text = "для здоровья для настроения"
    tokens = ["для", " здоровья", " для", " настроения"]
    timestamps = [0.0, 0.15, 0.55, 0.70]

    assert build_pause_aware_text(text, tokens, timestamps) == text


@pytest.mark.parametrize("ending", [".", "!", "?", ",", ";", ":"])
def test_pause_aware_text_does_not_duplicate_existing_punctuation(ending: str) -> None:
    text = f"готово{ending} дальше"
    tokens = [f"готово{ending}", " дальше"]
    timestamps = [0.0, 1.0]

    assert build_pause_aware_text(text, tokens, timestamps) == text


@pytest.mark.parametrize(
    ("tokens", "timestamps"),
    [
        (None, None),
        (["слова"], [0.0, 1.0]),
        (["другое"], [0.0]),
        (["слова", " ещё"], [0.0, math.nan]),
        (["слова", " ещё"], [1.0, 0.5]),
    ],
)
def test_pause_aware_text_falls_back_when_metadata_is_invalid(tokens, timestamps) -> None:
    text = "слова ещё"
    assert build_pause_aware_text(text, tokens, timestamps) == text


def test_pause_aware_text_falls_back_if_tokens_do_not_reconstruct_text() -> None:
    assert build_pause_aware_text(
        "исходный текст",
        ["другой", " текст"],
        [0.0, 1.0],
    ) == "исходный текст"
