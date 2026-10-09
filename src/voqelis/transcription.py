from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
import uuid
from collections.abc import Sequence
from pathlib import Path

from faster_whisper import WhisperModel

from .audio import convert_audio_to_wav, probe_duration_seconds
from .config import Settings
from .domain import TranscriptionResult, TranscriptSegment

logger = logging.getLogger(__name__)

GIGAAM_BACKENDS = frozenset(
    {"gigaam-v3-rnnt-int8", "gigaam-v3-ctc-int8"}
)
_WORKER_START_TIMEOUT_SECONDS = 240
_WORKER_INFERENCE_TIMEOUT_SECONDS = 240
_PAUSE_THRESHOLD_SECONDS = 0.6
_PAUSE_PUNCTUATION = frozenset(".,;:!?…")


def build_pause_aware_text(
    text: str,
    tokens: Sequence[str] | None,
    timestamps: Sequence[float] | None,
) -> str:
    """Insert commas at long timestamp gaps between recognized words.

    The original text is the source of truth. If token/timestamp metadata is
    missing, inconsistent, non-finite, or cannot reconstruct that text, return
    the original unchanged instead of guessing.
    """
    if (
        not tokens
        or timestamps is None
        or len(tokens) != len(timestamps)
        or not all(isinstance(token, str) for token in tokens)
    ):
        return text

    if "".join(tokens).strip() != text.strip():
        return text

    normalized_timestamps: list[float] = []
    for timestamp in timestamps:
        if isinstance(timestamp, bool):
            return text
        try:
            value = float(timestamp)
        except (TypeError, ValueError):
            return text
        if not math.isfinite(value):
            return text
        if normalized_timestamps and value < normalized_timestamps[-1]:
            return text
        normalized_timestamps.append(value)

    parts: list[str] = []
    previous_timestamp: float | None = None
    for token, timestamp in zip(tokens, normalized_timestamps, strict=True):
        if (
            parts
            and previous_timestamp is not None
            and timestamp - previous_timestamp >= _PAUSE_THRESHOLD_SECONDS
            and token[:1].isspace()
            and token.lstrip()[:1] not in _PAUSE_PUNCTUATION
        ):
            prefix = "".join(parts).rstrip()
            if prefix and prefix[-1] not in _PAUSE_PUNCTUATION:
                parts = [prefix, ","]
        parts.append(token)
        previous_timestamp = timestamp

    return "".join(parts).strip()


