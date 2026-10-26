"""Pure offline tests for the new optional Horizon benchmark adapter."""

from __future__ import annotations

import pytest

from tools.horizon_onnx_probe import last_token_indices, render_marks, word_spans


def test_last_subtoken_is_selected_for_each_word() -> None:
    text = "друг общался раздраженно"
    assert word_spans(text) == [(0, 4), (5, 12), (13, 24)]
    offsets = [
        (0, 0), (0, 4), (5, 7), (7, 12),
        (13, 16), (16, 20), (20, 24), (0, 0),
    ]
    assert last_token_indices(text, offsets) == [1, 3, 6]


def test_fails_closed_on_truncated_or_missing_word() -> None:
    with pytest.raises(ValueError, match="cannot align"):
        last_token_indices("друг общался", [(0, 4), (5, 8)])
    with pytest.raises(ValueError, match="cannot align"):
        last_token_indices("друг", [(0, 0)])


def test_renders_only_punctuation_and_preserves_original_words() -> None:
    assert render_marks("друг общался раздраженно", ["0", "0", "."]) == (
        "друг общался раздраженно."
    )
    assert render_marks("мы разошлись по домам", ["0", "0", "0", "."]) == (
        "мы разошлись по домам."
    )


def test_suppresses_punctuation_inside_clock_values_and_dashes() -> None:
    assert render_marks("завтра в 13:00 созвон", ["0", "0", ",", "."]) == (
        "завтра в 13:00 созвон."
    )
    assert render_marks("сегодня завтра", ["-", "."]) == "сегодня завтра."


def test_rejects_wrong_count_bad_label_and_prepunctuated_text() -> None:
    with pytest.raises(ValueError, match="count mismatch"):
        render_marks("один два", ["."])
    with pytest.raises(ValueError, match="Unexpected label"):
        render_marks("один", ["BAD"])
    with pytest.raises(ValueError, match="already contains"):
        render_marks("друг пришёл.", ["0", "0"])
