from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction, ChatType
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from .audio import AudioProcessingError, is_audio_document, probe_duration_seconds
from .config import Settings
from .day_facts.bot import facts_markup
from .day_facts.service import DayFactsService
from .domain import AudioJob
from .menu import main_keyboard
from .notes.service import NotesService
from .planner.bot import planner_markup_for_state
from .planner.service import PlannerService
from .queue import JobQueue
from .text import chunk_text
from .text_punctuation import TextPunctuationProcessor
from .transcription import Transcriber

logger = logging.getLogger(__name__)


def create_router(
    *, settings: Settings, queue: JobQueue, planner: PlannerService | None = None,
    notes: NotesService | None = None,
    day_facts: DayFactsService | None = None,
) -> Router:
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
            "Я расшифрую его локально и верну текст.\n\n"
            "Главный раздел: 📘 Факты дня.\n"
            "История и экспорт доступны кнопками в меню.\n"
            "Планировщик по-прежнему доступен через команды.",
            reply_markup=main_keyboard(),
        )

    @router.message(Command("status"))
    async def on_status(message: Message) -> None:
        if not is_allowed(message):
            return
        snapshot = queue.snapshot()
        asr_label = (
            f"Whisper {settings.model_size}"
            if settings.asr_backend == "whisper"
            else settings.asr_backend
        )
        await message.answer(
            "Статус:\n"
            f"• В очереди: {snapshot.queued}\n"
            f"• Зарезервировано задач: {snapshot.reserved}\n"
            f"• ASR: {asr_label}\n"
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
        if day_facts is not None:
            current_facts = day_facts.store.draft(user_id)
            if current_facts is not None and current_facts.edit_field == "select":
                await message.reply(
                    "✏️ Сначала выбери раздел и конкретный пункт кнопками. "
                    "Голосовое пока не добавлено."
                )
                return
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

            planner_session = planner.store.session(user_id) if planner else None
            facts_draft = (
                day_facts.store.draft(user_id) if day_facts is not None else None
            )
            day_facts_token = (
                facts_draft.token
                if message.voice is not None
                and planner_session is None
                and facts_draft is not None
                else None
            )
            capture_token = (
                notes.claim_voice(user_id)
                if message.voice is not None
                and planner_session is None
                and facts_draft is None
                and notes is not None
                else None
            )
            job = AudioJob(
                chat_id=message.chat.id,
                reply_to_message_id=message.message_id,
                user_id=user_id,
                file_path=raw_path,
                capture_token=capture_token,
                day_facts_token=day_facts_token,
            )
            await queue.put(job)
            if day_facts_token is not None:
                await message.reply("✅ Принял. Добавлю в черновик «Факты дня».")
            elif capture_token is not None:
                await message.reply("✅ Принял. Распознаю и сохраню как заметку.")
            elif planner_session is None:
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
    log_content: bool = False,
    punctuation_processor: TextPunctuationProcessor | None = None,
    notes: NotesService | None = None,
    day_facts: DayFactsService | None = None,
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
            if planner and session and (
                job.day_facts_token is not None or job.capture_token is not None
            ):
                # A note/facts recording accepted before the Planner session
                # appeared must never be converted into Planner tasks.
                await bot.send_message(
                    job.chat_id,
                    "Режим изменился во время распознавания. "
                    "Это сообщение не добавлено ни в Planner, ни в дневник. "
                    "Повтори его в нужном режиме.",
                    reply_to_message_id=job.reply_to_message_id,
                )
                continue
            if planner and session:
                today = datetime.now(planner_tz).date()
                if log_content:
                    logger.info(
                        "PLANNER_INPUT user=%s text=%r state=%s target_day=%s",
                        job.user_id,
                        result.text,
                        session["state"],
                        session["target_day"],
                    )
                replies = await planner.handle_text(job.user_id, result.pause_aware_text or result.text, today)
                if log_content:
                    logger.info("PLANNER_OUTPUT user=%s replies=%r", job.user_id, replies)
                for part in replies:
                    await bot.send_message(
                        job.chat_id,
                        part,
                        reply_to_message_id=job.reply_to_message_id,
                        parse_mode=None,
                        reply_markup=planner_markup_for_state(planner, job.user_id),
                    )
            elif day_facts is not None and job.day_facts_token is not None:
                # Route by the immutable intent captured at queue admission,
                # NOT whichever module happens to be active after ASR.
                display_text = result.pause_aware_text or result.text
                if punctuation_processor is not None:
                    display_text = await punctuation_processor.format(
                        result.text, fallback_text=display_text
                    )
                try:
                    updated = day_facts.accept(
                        job.user_id, job.day_facts_token,
                        display_text, raw=result.text, voice_dictation=True,
                    )
                except ValueError:
                    await bot.send_message(
                        job.chat_id, "Текст слишком длинный. Раздели запись на части.",
                        reply_to_message_id=job.reply_to_message_id,
                    )
                    continue
                if updated is None:
                    await bot.send_message(
                        job.chat_id,
                        "Черновик уже изменился. Это голосовое не сохранено. "
                        "Повтори запись в текущем разделе: /facts.",
                        reply_to_message_id=job.reply_to_message_id,
                    )
                else:
                    parts = chunk_text(day_facts.render(updated))
                    for index, part in enumerate(parts):
                        await bot.send_message(
                            job.chat_id, part, parse_mode=None,
                            reply_to_message_id=(
                                job.reply_to_message_id if index == 0 else None
                            ),
                            reply_markup=(
                                facts_markup(updated) if index == len(parts) - 1 else None
                            ),
                        )
                        if index < len(parts) - 1:
                            await asyncio.sleep(1.05)
            else:
                display_text = result.pause_aware_text or result.text
                if punctuation_processor is not None:
                    display_text = await punctuation_processor.format(
                        result.text, fallback_text=display_text
                    )
                delivery = display_text
                if job.capture_token is not None and notes is not None:
                    try:
                        note = notes.capture_voice(
                            job.user_id, display_text, original=result.text,
                            capture_token=job.capture_token,
                        )
                    except ValueError:
                        delivery = (
                            "⚠️ Голосовая заметка длиннее 12000 символов "
                            "и не сохранена. Пришли более короткую запись "
                            "или используй /note_cancel."
                        )
                    else:
                        if note is not None:
                            delivery = notes.saved_message(note)
                parts = chunk_text(delivery)
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
