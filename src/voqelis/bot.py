from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
from contextlib import suppress
from pathlib import Path

from aiogram import Bot, Router, F
from aiogram.enums import ChatAction, ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from .audio import AudioProcessingError, is_audio_document, probe_duration_seconds
from .config import Settings
from .domain import AudioJob
from .queue import JobQueue
from .text import chunk_text
from .planner.service import PlannerService
from .planner.bot import planner_markup_for_state
from .transcription import Transcriber


logger = logging.getLogger(__name__)


def create_router(*, settings: Settings, queue: JobQueue) -> Router:
    router = Router(name="audio")

    def is_allowed(message: Message) -> bool:
        return (
            message.chat.type == ChatType.PRIVATE
            and message.from_user is not None
            and message.from_user.id in settings.allowed_user_ids
        )

    @router.message(Command("id"))
    async def on_id(message: Message) -> None:
        if message.from_user is None:
            return
        await message.answer(
            "Твой Telegram ID:\n"
            f"{message.from_user.id}\n\n"
            "Добавь его в ALLOWED_USER_IDS в .env, затем перезапусти сервис."
        )

    @router.message(CommandStart())
    async def on_start(message: Message) -> None:
        if not is_allowed(message):
            await message.answer(
                "Доступ к транскрибации закрыт.\n"
                "Сначала используй /id и добавь свой Telegram ID в ALLOWED_USER_IDS."
            )
            return

        await message.answer(
            "🎙️ Пришли голосовое сообщение или аудиофайл.\n"
            "Я расшифрую его локально и верну текст."
        )

    @router.message(Command("status"))
    async def on_status(message: Message) -> None:
        if not is_allowed(message):
            return
        snapshot = queue.snapshot()
        await message.answer(
            "Статус:\n"
            f"• В очереди: {snapshot.queued}\n"
            f"• Зарезервировано задач: {snapshot.reserved}\n"
            f"• Модель: {settings.model_size}\n"
            f"• Лимит записи: {settings.max_audio_seconds // 60} мин\n"
            f"• Лимит файла: {settings.max_file_size_bytes // 1024 // 1024} МБ"
        )

    @router.message(F.voice | F.audio | F.document)
    async def on_audio(message: Message, bot: Bot) -> None:
        if not is_allowed(message):
            if message.chat.type == ChatType.PRIVATE:
                await message.reply("Доступ к транскрибации закрыт. Используй /id для настройки.")
            return

        document = message.document
        if document is not None and not is_audio_document(
            mime_type=document.mime_type,
            file_name=document.file_name,
        ):
            await message.reply("Пришли голосовое сообщение или аудиофайл.")
            return

        file_id: str
        known_duration: int | None
        declared_size: int | None

        if message.voice is not None:
            file_id = message.voice.file_id
            known_duration = message.voice.duration
            declared_size = message.voice.file_size
        elif message.audio is not None:
            file_id = message.audio.file_id
            known_duration = message.audio.duration
            declared_size = message.audio.file_size
        elif document is not None:
            file_id = document.file_id
            known_duration = None
            declared_size = document.file_size
        else:
            return

        if declared_size is not None and declared_size > settings.max_file_size_bytes:
            await message.reply(
                f"Файл слишком большой. Максимум для этого бота: "
                f"{settings.max_file_size_bytes // 1024 // 1024} МБ."
            )
            return

        if known_duration is not None and known_duration > settings.max_audio_seconds:
            await message.reply(
                f"Запись длиннее {settings.max_audio_seconds // 60} мин. "
                "Пришли более короткий фрагмент."
            )
            return

        user_id = message.from_user.id
        if not await queue.reserve(user_id):
            snapshot = queue.snapshot()
            await message.reply(
                "Сейчас слишком много задач. "
                f"Попробуй чуть позже (занято: {snapshot.reserved})."
            )
            return

        raw_path = settings.temp_dir / uuid.uuid4().hex
        try:
            file_info = await bot.get_file(file_id)
            remote_size = getattr(file_info, "file_size", None)
            if remote_size is not None and remote_size > settings.max_file_size_bytes:
                raise _UserInputError(
                    f"Файл слишком большой. Максимум: "
                    f"{settings.max_file_size_bytes // 1024 // 1024} МБ."
                )
            if not file_info.file_path:
                raise _UserInputError("Telegram не вернул путь к файлу.")

            await bot.download_file(
                file_info.file_path,
                destination=raw_path,
                timeout=settings.download_timeout_seconds,
            )

            actual_size = raw_path.stat().st_size
            if actual_size <= 0:
                raise _UserInputError("Telegram прислал пустой файл.")
            if actual_size > settings.max_file_size_bytes:
                raise _UserInputError(
                    f"Файл слишком большой. Максимум: "
                    f"{settings.max_file_size_bytes // 1024 // 1024} МБ."
                )

            job = AudioJob(
                chat_id=message.chat.id,
                reply_to_message_id=message.message_id,
                user_id=user_id,
                file_path=raw_path,
            )
            await queue.put(job)
            await message.reply("✅ Принял. Распознаю по очереди.")
        except _UserInputError as exc:
            raw_path.unlink(missing_ok=True)
            await queue.release(user_id)
            await message.reply(str(exc))
        except (TelegramNetworkError, TelegramBadRequest) as exc:
            raw_path.unlink(missing_ok=True)
            await queue.release(user_id)
            logger.warning("Telegram file handling failed: %s", exc)
            await message.reply("Не удалось получить аудиофайл из Telegram. Попробуй отправить его ещё раз.")
        except TelegramRetryAfter as exc:
            raw_path.unlink(missing_ok=True)
            await queue.release(user_id)
            await message.reply(f"Telegram временно ограничил запросы. Повтори через {exc.retry_after} сек.")
        except Exception:
            raw_path.unlink(missing_ok=True)
            await queue.release(user_id)
            logger.exception("Failed to accept incoming audio")
            await message.reply("Не удалось принять файл. Подробность записана в журнал.")

    return router


