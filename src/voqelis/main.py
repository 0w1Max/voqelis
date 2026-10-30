from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from contextlib import suppress
from dataclasses import replace
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from .access import AccessStore, create_access_router
from .asr_gguf_test_bot import GgufTestManager, create_asr_gguf_test_router
from .asr_test_bot import AsrBenchmarkManager, create_asr_test_router
from .bot import cleanup_temp_dir, create_router, run_worker
from .config import load_settings
from .day_facts.bot import create_day_facts_router
from .day_facts.service import DayFactsService
from .day_facts.store import DayFactsStore
from .navigation import NavigationStore, create_navigation_router
from .notes.bot import create_notes_router
from .notes.service import NotesService
from .notes.store import NotesStore
from .notifications.bot import create_notification_router
from .notifications.delivery import run_announcement_sender
from .notifications.store import AnnouncementStore
from .operations.bot import create_operations_router
from .operations.github import GitHubAppConfig
from .operations.store import OperationsStore
from .operations.telemetry import OperationsTelemetry
from .planner.ai import (
    AIProviderRouter,
    CloudflarePlannerAI,
    GeminiPlannerAI,
    GroqPlannerAI,
)
from .planner.bot import create_planner_router
from .planner.config import PlannerConfig
from .planner.service import PlannerService
from .planner.store import PlannerStore
from .queue import JobQueue
from .text_punctuation import TextPunctuationProcessor
from .todo.bot import create_todo_router
from .todo.service import TodoService
from .todo.store import TodoStore
from .transcription import Transcriber

logger = logging.getLogger(__name__)


