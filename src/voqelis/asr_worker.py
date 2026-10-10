from __future__ import annotations

import argparse
import json
import logging
import sys
import traceback
from contextlib import redirect_stdout
from typing import Any, TextIO

from .gigaam import SUPPORTED_GIGAAM_MODELS, load_gigaam_model, load_gigaam_vad

logger = logging.getLogger(__name__)

_VAD_MIN_SILENCE_DURATION_MS = 600


def _emit(payload: dict[str, Any], stream: TextIO | None = None) -> None:
    destination = stream or sys.stdout
    destination.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    destination.write("\n")
    destination.flush()


def serve(
    model_name: str,
    *,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
) -> int:
    """Serve line-delimited JSON requests while keeping ASR and VAD loaded."""
    source = input_stream or sys.stdin
    destination = output_stream or sys.stdout

    if model_name not in SUPPORTED_GIGAAM_MODELS:
        _emit(
            {"ready": False, "error": f"Unsupported GigaAM model: {model_name!r}"},
            destination,
        )
        return 2

    try:
        # Third-party model/runtime output must not corrupt our JSON stdout protocol.
        with redirect_stdout(sys.stderr):
            asr_model = load_gigaam_model(model_name)
            vad_model = load_gigaam_vad()
            model = asr_model.with_vad(
                vad_model,
                min_silence_duration_ms=_VAD_MIN_SILENCE_DURATION_MS,
                batch_size=1,
            ).with_timestamps()
    except Exception as exc:  # noqa: BLE001 - Serialize startup failures for the parent process.
        traceback.print_exc(file=sys.stderr)
        _emit(
            {"ready": False, "error": f"{type(exc).__name__}: {exc}"},
            destination,
        )
        return 1

    _emit({"ready": True, "model": model_name, "vad": "silero"}, destination)

    for line in source:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            audio_path = request.get("audio_path")
            if not isinstance(audio_path, str) or not audio_path:
                raise ValueError("Request must contain a non-empty audio_path string.")

            with redirect_stdout(sys.stderr):
                recognized_segments = list(model.recognize(audio_path))
            segments = [
                {
                    "start": float(segment.start),
                    "end": float(segment.end),
                    "text": str(segment.text).strip(),
                }
                for segment in recognized_segments
            ]
            _emit({"ok": True, "segments": segments}, destination)
        except Exception as exc:  # noqa: BLE001 - Keep each request's failure in the line protocol.
            traceback.print_exc(file=sys.stderr)
            _emit(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                destination,
            )

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Persistent GigaAM inference worker.")
    parser.add_argument("--model", choices=sorted(SUPPORTED_GIGAAM_MODELS), required=True)
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    return serve(args.model)


if __name__ == "__main__":
    raise SystemExit(main())