class Transcriber:
    """Owns one selected ASR backend and serializes inference."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._backend = getattr(settings, "asr_backend", "whisper")
        self._model: WhisperModel | None = None
        self._worker: asyncio.subprocess.Process | None = None
        self._load_lock = asyncio.Lock()
        self._inference_lock = asyncio.Lock()
        self._repo_root = Path(__file__).resolve().parents[2]
        worker_python = Path(
            getattr(
                settings,
                "asr_worker_python",
                Path(".venv-asr-benchmark/bin/python"),
            )
        ).expanduser()
        self._worker_python = (
            worker_python
            if worker_python.is_absolute()
            else (self._repo_root / worker_python).resolve()
        )

    def _load_sync(self) -> WhisperModel:
        logger.info(
            "Loading ASR backend: whisper size=%s device=%s compute=%s threads=%s",
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

    async def _start_gigaam_worker(self) -> None:
        if self._backend not in GIGAAM_BACKENDS:
            raise ValueError(f"Unsupported ASR_BACKEND: {self._backend!r}")
        if not self._worker_python.is_file() or not os.access(self._worker_python, os.X_OK):
            raise RuntimeError(
                "GigaAM worker Python is missing or not executable: "
                f"{self._worker_python}. Set ASR_WORKER_PYTHON to the isolated "
                ".venv-asr-benchmark/bin/python interpreter."
            )

        worker_env = os.environ.copy()
        source_root = str(self._repo_root / "src")
        current_pythonpath = worker_env.get("PYTHONPATH")
        worker_env["PYTHONPATH"] = (
            source_root
            if not current_pythonpath
            else source_root + os.pathsep + current_pythonpath
        )
        logger.info(
            "Starting persistent GigaAM worker: backend=%s interpreter=%s",
            self._backend,
            self._worker_python,
        )
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                str(self._worker_python),
                "-m",
                "voqelis.asr_worker",
                "--model",
                self._backend,
                cwd=str(self._repo_root),
                env=worker_env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=None,
            )
            self._worker = process
            if process.stdout is None:
                raise RuntimeError("GigaAM worker stdout pipe was not created.")

            first_line = await asyncio.wait_for(
                process.stdout.readline(),
                timeout=_WORKER_START_TIMEOUT_SECONDS,
            )
            if not first_line:
                return_code = await process.wait()
                raise RuntimeError(
                    f"GigaAM worker exited before ready (exit={return_code})."
                )
            ready = json.loads(first_line)
            if not ready.get("ready"):
                raise RuntimeError(
                    "GigaAM worker failed to load model: "
                    f"{ready.get('error', 'unknown error')}"
                )
            logger.info("GigaAM worker is ready: backend=%s", self._backend)
        except BaseException:
            if process is not None:
                await self._stop_process(process)
            self._worker = None
            raise

    async def start(self) -> None:
        async with self._load_lock:
            if self._backend == "whisper":
                if self._model is None:
                    self._model = await asyncio.to_thread(self._load_sync)
                return

            if self._backend not in GIGAAM_BACKENDS:
                raise ValueError(f"Unsupported ASR_BACKEND: {self._backend!r}")
            if self._worker is None or self._worker.returncode is not None:
                await self._start_gigaam_worker()

    async def _stop_process(
        self,
        process: asyncio.subprocess.Process,
    ) -> None:
        if process.returncode is not None:
            return
        if process.stdin is not None and not process.stdin.is_closing():
            process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()

    async def close(self) -> None:
        process = self._worker
        self._worker = None
        if process is not None:
            await self._stop_process(process)

    async def _transcribe_gigaam(self, audio_path: str) -> TranscriptionResult:
        started = time.monotonic()
        temporary_wav = self.settings.temp_dir / f"voqelis-gigaam-{uuid.uuid4().hex}.wav"
        try:
            await asyncio.to_thread(
                convert_audio_to_wav,
                Path(audio_path),
                temporary_wav,
            )
            duration = await asyncio.to_thread(
                probe_duration_seconds,
                temporary_wav,
            )

            async with self._inference_lock:
                await self.start()
                process = self._worker
                if (
                    process is None
                    or process.returncode is not None
                    or process.stdin is None
                    or process.stdout is None
                ):
                    raise RuntimeError("GigaAM worker is not running.")

                request = json.dumps(
                    {"audio_path": str(temporary_wav)},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                try:
                    process.stdin.write((request + "\n").encode("utf-8"))
                    await process.stdin.drain()
                    response_line = await asyncio.wait_for(
                        process.stdout.readline(),
                        timeout=_WORKER_INFERENCE_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError as exc:
                    await self._stop_process(process)
                    self._worker = None
                    raise TimeoutError("GigaAM recognition exceeded 240 seconds.") from exc

                if not response_line:
                    return_code = await process.wait()
                    self._worker = None
                    raise RuntimeError(
                        f"GigaAM worker stopped during inference (exit={return_code})."
                    )
                try:
                    response = json.loads(response_line)
                except json.JSONDecodeError as exc:
                    await self._stop_process(process)
                    self._worker = None
                    raise RuntimeError("GigaAM worker returned invalid JSON.") from exc

                if not response.get("ok"):
                    raise RuntimeError(
                        f"GigaAM inference failed: {response.get('error', 'unknown error')}"
                    )
                text = str(response.get("text", "")).strip()
                tokens = response.get("tokens")
                timestamps = response.get("timestamps")
                if not isinstance(tokens, list):
                    tokens = None
                if not isinstance(timestamps, list):
                    timestamps = None
                pause_aware_text = build_pause_aware_text(text, tokens, timestamps)

            elapsed = time.monotonic() - started
            return TranscriptionResult(
                text=text,
                language="ru",
                language_probability=0.0,
                duration_seconds=duration,
                duration_after_vad_seconds=duration,
                processing_seconds=elapsed,
                pause_aware_text=pause_aware_text,
                segments=(),
            )
        finally:
            temporary_wav.unlink(missing_ok=True)

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
            without_timestamps=False,
            word_timestamps=False,
        )

        # faster-whisper starts inference while the segment generator is iterated.
        segment_list = list(segments)
        text = "".join(segment.text for segment in segment_list).strip()
        pause_parts: list[str] = []
        previous_end = 0.0
        for segment in segment_list:
            segment_text = segment.text.strip()
            if not segment_text:
                continue
            if pause_parts and segment.start - previous_end >= 0.6:
                pause_parts.append(",")
            pause_parts.append(segment_text)
            previous_end = segment.end
        pause_aware_text = " ".join(pause_parts).replace(" ,", ",").strip()
        elapsed = time.monotonic() - started

        transcript_segments = tuple(
            TranscriptSegment(
                text=segment.text.strip(),
                start_seconds=float(segment.start),
                end_seconds=float(segment.end),
                average_logprob=float(segment.avg_logprob),
                no_speech_probability=float(segment.no_speech_prob),
            )
            for segment in segment_list
            if segment.text.strip()
        )

        return TranscriptionResult(
            text=text,
            language=info.language or self.settings.language or "unknown",
            language_probability=float(info.language_probability or 0.0),
            duration_seconds=float(info.duration),
            duration_after_vad_seconds=float(info.duration_after_vad),
            processing_seconds=elapsed,
            pause_aware_text=pause_aware_text,
            segments=transcript_segments,
        )

    async def transcribe(self, audio_path: str) -> TranscriptionResult:
        await self.start()
        if self._backend == "whisper":
            return await asyncio.to_thread(self._transcribe_sync, audio_path)
        return await self._transcribe_gigaam(audio_path)
