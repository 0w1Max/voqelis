"""Privacy-conscious, reproducible A/B scoring for punctuation candidates.

All candidates see the exact same *lexical* words. No text/audio is uploaded,
and the report contains aggregate scores and case IDs only, not transcripts.

Input manifest: JSON list of {id, reference_words, reference_punctuation,
duration_seconds}. Optional candidate: JSON mapping id -> punctuated text, or
id -> {"text": ..., "latency_ms": ...}. For the existing ONNX baseline:
--rupunct-model-dir /var/lib/voqelis/rupunct-model-v1

Run: PYTHONPATH=src python -m tools.compare_punctuation --manifest FILE \
     --candidate e2e=/private/outputs.json \
     --rupunct-model-dir /private/rupunct-model-v1 --output /private/report.json

This is not an ASR benchmark. Measure original word-recognition separately.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from tools.rupunct_fleurs_eval import choose_unique

_WORD = re.compile(r"[^\W_]+(?::[^\W_]+)*", re.UNICODE)
_MARK = re.compile(r"[,.;:!?…]+")
_SENTENCE = frozenset(".!?…")
_ALLOWED = frozenset({"raw", "rupunct", "gigaam-e2e", "horizon", "other"})


def _words_and_marks(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    matches = list(_WORD.finditer(text))
    words = tuple(m.group().casefold() for m in matches)
    marks = []
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        gap = text[match.end():end]
        marks.append("".join(_MARK.findall(gap)))
    return words, tuple(marks)


def _counts(gold: tuple[bool, ...], pred: tuple[bool, ...]) -> dict[str, int]:
    if len(gold) != len(pred):
        raise ValueError("Different number of word boundaries")
    tp = sum(a and b for a, b in zip(gold, pred, strict=True))
    fp = sum(not a and b for a, b in zip(gold, pred, strict=True))
    fn = sum(a and not b for a, b in zip(gold, pred, strict=True))
    return {"tp": tp, "fp": fp, "fn": fn}


def _f1(c: dict[str, int]) -> float:
    den = 2 * c["tp"] + c["fp"] + c["fn"]
    return round(100 * 2 * c["tp"] / den, 2) if den else 100.0


def _proportions(marks: tuple[str, ...], kind: str) -> tuple[bool, ...]:
    if kind == "comma":
        return tuple("," in m for m in marks)
    if kind == "period":
        return tuple("." in m or "…" in m for m in marks)
    if kind == "end":
        return tuple(any(char in _SENTENCE for char in m) for m in marks)
    raise ValueError(f"Unsupported mark kind: {kind}")


def _entry(text: str, latency_ms: float | None = None) -> dict[str, Any]:
    if not isinstance(text, str):
        raise ValueError("Candidate output must be text")
    if latency_ms is not None and (
        isinstance(latency_ms, bool)
        or not isinstance(latency_ms, (int, float))
        or not math.isfinite(latency_ms)
        or latency_ms < 0
    ):
        raise ValueError("Invalid latency measurement")
    return {"text": text, "latency_ms": latency_ms}


def load_predictions(path: Path) -> dict[str, dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Candidate file must be an object keyed by case ID")
    output = {}
    for case_id, entry in payload.items():
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("Case IDs must be nonempty strings")
        if isinstance(entry, str):
            output[case_id] = _entry(entry)
        elif isinstance(entry, dict):
            output[case_id] = _entry(entry.get("text"), entry.get("latency_ms"))
        else:
            raise ValueError("Invalid candidate row")
    return output


def run_local_rupunct(
    cases: list[dict[str, Any]], model_dir: Path,
) -> dict[str, dict[str, Any]]:
    from voqelis.rupunct import OnnxRUPunct

    model = OnnxRUPunct(model_dir)
    predictions = {}
    for row in cases:
        start = time.perf_counter()
        result = model.punctuate(row["reference_words"])
        predictions[row["id"]] = _entry(
            result, round((time.perf_counter() - start) * 1000, 3)
        )
    return predictions


def compare(
    manifest: list[dict[str, Any]],
    candidates: Mapping[str, Mapping[str, dict[str, Any]]],
    *,
    min_cases: int = 10,
    max_duration: float = 25.0,
) -> dict[str, Any]:
    if not candidates:
        raise ValueError("At least one candidate is required")
    if min_cases < 1:
        raise ValueError("min_cases must be positive")
    selected, selection = choose_unique(manifest, max_duration)
    ids = [row.get("id") for row in selected]
    if any(not isinstance(value, str) or not value for value in ids):
        raise ValueError("Every chosen case needs a stable, nonempty ID")
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate case IDs")

    comparable = []
    for row in selected:
        src, _ = _words_and_marks(row["reference_words"])
        gold, _ = _words_and_marks(row["reference_punctuation"])
        if src == gold:
            comparable.append(row)
    if len(comparable) < min_cases:
        raise ValueError(
            f"Only {len(comparable)} lexically comparable examples; "
            f"need at least {min_cases}"
        )

    results = {}
    for name, predictions in candidates.items():
        if not name or name == "raw":
            raise ValueError("Candidate name must not be 'raw' or empty")
        missing = sorted(set(row["id"] for row in comparable) - predictions.keys())
        if missing:
            raise ValueError(
                f"{name}: missing {len(missing)} candidate predictions"
            )
        totals = {
            label: {"tp": 0, "fp": 0, "fn": 0}
            for label in ("comma", "period", "end")
        }
        exact = 0
        bad_boundary_case_ids = []
        latencies = []
        for row in comparable:
            candidate = predictions[row["id"]]
            output = candidate["text"]
            source_words, _ = _words_and_marks(row["reference_words"])
            predicted_words, predicted_marks = _words_and_marks(output)
            if predicted_words != source_words:
                # Fail closed. No model that changed words gets a quality score.
                raise ValueError(f"{name}: lexical change in case {row['id']}")
            _, gold_marks = _words_and_marks(row["reference_punctuation"])
            assert len(gold_marks) == len(predicted_marks)
            exact += gold_marks == predicted_marks
            for label in totals:
                a = _proportions(gold_marks, label)
                b = _proportions(predicted_marks, label)
                counts = _counts(a, b)
                for key in counts:
                    totals[label][key] += counts[key]
                if label == "end" and a != b:
                    bad_boundary_case_ids.append(row["id"])
            latency = candidate.get("latency_ms")
            if latency is not None:
                _entry(output, latency)
                latencies.append(latency)

        scores = {
            kind: {**counts, "f1_pct": _f1(counts)}
            for kind, counts in totals.items()
        }
        results[name] = {
            "examples": len(comparable),
            "exact_all_punctuation_pct": round(100 * exact / len(comparable), 2),
            "metrics": scores,
            "sentence_boundary_error_cases": len(bad_boundary_case_ids),
            # IDs only: no dictated phrases or therapist journal text in reports.
            "sentence_boundary_error_ids": bad_boundary_case_ids,
            "latency_ms": {
                "measured": len(latencies),
                "p50": round(statistics.median(latencies), 3)
                if latencies else None,
                "p95": round(sorted(latencies)[
                    max(0, math.ceil(len(latencies) * .95) - 1)
                ], 3) if latencies else None,
            },
        }
    return {
        "selection": {
            **selection,
            "gold_word_comparable": len(comparable),
            "gold_word_mismatch": len(selected) - len(comparable),
        },
        "candidates": results,
        "automatic_winner": None,
        "note": (
            "Compare comma F1, period F1, sentence-boundary errors and latency. "
            "Human review is mandatory. No model is deployed by this tool."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--candidate", action="append", default=[], metavar="NAME=FILE")
    parser.add_argument("--rupunct-model-dir", type=Path)
    parser.add_argument("--min-cases", type=int, default=10)
    parser.add_argument("--max-duration", type=float, default=25.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or args.output.is_symlink():
        parser.error("Refusing to replace an existing output")
    raw = json.loads(args.manifest.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        parser.error("Manifest must be a JSON list")
    if len(raw) > 2000:
        parser.error("Manifest is too large")
    candidates = {}
    for raw_spec in args.candidate:
        name, delim, filename = raw_spec.partition("=")
        if not delim or not name or not filename or name in candidates:
            parser.error("Use unique candidate=private_file.json arguments")
        candidates[name] = load_predictions(Path(filename))
    if args.rupunct_model_dir is not None:
        if "rupunct" in candidates:
            parser.error("RUPunct candidate was supplied twice")
        selected, _ = choose_unique(raw, args.max_duration)
        candidates["rupunct"] = run_local_rupunct(
            selected, args.rupunct_model_dir
        )
    report = compare(
        raw, candidates, min_cases=args.min_cases,
        max_duration=args.max_duration,
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    descriptor = os.open(args.output, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
