from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import BaseFilter, Command
from aiogram.types import Message

from .audio import convert_audio_to_wav, probe_duration_seconds
from .config import Settings

logger = logging.getLogger(__name__)

MAX_GGUF_AUDIO_SECONDS = 25
MAX_REFERENCE_CHARS = 2000
INFERENCE_TIMEOUT_SECONDS = 120


@dataclass(slots=True)
class GgufTestSession:
    user_id: int
    chat_id: int
    session_id: str
    directory: Path
    created_day: date
    state: str = "awaiting_audio"
    metadata: dict[str, object] = field(default_factory=dict)


class GgufTestManager:
    """Manage isolated Telegram recordings for the experimental GGUF model."""

    def __init__(
        self,
        *,
        data_dir: Path,
        timezone: str,
        model_path: Path,
        python_path: Path,
        worker_path: Path,
        repo_root: Path,
    ) -> None:
        self.data_dir = data_dir
        self.timezone = timezone
        self.model_path = model_path
        self.python_path = python_path
        self.worker_path = worker_path
        self.repo_root = repo_root
        self._sessions: dict[int, GgufTestSession] = {}

    @property
    def available(self) -> bool:
        return (
            self.model_path.is_file()
            and self.python_path.is_file()
            and os.access(self.python_path, os.X_OK)
            and self.worker_path.is_file()
        )

    def has_session(self, user_id: int) -> bool:
        return user_id in self._sessions

    def has_any_session(self) -> bool:
        return bool(self._sessions)

    def get_session(self, user_id: int) -> GgufTestSession | None:
        return self._sessions.get(user_id)

    def begin(
        self, *, user_id: int, chat_id: int, today: date
    ) -> tuple[GgufTestSession, bool]:
        current = self._sessions.get(user_id)
        if current is not None:
            return current, False

        self.data_dir.mkdir(parents=True, exist_ok=True)
        session_id = f"{today.isoformat()}-{uuid.uuid4().hex[:12]}"
        directory = self.data_dir / session_id
        directory.mkdir(parents=True, exist_ok=False)

        session = GgufTestSession(
            user_id=user_id,
            chat_id=chat_id,
            session_id=session_id,
            directory=directory,
            created_day=today,
        )
        self._sessions[user_id] = session
        self.persist(session)
        return session, True

    def persist(self, session: GgufTestSession) -> None:
        payload: dict[str, object] = {
            "session_id": session.session_id,
            "user_id": session.user_id,
            "chat_id": session.chat_id,
            "created_day": session.created_day.isoformat(),
            "state": session.state,
            "model": self.model_path.name,
            **session.metadata,
        }
        temporary = session.directory / "session.json.tmp"
        destination = session.directory / "session.json"
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, destination)

    def update(
        self,
        session: GgufTestSession,
        state: str,
        **metadata: object,
    ) -> None:
        session.state = state
        session.metadata.update(metadata)
        self.persist(session)

    def close_session(
        self,
        user_id: int,
        state: str,
        **metadata: object,
    ) -> tuple[str, GgufTestSession | None]:
        session = self._sessions.get(user_id)
        if session is None:
            return "missing", None
        if session.state == "processing":
            return "running", session

        self.update(session, state, **metadata)
        self._sessions.pop(user_id, None)
        return "closed", session

    async def run_inference(self, audio_path: Path, output_path: Path) -> dict:
        command = [
            str(self.python_path),
            str(self.worker_path),
            "--model",
            str(self.model_path),
            "--audio",
            str(audio_path),
            "--output",
            str(output_path),
        ]
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.data_dir.parent),
            "PYTHONUNBUFFERED": "1",
            "TRANSCRIBE_BACKEND": "cpu",
        }
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(self.repo_root),
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=INFERENCE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            process.kill()
            stdout, stderr = await process.communicate()
            self._save_runtime_log(audio_path.parent, stdout, stderr)
            raise TimeoutError(
                f"GGUF inference exceeded {INFERENCE_TIMEOUT_SECONDS} seconds."
            ) from exc

        self._save_runtime_log(audio_path.parent, stdout, stderr)
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            if not detail:
                detail = stdout.decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"GGUF worker exit={process.returncode}: {detail[-1200:]}"
            )

        if not output_path.is_file():
            raise RuntimeError("GGUF worker did not create its result JSON.")

        result = json.loads(output_path.read_text(encoding="utf-8"))
        if not isinstance(result, dict) or not isinstance(result.get("text"), str):
            raise RuntimeError("GGUF worker returned an invalid result.")  # noqa: TRY004 - worker protocol error
        return result

    @staticmethod
    def _save_runtime_log(directory: Path, stdout: bytes, stderr: bytes) -> None:
        combined = (
            "[stdout]\n"
            + stdout.decode("utf-8", errors="replace")
            + "\n[stderr]\n"
            + stderr.decode("utf-8", errors="replace")
        )
        (directory / "runtime.log").write_text(
            combined[-16000:],
            encoding="utf-8",
        )


