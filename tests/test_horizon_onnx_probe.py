"""Pure offline tests for the new optional Horizon benchmark adapter."""

from __future__ import annotations

import pytest

from tools.horizon_onnx_probe import last_token_indices, render_marks


def test_last_subtoken_is_selected_by_word_id() -> None:
    # Original text has Cyrillic and normalization-sensitive words. A
    # tokenizer can have offset end positions that differ from input spans;
    # word_ids from pretokenized input reliably select the last subtoken.
    ids = [None, 0, 1, 1, 2, 2, 2, 3, 4, 5, None]
    assert last_token_indices(6, ids) == [1, 3, 6, 7, 8, 9]


def test_aligns_user_failure_case_byl_and_clock() -> None:
    # 13:00 may be several subtokens, but is still one input word.
    # Same principle fixes failure at 25:28 ("был") in the live probe.
    ids = [None, 0, 0, 1, 1, 2, 2, 3, 4, 5, 5, 5, 6, None]
    assert last_token_indices(7, ids) == [2, 4, 6, 7, 8, 11, 12]


def test_fails_closed_on_truncated_or_missing_word() -> None:
    with pytest.raises(ValueError, match="lost or truncated"):
        last_token_indices(2, [None, 0, 0, None])
    with pytest.raises(ValueError, match="lost or truncated"):
        last_token_indices(1, [None, None])
    with pytest.raises(ValueError, match="No input words"):
        last_token_indices(0, [None])


def test_rejects_reordered_or_invalid_word_ids() -> None:
    with pytest.raises(ValueError, match="invalid or reordered"):
        last_token_indices(2, [None, 1, 0, None])
    with pytest.raises(ValueError, match="invalid or reordered"):
        last_token_indices(2, [None, 0, 2])
    with pytest.raises(ValueError, match="invalid or reordered"):
        last_token_indices(1, [None, True, None])


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
