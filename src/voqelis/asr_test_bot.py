from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.filters import BaseFilter, Command
from aiogram.types import Message

from .audio import (
    AudioProcessingError,
    convert_audio_to_wav,
    is_audio_document,
    probe_duration_seconds,
)
from .config import Settings

logger = logging.getLogger(__name__)

ASR_TEST_CASES: tuple[tuple[str, str], ...] = (
    (
        "natural_problem",
        "Послезавтра в тринадцать часов сделать тест распознавания послезавтра",
    ),
    ("tomorrow_morning", "Завтра в восемь утра проверить почту"),
    ("today_late_hour", "Сегодня в двадцать три часа подготовиться ко сну"),
    ("spoken_range", "С одиннадцати до тринадцати часов работать над проектом"),
    ("dated_range", "Завтра с девяти до одиннадцати часов написать отчёт"),
    ("period", "Завтра вечером купить продукты"),
    ("after_breakfast", "После завтрака разобрать почту"),
    ("before_lunch", "Перед обедом подготовить отчёт"),
    ("oblique_number", "К двадцати одному закончить работу"),
    (
        "overnight_range",
        "С двадцати трёх вечера до часа ночи подготовиться ко сну",
    ),
    ("spoken_thirteen", "Сегодня в тринадцать часов проверить результаты"),
    ("numeric_thirteen", "Сегодня в 13:00 проверить результаты"),
)

BENCHMARK_MODELS: tuple[str, ...] = (
    "whisper-base-current",
    "gigaam-v3-ctc-int8",
    "gigaam-v3-rnnt-int8",
)
_MODEL_LABELS = {
    "whisper-base-current": "Whisper base",
    "gigaam-v3-ctc-int8": "GigaAM CTC INT8",
    "gigaam-v3-rnnt-int8": "GigaAM RNNT INT8",
}


class AsrTestStateError(RuntimeError):
    """An ASR Telegram test session is not in a state that accepts this operation."""


@dataclass(slots=True)
class AsrTestSession:
    user_id: int
    chat_id: int
    session_id: str
    directory: Path
    started_day: date
    next_case: int = 0
    recorded_days: list[date] = field(default_factory=list)
    state: str = "collecting"

    @property
    def audio_dir(self) -> Path:
        return self.directory / "audio"

    @property
    def results_dir(self) -> Path:
        return self.directory / "results"

    @property
    def manifest_path(self) -> Path:
        return self.directory / "manifest.json"


