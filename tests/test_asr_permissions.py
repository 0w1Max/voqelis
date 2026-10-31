"""Only the bootstrap owner may use expensive internal ASR benchmark endpoints."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiogram.enums import ChatType

from voqelis.access import AccessStore
from voqelis.asr_gguf_test_bot import create_asr_gguf_test_router
from voqelis.asr_test_bot import create_asr_test_router


class FakeMessage:
    def __init__(self, user_id: int):
        self.from_user = SimpleNamespace(id=user_id)
        self.chat = SimpleNamespace(type=ChatType.PRIVATE)
        self.replies = []

    async def answer(self, text, **_kwargs):
        self.replies.append(text)


def command_handler(router, handler_name):
    return next(
        item.callback for item in router.message.handlers
        if item.callback.__name__ == handler_name
    )


@pytest.mark.asyncio
async def test_approved_user_still_cannot_start_asr_benchmark(tmp_path):
    store = AccessStore(
        tmp_path / "access.sqlite3", bootstrap_ids=frozenset({1})
    )
    try:
        assert store.request(2, display_name="Test", username="guest") == "created"
        assert store.decide(1, 2, approve=True)
        assert 2 in store.allowed

        owner_settings = SimpleNamespace(allowed_user_ids=frozenset({store.admin_id}))
        manager = SimpleNamespace(
            available=False, has_session=lambda _user_id: False
        )
        handler = command_handler(
            create_asr_test_router(manager=manager, settings=owner_settings),
            "on_asrtest",
        )

        guest = FakeMessage(2)
        await handler(guest)
        assert len(guest.replies) == 1
        assert "только разрешённому пользователю" in guest.replies[0]

        owner = FakeMessage(1)
        await handler(owner)
        assert "Не найден benchmark runner" in owner.replies[0]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_approved_user_still_cannot_start_gguf_experiment(tmp_path):
    store = AccessStore(
        tmp_path / "access.sqlite3", bootstrap_ids=frozenset({1})
    )
    try:
        assert store.request(2, display_name="Test", username="guest") == "created"
        assert store.decide(1, 2, approve=True)

        owner_settings = SimpleNamespace(allowed_user_ids=frozenset({store.admin_id}))
        manager = SimpleNamespace(
            available=False,
            has_session=lambda _user_id: False,
            has_any_session=lambda: False,
        )
        handler = command_handler(
            create_asr_gguf_test_router(
                manager=manager, settings=owner_settings,
                other_session_active=lambda: False,
            ),
            "on_asrgguf",
        )

        guest = FakeMessage(2)
        await handler(guest)
        assert not guest.replies

        owner = FakeMessage(1)
        await handler(owner)
        assert "GGUF-тест недоступен" in owner.replies[0]
    finally:
        store.close()
