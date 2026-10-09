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
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

MODEL_NAMES = (
    "whisper-base-original",
    "whisper-base-current",
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
    today: date


def _normalize_text(text: str) -> list[str]:
    text = text.casefold().replace("ё", "е")
    cleaned = "".join(
        char if char.isalnum() or char.isspace() else " "
        for char in text
    )
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


_NUMBER_WORDS = {
    "ноль": 0, "нуль": 0, "нуля": 0, "нулю": 0, "нулём": 0, "нулем": 0,
    "один": 1, "одна": 1, "одно": 1, "одну": 1, "одного": 1,
    "одной": 1, "одному": 1, "одним": 1, "одними": 1,
    "два": 2, "две": 2, "двух": 2, "двум": 2, "двумя": 2,
    "три": 3, "трех": 3, "трёх": 3, "трем": 3, "трём": 3, "тремя": 3,
    "четыре": 4, "четырех": 4, "четырёх": 4, "четырем": 4,
    "четырём": 4, "четырьмя": 4,
    "пять": 5, "пяти": 5, "пятью": 5,
    "шесть": 6, "шести": 6, "шестью": 6,
    "семь": 7, "семи": 7, "семью": 7,
    "восемь": 8, "восьми": 8, "восемью": 8,
    "девять": 9, "девяти": 9, "девятью": 9,
    "десять": 10, "десяти": 10, "десятью": 10,
    "одиннадцать": 11, "одиннадцати": 11,
    "двенадцать": 12, "двенадцати": 12,
    "тринадцать": 13, "тринадцати": 13,
    "четырнадцать": 14, "четырнадцати": 14,
    "пятнадцать": 15, "пятнадцати": 15,
    "шестнадцать": 16, "шестнадцати": 16,
    "семнадцать": 17, "семнадцати": 17,
    "восемнадцать": 18, "восемнадцати": 18,
    "девятнадцать": 19, "девятнадцати": 19,
}

_TENS_WORDS = {
    "двадцать": 20, "двадцати": 20,
    "тридцать": 30, "тридцати": 30,
    "сорок": 40, "сорока": 40,
    "пятьдесят": 50, "пятидесяти": 50,
    "шестьдесят": 60, "шестидесяти": 60,
    "семьдесят": 70, "семидесяти": 70,
    "восемьдесят": 80, "восьмидесяти": 80,
    "девяносто": 90, "девяноста": 90,
}


def _normalize_numeric_tokens(tokens: list[str]) -> list[str]:
    """Normalize common Russian cardinal numerals for benchmark WER only."""
    normalized: list[str] = []
    index = 0

    while index < len(tokens):
        token = tokens[index]
        tens = _TENS_WORDS.get(token)
        if tens is not None:
            if index + 1 < len(tokens):
                unit = _NUMBER_WORDS.get(tokens[index + 1])
                if unit is not None and 1 <= unit <= 9:
                    normalized.append(str(tens + unit))
                    index += 2
                    continue
            normalized.append(str(tens))
            index += 1
            continue

        value = _NUMBER_WORDS.get(token)
        normalized.append(str(value) if value is not None else token)
        index += 1

    return normalized


def _wer_numeric_normalized(reference: str, hypothesis: str) -> float:
    """WER with common Russian cardinal numerals normalized to numeric values."""
    ref = _normalize_numeric_tokens(_normalize_text(reference))
    hyp = _normalize_numeric_tokens(_normalize_text(hypothesis))
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


def _critical_facts(text: str, *, today: date) -> dict[str, Any]:
    from voqelis.planner.evidence import (
        SourceSpan,
        recognize_intent_evidence,
    )

    evidence = recognize_intent_evidence(text, today=today)
    facts = evidence.for_task(SourceSpan(0, len(text), text))
    return {
        "day": facts.day.isoformat() if facts.day else None,
        "start_minute": facts.start_minute,
        "end_minute": facts.end_minute,
        "period": facts.period,
        "relation": facts.relation,
        "anchor": facts.anchor,
        "relation_end": facts.relation_end,
        "anchor_end": facts.anchor_end,
    }


def _critical_match(
    reference: str,
    hypothesis: str,
    *,
    today: date,
) -> tuple[bool, dict[str, Any], dict[str, Any]]:
    try:
        ref_facts = _critical_facts(reference, today=today)
    except Exception as exc:  # noqa: BLE001 - Record recognition errors per case; keep the benchmark running.
        ref_facts = {"error": f"{type(exc).__name__}: {exc}"}
    try:
        hyp_facts = _critical_facts(hypothesis, today=today)
    except Exception as exc:  # noqa: BLE001 - Record recognition errors per case; keep the benchmark running.
        hyp_facts = {"error": f"{type(exc).__name__}: {exc}"}
    return ref_facts == hyp_facts, ref_facts, hyp_facts


def _peak_rss_mb() -> float:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value / 1024


def _load_model(name: str):
    if name in {
        "whisper-base-original",
        "whisper-base-current",
        "whisper-small",
    }:
        from faster_whisper import WhisperModel

        size = "small" if name == "whisper-small" else "base"
        return WhisperModel(
            size,
            device="cpu",
            compute_type="int8",
            cpu_threads=1,
            num_workers=1,
            download_root=os.environ.get(
                "ASR_BENCHMARK_MODEL_CACHE",
                str(Path.home() / ".cache" / "voqelis-asr-benchmark"),
            ),
        )

    from voqelis.gigaam import load_gigaam_model

    return load_gigaam_model(name)


def _recognize(model_name: str, model: Any, audio_path: Path) -> str:
    if model_name.startswith("whisper-"):
        use_timestamps = model_name != "whisper-base-original"
        segments, _ = model.transcribe(
            str(audio_path),
            language="ru",
            beam_size=5,
            temperature=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
            condition_on_previous_text=True,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            without_timestamps=not use_timestamps,
            word_timestamps=False,
        )
        return "".join(segment.text for segment in segments).strip()

    result = model.recognize(str(audio_path))
    return str(result).strip()


def _load_manifest(path: Path) -> tuple[date, list[Case]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    today = date.fromisoformat(data["today"])
    root = path.parent
    cases = [
        Case(
            case_id=item["id"],
            audio=(root / item["audio"]).resolve(),
            reference=item["reference"],
            today=date.fromisoformat(item.get("today", data["today"])),
        )
        for item in data["cases"]
    ]
    for case in cases:
        if not case.audio.is_file():
            raise FileNotFoundError(f"Audio file not found: {case.audio}")
    if not cases:
        raise ValueError("Manifest contains no cases")
    return today, cases


def _run_worker(
    model_name: str,
    manifest: Path,
    output: Path,
    warmup_count: int,
) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
    _, cases = _load_manifest(manifest)

    load_started = time.perf_counter()
    model = _load_model(model_name)
    load_seconds = time.perf_counter() - load_started

    warmup_ids = []
    for case in cases[:max(warmup_count, 0)]:
        _recognize(model_name, model, case.audio)
        warmup_ids.append(case.case_id)

    rows = []
    for case in cases:
        duration_seconds = _audio_duration(case.audio)
        started = time.perf_counter()
        hypothesis = _recognize(model_name, model, case.audio)
        asr_seconds = time.perf_counter() - started

        critical_match, ref_facts, hyp_facts = _critical_match(
            case.reference,
            hypothesis,
            today=case.today,
        )
        rows.append(
            {
                "id": case.case_id,
                "reference": case.reference,
                "hypothesis": hypothesis,
                "duration_seconds": duration_seconds,
                "asr_seconds": asr_seconds,
                "rtfx": (
                    duration_seconds / asr_seconds
                    if asr_seconds > 0
                    else None
                ),
                "wer": _wer(case.reference, hypothesis),
                "wer_numeric_normalized": _wer_numeric_normalized(
                    case.reference,
                    hypothesis,
                ),
                "cer": _cer(case.reference, hypothesis),
                "critical_match": critical_match,
                "reference_critical": ref_facts,
                "hypothesis_critical": hyp_facts,
            }
        )

    asr_times = [row["asr_seconds"] for row in rows]
    rtfx_values = [row["rtfx"] for row in rows if row["rtfx"] is not None]
    result = {
        "model": model_name,
        "load_seconds": load_seconds,
        "peak_rss_mb": _peak_rss_mb(),
        "warmup_cases": warmup_ids,
        "cases": rows,
        "summary": {
            "case_count": len(rows),
            "wer_mean": statistics.mean(row["wer"] for row in rows),
            "wer_numeric_normalized_mean": statistics.mean(
                row["wer_numeric_normalized"] for row in rows
            ),
            "cer_mean": statistics.mean(row["cer"] for row in rows),
            "critical_accuracy": sum(
                row["critical_match"] for row in rows
            ) / len(rows),
            "asr_seconds_total": sum(asr_times),
            "asr_seconds_p50": statistics.median(asr_times),
            "rtfx_mean": statistics.mean(rtfx_values) if rtfx_values else None,
        },
    }
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _run_parent(
    manifest: Path,
    models: list[str],
    output: Path,
    warmup_count: int,
) -> int:
    output.parent.mkdir(parents=True, exist_ok=True)
    final: dict[str, Any] = {
        "manifest": str(manifest),
        "models": {},
    }

    for model_name in models:
        with tempfile.TemporaryDirectory(prefix="voqelis-asr-") as temp_dir:
            result_path = Path(temp_dir) / "result.json"
            command = [
                sys.executable,
                str(Path(__file__).resolve()),
                "--worker",
                "--manifest",
                str(manifest.resolve()),
                "--model",
                model_name,
                "--output",
                str(result_path),
                "--warmup-count",
                str(warmup_count),
            ]
            completed = subprocess.run(command, check=False)
            if completed.returncode != 0:
                raise RuntimeError(
                    f"Model benchmark failed: {model_name}, "
                    f"exit={completed.returncode}"
                )
            final["models"][model_name] = json.loads(
                result_path.read_text(encoding="utf-8")
            )

    output.write_text(
        json.dumps(final, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark ASR models for Voqelis.")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("asr-benchmark.json"),
    )
    parser.add_argument(
        "--models",
        default="whisper-base-original,whisper-base-current,gigaam-v3-rnnt-int8,gigaam-v3-ctc-int8",
        help="Comma-separated model names.",
    )
    parser.add_argument("--warmup-count", type=int, default=1)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--model", choices=MODEL_NAMES)
    args = parser.parse_args()

    if args.worker:
        if args.model is None:
            parser.error("--model is required with --worker")
        _run_worker(
            args.model,
            args.manifest,
            args.output,
            args.warmup_count,
        )
        return 0

    models = [item.strip() for item in args.models.split(",") if item.strip()]
    unknown = sorted(set(models) - set(MODEL_NAMES))
    if unknown:
        parser.error(f"Unknown model(s): {', '.join(unknown)}")
    return _run_parent(args.manifest, models, args.output, args.warmup_count)


if __name__ == "__main__":
    raise SystemExit(main())
