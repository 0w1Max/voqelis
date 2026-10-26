"""Workflow Studio persistence: private SQLite, revisions, restart, owner isolation."""

import sqlite3
import stat

import pytest

from voqelis.workflows.spec import WorkflowSpec
from voqelis.workflows.store import WorkflowStore


@pytest.fixture
def templates():
    facts = {
        "id": "daily_reflections", "version": 1, "title": "Факты дня",
        "fields": [
            {"key": "event", "label": "Событие",
             "prompt": "Что произошло?", "kind": "text"},
            {"key": "facts", "label": "Факты",
             "prompt": "Что ты наблюдал?", "kind": "list"},
            {"key": "judgments", "label": "Суждения",
             "prompt": "Что подумал?", "kind": "list"},
        ],
        "exports": ["docx", "pdf", "pdf_mobile"],
    }
    shopping = {
        "id": "shopping", "version": 1, "title": "Покупки",
        "fields": [
            {"key": "shop", "label": "Магазин", "prompt": "Где?", "kind": "text"},
            {"key": "items", "label": "Товары", "prompt": "Что?", "kind": "list"},
        ],
        "exports": ["pdf"],
    }
    return WorkflowSpec.from_mapping(facts), WorkflowSpec.from_mapping(shopping)


@pytest.fixture
def store(tmp_path):
    instance = WorkflowStore(tmp_path / "workflows.sqlite3")
    yield instance
    instance.close()


def _filled(store: WorkflowStore, owner: int, template_id: str, version: int):
    draft_id, draft = store.begin(owner, template_id, version)
    draft = store.submit(owner, draft_id, draft.token, ("Прогулка",))
    draft = store.submit(
        owner, draft_id, draft.token,
        ("Друг говорил спокойно.", "Мы разошлись по домам."),
    )
    draft = store.advance(owner, draft_id, draft.token)
    if template_id == "daily_reflections":
        draft = store.submit(owner, draft_id, draft.token, ("Я недостаточно общителен.",))
        draft = store.advance(owner, draft_id, draft.token)
    return draft_id, draft


def test_publish_and_execute_two_user_defined_workflows(store, templates):
    facts, shopping = templates
    store.publish(5, facts)
    store.publish(5, shopping)
    one_id, one = _filled(store, 5, facts.id, 1)
    two_id, two = _filled(store, 5, shopping.id, 1)
    assert one.stage == "review" and two.stage == "review"
    id1, rec1 = store.confirm(5, one_id, one.token)
    id2, rec2 = store.confirm(5, two_id, two.token)
    assert id1 != id2
    assert rec1.values[1][1] == (
        "Друг говорил спокойно.", "Мы разошлись по домам.",
    )
    assert rec2.template_id == shopping.id
    assert len(store.records(5, facts.id)) == 1
    assert len(store.records(5, shopping.id)) == 1
    assert store.draft(5, one_id) is None


def test_persist_unfinished_draft_and_resume_after_process_restart(tmp_path, templates):
    db = tmp_path / "workflow-store.sqlite3"
    facts, _ = templates
    first = WorkflowStore(db)
    first.publish(5, facts)
    draft_id, draft = first.begin(5, facts.id, 1)
    draft = first.submit(5, draft_id, draft.token, ("Посмотрели фильм.",))
    first.close()
    reopened = WorkflowStore(db)
    try:
        same_id, recovered = reopened.begin(5, facts.id, 1)
        assert draft_id == same_id and recovered == draft
        completed = reopened.submit(5, draft_id, draft.token, ("Потом обсудили сюжет.",))
        assert completed.field_values("facts") == ("Потом обсудили сюжет.",)
        assert reopened.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        reopened.close()


def test_stale_audio_token_is_rejected_transactionally(store, templates):
    facts, _ = templates
    store.publish(5, facts)
    draft_id, draft = store.begin(5, facts.id, 1)
    advanced = store.submit(5, draft_id, draft.token, ("Событие.",))
    with pytest.raises(ValueError, match="Stale"):
        store.submit(5, draft_id, draft.token, ("потерянное старое сообщение",))
    assert store.draft(5, draft_id) == advanced


