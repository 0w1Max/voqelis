"""Operations V2: privacy, durable outbox, GitHub scope, callbacks and failures."""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from voqelis.access import AccessStore
from voqelis.navigation import NavigationStore
from voqelis.operations import bot as operations_bot
from voqelis.operations.bot import create_operations_router
from voqelis.operations.export import build_issue_body
from voqelis.operations.github import (
    BASE,
    REPO,
    GitHubAppConfig,
    GitHubExportFailure,
    GitHubIssuesClient,
    private_key_bytes,
)
from voqelis.operations.store import OperationsStore

SHA = "a" * 40


@pytest.fixture
def store(tmp_path):
    ops = OperationsStore(tmp_path / "operations.sqlite3", owner_id=1)
    try:
        yield ops
    finally:
        ops.close()


def report_for(store, user_id=2):
    store.record(
        user_id, "home", "audio", "failed",
        error_code="AUDIO_DECODE_FAILED",
    )
    token = store.begin_report(
        user_id, "User ID 987654321 password confidential transcript",
    )
    report = store.submit_report(user_id, token, 10)
    assert report is not None
    return report


def test_privacy_contract_excludes_comment_and_user_id(store):
    report = report_for(store)
    assert report.snapshot["error_codes"] == {"AUDIO_DECODE_FAILED": 1}
    preview = store.prepare_export(1, report.id, SHA)
    assert preview is not None
    body = preview.issue_body
    assert "AUDIO_DECODE_FAILED: 1" in body
    assert SHA in body
    assert "987654321" not in body
    assert "password" not in body
    assert "transcript" not in body.lower().split("###")[0]
    assert str(report.user_id) not in body.split("Export marker:")[0]
    assert "User ID" not in body
    assert "2026-10-29" not in body or "UTC date" in body
    assert preview.marker.startswith("VQEXP-")
    assert store.prepare_export(3, report.id, SHA) is None


def test_reject_unexpected_or_malicious_snapshot_fields(store):
    report = report_for(store)
    report.snapshot["error_codes"] = {"TOKEN_VALUE": 1}
    with pytest.raises(ValueError):
        build_issue_body(report, "VQEXP-" + "a" * 32, SHA)
    report.snapshot["error_codes"] = {}
    report.snapshot["failed"] = "secret"
    with pytest.raises(ValueError):
        build_issue_body(report, "VQEXP-" + "a" * 32, SHA)


def test_preview_token_one_time_and_persistent_sent_state(store):
    report = report_for(store)
    preview = store.prepare_export(1, report.id, SHA)
    assert preview
    again = store.prepare_export(1, report.id, SHA)
    assert again.marker == preview.marker
    assert again.token != preview.token
    assert not store.claim_export(1, report.id, preview.token)
    assert not store.claim_export(2, report.id, again.token)
    claimed = store.claim_export(1, report.id, again.token)
    assert claimed and claimed.state == "sending"
    assert store.claim_export(1, report.id, again.token) is None
    assert store.sent_export(report.id, claimed.marker, 17)
    assert not store.sent_export(report.id, claimed.marker, 18)
    assert store.get_export(report.id).issue_number == 17
    assert store.prepare_export(1, report.id, SHA).state == "sent"


def test_expiring_preview_and_explicit_definite_retry(store):
    report = report_for(store)
    start = 1_850_000_000
    preview = store.prepare_export(1, report.id, SHA, now=start)
    assert not store.claim_export(1, report.id, preview.token, now=start + 601)
    new = store.prepare_export(1, report.id, SHA, now=start + 602)
    assert store.claim_export(1, report.id, new.token, now=start + 603)
    assert store.fail_export(report.id, new.marker, "GITHUB_REJECTED", uncertain=False)
    assert store.get_export(report.id).state == "failed"
    retry = store.prepare_export(1, report.id, SHA, now=start + 604)
    assert retry.state == "pending"
    assert retry.token != new.token
    assert retry.marker == new.marker


