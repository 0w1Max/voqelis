from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from .bot import cleanup_temp_dir, create_router, run_worker
from .config import load_settings
from .planner.ai import GeminiPlannerAI
from .planner.bot import create_planner_router
from .planner.config import PlannerConfig
from .planner.service import PlannerService
from .planner.store import PlannerStore
from .queue import JobQueue
from .transcription import Transcriber

logger = logging.getLogger(__name__)


async def async_main() -> None:
    settings = load_settings()

    removed = cleanup_temp_dir(settings.temp_dir)
    if removed:
        logger.info("Removed %s stale temporary file(s)", removed)

    transcriber = Transcriber(settings)

    # Fail fast during service startup if the model cannot be loaded. This avoids
    # a bot that appears online but silently accumulates unusable jobs.
    await transcriber.start()

    planner_store = PlannerStore(settings.planner_db_path)
    planner_config = (
        PlannerConfig.from_json_file(settings.planner_config_path)
        if settings.planner_config_path.exists()
        else PlannerConfig()
    )
    planner_ai = (
        GeminiPlannerAI(
            settings.gemini_api_key,
            model=settings.gemini_model,
            timeout_seconds=settings.planner_ai_timeout_seconds,
        )
        if settings.gemini_api_key
        else None
    )
    planner = PlannerService(
        planner_store,
        config=planner_config,
        ai=planner_ai,
        export_dir=settings.temp_dir,
        log_content=settings.planner_log_content,
    )

    queue = JobQueue(
        max_pending_jobs=settings.max_pending_jobs,
        max_pending_per_user=settings.max_pending_per_user,
    )

    dp = Dispatcher()
    dp.include_router(create_planner_router(service=planner, allowed_user_ids=settings.allowed_user_ids))
    dp.include_router(create_router(settings=settings, queue=queue, planner=planner))

    async with Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=None),
    ) as bot:
        worker_task = asyncio.create_task(
            run_worker(
                bot=bot,
                queue=queue,
                transcriber=transcriber,
                settings=settings,
                planner=planner,
                log_content=settings.planner_log_content,
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
            planner_store.close()


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        logger.info("Stopped by user")


if __name__ == "__main__":
    main()
