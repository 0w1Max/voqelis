from __future__ import annotations

import asyncio
import logging
import time

from faster_whisper import WhisperModel

from .config import Settings
from .domain import TranscriptionResult


logger = logging.getLogger(__name__)


class Transcriber:
    """Owns exactly one Whisper model instance and serializes inference."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._model: WhisperModel | None = None
        self._load_lock = asyncio.Lock()

    def _load_sync(self) -> WhisperModel:
        logger.info(
            "Loading faster-whisper model: size=%s device=%s compute=%s threads=%s",
            self.settings.model_size,
            self.settings.model_device,
            self.settings.model_compute_type,
            self.settings.cpu_threads,
        )
        model = WhisperModel(
            self.settings.model_size,
            device=self.settings.model_device,
            compute_type=self.settings.model_compute_type,
            cpu_threads=self.settings.cpu_threads,
            num_workers=1,
            download_root=str(self.settings.model_cache_dir),
        )
        logger.info("Whisper model is ready")
        return model

    async def start(self) -> None:
        if self._model is not None:
            return

        async with self._load_lock:
            if self._model is None:
                self._model = await asyncio.to_thread(self._load_sync)

    def _transcribe_sync(self, audio_path: str) -> TranscriptionResult:
        assert self._model is not None

        started = time.monotonic()
        segments, info = self._model.transcribe(
            audio_path,
            language=self.settings.language,
            beam_size=self.settings.beam_size,
            temperature=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
            condition_on_previous_text=self.settings.condition_on_previous_text,
            vad_filter=True,
            vad_parameters={
                "min_silence_duration_ms": self.settings.vad_min_silence_ms,
            },
            without_timestamps=True,
            word_timestamps=False,
        )

        # faster-whisper starts inference while the segment generator is iterated.
        text = "".join(segment.text for segment in segments).strip()
        elapsed = time.monotonic() - started

        return TranscriptionResult(
            text=text,
            language=info.language or self.settings.language or "unknown",
            language_probability=float(info.language_probability or 0.0),
            duration_seconds=float(info.duration),
            duration_after_vad_seconds=float(info.duration_after_vad),
            processing_seconds=elapsed,
        )

    async def transcribe(self, audio_path: str) -> TranscriptionResult:
        await self.start()
        return await asyncio.to_thread(self._transcribe_sync, audio_path)