def test_unknown_network_result_blocks_retry_and_reconciles(tmp_path):
    path = tmp_path / "operations.sqlite3"
    store = OperationsStore(path, owner_id=1)
    try:
        report = report_for(store)
        preview = store.prepare_export(1, report.id, SHA)
        assert store.claim_export(1, report.id, preview.token)
    finally:
        store.close()
    recovered = OperationsStore(path, owner_id=1)
    try:
        assert recovered.get_export(report.id).state == "uncertain"
        assert recovered.prepare_export(1, report.id, SHA).state == "uncertain"
        assert not recovered.claim_export(1, report.id, preview.token)
        assert recovered.sent_export(report.id, preview.marker, 8, reconcile=True)
        assert recovered.get_export(report.id).issue_number == 8
    finally:
        recovered.close()


def test_sqlite_v1_migration_preserves_existing_events(tmp_path):
    path = tmp_path / "operations.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE ops_events (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "occurred_at INTEGER NOT NULL,user_id INTEGER NOT NULL,"
            "section TEXT NOT NULL,operation TEXT NOT NULL,outcome TEXT NOT NULL,"
            "elapsed_ms INTEGER NOT NULL)"
        )
        db.execute(
            "INSERT INTO ops_events"
            "(occurred_at,user_id,section,operation,outcome,elapsed_ms)"
            "VALUES(1,2,'todo','message','handled',100)"
        )
    upgraded = OperationsStore(path, owner_id=1)
    try:
        row = upgraded.db.execute("SELECT * FROM ops_events").fetchone()
        assert row["user_id"] == 2
        assert row["error_code"] == "NONE"
        assert upgraded.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        upgraded.close()


