from __future__ import annotations

import argparse
import json
import os
import resource
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

MODEL_NAMES = (
    "whisper-base",
    "whisper-small",
    "gigaam-v3-ctc-int8",
    "gigaam-v3-rnnt-int8",
    "gigaam-v3-e2e-ctc-int8",
    "gigaam-v3-e2e-rnnt-int8",
)


@dataclass(frozen=True, slots=True)
class Case:
    case_id: str
    audio: Path
    reference: str


def _normalize_text(text: str) -> list[str]:
    text = text.casefold().replace("ё", "е")
    cleaned = "".join(char if char.isalnum() or char.isspace() else " " for char in text)
    return cleaned.split()


def _edit_distance(left: list[str], right: list[str]) -> int:
    previous = list(range(len(right) + 1))
    for row_index, left_item in enumerate(left, 1):
        current = [row_index]
        for column_index, right_item in enumerate(right, 1):
            insert = current[column_index - 1] + 1
            delete = previous[column_index] + 1
            substitute = previous[column_index - 1] + (left_item != right_item)
            current.append(min(insert, delete, substitute))
        previous = current
    return previous[-1]


def _wer(reference: str, hypothesis: str) -> float:
    ref = _normalize_text(reference)
    hyp = _normalize_text(hypothesis)
    return _edit_distance(ref, hyp) / max(len(ref), 1)


def _cer(reference: str, hypothesis: str) -> float:
    ref = "".join(_normalize_text(reference))
    hyp = "".join(_normalize_text(hypothesis))
    return _edit_distance(list(ref), list(hyp)) / max(len(ref), 1)


def _audio_duration(path: Path) -> float:
    import wave

    with wave.open(str(path), "rb") as source:
        if source.getnchannels() != 1:
            raise ValueError(f"Audio must be mono WAV: {path}")
        if source.getsampwidth() != 2:
            raise ValueError(f"Audio must be 16-bit PCM WAV: {path}")
        if source.getframerate() != 16_000:
            raise ValueError(
                f"Audio must be 16 kHz WAV: {path} has {source.getframerate()} Hz"
            )
        return source.getnframes() / source.getframerate()

