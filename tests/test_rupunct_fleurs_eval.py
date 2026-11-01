"""Unit tests for held-out punctuation measurement, without loading ONNX."""

import pytest

from tools.rupunct_fleurs_eval import (
    boundaries,
    boundary_counts,
    choose_unique,
    evaluate,
    punctuation_summary,
    tokens,
)


def test_lexical_tokens_normalize_case_and_yo():
    assert tokens("Ёж, в 21:00!") == ["еж", "в", "21", "00"]


def test_punctuation_is_scored_at_word_boundaries():
    _, marks, cases = boundaries("Сегодня, хорошая погода.")
    assert marks == [",", "", "."]
    assert cases == [True, False, False]


def test_true_positive_false_positive_false_negative_counts():
    stats = boundary_counts([".", "", ",", "?"], [".", ",", ".", ""])
    assert stats == {
        "correct": 1,
        "total": 4,
        "tp": 1,
        "fp": 2,
        "fn": 2,
    }
    assert punctuation_summary(stats)["exact_mark_f1_pct"] == pytest.approx(33.33)


def test_selection_filters_long_and_duplicate_references():
    rows = [
        {"reference_words": "сегодня хорошая погода",
         "reference_punctuation": "Сегодня хорошая погода.",
         "duration_seconds": 4.0},
        {"reference_words": "СЕГОДНЯ хорошая погода",
         "reference_punctuation": "Сегодня хорошая погода.",
         "duration_seconds": 5.0},
        {"reference_words": "другой текст",
         "reference_punctuation": "Другой текст.", "duration_seconds": 26.0},
    ]
    selected, stats = choose_unique(rows)
    assert len(selected) == 1
    assert stats == {"total": 3, "over_duration": 1, "duplicate_text": 1}


def test_evaluate_counts_exact_word_subset_and_capitalization():
    rows = [
        {"file": "a.wav", "reference_words": "это проверка текста",
         "reference_punctuation": "Это проверка текста.",
         "duration_seconds": 4.0},
        {"file": "b.wav", "reference_words": "сегодня я работаю",
         "reference_punctuation": "Сегодня я работаю!",
         "duration_seconds": 4.0},
    ]
    outputs = {
        "это проверка текста": "Это проверка текста.",
        "сегодня я работаю": "Сегодня я работаю.",
    }
    result = evaluate(rows, outputs.__getitem__)
    assert result["selection"]["lexically_comparable"] == 2
    assert result["punctuation_raw"]["correct"] == 4
    assert result["punctuation_restored"]["correct"] == 5
    assert result["punctuation_restored"]["total"] == 6
    assert result["capitalization_initial_accuracy_pct"] == 100.0


def test_evaluation_skips_reference_word_mismatch_not_the_result():
    rows = [{"reference_words": "модель ошиблась",
             "reference_punctuation": "Модель не ошиблась.",
             "duration_seconds": 5.0}]
    result = evaluate(rows, lambda s: s)
    assert result["selection"]["lexically_comparable"] == 0
    assert result["selection"]["not_lexically_comparable"] == 1


def test_evaluation_rejects_any_word_rewriting():
    rows = [{"reference_words": "послезавтра гулять",
             "reference_punctuation": "Послезавтра гулять.",
             "duration_seconds": 5.0}]
    with pytest.raises(ValueError, match="lexical"):
        evaluate(rows, lambda s: "после завтра гулять")


def test_selection_rejects_invalid_metadata():
    with pytest.raises(ValueError, match="duration"):
        choose_unique([{
            "reference_words": "текст",
            "reference_punctuation": "Текст.",
            "duration_seconds": float("nan"),
        }])
