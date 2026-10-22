"""Isolated, real-ONNX smoke for Telegram-facing RUPunct.

PYTHONPATH=src python -m tools.rupunct_integration_smoke --model-dir DIR
Does not read production .env, interact with Telegram, or write anything.
"""

from __future__ import annotations

import argparse
import asyncio
import resource
import time
from pathlib import Path
from types import SimpleNamespace

from voqelis.text_punctuation import (
    TextPunctuationProcessor,
    _lexical_words,
)

SAMPLES = (
    "проверка распознавания речи сегодня хорошая погода а завтра продолжим тестирование",
    "завтра в 21:00 ужинать потом в 22:00 читать книгу",
    "что мне запланировать на завтра вечером",
    "сегодня я купил молоко хлеб и яблоки",
)


async def smoke(model_dir: Path) -> None:
    off_settings = SimpleNamespace(asr_punctuation_backend="none")
    assert TextPunctuationProcessor.from_settings(off_settings) is None
    on_settings = SimpleNamespace(
        asr_punctuation_backend="rupunct-small-int8",
        asr_text_punctuation_model_dir=model_dir,
    )
    processor = TextPunctuationProcessor.from_settings(on_settings)
    assert processor is not None
    for raw in SAMPLES:
        started = time.monotonic()
        formatted = await processor.format(raw, fallback_text=raw)
        elapsed_ms = (time.monotonic() - started) * 1000
        if _lexical_words(formatted) != _lexical_words(raw):
            raise ValueError("Smoke test detected changed words")
        if processor._disabled:
            raise RuntimeError("Smoke test processor disabled due to an inference error")
        if not formatted.strip():
            raise ValueError("Smoke test returned empty text")
        print(f"IN:   {raw}\nOUT:  {formatted}\nMS:   {elapsed_ms:.1f}\n")
    if processor._model is None:
        raise RuntimeError("Smoke test failed to load ONNX session")
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    print(f"RUPunct model loaded once; peak process RSS: {peak:.1f} MiB")
    print("PASS: optional text formatter, lexical preservation, ONNX inference")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(smoke(args.model_dir))


if __name__ == "__main__":
    main()
