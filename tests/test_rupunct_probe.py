"""Pure tests; no ONNX download or optional dependencies required."""

import pytest

from tools.rupunct_probe import render_labeled_words


def test_capitalization_and_punctuation_do_not_modify_words():
    original = "сегодня хорошая погода а завтра продолжим тестирование"
    labels = ["UPPER_O", "LOWER_O", "LOWER_COMMA", "LOWER_O", "LOWER_O", "LOWER_O", "LOWER_PERIOD"]
    assert render_labeled_words(original, labels) == (
        "Сегодня хорошая погода, а завтра продолжим тестирование."
    )


def test_protected_time_is_preserved():
    assert render_labeled_words(
        "завтра в 21:00 ужинать", ["UPPER_O", "LOWER_O", "LOWER_O", "LOWER_PERIOD"]
    ) == "Завтра в 21:00 ужинать."


def test_already_punctuated_input_is_not_touched():
    original = "Сегодня готово, а завтра продолжим."
    assert render_labeled_words(original, ["LOWER_O"] * 5) == original


def test_rejects_misaligned_model_output():
    with pytest.raises(ValueError, match="count differs"):
        render_labeled_words("два слова", ["LOWER_O"])


def test_rejects_unknown_model_labels():
    with pytest.raises(ValueError, match="unsupported model label"):
        render_labeled_words("слово", ["REWRITE_WORD"])


def test_empty_input():
    assert render_labeled_words("", []) == ""
