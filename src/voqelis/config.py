from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


ALLOWED_MODEL_SIZES = {
    "tiny", "tiny.en", "base", "base.en", "small", "small.en",
    "medium", "medium.en", "large-v1", "large-v2", "large-v3",
    "large-v3-turbo", "turbo",
}


def _parse_bool(value: str, *, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true/false, got {value!r}")


def _parse_user_ids(value: str) -> frozenset[int]:
    result: set[int] = set()
    for raw in value.split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            user_id = int(raw)
        except ValueError as exc:
            raise ValueError(f"ALLOWED_USER_IDS contains invalid Telegram user ID: {raw!r}") from exc
        if user_id <= 0:
            raise ValueError(f"ALLOWED_USER_IDS contains non-positive Telegram user ID: {user_id}")
        result.add(user_id)
    return frozenset(result)


def _positive_int(value: str, *, name: str, minimum: int = 1) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {value!r}") from exc
    if parsed < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {parsed}")
    return parsed


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    allowed_user_ids: frozenset[int]
    model_size: str
    model_device: str
    model_compute_type: str
    cpu_threads: int
    language: str | None
    beam_size: int
    vad_min_silence_ms: int
    condition_on_previous_text: bool
    max_file_size_bytes: int
    max_audio_seconds: int
    max_pending_jobs: int
    max_pending_per_user: int
    download_timeout_seconds: int
    temp_dir: Path
    model_cache_dir: Path
    log_level: str
    planner_db_path: Path
    planner_config_path: Path
    gemini_api_key: str
    gemini_model: str
    planner_ai_timeout_seconds: int


def load_settings(env_file: Path | None = None) -> Settings:
    """Load and validate environment configuration.

    dotenv is intentionally loaded here, not at module import time, so tests and
    future application entry points stay deterministic.
    """
    load_dotenv(dotenv_path=env_file, override=False)

    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        raise ValueError("BOT_TOKEN is required")

    model_size = os.environ.get("MODEL_SIZE", "base").strip().lower()
    if model_size not in ALLOWED_MODEL_SIZES:
        raise ValueError(
            f"MODEL_SIZE must be one of {sorted(ALLOWED_MODEL_SIZES)}, got {model_size!r}"
        )

    device = os.environ.get("MODEL_DEVICE", "cpu").strip().lower()
    if device not in {"cpu", "cuda", "auto"}:
        raise ValueError(f"MODEL_DEVICE must be cpu/cuda/auto, got {device!r}")

    compute_type = os.environ.get("MODEL_COMPUTE_TYPE", "int8").strip().lower()
    cpu_threads = _positive_int(
        os.environ.get("CPU_THREADS", "1"), name="CPU_THREADS", minimum=1
    )
    beam_size = _positive_int(
        os.environ.get("BEAM_SIZE", "5"), name="BEAM_SIZE", minimum=1
    )
    vad_min_silence_ms = _positive_int(
        os.environ.get("VAD_MIN_SILENCE_MS", "500"),
        name="VAD_MIN_SILENCE_MS",
        minimum=100,
    )

    raw_language = os.environ.get("LANGUAGE", "ru").strip().lower()
    language = None if raw_language in {"", "auto"} else raw_language

    max_file_size_mb = _positive_int(
        os.environ.get("MAX_FILE_SIZE_MB", "20"),
        name="MAX_FILE_SIZE_MB",
        minimum=1,
    )
    if max_file_size_mb > 20:
        raise ValueError(
            "MAX_FILE_SIZE_MB cannot exceed 20 because Telegram Bot API download is limited to 20 MB"
        )

    max_audio_minutes = _positive_int(
        os.environ.get("MAX_AUDIO_MINUTES", "20"),
        name="MAX_AUDIO_MINUTES",
        minimum=1,
    )

    max_pending_jobs = _positive_int(
        os.environ.get("MAX_PENDING_JOBS", "4"),
        name="MAX_PENDING_JOBS",
        minimum=1,
    )
    max_pending_per_user = _positive_int(
        os.environ.get("MAX_PENDING_PER_USER", "2"),
        name="MAX_PENDING_PER_USER",
        minimum=1,
    )

    settings = Settings(
        bot_token=token,
        allowed_user_ids=_parse_user_ids(os.environ.get("ALLOWED_USER_IDS", "")),
        model_size=model_size,
        model_device=device,
        model_compute_type=compute_type,
        cpu_threads=cpu_threads,
        language=language,
        beam_size=beam_size,
        vad_min_silence_ms=vad_min_silence_ms,
        condition_on_previous_text=_parse_bool(
            os.environ.get("CONDITION_ON_PREVIOUS_TEXT", "true"),
            name="CONDITION_ON_PREVIOUS_TEXT",
        ),
        max_file_size_bytes=max_file_size_mb * 1024 * 1024,
        max_audio_seconds=max_audio_minutes * 60,
        max_pending_jobs=max_pending_jobs,
        max_pending_per_user=max_pending_per_user,
        download_timeout_seconds=_positive_int(
            os.environ.get("DOWNLOAD_TIMEOUT_SECONDS", "60"),
            name="DOWNLOAD_TIMEOUT_SECONDS",
            minimum=5,
        ),
        temp_dir=Path(os.environ.get("TEMP_DIR", "./data/tmp")).expanduser(),
        model_cache_dir=Path(
            os.environ.get("MODEL_CACHE_DIR", "./data/models")
        ).expanduser(),
        log_level=os.environ.get("LOG_LEVEL", "INFO").strip().upper(),
        planner_db_path=Path(os.environ.get("PLANNER_DB_PATH", "./data/planner.sqlite3")).expanduser(),
        planner_config_path=Path(os.environ.get("PLANNER_CONFIG_PATH", "./config/planner.json")).expanduser(),
        gemini_api_key=os.environ.get("GEMINI_API_KEY", "").strip(),
        gemini_model=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite").strip(),
        planner_ai_timeout_seconds=_positive_int(os.environ.get("PLANNER_AI_TIMEOUT_SECONDS", "30"), name="PLANNER_AI_TIMEOUT_SECONDS", minimum=5),
    )

    settings.temp_dir.mkdir(parents=True, exist_ok=True)
    settings.model_cache_dir.mkdir(parents=True, exist_ok=True)

    if not isinstance(getattr(logging, settings.log_level, None), int):
        raise ValueError(f"Unsupported LOG_LEVEL: {settings.log_level!r}")

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )

    if not settings.allowed_user_ids:
        logging.getLogger(__name__).warning(
            "ALLOWED_USER_IDS is empty: transcription access is disabled for all users. "
            "Use /id in the bot and add the ID to .env."
        )

    return settings