class _UserInputError(Exception):
    pass


async def run_worker(
    *,
    bot: Bot,
    queue: JobQueue,
    transcriber: Transcriber,
    settings: Settings,
    planner: PlannerService | None = None,
) -> None:
    planner_tz = ZoneInfo(planner.config.timezone) if planner else None

    while True:
        job = await queue.get()
        try:
            await bot.send_chat_action(job.chat_id, ChatAction.TYPING)

            duration = await asyncio.to_thread(probe_duration_seconds, job.file_path)
            if duration > settings.max_audio_seconds:
                await bot.send_message(
                    job.chat_id,
                    f"Аудио длиннее {settings.max_audio_seconds // 60} мин — пропускаю.",
                    reply_to_message_id=job.reply_to_message_id,
                )
                continue

            result = await transcriber.transcribe(str(job.file_path))
            if not result.text:
                await bot.send_message(
                    job.chat_id,
                    "Не удалось распознать речь: файл пустой, без речи или слишком тихий.",
                    reply_to_message_id=job.reply_to_message_id,
                )
                continue

            session = planner.store.session(job.user_id) if planner else None
            if planner and session:
                today = datetime.now(planner_tz).date()
                replies = await planner.handle_text(job.user_id, result.text, today)
                if replies:
                    for part in replies:
                        await bot.send_message(
                            job.chat_id,
                            part,
                            reply_to_message_id=job.reply_to_message_id,
                            parse_mode=None,
                            reply_markup=planner_markup_for_state(planner, job.user_id),
                        )
                else:
                    await bot.send_message(
                        job.chat_id,
                        result.text,
                        reply_to_message_id=job.reply_to_message_id,
                        parse_mode=None,
                    )
            else:
                parts = chunk_text(result.text)
                for index, part in enumerate(parts):
                    await bot.send_message(
                        job.chat_id,
                        part,
                        reply_to_message_id=job.reply_to_message_id if index == 0 else None,
                        parse_mode=None,
                    )
                    if index < len(parts) - 1:
                        await asyncio.sleep(1.05)

            logger.info(
                "Transcribed user=%s duration=%.1fs processing=%.1fs language=%s prob=%.3f",
                job.user_id,
                result.duration_seconds,
                result.processing_seconds,
                result.language,
                result.language_probability,
            )
        except AudioProcessingError as exc:
            logger.warning("Audio probe/decoding failed for user=%s: %s", job.user_id, exc)
            await _safe_error_message(
                bot,
                job.chat_id,
                job.reply_to_message_id,
                "Не получилось прочитать аудиофайл. Возможно, он повреждён или формат не поддерживается.",
            )
        except (TelegramNetworkError, TelegramBadRequest) as exc:
            logger.warning("Telegram response failed for user=%s: %s", job.user_id, exc)
        except TelegramRetryAfter as exc:
            logger.warning("Telegram retry-after for user=%s: %ss", job.user_id, exc.retry_after)
            with suppress(Exception):
                await asyncio.sleep(float(exc.retry_after))
        except Exception:
            logger.exception("Unexpected worker failure for user=%s", job.user_id)
            await _safe_error_message(
                bot,
                job.chat_id,
                job.reply_to_message_id,
                "Внутренняя ошибка при обработке. Подробность записана в журнал.",
            )
        finally:
            job.file_path.unlink(missing_ok=True)
            await queue.release(job.user_id)
            queue.task_done()


async def _safe_error_message(
    bot: Bot,
    chat_id: int,
    reply_to_message_id: int,
    text: str,
) -> None:
    with suppress(Exception):
        await bot.send_message(
            chat_id,
            text,
            reply_to_message_id=reply_to_message_id,
            parse_mode=None,
        )


def cleanup_temp_dir(temp_dir: Path) -> int:
    """Remove stale files left by a previous crash/reboot."""
    removed = 0
    if not temp_dir.exists():
        temp_dir.mkdir(parents=True, exist_ok=True)
        return 0

    for path in temp_dir.iterdir():
        if path.is_file() or path.is_symlink():
            with suppress(OSError):
                path.unlink()
                removed += 1
    return removed