async def async_main() -> None:
    settings = load_settings()
    github_config = GitHubAppConfig.from_env()
    # Environment IDs bootstrap the owner; approvals remain isolated in access.sqlite3.
    # All existing routers use the *same live* membership policy.
    access_store = AccessStore(
        settings.planner_db_path.with_name("access.sqlite3"),
        bootstrap_ids=settings.allowed_user_ids,
    )
    settings = replace(settings, allowed_user_ids=access_store.allowed)
    # Experimental ASR commands may download models and consume substantial
    # CPU/disk. Approved users must never inherit those owner-only capabilities.
    # Fail closed if no unambiguous owner was configured.
    owner_only_settings = replace(
        settings,
        allowed_user_ids=(
            frozenset({access_store.admin_id})
            if access_store.admin_id is not None
            else frozenset()
        ),
    )
    if access_store.admin_id is None:
        logger.warning(
            "Owner approval disabled: ALLOWED_USER_IDS must contain exactly one ID"
        )

    removed = cleanup_temp_dir(settings.temp_dir)
    if removed:
        logger.info("Removed %s stale temporary file(s)", removed)

    transcriber = Transcriber(settings)
    punctuation_processor = TextPunctuationProcessor.from_settings(settings)

    # Fail fast during service startup if the model cannot be loaded. This avoids
    # a bot that appears online but silently accumulates unusable jobs.
    await transcriber.start()

    planner_store = PlannerStore(settings.planner_db_path)
    planner_config = (
        PlannerConfig.from_json_file(settings.planner_config_path)
        if settings.planner_config_path.exists()
        else PlannerConfig()
    )
    asr_test_manager = AsrBenchmarkManager(
        data_dir=settings.planner_db_path.parent / "asr-benchmark-telegram",
        timezone=planner_config.timezone,
    )
    repo_root = Path(__file__).resolve().parents[2]
    try:
        release_sha = (await asyncio.to_thread(
            subprocess.check_output,
            ["git", "-C", str(repo_root), "rev-parse", "--verify", "HEAD"],
            text=True, timeout=2,
        )).strip()
    except (OSError, subprocess.SubprocessError):
        release_sha = "unknown"
    gguf_work_dir = settings.planner_db_path.parent / "asr-benchmark-transcribe-cpp"
    gguf_test_manager = GgufTestManager(
        data_dir=settings.planner_db_path.parent / "asr-gguf-telegram",
        timezone=planner_config.timezone,
        model_path=gguf_work_dir / "gigaam-v3-e2e-ctc-Q4_K_M.gguf",
        python_path=gguf_work_dir / "venv" / "bin" / "python",
        worker_path=repo_root / "tools" / "asr_gguf_transcribe.py",
        repo_root=repo_root,
    )
    groq_ai = (
        GroqPlannerAI(
            settings.groq_api_key,
            model=settings.groq_model,
            timeout_seconds=settings.planner_ai_timeout_seconds,
        )
        if settings.groq_api_key
        else None
    )
    gemini_ai = (
        GeminiPlannerAI(
            settings.gemini_api_key,
            model=settings.gemini_model,
            timeout_seconds=settings.planner_ai_fallback_timeout_seconds,
        )
        if settings.gemini_api_key
        else None
    )
    cloudflare_ai = (
        CloudflarePlannerAI(
            settings.cloudflare_api_token,
            account_id=settings.cloudflare_account_id,
            model=settings.cloudflare_model,
            timeout_seconds=settings.cloudflare_timeout_seconds,
        )
        if settings.cloudflare_api_token and settings.cloudflare_account_id
        else None
    )
    planner_ai = (
        AIProviderRouter(
            primary=groq_ai,
            fallback=gemini_ai,
            tertiary=cloudflare_ai,
        )
        if groq_ai or gemini_ai or cloudflare_ai
        else None
    )
    if planner_ai is not None:
        logger.info(
            "Planner AI configured: primary=%s fallback=%s tertiary=%s",
            getattr(groq_ai, "provider_name", "none") if groq_ai else "none",
            getattr(gemini_ai, "provider_name", "none") if gemini_ai else "none",
            getattr(cloudflare_ai, "provider_name", "none") if cloudflare_ai else "none",
        )
    planner = PlannerService(
        planner_store,
        config=planner_config,
        ai=planner_ai,
        export_dir=settings.temp_dir,
        log_content=settings.planner_log_content,
    )

    # Notes has its own SQLite file; never migrate or write Planner tables.
    notes_store = NotesStore(settings.planner_db_path.with_name("notes.sqlite3"))
    notes = NotesService(notes_store, timezone=planner_config.timezone)

    day_facts_store = DayFactsStore(
        settings.planner_db_path.with_name("day_facts.sqlite3")
    )
    day_facts = DayFactsService(day_facts_store, timezone=planner_config.timezone)
    navigation = NavigationStore(settings.planner_db_path.with_name("navigation.sqlite3"))
    todo_store = TodoStore(settings.planner_db_path.with_name("todo.sqlite3"))
    todo = TodoService(todo_store)
    ops_store = OperationsStore(
        settings.planner_db_path.with_name("operations.sqlite3"),
        owner_id=access_store.admin_id,
    )
    announcement_store = AnnouncementStore(
        settings.planner_db_path.with_name("admin_notifications.sqlite3"),
        owner_id=access_store.admin_id,
    )
    # No user announcements until explicitly enabled after separate rollout.
    announcements_enabled = os.getenv(
        "ADMIN_ANNOUNCEMENTS_ENABLED", "false",
    ).lower() == "true"

    queue = JobQueue(
        max_pending_jobs=settings.max_pending_jobs,
        max_pending_per_user=settings.max_pending_per_user,
    )

    dp = Dispatcher()
    telemetry = OperationsTelemetry(ops_store, navigation, settings.allowed_user_ids)
    dp.message.outer_middleware(telemetry)
    dp.callback_query.outer_middleware(telemetry)
    dp.include_router(create_access_router(
        access_store, navigation=navigation, operations=ops_store,
    ))
    dp.include_router(create_operations_router(
        store=ops_store, access=access_store, navigation=navigation,
        github_config=github_config, release_sha=release_sha,
    ))
    dp.include_router(create_notification_router(
        store=announcement_store, access=access_store, navigation=navigation,
        enabled=announcements_enabled,
    ))
    dp.include_router(create_navigation_router(
        navigation=navigation, allowed_user_ids=settings.allowed_user_ids,
        admin_user_id=access_store.admin_id, operations=ops_store,
    ))
    dp.include_router(
        create_asr_test_router(
            manager=asr_test_manager,
            settings=owner_only_settings,
            other_session_active=gguf_test_manager.has_any_session,
        )
    )
    dp.include_router(
        create_asr_gguf_test_router(
            manager=gguf_test_manager,
            settings=owner_only_settings,
            other_session_active=asr_test_manager.has_any_session,
        )
    )
    # Explicit day facts commands and draft text precede Planner's catch-all.
    dp.include_router(create_day_facts_router(
        service=day_facts, allowed_user_ids=settings.allowed_user_ids,
        planner=planner, notes=notes, export_dir=settings.temp_dir,
        admin_user_id=access_store.admin_id, navigation=navigation,
    ))
    dp.include_router(create_todo_router(
        service=todo, navigation=navigation,
        allowed_user_ids=settings.allowed_user_ids,
    ))
    dp.include_router(create_notes_router(
        service=notes, allowed_user_ids=owner_only_settings.allowed_user_ids,
        planner=planner, day_facts=day_facts, navigation=navigation,
    ))
    dp.include_router(create_planner_router(
        service=planner, allowed_user_ids=owner_only_settings.allowed_user_ids,
        navigation=navigation,
    ))
    dp.include_router(create_router(
        settings=settings, queue=queue, planner=planner,
        notes=notes, day_facts=day_facts, todo=todo, navigation=navigation,
        owner_user_id=access_store.admin_id, operations=ops_store,
    ))

    async with Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=None),
    ) as bot:
        announcement_task = (
            asyncio.create_task(
                run_announcement_sender(bot, announcement_store, access_store),
                name="announcement-sender",
            )
            if announcements_enabled else None
        )
        worker_task = asyncio.create_task(
            run_worker(
                bot=bot,
                queue=queue,
                transcriber=transcriber,
                settings=settings,
                planner=planner,
                log_content=settings.planner_log_content,
                punctuation_processor=punctuation_processor,
                notes=notes,
                day_facts=day_facts, todo=todo, navigation=navigation,
                owner_user_id=access_store.admin_id, operations=ops_store,
            ),
            name="transcription-worker",
        )

        try:
            await dp.start_polling(
                bot,
                allowed_updates=dp.resolve_used_update_types(),
                tasks_concurrency_limit=8,
            )
        finally:
            worker_task.cancel()
            with suppress(asyncio.CancelledError):
                await worker_task
            if announcement_task is not None:
                announcement_task.cancel()
                with suppress(asyncio.CancelledError):
                    await announcement_task
            await transcriber.close()
            planner_store.close()
            notes_store.close()
            day_facts_store.close()
            todo_store.close()
            ops_store.close()
            announcement_store.close()
            access_store.close()
            navigation.close()


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        logger.info("Stopped by user")


if __name__ == "__main__":
    main()
