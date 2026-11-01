"""No Telegram or database dependencies: deterministic template engine contracts."""

import json

import pytest

from voqelis.workflows.runtime import (
    advance,
    begin_edit,
    cancel_edit,
    confirm,
    start_workflow,
    submit,
)
from voqelis.workflows.spec import FieldSpec, WorkflowSpec


def _facts():
    return WorkflowSpec.from_mapping({
        "id": "facts_day",
        "version": 1,
        "title": "Факты дня",
        "fields": [
            {"key": "event", "label": "Событие", "prompt": "Что произошло?", "kind": "text"},
            {"key": "facts", "label": "Факты", "prompt": "Расскажи факты", "kind": "list"},
            {"key": "thoughts", "label": "Мысли", "prompt": "Что подумал?", "kind": "list"},
        ],
        "exports": ["docx", "pdf", "pdf_mobile"],
    })


def _shopping():
    return WorkflowSpec.from_mapping({
        "id": "shopping",
        "version": 1,
        "title": "Список покупок",
        "fields": [
            {"key": "category", "label": "Категория",
             "prompt": "Какой магазин?", "kind": "text"},
            {"key": "items", "label": "Покупки",
             "prompt": "Что купить?", "kind": "list"},
        ],
        "exports": ["pdf"],
    })


def test_facts_template_produces_three_fields_and_review_record():
    spec = _facts()
    d = start_workflow(spec, 17)
    assert d.stage == "collect" and d.step == 0
    d = submit(d, spec, 17, d.token, ("Гуляли с другом.",))
    assert d.step == 1
    d = submit(d, spec, 17, d.token, (
        "Друг смотрел в окно.", "Мы разошлись по домам.",
    ))
    assert d.field_values("facts") == (
        "Друг смотрел в окно.", "Мы разошлись по домам.",
    )
    d = advance(d, spec, 17, d.token)
    assert d.step == 2
    d = submit(d, spec, 17, d.token, ("Я что-то сделал не так.",))
    d = advance(d, spec, 17, d.token)
    assert d.stage == "review"
    completed, record = confirm(d, spec, 17, d.token)
    assert completed.stage == "completed"
    assert record.template_id == "facts_day"
    assert record.template_version == 1
    assert record.values[1][1] == (
        "Друг смотрел в окно.", "Мы разошлись по домам.",
    )


def test_second_template_runs_same_kernel_without_new_bot_handlers():
    spec = _shopping()
    d = start_workflow(spec, 23)
    d = submit(d, spec, 23, d.token, ("Продукты",))
    d = submit(d, spec, 23, d.token, ("Хлеб", "Молоко"))
    d = advance(d, spec, 23, d.token)
    _, entry = confirm(d, spec, 23, d.token)
    assert entry.values == (
        ("category", ("Продукты",)),
        ("items", ("Хлеб", "Молоко")),
    )
    assert entry.template_id == "shopping"


def test_edit_selected_item_only_and_keep_others():
    spec = _facts()
    d = start_workflow(spec, 17)
    d = submit(d, spec, 17, d.token, ("Прогулка",))
    d = submit(d, spec, 17, d.token, ("Знак препинания, не там.", "Второй факт."))
    d = advance(d, spec, 17, d.token)
    d = submit(d, spec, 17, d.token, ("Мне неловко.",))
    d = advance(d, spec, 17, d.token)
    old = d.token
    d = begin_edit(d, spec, 17, d.token, "facts", 1)
    assert d.edit == ("facts", 1)
    with pytest.raises(ValueError, match="Stale"):
        submit(d, spec, 17, old, ("Потерянная запись",))
    with pytest.raises(ValueError, match="exactly one"):
        submit(d, spec, 17, d.token, ("Один.", "Два."))
    d = submit(d, spec, 17, d.token, ("Знак препинания не там, исправили.",))
    assert d.stage == "review" and d.edit is None
    assert d.field_values("facts") == (
        "Знак препинания не там, исправили.", "Второй факт.",
    )
    assert d.field_values("thoughts") == ("Мне неловко.",)


def test_edit_cancel_does_not_write():
    spec = _facts()
    d = start_workflow(spec, 17)
    d = submit(d, spec, 17, d.token, ("Ситуация",))
    d = submit(d, spec, 17, d.token, ("Факт",))
    d = advance(d, spec, 17, d.token)
    d = submit(d, spec, 17, d.token, ("Мысль",))
    d = advance(d, spec, 17, d.token)
    selected = begin_edit(d, spec, 17, d.token, "thoughts", 1)
    with pytest.raises(ValueError, match="corrections"):
        confirm(selected, spec, 17, selected.token)
    restored = cancel_edit(selected, spec, 17, selected.token)
    assert restored.values == d.values
    assert restored.edit is None
    assert restored.token != selected.token


def test_separate_owners_cannot_change_each_others_drafts():
    spec = _facts()
    a = start_workflow(spec, 17)
    b = start_workflow(spec, 42)
    with pytest.raises(ValueError, match="Unauthorized"):
        submit(a, spec, 42, a.token, ("Чужие данные",))
    with pytest.raises(ValueError, match="Unauthorized"):
        advance(a, spec, 42, a.token)
    assert a != b


def test_replayed_token_is_never_accepted():
    spec = _facts()
    a = start_workflow(spec, 17)
    b = submit(a, spec, 17, a.token, ("Событие",))
    with pytest.raises(ValueError, match="Stale"):
        submit(b, spec, 17, a.token, ("Повторный ввод",))


