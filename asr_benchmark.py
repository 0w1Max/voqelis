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


def _decode_audio(path: Path, *, sample_rate: int = 16_000) -> tuple[Any, float]:
    import av
    import numpy as np

    started = time.perf_counter()
    with av.open(str(path)) as container:
        stream = next(stream for stream in container.streams.audio)
        resampler = av.audio.resampler.AudioResampler(
            format="flt",
            layout="mono",
            rate=sample_rate,
        )
        chunks = []
        for frame in container.decode(stream):
            converted = resampler.resample(frame)
            if not isinstance(converted, list):
                converted = [converted]
            for output_frame in converted:
                if output_frame is not None:
                    chunks.append(output_frame.to_ndarray().reshape(-1))
        tail = resampler.resample(None)
        if tail is not None:
            if not isinstance(tail, list):
                tail = [tail]
            for output_frame in tail:
                if output_frame is not None:
                    chunks.append(output_frame.to_ndarray().reshape(-1))

    if not chunks:
        raise ValueError(f"No decoded audio samples: {path}")

    waveform = np.concatenate(chunks).astype(np.float32, copy=False)
    return waveform, time.perf_counter() - started


def _critical_facts(text: str, *, today: date) -> dict[str, Any]:
    from voqelis.planner.evidence import SourceSpan, recognize_intent_evidence

    evidence = recognize_intent_evidence(text, today=today)
    span = SourceSpan(0, len(text), text)
    facts = evidence.for_task(span)
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


def _critical_match(reference: str, hypothesis: str, *, today: date) -> tuple[bool, dict[str, Any], dict[str, Any]]:
    try:
        ref_facts = _critical_facts(reference, today=today)
    except Exception as exc:
        ref_facts = {"error": f"{type(exc).__name__}: {exc}"}
    try:
        hyp_facts = _critical_facts(hypothesis, today=today)
    except Exception as exc:
        hyp_facts = {"error": f"{type(exc).__name__}: {exc}"}
    return ref_facts == hyp_facts, ref_facts, hyp_facts


def _peak_rss_mb() -> float:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value / 1024


def _load_model(name: str):
    if name == "whisper-base" or name == "whisper-small":
        from faster_whisper import WhisperModel

        size = "base" if name == "whisper-base" else "small"
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

    import onnxruntime as ort
    import onnx_asr

    session_options = ort.SessionOptions()
    session_options.intra_op_num_threads = 1
    session_options.inter_op_num_threads = 1
    session_options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    session_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

    return onnx_asr.load_model(
        name.removesuffix("-int8"),
        quantization="int8",
        sess_options=session_options,
        providers=["CPUExecutionProvider"],
    )


def _recognize(model_name: str, model: Any, waveform: Any) -> str:
    if model_name.startswith("whisper-"):
        segments, _ = model.transcribe(
            waveform,
            language="ru",
            beam_size=5,
            temperature=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
            condition_on_previous_text=True,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            without_timestamps=False,
            word_timestamps=False,
        )
        return "".join(segment.text for segment in segments).strip()

    result = model.recognize(waveform, sample_rate=16_000)
    return str(getattr(result, "text", result)).strip()


def _load_manifest(path: Path) -> tuple[date, list[Case]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    today = date.fromisoformat(data["today"])
    root = path.parent
    cases = [
        Case(
            case_id=item["id"],
            audio=(root / item["audio"]).resolve(),
            reference=item["reference"],
        )
        for item in data["cases"]
    ]
    for case in cases:
        if not case.audio.is_file():
            raise FileNotFoundError(f"Audio file not found: {case.audio}")
    return today, cases


def _run_worker(model_name: str, manifest: Path, output: Path, warmup_count: int) -> None:
    today, cases = _load_manifest(manifest)
    load_started = time.perf_counter()
    model = _load_model(model_name)
    load_seconds = time.perf_counter() - load_started

    warmup_ids = []
    for case in cases[:warmup_count]:
        waveform, _ = _decode_audio(case.audio)
        _recognize(model_name, model, waveform)
        warmup_ids.append(case.case_id)

    rows = []
    for case in cases:
        waveform, decode_seconds = _decode_audio(case.audio)
        started = time.perf_counter()
        hypothesis = _recognize(model_name, model, waveform)
        asr_seconds = time.perf_counter() - started
        duration_seconds = len(waveform) / 16_000
        critical_match, ref_facts, hyp_facts = _critical_match(
            case.reference,
            hypothesis,
            today=today,
        )
        rows.append(
            {
                "id": case.case_id,
                "reference": case.reference,
                "hypothesis": hypothesis,
                "duration_seconds": duration_seconds,
                "decode_seconds": decode_seconds,
                "asr_seconds": asr_seconds,
                "rtfx": duration_seconds / asr_seconds if asr_seconds else None,
                "wer": _wer(case.reference, hypothesis),
                "cer": _cer(case.reference, hypothesis),
                "critical_match": critical_match,
                "reference_critical": ref_facts,
                "hypothesis_critical": hyp_facts,
            }
        )

    asr_times = [row["asr_seconds"] for row in rows]
    result = {
        "model": model_name,
        "load_seconds": load_seconds,
        "peak_rss_mb": _peak_rss_mb(),
        "warmup_cases": warmup_ids,
        "cases": rows,
        "summary": {
            "case_count": len(rows),
            "wer_mean": statistics.mean(row["wer"] for row in rows) if rows else None,
            "cer_mean": statistics.mean(row["cer"] for row in rows) if rows else None,
            "critical_accuracy": (
                sum(row["critical_match"] for row in rows) / len(rows)
                if rows
                else None
            ),
            "asr_seconds_total": sum(asr_times),
            "asr_seconds_p50": (
                statistics.median(asr_times) if asr_times else None
            ),
            "rtfx_mean": (
                statistics.mean(row["rtfx"] for row in rows if row["rtfx"] is not None)
                if rows
                else None
            ),
        },
    }
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _run_parent(manifest: Path, models: list[str], output: Path, warmup_count: int) -> int:
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
                    f"Model benchmark failed: {model_name}, exit={completed.returncode}"
                )
            final["models"][model_name] = json.loads(
                result_path.read_text(encoding="utf-8")
            )

    output.write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark ASR models for Voqelis.")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("asr-benchmark.json"))
    parser.add_argument(
        "--models",
        default="whisper-base,gigaam-v3-rnnt-int8,gigaam-v3-ctc-int8",
        help="Comma-separated model names.",
    )
    parser.add_argument("--warmup-count", type=int, default=1)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--model", choices=MODEL_NAMES)
    args = parser.parse_args()

    if args.worker:
        if args.model is None:
            parser.error("--model is required with --worker")
        _run_worker(args.model, args.manifest, args.output, args.warmup_count)
        return 0

    models = [item.strip() for item in args.models.split(",") if item.strip()]
    unknown = sorted(set(models) - set(MODEL_NAMES))
    if unknown:
        parser.error(f"Unknown model(s): {', '.join(unknown)}")
    return _run_parent(args.manifest, models, args.output, args.warmup_count)


if __name__ == "__main__":
    raise SystemExit(main())