def test_cross_user_ownership_is_enforced_on_all_actions(store, templates):
    facts, _ = templates
    store.publish(5, facts)
    draft_id, draft = store.begin(5, facts.id, 1)
    assert store.template(6, facts.id, 1) is None
    assert store.draft(6, draft_id) is None
    with pytest.raises(ValueError, match="owner"):
        store.submit(6, draft_id, draft.token, ("чужое",))
    with pytest.raises(ValueError, match="owner"):
        store.confirm(6, draft_id, draft.token)
    with pytest.raises(ValueError, match="owner"):
        store.begin(6, facts.id, 1)
    assert store.records(6, facts.id) == []
    assert store.draft(5, draft_id) == draft


def test_template_revision_is_immutable_and_historical_records_keep_version(
    store, templates
):
    facts, _ = templates
    store.publish(5, facts)
    store.publish(5, facts)  # idempotent, not a new revision
    draft_id, draft = _filled(store, 5, facts.id, 1)
    store.confirm(5, draft_id, draft.token)
    modified = facts.to_mapping()
    modified["fields"][1]["prompt"] = "Другой вопрос"
    with pytest.raises(ValueError, match="immutable"):
        store.publish(5, WorkflowSpec.from_mapping(modified))
    modified["version"] = 2
    updated = WorkflowSpec.from_mapping(modified)
    store.publish(5, updated)
    assert store.template(5, facts.id, 1) == facts
    assert store.template(5, facts.id, 2) == updated
    first = store.records(5, facts.id)
    assert first[0][1].template_version == 1
    assert first[0][1].template_fingerprint == facts.fingerprint


def test_changing_version_while_draft_exists_is_forbidden(store, templates):
    facts, _ = templates
    store.publish(5, facts)
    spec2 = facts.to_mapping()
    spec2["version"] = 2
    store.publish(5, WorkflowSpec.from_mapping(spec2))
    draft_id, draft = store.begin(5, facts.id, 1)
    with pytest.raises(ValueError, match="existing draft"):
        store.begin(5, facts.id, 2)
    assert store.draft(5, draft_id) == draft


def test_edit_selected_item_and_cancel_do_not_mutate_other_fields(store, templates):
    facts, _ = templates
    store.publish(5, facts)
    draft_id, d = _filled(store, 5, facts.id, 1)
    original = d.values
    d = store.begin_edit(5, draft_id, d.token, "facts", 1)
    restored = store.cancel_edit(5, draft_id, d.token)
    assert restored.values == original
    d = store.begin_edit(5, draft_id, restored.token, "facts", 1)
    d = store.submit(5, draft_id, d.token, ("Друг говорил раздражённо.",))
    assert d.field_values("facts") == (
        "Друг говорил раздражённо.", "Мы разошлись по домам.",
    )
    _, recorded = store.confirm(5, draft_id, d.token)
    assert recorded.values[2][1] == ("Я недостаточно общителен.",)


def test_private_permissions_and_symlink_refusal(tmp_path):
    path = tmp_path / "workflow.sqlite3"
    inst = WorkflowStore(path)
    try:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    finally:
        inst.close()
    link = tmp_path / "alias.sqlite3"
    link.symlink_to(path)
    with pytest.raises(OSError):
        WorkflowStore(link)


def test_original_planner_database_is_untouched(tmp_path, templates):
    planner = tmp_path / "planner.sqlite3"
    with sqlite3.connect(planner) as conn:
        conn.execute("CREATE TABLE planner_data(x INT)")
        conn.execute("INSERT INTO planner_data VALUES(42)")
    store = WorkflowStore(tmp_path / "workflows.sqlite3")
    store.publish(5, templates[0])
    store.close()
    with sqlite3.connect(f"file:{planner}?mode=ro", uri=True) as conn:
        assert conn.execute("SELECT x FROM planner_data").fetchone() == (42,)


def test_history_limit_and_bad_authenticated_id_fail_closed(store, templates):
    with pytest.raises(ValueError):
        store.publish(0, templates[0])
    with pytest.raises(ValueError):
        store.records(5, "shopping", limit=1000000)
