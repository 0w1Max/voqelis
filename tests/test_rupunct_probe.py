"""Pure tests; no ONNX download or optional dependencies required."""

import pytest

from tools.rupunct_probe import plan_token_windows, render_labeled_words


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


def test_time_tokens_do_not_get_extra_colons():
    assert render_labeled_words(
        "завтра в 21:00 ужинать потом в 22:00 читать книгу",
        [
            "UPPER_O", "LOWER_O", "LOWER_DVOETOCHIE", "LOWER_O",
            "LOWER_COMMA", "LOWER_O", "LOWER_DVOETOCHIE", "LOWER_O", "LOWER_PERIOD",
        ],
    ) == "Завтра в 21:00 ужинать потом, в 22:00 читать книгу."


def test_time_token_keeps_terminal_sentence_punctuation():
    assert render_labeled_words("встреча в 13:00", [
        "UPPER_O", "LOWER_O", "LOWER_PERIOD",
    ]) == "Встреча в 13:00."


def test_intermediate_times_cannot_be_broken_into_sentences():
    assert render_labeled_words(
        "с 23:00 до 01:00 читать", [
            "LOWER_O", "LOWER_PERIOD", "LOWER_O", "LOWER_COMMA", "LOWER_PERIOD",
        ]
    ) == "с 23:00 до 01:00 читать."


def test_windows_cover_every_word_with_overlap():
    count = lambda start, end: 2 + (end - start) * 2
    windows = plan_token_windows(
        90, count, max_subtokens=30, overlap_words=4
    )
    assert len(windows) > 2
    assert all(count(start, end) <= 30 for start, end, _ in windows)
    assert [i for start, _, emit_end in windows for i in range(start, emit_end)] == list(
        range(90)
    )
    assert any(next_start < end for (_, end, _), (next_start, _, _) in
               zip(windows, windows[1:], strict=False))


def test_window_planner_rejects_oversized_word():
    with pytest.raises(ValueError, match="single word"):
        plan_token_windows(1, lambda start, end: 1000, max_subtokens=256)


def test_window_planner_rejects_too_many_windows():
    with pytest.raises(ValueError, match="too many"):
        plan_token_windows(
            90, lambda start, end: 2 + (end - start) * 2,
            max_subtokens=8, overlap_words=1, max_windows=2
        )


def test_window_planner_handles_empty_transcript():
    assert plan_token_windows(0, lambda start, end: 2) == []
