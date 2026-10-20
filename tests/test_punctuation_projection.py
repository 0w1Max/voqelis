import re

from voqelis.punctuation_projection import project_punctuation

_WORD_RE = re.compile(r"[^\W_]+(?::[^\W_]+)*", re.UNICODE)


def words(text: str) -> list[str]:
    return [
        match.group().casefold().replace("ё", "е")
        for match in _WORD_RE.finditer(text)
    ]


def test_projects_list_punctuation_and_sentence_case_without_changing_words() -> None:
    e2e = (
        "Это тест списка. Для здоровья, для настроения, "
        "для духовного опыта. Ничего не добавляй в план."
    )
    rnnt = (
        "это тест списка для здоровья для настроения "
        "для духовного опыта ничего не добавляй в план"
    )

    result = project_punctuation(e2e, rnnt)

    assert result.applied
    assert result.text == (
        "Это тест списка. Для здоровья, для настроения, "
        "для духовного опыта. Ничего не добавляй в план."
    )
    assert words(result.text) == words(rnnt)
    assert result.source_coverage == 1.0
    assert result.target_coverage == 1.0


def test_preserves_rnnt_words_when_e2e_mangles_spoken_number() -> None:
    e2e = "С 2ти 3х вечера до часа ночи подготовиться ко сну."
    rnnt = "с двадцати трех вечера до часа ночи подготовиться ко сну"

    result = project_punctuation(e2e, rnnt)

    assert result.applied
    assert result.text == "С двадцати трех вечера до часа ночи подготовиться ко сну."
    assert words(result.text) == words(rnnt)
    assert "2ти" not in result.text
    assert "3х" not in result.text


def test_aligns_digit_time_to_spoken_number_without_copying_numeric_form() -> None:
    e2e = "Сегодня в 13:00 проверить результаты."
    rnnt = "сегодня в тринадцать ноль ноль проверить результаты"

    result = project_punctuation(e2e, rnnt)

    assert result.applied
    assert result.text == "Сегодня в тринадцать ноль ноль проверить результаты."
    assert words(result.text) == words(rnnt)


def test_projects_colon_commas_period_and_question_mark() -> None:
    e2e = "Это список: для здоровья, для настроения. Всё понятно?"
    rnnt = "это список для здоровья для настроения всё понятно"

    result = project_punctuation(e2e, rnnt)

    assert result.applied
    assert result.text == "Это список: для здоровья, для настроения. Всё понятно?"


def test_does_not_duplicate_existing_target_punctuation() -> None:
    result = project_punctuation("Привет, мир.", "привет, мир!")

    assert result.applied
    assert result.text == "Привет, мир!"


def test_falls_back_to_unchanged_rnnt_when_alignment_is_weak() -> None:
    rnnt = "совершенно неизвестная фраза без совпадающих слов"
    result = project_punctuation("Завтра в 13:00 проверить результаты.", rnnt)

    assert not result.applied
    assert result.text == rnnt
    assert result.reason != "aligned"


def test_empty_e2e_text_falls_back_to_rnnt() -> None:
    result = project_punctuation("", "исходный текст")

    assert not result.applied
    assert result.text == "исходный текст"


def test_capitalizes_sentence_after_lowercase_e2e_boundary() -> None:
    e2e = (
        "Это тест списка. для здоровья, для настроения, "
        "для духовного опыта. ничего не добавляй в план."
    )
    rnnt = (
        "это тест списка для здоровья для настроения "
        "для духовного опыта ничего не добавляй в план"
    )

    result = project_punctuation(e2e, rnnt)

    assert result.applied
    assert result.text == (
        "Это тест списка. Для здоровья, для настроения, "
        "для духовного опыта. Ничего не добавляй в план."
    )
    assert words(result.text) == words(rnnt)


def test_joins_hyphenated_words_without_inserting_spaces() -> None:
    e2e = "Во-первых, из-за Советско-Польского договора."
    rnnt = "во первых из за советско польского договора"

    result = project_punctuation(e2e, rnnt)

    assert result.applied
    assert result.text == e2e
    assert words(result.text) == words(rnnt)


def test_keeps_spaces_around_sentence_dash() -> None:
    result = project_punctuation("Он сказал — да.", "он сказал да")

    assert result.applied
    assert result.text == "Он сказал — да."
