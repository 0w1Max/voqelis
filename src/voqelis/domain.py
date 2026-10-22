from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AudioJob:
    """A downloaded audio file waiting for transcription."""

    chat_id: int
    reply_to_message_id: int
    user_id: int
    file_path: Path


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    text: str
    start_seconds: float
    end_seconds: float
    average_logprob: float | None = None
    no_speech_probability: float | None = None


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    """Normalized transcription output used by current and future processors."""

    text: str
    language: str
    language_probability: float
    duration_seconds: float
    duration_after_vad_seconds: float
    processing_seconds: float
    pause_aware_text: str | None = None
    segments: tuple[TranscriptSegment, ...] = ()
