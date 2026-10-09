from asr_benchmark import _wer_numeric_normalized


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
