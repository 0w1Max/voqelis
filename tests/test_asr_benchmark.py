import json
from datetime import date

from asr_benchmark import _load_manifest, _wer_numeric_normalized


def test_numeric_wer_treats_spoken_hour_as_numeric_hour() -> None:
    assert (
        _wer_numeric_normalized(
            "Послезавтра в 13 часов сделать тест",
            "Послезавтра в тринадцать часов сделать тест",
        )
        == 0.0
    )


def test_numeric_wer_does_not_hide_different_hour() -> None:
    assert (
        _wer_numeric_normalized(
            "Послезавтра в 13 часов сделать тест",
            "Послезавтра в четырнадцать часов сделать тест",
        )
        > 0.0
    )


def test_numeric_wer_normalizes_compound_number() -> None:
    assert (
        _wer_numeric_normalized(
            "в 21 час",
            "в двадцать один час",
        )
        == 0.0
    )



def test_manifest_accepts_per_case_reference_dates(tmp_path):
    audio = tmp_path / "sample.wav"
    audio.write_bytes(b"placeholder")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "today": "2026-10-09",
                "cases": [
                    {
                        "id": "tomorrow",
                        "audio": "sample.wav",
                        "reference": "завтра",
                        "today": "2026-10-10",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    _, cases = _load_manifest(manifest)

    assert cases[0].today == date(2026, 10, 10)
