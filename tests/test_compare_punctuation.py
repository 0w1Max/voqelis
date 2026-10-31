"""Offline punctuation A/B measurements never inspect private production files."""

import json
import stat

import pytest

from tools.compare_punctuation import (
    _counts,
    _words_and_marks,
    compare,
    load_predictions,
    main,
)

CASES = [
    {
        "id": "synthetic_1",
        "reference_words": "мы посмотрели фильм потом пошли домой",
        "reference_punctuation": "Мы посмотрели фильм. Потом пошли домой.",
        "duration_seconds": 4.0,
    },
    {
        "id": "synthetic_2",
        "reference_words": "она ответила спокойно мы продолжили обсуждение",
        "reference_punctuation": "Она ответила спокойно, мы продолжили обсуждение.",
        "duration_seconds": 4.0,
    },
]


def _pred(*texts):
    return {
        c["id"]: {"text": text, "latency_ms": 12.0}
        for c, text in zip(CASES, texts, strict=True)
    }


def test_token_alignment_supports_times_and_cyrillic():
    words, marks = _words_and_marks("Завтра в 21:00, ужин.")
    assert words == ("завтра", "в", "21:00", "ужин")
    assert marks == ("", "", ",", ".")


def test_counts_for_extra_comma_and_missing_period():
    assert _counts((False, True), (True, False)) == {
        "tp": 0, "fp": 1, "fn": 1,
    }


def test_bad_punctuation_has_worse_scores_than_good():
    good = _pred(
        "Мы посмотрели фильм. Потом пошли домой.",
        "Она ответила спокойно, мы продолжили обсуждение.",
    )
    bad = _pred(
        "Мы посмотрели, фильм потом пошли домой.",
        "Она ответила, спокойно мы продолжили обсуждение.",
    )
    result = compare(CASES, {"good": good, "bad": bad}, min_cases=2)
    assert result["candidates"]["good"]["metrics"]["comma"]["f1_pct"] == 100
    assert result["candidates"]["good"]["metrics"]["end"]["f1_pct"] == 100
    assert result["candidates"]["good"]["exact_all_punctuation_pct"] == 100
    assert result["candidates"]["bad"]["metrics"]["comma"]["f1_pct"] < 100
    assert result["candidates"]["bad"]["sentence_boundary_error_cases"] == 1
    assert result["automatic_winner"] is None
    assert "пошли домой" not in json.dumps(result, ensure_ascii=False)


def test_candidate_changing_one_word_is_rejected():
    with pytest.raises(ValueError, match="lexical change"):
        compare(
            CASES,
            {"unsafe": _pred(
                "Мы посмотрели сериал. Потом пошли домой.",
                "Она ответила спокойно, мы продолжили обсуждение.",
            )},
            min_cases=2,
        )


def test_candidate_changing_e_to_yo_is_rejected():
    data = [{
        "id": "yo", "reference_words": "мы идем домой",
        "reference_punctuation": "Мы идем домой.", "duration_seconds": 1.0,
    }]
    with pytest.raises(ValueError, match="lexical change"):
        compare(
            data, {"unsafe": {"yo": {"text": "Мы идём домой."}}},
            min_cases=1,
        )


def test_unmatched_gold_is_excluded_equally_for_all_models():
    data = [
        *CASES,
        {
            "id": "not-comparable",
            "reference_words": "одно слово",
            "reference_punctuation": "Два других слова.",
            "duration_seconds": 3.0,
        },
    ]
    report = compare(
        data, {"baseline": _pred(
            "Мы посмотрели фильм. Потом пошли домой.",
            "Она ответила спокойно, мы продолжили обсуждение.",
        )}, min_cases=2,
    )
    assert report["selection"]["gold_word_mismatch"] == 1
    assert report["candidates"]["baseline"]["examples"] == 2


def test_missing_candidate_prediction_is_error():
    with pytest.raises(ValueError, match="missing"):
        compare(CASES, {"empty": {}}, min_cases=1)


def test_too_few_examples_cannot_count_as_success():
    with pytest.raises(ValueError, match="need at least 30"):
        compare(CASES, {"empty": {}}, min_cases=30)


def test_invalid_negative_latency_is_rejected(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('{"one":{"text":"пример", "latency_ms":-1}}')
    with pytest.raises(ValueError, match="latency"):
        load_predictions(path)


def test_load_predictions_requires_ids_and_text(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('["bad"]')
    with pytest.raises(ValueError, match="object"):
        load_predictions(path)


def test_file_cli_writes_private_aggregate_report(tmp_path, monkeypatch, capsys):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(CASES, ensure_ascii=False))
    candidate = tmp_path / "candidate.json"
    candidate.write_text(json.dumps(_pred(
        "Мы посмотрели фильм. Потом пошли домой.",
        "Она ответила спокойно, мы продолжили обсуждение.",
    ), ensure_ascii=False))
    output = tmp_path / "report.json"
    monkeypatch.setattr(
        "sys.argv",
        ["compare", "--manifest", str(manifest), "--candidate",
         f"good={candidate}", "--output", str(output), "--min-cases", "2"],
    )
    assert main() == 0
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert "Мы посмотрели" not in output.read_text()
    assert "Мы посмотрели" not in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main()
