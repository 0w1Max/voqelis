from __future__ import annotations

import argparse
import array
import json
import os
import sys
import time
import wave
from pathlib import Path

import transcribe_cpp

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument(
        "--audio", type=Path, action="append", required=True
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    chunk_results: list[dict[str, object]] = []

    started = time.monotonic()
    with transcribe_cpp.Model(str(args.model), backend="cpu") as model:
        load_seconds = time.monotonic() - started
        with model.session(n_threads=1) as session:
            for audio_path in args.audio:
                with wave.open(str(audio_path), "rb") as wav:
                    if (
                        wav.getframerate() != 16000
                        or wav.getnchannels() != 1
                        or wav.getsampwidth() != 2
                    ):
                        raise ValueError(
                            "Audio must be 16 kHz, mono, signed 16-bit PCM."
                        )
                    raw_pcm = array.array("h")
                    raw_pcm.frombytes(wav.readframes(wav.getnframes()))

                if sys.byteorder != "little":
                    raw_pcm.byteswap()

                pcm = array.array(
                    "f", (sample / 32768.0 for sample in raw_pcm)
                )
                audio_seconds = len(pcm) / 16000.0

                started = time.monotonic()
                result = session.run(pcm)
                inference_seconds = time.monotonic() - started

                chunk_results.append({
                    "text": str(result.text).strip(),
                    "audio_seconds": audio_seconds,
                    "inference_seconds": inference_seconds,
                })
                del pcm, raw_pcm

    payload = {
        "text": (
            chunk_results[0]["text"]
            if len(chunk_results) == 1
            else " ".join(str(chunk["text"]) for chunk in chunk_results).strip()
        ),
        "audio_seconds": sum(
            float(chunk["audio_seconds"]) for chunk in chunk_results
        ),
        "load_seconds": load_seconds,
        "inference_seconds": sum(
            float(chunk["inference_seconds"]) for chunk in chunk_results
        ),
        "model": args.model.name,
        "backend": "cpu",
        "chunks": chunk_results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(args.output.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, args.output)
    print(json.dumps(
        {
            "audio_seconds": audio_seconds,
            "load_seconds": load_seconds,
            "inference_seconds": inference_seconds,
        }
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
