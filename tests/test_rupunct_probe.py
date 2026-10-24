"""Pure tests; no ONNX download or optional dependencies required."""

from itertools import pairwise

import pytest

from tools.rupunct_probe import OnnxRUPunct, plan_token_windows, render_labeled_words


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
    assert any(
        next_start < end
        for (_, end, _), (next_start, _, _) in pairwise(windows)
    )


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


class _FakeTokenizer:
    def encode(self, words, *, is_pretokenized):
        assert is_pretokenized
        return type("Encoding", (), {"ids": [0] * (2 + 2 * len(words))})()


class _FakePunctuator(OnnxRUPunct):
    def __init__(self):
        self.tokenizer = _FakeTokenizer()
        self.inference_calls = 0

    def _predict_labels(self, words):
        self.inference_calls += 1
        return ["LOWER_O"] * len(words)


def test_long_transcript_is_not_dropped_or_truncated():
    model = _FakePunctuator()
    original = " ".join(["слово"] * 500)
    assert model.punctuate(original) == original
    assert model.inference_calls > 1


def test_excessively_long_transcript_fails_open_without_inference():
    model = _FakePunctuator()
    original = " ".join(["слово"] * 2049)
    assert model.punctuate(original) == original
    assert model.inference_calls == 0


def test_sentence_boundary_capitalizes_next_word_even_when_model_says_lower():
    raw = "привет сегодня работаю нужно купить продукты"
    labels = [
        "UPPER_COMMA", "LOWER_O", "LOWER_PERIOD",
        "LOWER_O", "LOWER_O", "LOWER_PERIOD",
    ]
    assert render_labeled_words(raw, labels) == (
        "Привет, сегодня работаю. Нужно купить продукты."
    )


def test_question_and_exclamation_capitalize_following_sentence():
    raw = "как дела всё хорошо тогда отлично"
    labels = [
        "UPPER_O", "LOWER_QUESTION", "LOWER_O", "LOWER_VOSKL",
        "LOWER_O", "LOWER_PERIOD",
    ]
    assert render_labeled_words(raw, labels) == (
        "Как дела? Всё хорошо! Тогда отлично."
    )


def test_comma_colon_semicolon_dashes_do_not_start_a_new_sentence():
    raw = "один два три четыре пять"
    labels = [
        "UPPER_COMMA", "LOWER_DVOETOCHIE", "LOWER_PERIODCOMMA",
        "LOWER_TIRE", "LOWER_PERIOD",
    ]
    assert render_labeled_words(raw, labels) == (
        "Один, два: три; четыре — пять."
    )


def test_clock_time_suppression_does_not_trigger_capitalization():
    raw = "в 21:00 ужинать затем читать"
    labels = ["LOWER_O", "LOWER_PERIOD", "LOWER_O", "LOWER_PERIOD", "LOWER_PERIOD"]
    assert render_labeled_words(raw, labels) == "в 21:00 ужинать. Затем читать."


def test_sentence_case_preserves_times_and_lexical_content():
    raw = "в 21:00 ужинать потом в 22:00 читать книгу"
    labels = [
        "LOWER_O", "LOWER_O", "LOWER_PERIOD", "LOWER_O", "LOWER_O",
        "LOWER_O", "LOWER_O", "LOWER_PERIOD",
    ]
    result = render_labeled_words(raw, labels)
    assert result == "в 21:00 ужинать. Потом в 22:00 читать книгу."
    assert result.replace(".", "").replace(":", ":").split()[0] == "в"