@pytest.fixture
def app_config(tmp_path, monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path / "app.pem"
    path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    path.chmod(0o600)
    config = GitHubAppConfig(app_id=123, installation_id=456, private_key_path=path)
    return config


def test_key_file_rejects_insecure_mode_and_symlink(app_config, tmp_path):
    path = app_config.private_key_path
    assert private_key_bytes(path).startswith(b"-----BEGIN")
    path.chmod(0o644)
    with pytest.raises(ValueError):
        private_key_bytes(path)
    path.chmod(0o600)
    link = tmp_path / "indirect.pem"
    link.symlink_to(path)
    with pytest.raises(OSError):
        private_key_bytes(link)


def test_config_off_by_default_and_fail_closed(app_config, monkeypatch):
    for name in (
        "OPERATIONS_GITHUB_EXPORT_ENABLED", "OPERATIONS_GITHUB_APP_ID",
        "OPERATIONS_GITHUB_INSTALLATION_ID", "OPERATIONS_GITHUB_KEY_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    assert GitHubAppConfig.from_env() is None
    monkeypatch.setenv("OPERATIONS_GITHUB_EXPORT_ENABLED", "true")
    with pytest.raises(ValueError, match="Incomplete"):
        GitHubAppConfig.from_env()
    monkeypatch.setenv("OPERATIONS_GITHUB_APP_ID", "123")
    monkeypatch.setenv("OPERATIONS_GITHUB_INSTALLATION_ID", "456")
    monkeypatch.setenv("OPERATIONS_GITHUB_KEY_FILE", str(app_config.private_key_path))
    assert GitHubAppConfig.from_env() == app_config


@pytest.mark.asyncio
async def test_app_token_limited_to_one_repo_and_issue_creation(app_config):
    seen = []
    def handler(req):
        seen.append(req)
        assert req.url.host == "api.github.com"
        assert req.headers["authorization"].startswith("Bearer ")
        if req.url.path.endswith("/access_tokens"):
            data = json.loads(req.content)
            assert data == {
                "repositories": ["voqelis-ops"], "permissions": {"issues": "write"},
            }
            return httpx.Response(201, json={
                "token": "short-lived-test-token",
                "permissions": {"issues": "write"},
                "repositories": [{"full_name": REPO}],
            })
        assert req.url.path == f"/repos/{REPO}/issues"
        data = json.loads(req.content)
        assert data["body"] == "metadata only"
        assert data["title"].startswith("Voqelis technical diagnostic")
        return httpx.Response(201, json={"number": 41})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await GitHubIssuesClient(app_config, client).create_issue(
            "VQEXP-" + "b" * 32, "metadata only",
        )
    assert result == 41
    assert len(seen) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("code,uncertain", [
    (403, False), (500, True), (429, True),
])
async def test_github_issue_failure_is_classified(app_config, code, uncertain):
    def handler(req):
        if req.url.path.endswith("/access_tokens"):
            return httpx.Response(201, json={
                "token": "test",
                "permissions": {"issues": "write"},
                "repositories": [{"full_name": REPO}],
            })
        return httpx.Response(code)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(GitHubExportFailure) as exc:
            await GitHubIssuesClient(app_config, client).create_issue(
                "VQEXP-" + "b" * 32, "body",
            )
    assert exc.value.uncertain is uncertain


@pytest.mark.asyncio
async def test_network_timeout_is_uncertain(app_config):
    def handler(req):
        if req.url.path.endswith("/access_tokens"):
            return httpx.Response(201, json={
                "token": "test",
                "permissions": {"issues": "write"},
                "repositories": [{"full_name": REPO}],
            })
        raise httpx.ReadTimeout("request body may have been delivered")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(GitHubExportFailure) as exc:
            await GitHubIssuesClient(app_config, client).create_issue(
                "VQEXP-" + "b" * 32, "body",
            )
    assert exc.value.uncertain


@pytest.mark.asyncio
async def test_reconcile_finds_exact_marker_only(app_config):
    marker = "VQEXP-" + "c" * 32
    def handler(req):
        if req.url.path.endswith("/access_tokens"):
            return httpx.Response(201, json={
                "token": "test", "permissions": {"issues": "write"},
                "repositories": [{"full_name": REPO}],
            })
        assert req.url.path == "/search/issues"
        return httpx.Response(200, json={"items": [
            {"number": 8, "repository_url": f"{BASE}/repos/{REPO}",
             "body": f"Export marker: {marker}\nmetadata"},
            {"number": 9, "repository_url": f"{BASE}/repos/other/repo",
             "body": f"Export marker: {marker}\nmetadata"},
        ]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await GitHubIssuesClient(app_config, client).reconcile(marker) == 8


class FakeMessage:
    def __init__(self, user_id=1):
        self.from_user = SimpleNamespace(id=user_id)
        self.chat = SimpleNamespace(type="private")
        self.messages = []

    async def answer(self, text, **kwargs):
        self.messages.append((text, kwargs))


class FakeCallback:
    def __init__(self, user_id, data, message):
        self.from_user = SimpleNamespace(id=user_id)
        self.data = data
        self.message = message
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))


class DummyHTTP:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *args):
        return None


@pytest.mark.asyncio
async def test_only_owner_can_preview_confirm_and_confirm_once(store, tmp_path, monkeypatch):
    access = AccessStore(tmp_path / "access.sqlite3", bootstrap_ids=frozenset({1}))
    nav = NavigationStore(tmp_path / "navigation.sqlite3")
    report = report_for(store)
    mock_config = SimpleNamespace()
    created = []
    class StubGithub:
        def __init__(self, config, client):
            assert config is mock_config
        async def create_issue(self, marker, body):
            created.append(marker)
            assert "password" not in body
            return 55

    monkeypatch.setattr(operations_bot, "make_http_client", DummyHTTP)
    monkeypatch.setattr(operations_bot, "GitHubIssuesClient", StubGithub)
    try:
        router = create_operations_router(
            store=store, access=access, navigation=nav,
            github_config=mock_config, release_sha=SHA,
        )
        callback_handler = router.callback_query.handlers[0].callback
        friend = FakeMessage(2)
        await callback_handler(
            FakeCallback(2, f"ops:preview:{report.id}", friend), object(),
        )
        assert not friend.messages
        owner = FakeMessage()
        await callback_handler(
            FakeCallback(1, f"ops:preview:{report.id}", owner), object(),
        )
        ticket = store.get_export(report.id)
        assert ticket and ticket.state == "pending"
        assert ticket.marker in owner.messages[-1][0]
        send = f"ops:send:{report.id}:{ticket.token}"
        await callback_handler(FakeCallback(1, send, owner), object())
        assert store.get_export(report.id).state == "sent"
        await callback_handler(FakeCallback(1, send, owner), object())
        assert created == [ticket.marker]
    finally:
        nav.close()
        access.close()


@pytest.mark.asyncio
async def test_disabled_export_does_not_dispatch(store, tmp_path):
    access = AccessStore(tmp_path / "access.sqlite3", bootstrap_ids=frozenset({1}))
    nav = NavigationStore(tmp_path / "navigation.sqlite3")
    report = report_for(store)
    try:
        router = create_operations_router(
            store=store, access=access, navigation=nav, github_config=None,
        )
        handler = router.callback_query.handlers[0].callback
        owner = FakeMessage()
        await handler(FakeCallback(1, f"ops:preview:{report.id}", owner), object())
        assert "Экспорт отключён" in owner.messages[-1][0]
        ticket = store.get_export(report.id)
        await handler(FakeCallback(
            1, f"ops:send:{report.id}:{ticket.token}", owner,
        ), object())
        assert store.get_export(report.id).state == "pending"
    finally:
        nav.close()
        access.close()



def test_v1_report_and_tester_permission_survive_upgrade(tmp_path):
    path = tmp_path / "operations.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript("""
        CREATE TABLE ops_config (
            id INTEGER PRIMARY KEY CHECK(id=1), reporter_user_id INTEGER
        );
        CREATE TABLE ops_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            occurred_at INTEGER NOT NULL, user_id INTEGER NOT NULL,
            section TEXT NOT NULL, operation TEXT NOT NULL,
            outcome TEXT NOT NULL, elapsed_ms INTEGER NOT NULL
        );
        CREATE TABLE ops_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL, created_at INTEGER NOT NULL,
            window_minutes INTEGER NOT NULL, description TEXT NOT NULL,
            snapshot TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new'
        );
        """)
        db.execute("INSERT INTO ops_config VALUES(1,2)")
        db.execute(
            "INSERT INTO ops_reports "
            "(id,user_id,created_at,window_minutes,description,snapshot,status) "
            "VALUES(77,2,1793000000,30,?,?,?)",
            ("private local comment", json.dumps({
                "handled": 7, "completed": 1, "failed": 0,
            }), "resolved"),
        )
    upgraded = OperationsStore(path, owner_id=1)
    try:
        assert upgraded.reporter_id() == 2
        report = upgraded.get_report(77)
        assert report is not None
        assert report.id == 77
        assert report.user_id == 2
        assert report.description == "private local comment"
        assert report.status == "resolved"
        export = upgraded.prepare_export(1, 77, SHA)
        assert export is not None
        assert "private local comment" not in export.issue_body
        assert upgraded.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        upgraded.close()


@pytest.mark.asyncio
async def test_wrong_github_repository_or_extra_permissions_fail_closed(app_config):
    calls = []
    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(201, json={
            "token": "scoped-token",
            "permissions": {"issues": "write", "contents": "write"},
            "repositories": [{"full_name": "0w1Max/voqelis"}],
        })
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(GitHubExportFailure) as exc:
            await GitHubIssuesClient(app_config, client).create_issue(
                "VQEXP-" + "d" * 32, "safe body",
            )
    assert exc.value.code == "APP_SCOPE_INVALID"
    assert not exc.value.uncertain
    assert calls == ["/app/installations/456/access_tokens"]