class AsrBenchmarkManager:
    """Collect Telegram voice notes and compare fixed ASR candidates offline."""

    def __init__(
        self,
        *,
        data_dir: Path,
        timezone: str,
        repo_root: Path | None = None,
        benchmark_python: Path | None = None,
    ) -> None:
        self.data_dir = data_dir
        self.timezone = timezone
        self.repo_root = repo_root or Path(__file__).resolve().parents[2]
        self.benchmark_script = self.repo_root / "asr_benchmark.py"
        self.benchmark_python = benchmark_python or (
            self.repo_root / ".venv-asr-benchmark" / "bin" / "python"
        )
        self._sessions: dict[int, AsrTestSession] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    @property
    def available(self) -> bool:
        return self.benchmark_script.is_file() and self.benchmark_python.is_file()

    def has_session(self, user_id: int) -> bool:
        return user_id in self._sessions

    def get_session(self, user_id: int) -> AsrTestSession | None:
        return self._sessions.get(user_id)

    def begin(
        self, *, user_id: int, chat_id: int, today: date
    ) -> tuple[AsrTestSession, bool]:
        current = self._sessions.get(user_id)
        if current is not None:
            return current, False

        self.data_dir.mkdir(parents=True, exist_ok=True)
        session_id = f"{today.isoformat()}-{uuid.uuid4().hex[:12]}"
        directory = self.data_dir / session_id
        (directory / "audio").mkdir(parents=True, exist_ok=False)
        (directory / "results").mkdir()
        session = AsrTestSession(
            user_id=user_id,
            chat_id=chat_id,
            session_id=session_id,
            directory=directory,
            started_day=today,
        )
        self._sessions[user_id] = session
        self._locks[user_id] = asyncio.Lock()
        return session, True

    def cancel(self, user_id: int) -> str:
        session = self._sessions.get(user_id)
        if session is None:
            return "missing"
        if session.state != "collecting":
            return "running"
        self._sessions.pop(user_id, None)
        self._locks.pop(user_id, None)
        shutil.rmtree(session.directory, ignore_errors=True)
        return "cancelled"

    @staticmethod
    def prompt_text(case_index: int) -> str:
        _, phrase = ASR_TEST_CASES[case_index]
        return (
            f"🎙️ Фраза {case_index + 1}/{len(ASR_TEST_CASES)}\n\n"
            f"Прочитай вслух:\n«{phrase}»\n\n"
            "Отправь её именно голосовым сообщением или аудиофайлом. "
            "Не пиши текст вместо записи."
        )

    async def accept_recording(
        self,
        *,
        user_id: int,
        source_path: Path,
        today: date,
    ) -> tuple[AsrTestSession, int, bool]:
        lock = self._locks.get(user_id)
        try:
            if lock is None:
                raise AsrTestStateError("Тестовая сессия уже завершена.")
            async with lock:
                session = self._sessions.get(user_id)
                if session is None or session.state != "collecting":
                    raise AsrTestStateError("Сессия сейчас не принимает записи.")

                case_index = session.next_case
                destination = session.audio_dir / f"case{case_index + 1:02d}.wav"
                await asyncio.to_thread(convert_audio_to_wav, source_path, destination)
                session.recorded_days.append(today)
                session.next_case += 1
                completed = session.next_case == len(ASR_TEST_CASES)
                if completed:
                    self._write_manifest(session)
                    session.state = "running"
                return session, case_index, completed
        finally:
            source_path.unlink(missing_ok=True)

    @staticmethod
    def _write_manifest(session: AsrTestSession) -> None:
        cases = []
        for index, ((case_id, reference), recorded_day) in enumerate(
            zip(ASR_TEST_CASES, session.recorded_days, strict=True),
            start=1,
        ):
            cases.append(
                {
                    "id": case_id,
                    "audio": f"audio/case{index:02d}.wav",
                    "reference": reference,
                    "today": recorded_day.isoformat(),
                }
            )
        data = {
            "today": session.started_day.isoformat(),
            "cases": cases,
        }
        session.manifest_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    async def _run_model(self, session: AsrTestSession, model_name: str) -> dict:
        output_path = session.results_dir / f"{model_name}.json"
        command = [
            str(self.benchmark_python),
            str(self.benchmark_script),
            "--worker",
            "--manifest",
            str(session.manifest_path),
            "--model",
            model_name,
            "--output",
            str(output_path),
            "--warmup-count",
            "0",
        ]
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(self.repo_root),
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=20 * 60,
            )
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise TimeoutError(f"{model_name} превысила лимит 20 минут.") from exc

        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            if not detail:
                detail = stdout.decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"{model_name}: exit={process.returncode}; {detail[-1200:]}"
            )
        return json.loads(output_path.read_text(encoding="utf-8"))

    async def run_benchmark(self, session: AsrTestSession, bot: Bot) -> None:
        results: dict[str, dict] = {}
        errors: dict[str, str] = {}
        try:
            for index, model_name in enumerate(BENCHMARK_MODELS, start=1):
                await bot.send_message(
                    session.chat_id,
                    f"🔬 Модель {index}/{len(BENCHMARK_MODELS)}: "
                    f"{_MODEL_LABELS[model_name]}. "
                    "Обрабатываю все 12 записей; текущую production-модель не меняю.",
                    parse_mode=None,
                )
                try:
                    results[model_name] = await self._run_model(session, model_name)
                except Exception as exc:
                    logger.exception("ASR benchmark model failed: %s", model_name)
                    errors[model_name] = str(exc)[:1200]

            aggregate = {
                "session_id": session.session_id,
                "started_day": session.started_day.isoformat(),
                "models": results,
                "errors": errors,
            }
            (session.directory / "results.json").write_text(
                json.dumps(aggregate, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            report = _format_report(results, errors)
            for part in _split_report(report):
                await bot.send_message(session.chat_id, part, parse_mode=None)
        except Exception:
            logger.exception("ASR Telegram benchmark failed")
            try:
                await bot.send_message(
                    session.chat_id,
                    "Не удалось завершить ASR-сравнение. "
                    f"Результаты и диагностика сохранены в {session.directory}.",
                    parse_mode=None,
                )
            except Exception:
                logger.exception("Unable to send ASR benchmark failure notice")
        finally:
            shutil.rmtree(session.audio_dir, ignore_errors=True)
            session.manifest_path.unlink(missing_ok=True)
            self._sessions.pop(session.user_id, None)
            self._locks.pop(session.user_id, None)


def _format_metric(value: float | None, *, suffix: str = "") -> str:
    if value is None:
        return "н/д"
    return f"{value:.2f}{suffix}"


def _format_report(results: dict[str, dict], errors: dict[str, str]) -> str:
    if not results:
        detail = "\n".join(f"{_MODEL_LABELS.get(k, k)}: {v}" for k, v in errors.items())
        return "❌ Ни одна модель не завершила benchmark.\n" + detail

    lines = [
        f"✅ ASR-сравнение завершено. Эталонных фраз: {len(ASR_TEST_CASES)}.",
        "Одинаковые Telegram-записи обработаны каждой успешно запущенной моделью.",
        "WER — ошибка текста; нормализованный WER приравнивает словесные числа к цифрам.",
        "Точность фактов означает точное совпадение извлечённых даты, времени, диапазона и отношений.",
        "",
        "СВОДКА",
    ]
    for model_name in BENCHMARK_MODELS:
        result = results.get(model_name)
        if result is None:
            continue
        summary = result["summary"]
        lines.append(
            f"{_MODEL_LABELS[model_name]}: "
            f"WER {_format_metric(summary.get('wer_mean') * 100, suffix='%')}; "
            f"WER(числа) {_format_metric(summary.get('wer_numeric_normalized_mean') * 100, suffix='%')}; "
            f"факты {summary.get('critical_accuracy', 0) * 100:.1f}%; "
            f"RTFx {_format_metric(summary.get('rtfx_mean'))}; "
            f"загрузка {_format_metric(result.get('load_seconds'), suffix=' с')}; "
            f"пик RAM {_format_metric(result.get('peak_rss_mb'), suffix=' MiB')}."
        )
    if errors:
        lines.extend(["", "МОДЕЛИ С ОШИБКОЙ"])
        lines.extend(
            f"{_MODEL_LABELS.get(name, name)}: {detail}"
            for name, detail in errors.items()
        )

    lines.extend(["", "ПО КАЖДОЙ ФРАЗЕ"])
    for index, (_, reference) in enumerate(ASR_TEST_CASES):
        lines.append(f"{index + 1:02d}. Эталон: {reference}")
        for model_name in BENCHMARK_MODELS:
            result = results.get(model_name)
            if result is None or index >= len(result.get("cases", [])):
                continue
            row = result["cases"][index]
            marker = "✓" if row.get("critical_match") else "!"
            lines.append(
                f"  {marker} {_MODEL_LABELS[model_name]} "
                f"(WER {_format_metric(row.get('wer'))}, "
                f"числа {_format_metric(row.get('wer_numeric_normalized'))}): "
                f"{row.get('hypothesis', '')}"
            )
    return "\n".join(lines)


def _split_report(text: str, limit: int = 3500) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in text.splitlines():
        pieces = [line[i : i + limit] for i in range(0, len(line), limit)] or [""]
        for piece in pieces:
            candidate = f"{current}\n{piece}" if current else piece
            if len(candidate) > limit and current:
                chunks.append(current)
                current = piece
            else:
                current = candidate
    if current:
        chunks.append(current)
    return chunks


class _ActiveTestSessionFilter(BaseFilter):
    def __init__(self, manager: AsrBenchmarkManager, settings: Settings) -> None:
        self.manager = manager
        self.allowed_user_ids = settings.allowed_user_ids

    async def __call__(self, message: Message) -> bool:
        return (
            message.chat.type == ChatType.PRIVATE
            and message.from_user is not None
            and message.from_user.id in self.allowed_user_ids
            and self.manager.has_session(message.from_user.id)
        )


def create_asr_test_router(
    *,
    manager: AsrBenchmarkManager,
    settings: Settings,
) -> Router:
    router = Router(name="asr-telegram-benchmark")
    is_active_test_session = _ActiveTestSessionFilter(manager, settings)

    def is_allowed(message: Message) -> bool:
        return (
            message.chat.type == ChatType.PRIVATE
            and message.from_user is not None
            and message.from_user.id in settings.allowed_user_ids
        )

    @router.message(Command("asrtest"))
    async def on_asrtest(message: Message) -> None:
        if not is_allowed(message):
            await message.answer("ASR-тест доступен только разрешённому пользователю в личном чате.")
            return
        if not manager.available:
            await message.answer(
                "Не найден benchmark runner на сервере. "
                "Проверь .venv-asr-benchmark/bin/python и asr_benchmark.py в каталоге Voqelis."
            )
            return
        today = datetime.now(ZoneInfo(manager.timezone)).date()
        try:
            session, created = manager.begin(
                user_id=message.from_user.id,
                chat_id=message.chat.id,
                today=today,
            )
        except OSError:
            logger.exception("Failed to create ASR benchmark session")
            await message.answer("Не удалось создать ASR-тестовую сессию. Подробность есть в журнале.")
            return
        if not created:
            if session.state == "collecting":
                await message.answer(
                    f"Тест уже запущен. Следующая запись: "
                    f"{session.next_case + 1}/{len(ASR_TEST_CASES)}.\n\n"
                    + manager.prompt_text(session.next_case)
                )
            else:
                await message.answer("ASR-сравнение уже выполняется. Дождись итогового отчёта.")
            return
        await message.answer(
            "🧪 Запускаю ASR-тест через Telegram. "
            "Прочитай эталонную фразу и отправь голосовое сообщение. "
            "Для каждой из 12 фраз сохраняется дата записи, чтобы правильно оценивать «сегодня/завтра/послезавтра». "
            "После сбора сравню Whisper base, GigaAM CTC и GigaAM RNNT. "
            "Production-модель не переключается.\n\n"
            + manager.prompt_text(0),
            parse_mode=None,
        )

    @router.message(Command("asrtest_cancel"))
    async def on_asrtest_cancel(message: Message) -> None:
        if not is_allowed(message):
            return
        status = manager.cancel(message.from_user.id)
        if status == "cancelled":
            await message.answer("ASR-тест отменён; несохранённые записи удалены.")
        elif status == "running":
            await message.answer("Сравнение моделей уже запущено; отменить его этой командой нельзя.")
        else:
            await message.answer("Активной ASR-тестовой сессии нет.")

    @router.message((F.voice | F.audio | F.document), is_active_test_session)
    async def on_test_audio(message: Message, bot: Bot) -> None:
        if not is_allowed(message) or message.from_user is None:
            return
        session = manager.get_session(message.from_user.id)
        if session is None:
            return
        if session.state != "collecting":
            await message.reply("Сравнение моделей уже выполняется. Дождись итогового отчёта.")
            return

        document = message.document
        if document is not None and not is_audio_document(
            mime_type=document.mime_type,
            file_name=document.file_name,
        ):
            await message.reply("Для текущего теста пришли голосовое сообщение или аудиофайл.")
            return

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
                f"Файл слишком большой. Максимум: "
                f"{settings.max_file_size_bytes // 1024 // 1024} МБ."
            )
            return
        if known_duration is not None and known_duration > settings.max_audio_seconds:
            await message.reply(
                f"Запись длиннее {settings.max_audio_seconds // 60} минут. "
                "Пришли более короткую фразу."
            )
            return

        raw_path = settings.temp_dir / f"asr-test-{uuid.uuid4().hex}"
        try:
            file_info = await bot.get_file(file_id)
            if not file_info.file_path:
                raise RuntimeError("Telegram не вернул путь к аудиофайлу.")
            remote_size = getattr(file_info, "file_size", None)
            if remote_size is not None and remote_size > settings.max_file_size_bytes:
                await message.reply(
                    f"Файл слишком большой. Максимум: "
                    f"{settings.max_file_size_bytes // 1024 // 1024} МБ."
                )
                return
            await bot.download_file(
                file_info.file_path,
                destination=raw_path,
                timeout=settings.download_timeout_seconds,
            )
            actual_size = raw_path.stat().st_size
            if actual_size <= 0:
                raise RuntimeError("Telegram прислал пустой файл.")
            if actual_size > settings.max_file_size_bytes:
                await message.reply(
                    f"Файл слишком большой. Максимум: "
                    f"{settings.max_file_size_bytes // 1024 // 1024} МБ."
                )
                return
            duration = (
                float(known_duration)
                if known_duration is not None
                else await asyncio.to_thread(probe_duration_seconds, raw_path)
            )
            if duration > settings.max_audio_seconds:
                await message.reply(
                    f"Запись длиннее {settings.max_audio_seconds // 60} минут. "
                    "Пришли более короткую фразу."
                )
                return

            today = datetime.now(ZoneInfo(manager.timezone)).date()
            session, accepted_index, completed = await manager.accept_recording(
                user_id=message.from_user.id,
                source_path=raw_path,
                today=today,
            )
            raw_path = None
            if completed:
                await message.answer(
                    "✅ Получены все 12 записей. Запускаю модели по очереди и буду присылать статус.",
                    parse_mode=None,
                )
                await manager.run_benchmark(session, bot)
            else:
                await message.answer(
                    f"✅ Запись {accepted_index + 1}/{len(ASR_TEST_CASES)} принята.\n\n"
                    + manager.prompt_text(session.next_case),
                    parse_mode=None,
                )
        except AsrTestStateError as exc:
            await message.reply(str(exc))
        except AudioProcessingError as exc:
            logger.warning("Invalid audio in ASR benchmark: %s", exc)
            await message.reply(
                "Не получилось декодировать запись. Попробуй отправить голосовое сообщение ещё раз."
            )
        except (TelegramNetworkError, TelegramBadRequest) as exc:
            logger.warning("Telegram download failed for ASR benchmark: %s", exc)
            await message.reply("Не удалось скачать запись из Telegram. Отправь её ещё раз.")
        except TelegramRetryAfter as exc:
            await message.reply(
                f"Telegram временно ограничил запросы. Повтори через {exc.retry_after} сек."
            )
        except Exception:
            logger.exception("Failed to accept ASR benchmark recording")
            await message.reply("Не удалось принять запись. Подробность записана в журнал.")
        finally:
            if raw_path is not None:
                raw_path.unlink(missing_ok=True)

    @router.message(F.text, is_active_test_session)
    async def on_test_text(message: Message) -> None:
        if not is_allowed(message) or message.from_user is None:
            return
        session = manager.get_session(message.from_user.id)
        if session is None:
            return
        if session.state == "collecting":
            await message.reply(
                "Сейчас я жду голосовую запись, а не текст.\n\n"
                + manager.prompt_text(session.next_case),
                parse_mode=None,
            )
        else:
            await message.reply(
                "Сравнение ASR уже идёт. Дождись отчёта или используй /asrtest_cancel "
                "до начала сравнения в следующий раз.",
                parse_mode=None,
            )

    return router