class _ActiveGgufSessionFilter(BaseFilter):
    def __init__(self, manager: GgufTestManager, settings: Settings) -> None:
        self.manager = manager
        self.allowed_user_ids = settings.allowed_user_ids

    async def __call__(self, message: Message) -> bool:
        return (
            message.chat.type == ChatType.PRIVATE
            and message.from_user is not None
            and message.from_user.id in self.allowed_user_ids
            and self.manager.has_session(message.from_user.id)
        )


def create_asr_gguf_test_router(
    *,
    manager: GgufTestManager,
    settings: Settings,
    other_session_active: Callable[[], bool],
) -> Router:
    router = Router(name="asr-gguf-telegram-test")
    active_session = _ActiveGgufSessionFilter(manager, settings)

    def is_allowed(message: Message) -> bool:
        return (
            message.chat.type == ChatType.PRIVATE
            and message.from_user is not None
            and message.from_user.id in settings.allowed_user_ids
        )

    @router.message(Command("asrgguf"))
    async def on_asrgguf(message: Message) -> None:
        if not is_allowed(message) or message.from_user is None:
            return
        user_id = message.from_user.id

        if other_session_active():
            await message.answer(
                "Сначала заверши или отмени /asrtest, затем запусти /asrgguf."
            )
            return
        if manager.has_any_session() and not manager.has_session(user_id):
            await message.answer(
                "Другой GGUF-тест уже активен. Дождись его завершения."
            )
            return
        if not manager.available:
            await message.answer(
                "GGUF-тест недоступен: проверь файл модели, отдельный Python "
                "runtime и tools/asr_gguf_transcribe.py."
            )
            return

        today = datetime.now(ZoneInfo(manager.timezone)).date()
        try:
            session, created = manager.begin(
                user_id=user_id,
                chat_id=message.chat.id,
                today=today,
            )
        except OSError:
            logger.exception("Failed to create GGUF test session")
            await message.answer("Не удалось создать GGUF-тестовую сессию.")
            return

        if not created:
            if session.state == "awaiting_audio":
                response = "Сессия уже запущена. Отправь одно голосовое до 25 секунд."
            elif session.state == "awaiting_reference":
                response = (
                    "Распознавание уже выполнено. Пришли эталонный текст "
                    "или используй /asrgguf_done."
                )
            else:
                response = (
                    f"Сессия сейчас в состоянии {session.state}. "
                    "Используй /asrgguf_done, чтобы закрыть её."
                )
            await message.answer(response)
            return

        await message.answer(
            "🧪 Экспериментальный тест GigaAM-v3 E2E CTC GGUF.\n\n"
            "Отправь одно голосовое сообщение длительностью до 25 секунд. "
            "Модель запускается отдельным Python-процессом. "
            "Запись не передаётся планировщику и не создаёт задач.\n\n"
            "После распознавания пришли эталонный текст следующим сообщением "
            "или используй /asrgguf_done. Аудио и результаты будут сохранены "
            f"в отдельном каталоге: {session.directory}",
            parse_mode=None,
        )

    @router.message(Command("asrgguf_cancel"))
    async def on_asrgguf_cancel(message: Message) -> None:
        if not is_allowed(message) or message.from_user is None:
            return
        status, session = manager.close_session(
            message.from_user.id,
            "cancelled",
        )
        if status == "closed" and session is not None:
            await message.answer(
                "GGUF-сессия закрыта. Уже сохранённые файлы оставлены в:\n"
                f"{session.directory}"
            )
        elif status == "running":
            await message.answer(
                "Распознавание уже выполняется; дождись результата."
            )
        else:
            await message.answer("Активной GGUF-сессии нет.")

    @router.message(Command("asrgguf_done"))
    async def on_asrgguf_done(message: Message) -> None:
        if not is_allowed(message) or message.from_user is None:
            return
        status, session = manager.close_session(
            message.from_user.id,
            "finished",
        )
        if status == "closed" and session is not None:
            await message.answer(
                "GGUF-сессия закрыта. Файлы сохранены в:\n"
                f"{session.directory}"
            )
        elif status == "running":
            await message.answer(
                "Распознавание ещё выполняется. Повтори команду после результата."
            )
        else:
            await message.answer("Активной GGUF-сессии нет.")

    @router.message(F.voice, active_session)
    async def on_gguf_voice(message: Message, bot: Bot) -> None:
        if (
            not is_allowed(message)
            or message.from_user is None
            or message.voice is None
        ):
            return

        session = manager.get_session(message.from_user.id)
        if session is None:
            return
        if session.state != "awaiting_audio":
            if session.state == "awaiting_reference":
                response = (
                    "Аудио уже распознано. Пришли эталонный текст "
                    "или используй /asrgguf_done."
                )
            else:
                response = (
                    "Для этой сессии нельзя принять ещё одно голосовое. "
                    "Закрой её командой /asrgguf_done."
                )
            await message.reply(response)
            return

        voice = message.voice
        max_duration = min(MAX_GGUF_AUDIO_SECONDS, settings.max_audio_seconds)
        if voice.duration > max_duration:
            await message.reply(
                f"Запись слишком длинная. Лимит GGUF-теста: {max_duration} секунд."
            )
            return
        if (
            voice.file_size is not None
            and voice.file_size > settings.max_file_size_bytes
        ):
            await message.reply("Файл превышает настроенный лимит размера.")
            return

        manager.update(session, "processing")
        partial_path = session.directory / "original.ogg.part"
        source_path = session.directory / "original.ogg"
        audio_path = session.directory / "audio.wav"
        output_path = session.directory / "transcription.json"

        try:
            file_info = await bot.get_file(voice.file_id)
            if not file_info.file_path:
                raise RuntimeError("Telegram did not return an audio file path.")
            remote_size = getattr(file_info, "file_size", None)
            if remote_size is not None and remote_size > settings.max_file_size_bytes:
                manager.update(session, "awaiting_audio")
                await message.reply("Файл превышает настроенный лимит размера.")
                return

            await bot.download_file(
                file_info.file_path,
                destination=partial_path,
                timeout=settings.download_timeout_seconds,
            )

            actual_size = partial_path.stat().st_size
            if actual_size <= 0 or actual_size > settings.max_file_size_bytes:
                manager.update(session, "awaiting_audio")
                await message.reply("Получен пустой файл или превышен лимит размера.")
                return

            # Telegram duration is rounded; inspect the downloaded audio.
            duration = await asyncio.to_thread(
                probe_duration_seconds,
                partial_path,
            )
            if duration > max_duration:
                manager.update(session, "awaiting_audio")
                await message.reply(
                    f"Запись длиннее лимита {max_duration} секунд."
                )
                return

            os.replace(partial_path, source_path)
            await asyncio.to_thread(
                convert_audio_to_wav,
                source_path,
                audio_path,
            )
            result = await manager.run_inference(audio_path, output_path)
            transcript = result["text"].strip()

            manager.update(
                session,
                "awaiting_reference",
                audio_seconds=result.get("audio_seconds", duration),
                load_seconds=result.get("load_seconds"),
                inference_seconds=result.get("inference_seconds"),
                transcript=transcript,
                result_file=output_path.name,
                audio_file=audio_path.name,
                source_file=source_path.name,
            )

            await message.answer(
                "✅ GGUF-распознавание завершено.\n\n"
                f"Результат:\n{transcript or '(пустой текст)'}\n\n"
                f"Загрузка модели: {result.get('load_seconds', 'н/д')} с\n"
                f"Распознавание: {result.get('inference_seconds', 'н/д')} с\n"
                f"Аудио и результат: {session.directory}\n\n"
                "Теперь пришли эталонный текст следующим сообщением "
                "или используй /asrgguf_done.",
                parse_mode=None,
            )
        except Exception as exc:
            logger.exception("GGUF Telegram inference failed")
            if session.state == "processing":
                manager.update(
                    session,
                    "failed",
                    error_type=type(exc).__name__,
                    error=str(exc)[:1200],
                )
            await message.answer(
                "❌ GGUF-тест завершился ошибкой. Текущая запись и диагностика, "
                "если они были созданы, сохранены в:\n"
                f"{session.directory}\n\n"
                "Закрой сессию командой /asrgguf_done и попробуй снова."
            )
        finally:
            partial_path.unlink(missing_ok=True)

    @router.message(F.text, active_session)
    async def on_gguf_text(message: Message) -> None:
        if (
            not is_allowed(message)
            or message.from_user is None
            or message.text is None
        ):
            return

        session = manager.get_session(message.from_user.id)
        if session is None:
            return

        if session.state == "awaiting_audio":
            await message.reply("Сейчас ожидается голосовое сообщение до 25 секунд.")
            return
        if session.state == "processing":
            await message.reply("Распознавание выполняется. Дождись результата.")
            return
        if session.state == "failed":
            await message.reply(
                "Тест завершился ошибкой. Диагностика сохранена. "
                "Используй /asrgguf_done, чтобы закрыть сессию."
            )
            return
        if session.state != "awaiting_reference":
            await message.reply(
                f"Сессия сейчас в состоянии {session.state}. "
                "Используй /asrgguf_done."
            )
            return

        reference = message.text.strip()
        if not reference:
            await message.reply("Эталонный текст пуст. Пришли его обычным текстовым сообщением.")
            return
        if len(reference) > MAX_REFERENCE_CHARS:
            await message.reply(
                f"Эталонный текст слишком длинный: максимум {MAX_REFERENCE_CHARS} символов."
            )
            return

        (session.directory / "reference.txt").write_text(
            reference + "\n",
            encoding="utf-8",
        )
        status, closed = manager.close_session(
            message.from_user.id,
            "complete",
            reference_chars=len(reference),
            reference_file="reference.txt",
        )
        if status == "closed" and closed is not None:
            await message.answer(
                "✅ Эталон сохранён. Сравнивать можно по файлам:\n"
                f"{closed.directory}\n\n"
                "Для следующей записи запусти /asrgguf.",
                parse_mode=None,
            )
        else:
            await message.answer("Не удалось закрыть сессию. Проверь её состояние.")

    return router