def test_schema_revision_is_immutable_and_bound_to_draft():
    spec = _facts()
    d = start_workflow(spec, 17)
    data = spec.to_mapping()
    data["fields"][0]["prompt"] = "Новый вопрос без версии"
    edited = WorkflowSpec.from_mapping(data)
    assert edited.fingerprint != spec.fingerprint
    with pytest.raises(ValueError, match="revision"):
        submit(d, edited, 17, d.token, ("Событие",))
    new_version = spec.to_mapping()
    new_version["version"] = 2
    with pytest.raises(ValueError, match="revision"):
        submit(d, WorkflowSpec.from_mapping(new_version), 17, d.token, ("Событие",))


def test_schema_json_round_trip_and_stable_fingerprint():
    spec = _facts()
    loaded = WorkflowSpec.from_json(spec.canonical_json())
    assert loaded == spec
    assert loaded.fingerprint == spec.fingerprint
    assert loaded.to_mapping()["fields"][1]["kind"] == "list"


@pytest.mark.parametrize("invalid", [
    {"id": "bad; rm -rf", "version": 1, "title": "t", "fields": [], "exports": []},
    {"id": "valid", "version": True, "title": "t", "fields": [], "exports": []},
    {"id": "valid", "version": 1, "title": "", "fields": [], "exports": []},
    {"id": "valid", "version": 1, "title": "t", "fields": [], "exports": ["pdf"]},
    {"id": "valid", "version": 1, "title": "t", "fields": [], "exports": [],
     "exec": "print('hello')"},
])
def test_malformed_template_rejected(invalid):
    with pytest.raises(ValueError):
        WorkflowSpec.from_mapping(invalid)


def test_duplicate_fields_and_unknown_export_rejected():
    data = _facts().to_mapping()
    data["fields"][2]["key"] = "facts"
    with pytest.raises(ValueError, match="Duplicate"):
        WorkflowSpec.from_mapping(data)
    data = _facts().to_mapping()
    data["exports"].append("send_to_doctor_automatically")
    with pytest.raises(ValueError, match="export"):
        WorkflowSpec.from_mapping(data)


def test_unknown_field_type_and_extra_field_attributes_rejected():
    data = _facts().to_mapping()
    data["fields"][1]["kind"] = "python"
    with pytest.raises(ValueError, match="text and list"):
        WorkflowSpec.from_mapping(data)
    data = _facts().to_mapping()
    data["fields"][0]["on_save"] = "exec arbitrary_code"
    with pytest.raises(ValueError):
        WorkflowSpec.from_mapping(data)


def test_caps_for_input_size_and_list_length():
    spec = _facts()
    d = start_workflow(spec, 17)
    with pytest.raises(ValueError):
        submit(d, spec, 17, d.token, ("x" * 12001,))
    d = submit(d, spec, 17, d.token, ("Событие",))
    with pytest.raises(ValueError):
        submit(d, spec, 17, d.token, tuple("пункт" for _ in range(251)))


def test_all_fields_must_be_confirmed_before_saving():
    spec = _facts()
    d = start_workflow(spec, 17)
    with pytest.raises(ValueError):
        confirm(d, spec, 17, d.token)
    d = submit(d, spec, 17, d.token, ("Событие",))
    with pytest.raises(ValueError, match="populated lists"):
        advance(d, spec, 17, d.token)


def test_json_template_size_cap_and_unsafe_ids():
    data = _facts().to_mapping()
    data["fields"][0]["prompt"] = "x" * 35000
    with pytest.raises(ValueError):
        WorkflowSpec.from_json(json.dumps(data))
    with pytest.raises(ValueError):
        FieldSpec(key="../evil", label="Поле", prompt="Вопрос", kind="text")



def test_malformed_unhashable_field_kind_is_validation_error():
    data = _facts().to_mapping()
    data["fields"][0]["kind"] = ["python"]
    with pytest.raises(ValueError, match="text and list"):
        WorkflowSpec.from_mapping(data)


def test_malformed_unhashable_export_is_validation_error():
    data = _facts().to_mapping()
    data["exports"] = [["run-code"]]
    with pytest.raises(ValueError, match="export"):
        WorkflowSpec.from_mapping(data)


def test_unknown_field_edit_uses_safe_validation_error():
    spec = _shopping()
    d = start_workflow(spec, 17)
    d = submit(d, spec, 17, d.token, ("Магазин",))
    d = submit(d, spec, 17, d.token, ("Книга",))
    d = advance(d, spec, 17, d.token)
    with pytest.raises(ValueError, match="Unknown field"):
        begin_edit(d, spec, 17, d.token, "system_command", 1)


def test_excessive_total_transcript_is_not_accepted():
    spec = WorkflowSpec.from_mapping({
        "id": "long_lists", "version": 1, "title": "Длинные списки",
        "fields": [
            {"key": "a", "label": "A", "prompt": "A?", "kind": "list"},
            {"key": "b", "label": "B", "prompt": "B?", "kind": "list"},
        ],
        "exports": [],
    })
    d = start_workflow(spec, 17)
    for _ in range(10):
        d = submit(d, spec, 17, d.token, ("a" * 10_000,))
    with pytest.raises(ValueError, match="size limit"):
        submit(d, spec, 17, d.token, ("b" * 10_000, "c" * 10_000, "d" * 10_000))
    assert sum(len(s) for s in d.field_values("a")) == 100_000
