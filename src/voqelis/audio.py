from __future__ import annotations

import logging
from pathlib import Path


logger = logging.getLogger(__name__)


class AudioProcessingError(Exception):
    """The input cannot be probed or decoded as audio."""


AUDIO_EXTENSIONS = frozenset(
    {
        ".aac", ".aiff", ".alac", ".amr", ".flac", ".m4a", ".mp3",
        ".oga", ".ogg", ".opus", ".wav", ".webm", ".wma",
    }
)


def is_audio_document(*, mime_type: str | None, file_name: str | None) -> bool:
    if mime_type and mime_type.lower().startswith("audio/"):
        return True
    return bool(file_name and Path(file_name).suffix.lower() in AUDIO_EXTENSIONS)


def probe_duration_seconds(path: Path) -> float:
    """Read container metadata without decoding the whole audio stream."""
    try:
        import av
    except ImportError as exc:
        raise AudioProcessingError("PyAV is not installed") from exc

    try:
        with av.open(str(path)) as container:
            if container.duration is not None and container.duration >= 0:
                return float(container.duration) / 1_000_000.0

            for stream in container.streams.audio:
                if stream.duration is not None and stream.time_base is not None:
                    duration = float(stream.duration * stream.time_base)
                    if duration >= 0:
                        return duration
    except (av.error.FFmpegError, OSError, ValueError) as exc:
        raise AudioProcessingError(
            f"Unable to read or probe audio file: {exc}"
        ) from exc

    raise AudioProcessingError("Unable to determine audio duration")
