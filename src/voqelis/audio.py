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


def convert_audio_to_wav(source: Path, destination: Path) -> None:
    """Convert an uploaded audio file to mono 16 kHz, 16-bit PCM WAV."""
    try:
        import av
    except ImportError as exc:
        raise AudioProcessingError("PyAV is not installed") from exc

    import wave

    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with av.open(str(source)) as container:
            if not container.streams.audio:
                raise AudioProcessingError("The uploaded file contains no audio stream.")
            stream = container.streams.audio[0]
            resampler = av.AudioResampler(format="s16", layout="mono", rate=16_000)
            with wave.open(str(destination), "wb") as target:
                target.setnchannels(1)
                target.setsampwidth(2)
                target.setframerate(16_000)
                for frame in container.decode(stream):
                    for converted in resampler.resample(frame):
                        target.writeframes(converted.to_ndarray().tobytes())
                for converted in resampler.resample(None):
                    target.writeframes(converted.to_ndarray().tobytes())
    except AudioProcessingError:
        destination.unlink(missing_ok=True)
        raise
    except (av.error.FFmpegError, OSError, ValueError) as exc:
        destination.unlink(missing_ok=True)
        raise AudioProcessingError(f"Unable to convert uploaded audio: {exc}") from exc

    if not destination.is_file() or destination.stat().st_size <= 44:
        destination.unlink(missing_ok=True)
        raise AudioProcessingError("The uploaded audio contains no decodable samples.")


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
